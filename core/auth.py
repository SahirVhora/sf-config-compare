import urllib.request
import urllib.parse
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "sfvault"

# Try to use keyring; fall back to a local encrypted-at-rest JSON file on
# systems that have no keyring daemon (e.g. WSL, headless Linux).
try:
    import keyring as _keyring
    _keyring.get_password(KEYRING_SERVICE, "__probe__")  # raises if no backend
    _USE_KEYRING = True
    logger.debug("keyring backend available")
except Exception:
    _USE_KEYRING = False
    logger.warning("No keyring backend - credentials stored in .secrets.json (chmod 600)")

_SECRETS_FILE = Path(__file__).parent.parent / ".secrets.json"


def _file_load() -> dict:
    if _SECRETS_FILE.exists():
        try:
            return json.loads(_SECRETS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _file_save(data: dict):
    tmp = Path(str(_SECRETS_FILE) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, _SECRETS_FILE)
    try:
        os.chmod(_SECRETS_FILE, 0o600)
    except Exception as e:
        logger.warning("chmod 600 on %s failed: %s", _SECRETS_FILE, e)


def _store(key: str, value: str):
    if _USE_KEYRING:
        _keyring.set_password(KEYRING_SERVICE, key, value)
    else:
        data = _file_load()
        data[key] = value
        _file_save(data)


def _get(key: str) -> str | None:
    if _USE_KEYRING:
        return _keyring.get_password(KEYRING_SERVICE, key)
    return _file_load().get(key)


def _delete(key: str):
    if _USE_KEYRING:
        try:
            _keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
    else:
        data = _file_load()
        data.pop(key, None)
        _file_save(data)


def store_password(alias: str, password: str):
    """Persist a basic-auth password for the given instance alias."""
    _store(f"{alias}:password", password)


def get_password(alias: str) -> str | None:
    """Retrieve the stored basic-auth password for the given instance alias."""
    return _get(f"{alias}:password")


def store_client_secret(alias: str, secret: str):
    """Persist an OAuth client secret for the given instance alias."""
    _store(f"{alias}:client_secret", secret)


def get_client_secret(alias: str) -> str | None:
    """Retrieve the stored OAuth client secret for the given instance alias."""
    return _get(f"{alias}:client_secret")


def format_basic_username(username: str | None, company_id: str | None) -> str:
    """Return the SuccessFactors OData basic-auth username."""
    clean_username = (username or "").strip()
    clean_company_id = (company_id or "").strip()
    if not clean_username or "@" in clean_username or not clean_company_id:
        return clean_username
    return f"{clean_username}@{clean_company_id}"


def delete_credentials(alias: str):
    """Delete both the password and client secret for the given instance alias."""
    _delete(f"{alias}:password")
    _delete(f"{alias}:client_secret")


def fetch_oauth_token(token_url: str, client_id: str, client_secret: str, company_id: str) -> str:
    """Fetch a client-credentials OAuth token and return the access_token string."""
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "company_id": company_id,
    }).encode()
    req = urllib.request.Request(token_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {data}")
    logger.info("OAuth token fetched successfully for %s", token_url)
    return token
