"""Exercise the browser-facing token-management entry point."""

import re

from fastapi import status
from fastapi.testclient import TestClient


def test_token_management_page_links_sign_in_and_serves_protected_assets(
    integration_api_client: TestClient,
) -> None:
    """The entry page links to sign-in and serves its protected static assets."""
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
    assert '<a href="/auth/github/login"' in response.text
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


def test_token_management_page_contains_every_element_the_script_queries(
    integration_api_client: TestClient,
) -> None:
    """Every element and form field the page script uses exists in the served page."""
    page = integration_api_client.get("/token-management").text
    script = integration_api_client.get("/assets/token-management.js").text

    queried_ids = set(
        re.findall(r"""querySelector\(\s*["']#([\w-]+)["']\s*\)""", script)
    )
    field_names = set(re.findall(r"createForm\.elements\.(\w+)", script))
    choices = set(re.findall(r'choice === "(\w+)"', script)) | {"custom"}

    assert queried_ids and field_names and choices >= {"week", "month", "year"}
    assert not {i for i in queried_ids if f'id="{i}"' not in page}
    assert not {n for n in field_names if f'name="{n}"' not in page}
    assert not {c for c in choices if f'value="{c}"' not in page}
    assert 'type="submit"' in page
