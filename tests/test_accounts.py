"""Balances become accounts. Made-up fixtures only (D3)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from securo_wise.errors import NeedsAuth, WiseError
from securo_wise.payload import account, account_id, number
from securo_wise.plugin import Wise
from tests.fake_wise import FakeWise, load

CONTEXT = SimpleNamespace(secrets={"token": "0000-fake-token"}, config={}, securo={})


def sync(routes: dict | None = None) -> tuple[dict, FakeWise]:
    fake = FakeWise(routes)
    return Wise(http=fake).sync({"state": {}}, CONTEXT), fake


def by_id(data: dict) -> dict[str, dict]:
    return {a["external_id"]: a for a in data["accounts"]}


def test_every_balance_of_every_profile_is_an_account():
    result, _ = sync()
    accounts = by_id(result["data"])
    assert {k: (a["name"], a["type"], a["currency"], a["balance"]) for k, a in accounts.items()} == {
        "wise:10000001:50000011": ("Wise BRL", "checking", "BRL", "0"),
        "wise:10000001:50000012": ("Wise USD", "checking", "USD", "395.15"),
        "wise:10000002:50000021": ("Wise Business USD", "checking", "USD", "1101.98"),
        "wise:10000002:50000022": ("Wise Business BRL", "checking", "BRL", "380"),
        "wise:10000002:50000023": ("Wise Business EUR", "checking", "EUR", "0"),
    }


def test_the_same_currency_in_two_profiles_is_two_accounts():
    result, _ = sync()
    usd = [a["external_id"] for a in result["data"]["accounts"] if a["currency"] == "USD"]
    assert usd == ["wise:10000001:50000012", "wise:10000002:50000021"]


def test_transactions_are_not_sent_yet():
    result, _ = sync()
    assert all(a["transactions"] is None for a in result["data"]["accounts"])  # null keeps what Securo has


def test_it_asks_for_jars_as_well_as_currency_balances():
    _, fake = sync()
    assert "/v4/profiles/10000001/balances?types=STANDARD,SAVINGS" in fake.requests


def test_a_jar_is_a_savings_account_named_after_the_jar():
    jar = load("edge_cases.json")["balances"][3]
    record = account({"id": 10000001, "type": "PERSONAL"}, jar)
    assert (record["external_id"], record["name"], record["type"], record["balance"]) == (
        "wise:10000001:51000004", "Wise Jar: Holiday (EUR)", "savings", "25")


def test_an_unnamed_jar_and_a_business_jar_still_get_a_name():
    jar = {"id": 1, "currency": "GBP", "type": "SAVINGS", "name": None, "amount": {"value": 0}}
    assert account({"id": 2, "type": "BUSINESS"}, jar)["name"] == "Wise Business Jar: Jar (GBP)"


def test_external_ids_never_change_format():
    assert account_id(10000001, 50000012) == "wise:10000001:50000012"


@pytest.mark.parametrize(
    ("value", "text"),
    [(Decimal("395.15"), "395.15"), (Decimal("380.0"), "380"), (0, "0"), (Decimal("1E+6"), "1000000"),
     (Decimal("0.123456789"), "0.12345679"), (Decimal("-12.50"), "-12.5")],
)
def test_amounts_are_plain_strings(value, text):
    assert number(value) == text


def test_balances_arrive_as_decimals_not_floats():
    # 0.1 + 0.2 is the classic float trap; the client parses JSON numbers as Decimal.
    routes = load()
    routes["/v4/profiles/10000001/balances?types=STANDARD,SAVINGS"][1]["amount"]["value"] = 0.30000000000000004
    result, _ = sync(routes)
    assert by_id(result["data"])["wise:10000001:50000012"]["balance"] == "0.3"


def test_a_refused_token_needs_auth():
    with pytest.raises(NeedsAuth, match="didn't let this token read /v2/profiles"):
        sync({"/v2/profiles": {"_status": 403}})


def test_an_unexpected_profiles_answer_fails_clearly():
    with pytest.raises(WiseError, match="/v2/profiles"):
        sync({"/v2/profiles": {"not": "a list"}})
