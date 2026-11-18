"""Parsing one feed item. Every format here was seen in Wise's answers (docs/wise-api.md); values are made up (D3)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from securo_wise.activity import Amount, parse, parse_amount, plain
from securo_wise.errors import WiseError


@pytest.mark.parametrize(
    ("text", "value", "currency", "incoming"),
    [
        ("1,204.85 USD", "1204.85", "USD", False),
        ("999 BRL", "999", "BRL", False),
        ("42.90 USD", "42.90", "USD", False),
        ("1,250,412.80 GBP", "1250412.80", "GBP", False),
        ("<positive>+ 1,600 USD</positive>", "1600", "USD", True),
        ("<positive>+ 3,250.99 USD</positive>", "3250.99", "USD", True),
    ],
)
def test_every_amount_format_seen_parses(text, value, currency, incoming):
    assert parse_amount(text) == (Amount(Decimal(value), currency), incoming)


@pytest.mark.parametrize(
    "text",
    ["1.234,50 EUR", "-12 USD", "12 usd", "USD 12", "<positive>12 USD</positive>", "+ 12 USD", "1,23 USD", ""],
)
def test_an_unknown_format_fails_with_its_digits_masked(text):
    with pytest.raises(WiseError, match="format this plugin doesn't know") as raised:
        parse_amount(text)
    assert not any(ch.isdigit() and ch != "9" for ch in str(raised.value).split(":")[-1])


def test_titles_lose_their_markup():
    assert plain("For <strong>your account details</strong>") == "For your account details"
    assert plain("<strong>Example  Coffee</strong>") == "Example Coffee"


def _item(**overrides) -> dict:
    item = {"id": "activity-1", "type": "CARD_PAYMENT", "resource": {"type": "CARD_TRANSACTION", "id": "80000001"},
            "title": "<strong>Example Coffee</strong>", "description": "", "primaryAmount": "42.90 USD",
            "secondaryAmount": "", "status": "COMPLETED", "createdOn": "2026-11-10T16:00:00.000Z"}
    return {**item, **overrides}


def test_an_activity_parses_with_its_balance_side_amount():
    sent = parse(_item(primaryAmount="2,731.50 BRL", secondaryAmount="505.12 USD", type="TRANSFER"))
    assert sent.primary == Amount(Decimal("2731.50"), "BRL")
    assert sent.balance_amount == Amount(Decimal("505.12"), "USD")  # what left the balance, fee included
    assert (sent.incoming, sent.status, sent.day, sent.title) == (False, "posted", date(2026, 11, 10), "Example Coffee")


@pytest.mark.parametrize(
    ("status", "expected"),
    [("COMPLETED", "posted"), ("IN_PROGRESS", "pending"), ("REQUIRES_ATTENTION", None), ("CANCELLED", None), ("UPCOMING", None)],
)
def test_statuses_decide_whether_money_moved(status, expected):
    assert parse(_item(status=status)).status == expected


def test_an_unknown_status_fails():
    with pytest.raises(WiseError, match="status this plugin doesn't know"):
        parse(_item(status="SOMETHING_NEW"))


def test_two_amounts_that_disagree_on_direction_fail():
    with pytest.raises(WiseError, match="disagree on direction"):
        parse(_item(primaryAmount="<positive>+ 10 USD</positive>", secondaryAmount="10 USD"))


def test_the_day_is_the_utc_day_wise_created_it():
    assert parse(_item(createdOn="2026-11-10T23:59:59.999Z")).day == date(2026, 11, 10)
    with pytest.raises(WiseError, match="date"):
        parse(_item(createdOn="10/11/2026"))
