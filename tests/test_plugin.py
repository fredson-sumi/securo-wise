"""The entrypoint and the manifest that points at it. Fake data only (D3)."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from securo_wise import __version__
from securo_wise.errors import HELP_URL, NeedsAuth
from securo_wise.mapping import OWN_BALANCES_PREFIX
from securo_wise.plugin import Wise, plugin
from tests.fake_wise import FakeWise

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "securo-plugin.json").read_text())


def _context(secrets: dict | None) -> SimpleNamespace:
    return SimpleNamespace(secrets=secrets, config={}, securo={})


@pytest.mark.parametrize("secrets", [None, {}, {"token": ""}, {"token": "   "}])
def test_a_missing_token_needs_auth(secrets):
    with pytest.raises(NeedsAuth, match="missing") as raised:
        plugin.sync({}, _context(secrets))
    assert HELP_URL in str(raised.value)


@pytest.mark.parametrize("value", ["two words", "x" * 5000, "line\nbreak"])
def test_something_that_is_not_a_token_needs_auth(value):
    with pytest.raises(NeedsAuth, match="paste the token alone") as raised:
        plugin.sync({}, _context({"token": value}))
    assert value not in str(raised.value)


def test_a_token_with_surrounding_spaces_is_accepted():
    fake = FakeWise()
    result = Wise(http=fake).sync({"state": {}}, _context({"token": "  0000-fake-token  "}))
    assert set(result["state"]["profiles"]) == {"10000001", "10000002"} and result["data"]["contract"] == 1
    assert fake.requests[0] == "/v2/profiles"


def test_the_manifest_entrypoint_is_this_plugin():
    module, attribute = MANIFEST["runtime"]["entrypoint"].split(":")
    assert getattr(importlib.import_module(module), attribute) is plugin


def test_the_manifest_asks_only_for_a_secret_token_and_reaches_only_wise():
    (integration,) = MANIFEST["contributes"]["integrations"]
    assert [(f["key"], f.get("secret")) for f in integration["fields"]] == [("token", True)]
    assert integration["provides"] == ["accounts", "transactions"]
    assert [h["host"] for h in MANIFEST["network"]] == ["api.wise.com"]


def test_versions_agree():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{MANIFEST["version"]}"' in pyproject
    assert __version__ == MANIFEST["version"]  # it goes out in the User-Agent


def test_the_manifest_files_moves_between_own_balances_under_transfers():
    (rule,) = MANIFEST["contributes"]["rules"]
    assert rule["conditions"] == [{"field": "description", "op": "starts_with", "value": OWN_BALANCES_PREFIX}]
    assert rule["actions"] == [{"op": "set_category", "value": "transfers"}]  # Securo's own, treated as a transfer
