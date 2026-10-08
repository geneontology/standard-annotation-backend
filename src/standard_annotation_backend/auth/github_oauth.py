"""Verify GitHub identities without retaining or logging upstream credentials."""

from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx2
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError

from standard_annotation_backend.config import Settings
from standard_annotation_backend.domain.errors import UnavailableError, UpstreamError


class OAuthConfigurationError(UnavailableError):
    """Report missing or invalid OAuth configuration without exposing its values."""

    code = "oauth_not_configured"
    message = "GitHub authentication is not configured"


class OAuthUpstreamError(UpstreamError):
    """Report upstream failure without retaining a response or credential."""

    code = "github_oauth_unavailable"
    message = "GitHub authentication is unavailable"


class _OAuthConfiguration(BaseModel):
    """Validate optional deployment settings only when OAuth is used."""

    client_id: Annotated[str, Field(min_length=1, pattern=r"\S")]
    client_secret: SecretStr
    callback_url: HttpUrl


class _AccessToken(BaseModel):
    """Validate GitHub's token-exchange response without displaying the token."""

    access_token: SecretStr
    token_type: Literal["bearer"]


class _UserProfile(BaseModel):
    """Validate the identity fields used from the authenticated GitHub profile."""

    model_config = ConfigDict(strict=True)

    login: Annotated[
        str,
        Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$"),
    ]
    name: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    """Carry identity verified with a newly exchanged GitHub access token.

    Attributes:
        github_login: Current GitHub login normalized to lowercase.
        display_name: Current GitHub profile name, when present.
    """

    github_login: str
    display_name: str | None


class GitHubOAuthClient:
    """Use GitHub's web OAuth flow to verify a user's current login.

    HTTP failures and malformed responses become safe application errors.
    Requests have bounded timeouts and never follow upstream redirects.

    GitHub documents the flow at
    https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps.

    Args:
        settings: Application settings containing the OAuth client registration.

    Raises:
        OAuthConfigurationError: If required settings are absent or invalid.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            self._configuration = _OAuthConfiguration.model_validate(
                {
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "callback_url": settings.github_oauth_callback_url,
                }
            )
            if not self._configuration.client_secret.get_secret_value().strip():
                raise OAuthConfigurationError
            if (
                settings.environment not in {"development", "testing"}
                and self._configuration.callback_url.scheme != "https"
            ):
                raise OAuthConfigurationError
        except ValidationError:
            raise OAuthConfigurationError from None

    def authorization_url(self, state: str) -> str:
        """Build a GitHub authorization URL requesting only basic identity.

        Args:
            state: Unpredictable value used to bind the callback to the browser.

        Returns:
            GitHub authorization URL with the callback and state parameters.
        """
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {
                "client_id": self._configuration.client_id,
                "redirect_uri": str(self._configuration.callback_url),
                "state": state,
                "scope": "",
            }
        )

    def exchange_code(self, code: str) -> str:
        """Exchange a callback code without persisting the returned credential.

        Args:
            code: Short-lived authorization code supplied by GitHub.

        Returns:
            GitHub access token used to retrieve the current profile.

        Raises:
            OAuthUpstreamError: If GitHub fails or returns an invalid response.
        """
        try:
            response = httpx2.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._configuration.client_id,
                    "client_secret": self._configuration.client_secret.get_secret_value(),
                    "redirect_uri": str(self._configuration.callback_url),
                    "code": code,
                },
                timeout=10,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise OAuthUpstreamError
            token = _AccessToken.model_validate(response.json())
            raw = token.access_token.get_secret_value()
            if not raw or "\n" in raw or "\r" in raw:
                raise OAuthUpstreamError
            return raw
        except (httpx2.RequestError, ValueError, OAuthUpstreamError):
            raise OAuthUpstreamError from None

    def get_user(self, access_token: str) -> GitHubIdentity:
        """Retrieve and validate the GitHub profile for an access token.

        Args:
            access_token: Newly exchanged GitHub access token.

        Returns:
            Verified login and optional profile name.

        Raises:
            OAuthUpstreamError: If GitHub fails or returns an invalid profile.
        """
        try:
            response = httpx2.get(
                "https://api.github.com/user",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=10,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise OAuthUpstreamError
            profile = _UserProfile.model_validate(response.json())
            return GitHubIdentity(profile.login.lower(), profile.name)
        except (httpx2.RequestError, ValueError, OAuthUpstreamError):
            raise OAuthUpstreamError from None
