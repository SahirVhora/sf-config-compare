"""Auth shim: delegates to sapsf_shared.auth.

All credential storage and authentication logic now lives in the shared SDK.
This module keeps the original service name ("sfvault") and .secrets.json
path so that existing stored credentials are not lost.

The public API (store_password, get_password, store_client_secret,
get_client_secret, format_basic_username, delete_credentials,
fetch_oauth_token, build_instance_auth) is preserved for callers that have
not yet been updated.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sapsf_shared.auth import (
    AuthConfig,
    CredentialStore,
    OAuth2Auth,
    build_requests_auth,
)

logger = logging.getLogger(__name__)

# Preserve the original keyring service name so existing secrets are readable.
_STORE = CredentialStore(
    service="sfvault",
    fallback_path=Path(__file__).parent.parent / ".secrets.json",
)


# ── Credential storage (thin wrappers around CredentialStore) ─────────────


def store_password(alias: str, password: str) -> None:
    """Persist a basic-auth password for the given instance alias."""
    _STORE.set(f"{alias}:password", password)


def get_password(alias: str) -> str | None:
    """Retrieve the stored basic-auth password for the given instance alias."""
    return _STORE.get(f"{alias}:password")


def store_client_secret(alias: str, secret: str) -> None:
    """Persist an OAuth client secret for the given instance alias."""
    _STORE.set(f"{alias}:client_secret", secret)


def get_client_secret(alias: str) -> str | None:
    """Retrieve the stored OAuth client secret for the given instance alias."""
    return _STORE.get(f"{alias}:client_secret")


def delete_credentials(alias: str) -> None:
    """Delete both the password and client secret for the given instance alias."""
    _STORE.clear_alias(alias)


# ── Username formatting (kept for backward compat + tests) ────────────────


def format_basic_username(username: str | None, company_id: str | None) -> str:
    """Return the SuccessFactors OData basic-auth username (user@company)."""
    clean_username = (username or "").strip()
    clean_company_id = (company_id or "").strip()
    if not clean_username or "@" in clean_username or not clean_company_id:
        return clean_username
    return f"{clean_username}@{clean_company_id}"


# ── OAuth token fetch (delegates to SDK with caching) ─────────────────────


def fetch_oauth_token(
    token_url: str, client_id: str, client_secret: str, company_id: str
) -> str:
    """Fetch a client-credentials OAuth token (uses SDK token cache)."""
    cfg = AuthConfig(
        base_url="",  # not needed for token fetch
        auth_type="oauth2",
        client_id=client_id,
        client_secret=client_secret,
        company_id=company_id,
        token_url=token_url,
    )
    return OAuth2Auth.fetch_token(cfg)


# ── Unified auth builder for instance dicts ──────────────────────────────


def build_instance_auth(instance: dict, alias: str) -> tuple:
    """Return (headers_dict, requests_auth_or_None) for the instance.

    Replaces the duplicated _build_auth() helpers in picklist_pull and
    odata_metadata_pull.  Returns the same two-tuple those helpers returned.
    """
    if instance["auth_type"] == "oauth2":
        secret = get_client_secret(alias)
        if not secret:
            raise RuntimeError("OAuth client secret not found in keyring")
        cfg = AuthConfig(
            base_url=instance.get("base_url", ""),
            auth_type="oauth2",
            client_id=instance["client_id"],
            client_secret=secret,
            company_id=instance["company_id"],
            token_url=instance["token_url"],
        )
        auth_obj, _ = build_requests_auth(cfg)
        return {}, auth_obj
    else:
        password = get_password(alias)
        if not password:
            raise RuntimeError("Password not found in keyring")
        username = format_basic_username(instance["username"], instance["company_id"])
        cfg = AuthConfig(
            base_url=instance.get("base_url", ""),
            auth_type="basic",
            username=username,
            password=password,
            company_id=instance.get("company_id", ""),
        )
        auth_obj, _ = build_requests_auth(cfg)
        return {}, auth_obj
