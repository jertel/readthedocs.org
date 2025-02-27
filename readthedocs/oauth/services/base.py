"""OAuth utility functions."""

from datetime import datetime

import structlog
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers import registry
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from oauthlib.oauth2.rfc6749.errors import InvalidClientIdError
from requests.exceptions import RequestException
from requests_oauthlib import OAuth2Session

log = structlog.get_logger(__name__)


class SyncServiceError(Exception):

    """Error raised when a service failed to sync."""

    INVALID_OR_REVOKED_ACCESS_TOKEN = _(
        "Our access to your following accounts was revoked: {provider}. "
        "Please, reconnect them from your social account connections."
    )


class Service:

    """
    Service mapping for local accounts.

    :param user: User to use in token lookup and session creation
    :param account: :py:class:`SocialAccount` instance for user
    """

    adapter = None
    url_pattern = None
    vcs_provider_slug = None

    default_user_avatar_url = settings.OAUTH_AVATAR_USER_DEFAULT_URL
    default_org_avatar_url = settings.OAUTH_AVATAR_ORG_DEFAULT_URL

    def __init__(self, user, account):
        self.session = None
        self.user = user
        self.account = account
        log.bind(
            user_username=self.user.username,
            social_provider=self.provider_id,
            social_account_id=self.account.pk,
        )

    @classmethod
    def for_user(cls, user):
        """Return list of instances if user has an account for the provider."""
        try:
            accounts = SocialAccount.objects.filter(
                user=user,
                provider=cls.adapter.provider_id,
            )
            return [cls(user=user, account=account) for account in accounts]
        except SocialAccount.DoesNotExist:
            return []

    def get_adapter(self):
        return self.adapter

    @property
    def provider_id(self):
        return self.get_adapter().provider_id

    @property
    def provider_name(self):
        return registry.by_id(self.provider_id).name

    def get_session(self):
        if self.session is None:
            self.create_session()
        return self.session

    def create_session(self):
        """
        Create OAuth session for user.

        This configures the OAuth session based on the :py:class:`SocialToken`
        attributes. If there is an ``expires_at``, treat the session as an auto
        renewing token. Some providers expire tokens after as little as 2 hours.
        """
        token = self.account.socialtoken_set.first()
        if token is None:
            return None

        token_config = {
            "access_token": token.token,
            "token_type": "bearer",
        }
        if token.expires_at is not None:
            token_expires = (token.expires_at - timezone.now()).total_seconds()
            token_config.update(
                {
                    "refresh_token": token.token_secret,
                    "expires_in": token_expires,
                }
            )

        self.session = OAuth2Session(
            client_id=token.app.client_id,
            token=token_config,
            auto_refresh_kwargs={
                "client_id": token.app.client_id,
                "client_secret": token.app.secret,
            },
            auto_refresh_url=self.get_adapter().access_token_url,
            token_updater=self.token_updater(token),
        )

        return self.session or None

    def token_updater(self, token):
        """
        Update token given data from OAuth response.

        Expect the following response into the closure::

            {
                u'token_type': u'bearer',
                u'scopes': u'webhook repository team account',
                u'refresh_token': u'...',
                u'access_token': u'...',
                u'expires_in': 3600,
                u'expires_at': 1449218652.558185
            }
        """

        def _updater(data):
            token.token = data["access_token"]
            token.token_secret = data.get("refresh_token", "")
            token.expires_at = timezone.make_aware(
                datetime.fromtimestamp(data["expires_at"]),
            )
            token.save()
            log.info("Updated token.", token_id=token.pk)

        return _updater

    def paginate(self, url, **kwargs):
        """
        Recursively combine results from service's pagination.

        :param url: start url to get the data from.
        :type url: unicode
        :param kwargs: optional parameters passed to .get() method
        :type kwargs: dict
        """
        resp = None
        try:
            resp = self.get_session().get(url, params=kwargs)

            # TODO: this check of the status_code would be better in the
            # ``create_session`` method since it could be used from outside, but
            # I didn't find a generic way to make a test request to each
            # provider.
            if resp.status_code == 401:
                # Bad credentials: the token we have in our database is not
                # valid. Probably the user has revoked the access to our App. He
                # needs to reconnect his account
                raise SyncServiceError(
                    SyncServiceError.INVALID_OR_REVOKED_ACCESS_TOKEN.format(
                        provider=self.provider_name
                    )
                )

            next_url = self.get_next_url_to_paginate(resp)
            results = self.get_paginated_results(resp)
            if next_url:
                results.extend(self.paginate(next_url))
            return results
        # Catch specific exception related to OAuth
        except InvalidClientIdError:
            log.warning("access_token or refresh_token failed.", url=url)
            raise SyncServiceError(
                SyncServiceError.INVALID_OR_REVOKED_ACCESS_TOKEN.format(
                    provider=self.provider_name
                )
            )
        # Catch exceptions with request or deserializing JSON
        except (RequestException, ValueError):
            # Response data should always be JSON, still try to log if not
            # though
            try:
                debug_data = resp.json() if resp else {}
            except ValueError:
                debug_data = resp.content
            log.debug(
                "Paginate failed at URL.",
                url=url,
                debug_data=debug_data,
            )

        return []

    def sync(self):
        """
        Sync repositories (RemoteRepository) and organizations (RemoteOrganization).

        - creates a new RemoteRepository/Organization per new repository
        - updates fields for existing RemoteRepository/Organization
        - deletes old RemoteRepository/Organization that are not present
          for this user in the current provider
        """
        remote_repositories = self.sync_repositories()
        (
            remote_organizations,
            remote_repositories_organizations,
        ) = self.sync_organizations()

        # Delete RemoteRepository where the user doesn't have access anymore
        # (skip RemoteRepository tied to a Project on this user)
        all_remote_repositories = (
            remote_repositories + remote_repositories_organizations
        )
        repository_remote_ids = [
            r.remote_id for r in all_remote_repositories if r is not None
        ]
        (
            self.user.remote_repository_relations.exclude(
                remote_repository__remote_id__in=repository_remote_ids,
                remote_repository__vcs_provider=self.vcs_provider_slug,
            )
            .filter(account=self.account)
            .delete()
        )

        # Delete RemoteOrganization where the user doesn't have access anymore
        organization_remote_ids = [
            o.remote_id for o in remote_organizations if o is not None
        ]
        (
            self.user.remote_organization_relations.exclude(
                remote_organization__remote_id__in=organization_remote_ids,
                remote_organization__vcs_provider=self.vcs_provider_slug,
            )
            .filter(account=self.account)
            .delete()
        )

    def get_next_url_to_paginate(self, response):
        """
        Return the next url to feed the `paginate` method.

        :param response: response from where to get the `next_url` attribute
        :type response: requests.Response
        """
        raise NotImplementedError

    def get_paginated_results(self, response):
        """
        Return the results for the current response/page.

        :param response: response from where to get the results.
        :type response: requests.Response
        """
        raise NotImplementedError

    def get_webhook_url(self, project, integration):
        """Get the webhook URL for the project's integration."""
        return "{base_url}{path}".format(
            base_url=settings.PUBLIC_API_URL,
            path=reverse(
                "api_webhook",
                kwargs={
                    "project_slug": project.slug,
                    "integration_pk": integration.pk,
                },
            ),
        )

    def get_provider_data(self, project, integration):
        """
        Gets provider data from Git Providers Webhooks API.

        :param project: project
        :type project: Project
        :param integration: Integration for the project
        :type integration: Integration
        :returns: Dictionary containing provider data from the API or None
        :rtype: dict
        """
        raise NotImplementedError

    def setup_webhook(self, project, integration=None):
        """
        Setup webhook for project.

        :param project: project to set up webhook for
        :type project: Project
        :param integration: Integration for the project
        :type integration: Integration
        :returns: boolean based on webhook set up success, and requests Response object
        :rtype: (Bool, Response)
        """
        raise NotImplementedError

    def update_webhook(self, project, integration):
        """
        Update webhook integration.

        :param project: project to set up webhook for
        :type project: Project
        :param integration: Webhook integration to update
        :type integration: Integration
        :returns: boolean based on webhook update success, and requests Response object
        :rtype: (Bool, Response)
        """
        raise NotImplementedError

    def send_build_status(self, build, commit, status):
        """
        Create commit status for project.

        :param build: Build to set up commit status for
        :type build: Build
        :param commit: commit sha of the pull/merge request
        :type commit: str
        :param status: build state failure, pending, or success.
        :type status: str
        :returns: boolean based on commit status creation was successful or not.
        :rtype: Bool
        """
        raise NotImplementedError

    @classmethod
    def is_project_service(cls, project):
        """
        Determine if this is the service the project is using.

        .. note::

            This should be deprecated in favor of attaching the
            :py:class:`RemoteRepository` to the project instance. This is a
            slight improvement on the legacy check for webhooks
        """
        # TODO Replace this check by keying project to remote repos
        return (
            cls.url_pattern is not None
            and cls.url_pattern.search(project.repo) is not None
        )
