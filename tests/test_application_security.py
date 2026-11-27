from unittest.mock import Mock, patch

import pytest
from app import app
from core.auth import fetch_oauth_token


@pytest.mark.parametrize("path", ["/", "/api/v1/instances", "/instances/add"])
def test_application_token_protects_all_routes(monkeypatch, path):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "synthetic-token")
    client = app.test_client()
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-App-Token": "wrong"}).status_code == 401
    assert (
        client.get(path, headers={"X-App-Token": "synthetic-token"}).status_code == 200
    )


def test_remote_access_without_token_fails_closed(monkeypatch):
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    response = app.test_client().get(
        "/", environ_overrides={"REMOTE_ADDR": "198.51.100.10"}
    )
    assert response.status_code == 503


def test_oauth_helper_reaches_token_endpoint():
    response = Mock(ok=True)
    response.json.return_value = {"access_token": "synthetic", "expires_in": 0}
    with patch("sapsf_shared.auth.requests.post", return_value=response) as post:
        assert (
            fetch_oauth_token(
                "https://tenant.example/oauth/token", "client", "secret", "company"
            )
            == "synthetic"
        )
    post.assert_called_once()
