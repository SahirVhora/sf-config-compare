"""Regression tests for credential-safe picklist pagination."""

from unittest.mock import MagicMock

import pytest

from core import picklist_pull
from sapsf_shared.exceptions import SFClientError


def _page(results, next_url):
    response = MagicMock(status_code=200)
    response.json.return_value = {"d": {"results": results, "__next": next_url}}
    return response


def test_rejects_cross_origin_next_link(monkeypatch):
    first = _page([{"externalCode": "A"}], "https://attacker.example/steal")
    request = MagicMock(side_effect=[first, AssertionError("credentials left trusted origin")])
    monkeypatch.setattr(picklist_pull, "get_with_retry", request)

    with pytest.raises(SFClientError, match="cross-origin"):
        picklist_pull._fetch_all_pages("https://api.example.com", {}, None, lambda *args: None)

    request.assert_called_once()


def test_rejects_pagination_cycle(monkeypatch):
    initial = picklist_pull._build_picklist_url(
        "https://api.example.com/odata/v2/PickListValueV2", 0
    )
    first = _page([{"externalCode": "A"}], initial)
    request = MagicMock(side_effect=[first, AssertionError("cycle was requested twice")])
    monkeypatch.setattr(picklist_pull, "get_with_retry", request)

    with pytest.raises(SFClientError, match="cycle"):
        picklist_pull._fetch_all_pages("https://api.example.com", {}, None, lambda *args: None)

    request.assert_called_once()
