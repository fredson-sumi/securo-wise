"""Activities become transactions on the right accounts. Made-up fixtures only (D3)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from securo_wise.activity import parse
from securo_wise.errors import WiseError
from securo_wise.mapping import Balances, transactions
from securo_wise.plugin import Wise
from tests.fake_wise import FakeWise, load

CONTEXT = SimpleNamespace(secrets={"token": "0000-fake-token"}, config={}, securo={})
EDGE = load("edge_cases.json")


def sync(routes: dict | None = None, **kwargs) -> dict:
    return Wise(http=FakeWise(routes), **kwargs).sync({"state": {}}, CONTEXT)["data"]


def by_account(data: dict) -> dict[str, dict[str, tuple]]:
    return {
        a["external_id"]: {t["external_id"]: (t["type"], t["amount"], t["currency"], t["date"], t["status"])
                           for t in a["transactions"]}
        for a in data["accounts"]
    }


def test_each_activity_lands_once_on_its_balance_with_what_the_balance_moved():
    accounts = by_account(sync())
    assert accounts["wise:10000001:50000012"] == {
        "wise:10000001:activity-0001": ("debit", "1204.85", "USD", "2026-10-20", "posted"),  # sent, fee included
        "wise:10000001:activity-0002": ("credit", "1600", "USD", "2026-10-19", "posted"),
    }
    assert accounts["wise:10000002:50000021"] == {
        "wise:10000002:activity-0003": ("debit", "42.9", "USD", "2026-11-10", "posted"),  # card payment
        "wise:10000002:activity-0004": ("credit", "3250", "USD", "2026-11-03", "posted"),
        "wise:10000002:activity-0005": ("debit", "1600", "USD", "2026-10-19", "posted"),
        "wise:10000002:activity-0006": ("debit", "505.12", "USD", "2026-08-12", "posted"),  # USD -> BRL send
    }
    assert accounts["wise:10000002:50000022"] == {
        "wise:10000002:activity-0007": ("debit", "120", "BRL", "2026-06-05", "posted"),  # account details fee
        "wise:10000002:activity-0008": ("credit", "500", "BRL", "2026-08-01", "posted"),
    }
    assert accounts["wise:10000001:50000011"] == accounts["wise:10000002:50000023"] == {}  # fetched, none


def test_the_transactions_add_up_to_each_balance():
    # What held on the real account (docs/wise-api.md), held by the fixture too.
    data = sync()
    for account in data["accounts"]:
        total = sum(Decimal(t["amount"]) * (1 if t["type"] == "credit" else -1) for t in account["transactions"])
        assert total == Decimal(account["balance"]), account["name"]


def test_a_transaction_names_its_payee_and_where_it_came_from():
    data = sync()
    card = next(t for a in data["accounts"] for t in a["transactions"] if t["external_id"].endswith("activity-0003"))
    assert (card["description"], card["payee"], card["provider_category"]) == ("Example Coffee", "Example Coffee", "CARD_PAYMENT")
    assert card["raw_data"] == {"activity_type": "CARD_PAYMENT", "resource_type": "CARD_TRANSACTION"}


def _edge(index: int) -> list[tuple[str, tuple]]:
    lookup = Balances(10000001, EDGE["balances"])
    return [(account, (t["external_id"], t["type"], t["amount"], t["currency"], t["description"], t["payee"]))
            for account, t in transactions(parse(EDGE["activities"][index]), lookup)]


def test_money_into_a_jar_leaves_the_currency_balance_and_reaches_the_jar():
    assert _edge(0) == [
        ("wise:10000001:51000001", ("wise:10000001:edge-0001", "debit", "40", "EUR", "Moved to Holiday", None)),
        ("wise:10000001:51000004", ("wise:10000001:edge-0001:in", "credit", "40", "EUR", "Moved to Holiday", None)),
    ]


def test_money_out_of_a_jar_goes_back_to_the_currency_balance():
    assert _edge(1) == [
        ("wise:10000001:51000004", ("wise:10000001:edge-0002", "debit", "15", "EUR", "Moved to EUR", None)),
        ("wise:10000001:51000001", ("wise:10000001:edge-0002:in", "credit", "15", "EUR", "Moved to EUR", None)),
    ]


def test_a_conversion_leaves_one_currency_and_arrives_in_the_other():
    assert _edge(2) == [
        ("wise:10000001:51000001", ("wise:10000001:edge-0003", "debit", "50", "EUR", "Moved to USD", None)),
        ("wise:10000001:51000002", ("wise:10000001:edge-0003:in", "credit", "54.62", "USD", "Moved to USD", None)),
    ]
    assert _edge(3)[0][0] == "wise:10000001:51000003" and _edge(3)[1][0] == "wise:10000001:51000001"  # GBP -> EUR


def test_unpaid_and_cancelled_transfers_move_no_money():
    assert _edge(4) == _edge(5) == _edge(6) == []


def test_a_large_send_keeps_every_digit():
    (_, record), = _edge(7)
    assert record[1:4] == ("debit", "1250412.8", "GBP")


def test_a_move_between_balances_it_cant_tell_apart_fails():
    two_jars = EDGE["balances"] + [{**EDGE["balances"][3], "id": 51000005, "name": "Car"}]
    with pytest.raises(WiseError, match="can't tell which one it came from"):
        transactions(parse(EDGE["activities"][1]), Balances(10000001, two_jars))
    jar_named_like_a_currency = EDGE["balances"][:3] + [{**EDGE["balances"][3], "name": "EUR"}]
    with pytest.raises(WiseError, match="can't tell apart"):
        transactions(parse(EDGE["activities"][1]), Balances(10000001, jar_named_like_a_currency))


def test_an_activity_in_a_currency_without_a_balance_fails():
    item = {**EDGE["activities"][7], "primaryAmount": "10 JPY", "secondaryAmount": ""}
    with pytest.raises(WiseError, match="no JPY balance"):
        transactions(parse(item), Balances(10000001, EDGE["balances"]))


def _paged_routes() -> dict:
    routes = load()
    feed = routes.pop("/v1/profiles/10000002/activities?size=100")["activities"]
    routes["/v1/profiles/10000002/activities?size=100"] = {"cursor": "page-2", "activities": feed[:4]}
    # The second page repeats the first page's last item, as a moving feed can.
    routes["/v1/profiles/10000002/activities?size=100&nextCursor=page-2"] = {"cursor": None, "activities": feed[3:]}
    return routes


def test_the_feed_is_read_to_its_last_page_and_a_repeated_item_counts_once():
    paged, whole = by_account(sync(_paged_routes())), by_account(sync())
    assert paged == whole


def test_running_out_of_time_keeps_what_was_read():
    now = [0.0]
    fake = FakeWise(_paged_routes())

    def http(url, headers, timeout):
        answer = fake(url, headers, timeout)
        if "/10000002/activities" in url and "nextCursor" not in url:
            now[0] = 100.0  # the deadline passes while the first business page arrives
        return answer

    data = Wise(http=http, clock=lambda: now[0]).sync({"state": {}}, CONTEXT)["data"]
    accounts = by_account(data)
    assert "wise:10000002:activity-0003" in accounts["wise:10000002:50000021"]  # from the first page
    assert "wise:10000002:activity-0008" not in accounts["wise:10000002:50000022"]  # on the page never read
    assert not any("nextCursor" in r for r in fake.requests)
