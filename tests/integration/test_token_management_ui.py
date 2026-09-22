"""Exercise the browser-facing token-management entry point."""

from fastapi import status
from fastapi.testclient import TestClient


def test_token_management_page_exposes_accessible_sign_in_and_assets(
    integration_api_client: TestClient,
) -> None:
    """The entry page provides a protected, accessible browser workflow."""
    response = integration_api_client.get("/token-management")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    assert '<main id="token-management">' in response.text
    assert '<a href="/auth/github/login"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'aria-live="assertive"' in response.text
    assert 'href="/assets/favicon.svg"' in response.text
    assert 'href="/assets/token-management.css"' in response.text
    assert 'src="/assets/token-management.js"' in response.text

    css = integration_api_client.get("/assets/token-management.css")
    script = integration_api_client.get("/assets/token-management.js")
    favicon = integration_api_client.get("/assets/favicon.svg")
    assert css.status_code == status.HTTP_200_OK
    assert css.headers["content-type"].startswith("text/css")
    assert script.status_code == status.HTTP_200_OK
    assert script.headers["content-type"].startswith("text/javascript")
    assert favicon.status_code == status.HTTP_200_OK
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    for asset in (css, script, favicon):
        assert asset.headers["cache-control"] == "no-store"
        assert asset.headers["referrer-policy"] == "no-referrer"
        assert asset.headers["x-content-type-options"] == "nosniff"
        assert (
            asset.headers["content-security-policy"]
            == response.headers["content-security-policy"]
        )


def test_token_management_page_exposes_complete_accessible_workflow(
    integration_api_client: TestClient,
) -> None:
    """The page labels creation, one-time secret, history, and revocation controls."""
    response = integration_api_client.get("/token-management")

    assert '<section id="authenticated" hidden' in response.text
    assert '<form id="create-token-form"' in response.text
    assert '<label for="token-name">Token name</label>' in response.text
    assert '<label for="authorization-context">Authorization</label>' in response.text
    assert '<select id="authorization-context"' in response.text
    assert 'id="no-contexts"' in response.text
    assert response.text.count('name="expiration"') == 4
    for choice in ("week", "month", "year", "custom"):
        assert f'value="{choice}"' in response.text
    assert '<input id="custom-expiration" type="datetime-local"' in response.text
    assert '<dialog id="token-secret-dialog"' in response.text
    assert 'aria-labelledby="token-secret-heading"' in response.text
    assert 'id="new-token-secret"' in response.text
    assert "This token cannot be shown again" in response.text
    assert '<table id="token-table">' in response.text
    assert '<tbody id="token-list">' in response.text
    assert '<dialog id="revoke-token-dialog"' in response.text
    assert 'id="revoke-token-target"' in response.text
    assert '<button id="confirm-revoke"' in response.text


def test_token_management_assets_support_static_head_requests(
    integration_api_client: TestClient,
) -> None:
    """Packaged browser assets expose standard static-file HEAD behavior."""
    for path, content_type in (
        ("/assets/favicon.svg", "image/svg+xml"),
        ("/assets/token-management.css", "text/css"),
        ("/assets/token-management.js", "text/javascript"),
    ):
        response = integration_api_client.head(path)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b""
        assert response.headers["content-type"].startswith(content_type)
        assert int(response.headers["content-length"]) > 0
        assert response.headers["cache-control"] == "no-store"
