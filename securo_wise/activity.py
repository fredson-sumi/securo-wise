"""One item of Wise's activities feed, parsed (D5). See docs/wise-api.md for where each rule comes from.

The feed gives amounts as display text ("1,204.85 USD", "<positive>+ 500 BRL</positive>").
Parsing is strict: a format not seen before fails the sync with the format
(digits masked) in the message, rather than turning into a wrong amount.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from securo_wise.errors import WiseError

_TAG = re.compile(r"</?[a-z]+>")
_AMOUNT = re.compile(r"(?P<sign>\+\s*)?(?P<number>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s(?P<currency>[A-Z]{3})")

#: Feed status -> contract status; None: the activity moved no money (yet), leave it out (D10).
STATUS = {
    "COMPLETED": "posted",
    "IN_PROGRESS": "pending",
    "UPCOMING": None,  # scheduled, nothing has moved
    "REQUIRES_ATTENTION": None,  # "Waiting for you to pay", "Sending paused": unfunded
    "CANCELLED": None,
}


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str


@dataclass(frozen=True)
class Activity:
    id: str
    type: str
    resource_type: str
    resource_id: str
    title: str  # tags removed
    description: str
    primary: Amount
    secondary: Amount | None
    incoming: bool
    status: str | None  # "posted", "pending", or None when it moved no money
    day: date

    @property
    def balance_amount(self) -> Amount:
        """What the balance moved by: the secondary amount when there is one, else the primary."""
        return self.secondary or self.primary


def masked(text: str) -> str:
    return re.sub(r"\d", "9", text)


def parse_amount(text: str) -> tuple[Amount, bool]:
    """`"<positive>+ 1,234.50 USD</positive>"` -> (Amount(1234.50, USD), incoming=True)."""
    incoming = "<positive>" in text
    bare = _TAG.sub("", text).strip()
    match = _AMOUNT.fullmatch(bare)
    if match is None or bool(match.group("sign")) != incoming:
        raise WiseError(f"Wise sent an amount in a format this plugin doesn't know: {masked(text)!r}.")
    return Amount(Decimal(match.group("number").replace(",", "")), match.group("currency")), incoming


def plain(text: str) -> str:
    """A title without its markup: "<strong>Example Coffee</strong>" -> "Example Coffee"."""
    return re.sub(r"\s+", " ", _TAG.sub("", text or "")).strip()


def parse(item: dict[str, Any]) -> Activity:
    status_text = str(item.get("status") or "")
    if status_text not in STATUS:
        raise WiseError(f"Wise sent an activity with a status this plugin doesn't know: {status_text!r}.")
    primary, incoming = parse_amount(str(item.get("primaryAmount") or ""))
    secondary_text = str(item.get("secondaryAmount") or "")
    secondary = None
    if secondary_text:
        secondary, secondary_in = parse_amount(secondary_text)
        if secondary_in != incoming:
            raise WiseError(f"Wise sent an activity whose two amounts disagree on direction: {masked(secondary_text)!r}.")
    created = str(item.get("createdOn") or "")  # the UTC day (D12)
    try:
        day = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
    except ValueError:
        raise WiseError(f"Wise sent an activity date this plugin doesn't know: {masked(created)!r}.") from None
    resource = item.get("resource") or {}
    return Activity(
        id=str(item["id"]),
        type=str(item.get("type") or ""),
        resource_type=str(resource.get("type") or ""),
        resource_id=str(resource.get("id") or ""),
        title=plain(str(item.get("title") or "")),
        description=plain(str(item.get("description") or "")),
        primary=primary,
        secondary=secondary,
        incoming=incoming,
        status=STATUS[status_text],
        day=day,
    )
