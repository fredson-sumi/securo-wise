"""Activities become contract transactions on the right balance's account.

One-sided activities (card payments, transfers, deposits, fees...) move one
balance by their balance-side amount: on the real account, adding these up
per currency gave Wise's balances exactly. `INTERBALANCE` activities
(conversions and Jar moves) move two balances: a debit on the source and a
credit on the destination, which the title names ("To <Jar name>" / "To EUR").

An activity the plugin can't place on exactly one balance fails the sync,
naming why: a guess would put money on the wrong account.
"""
from __future__ import annotations

from typing import Any

from securo_wise.activity import Activity
from securo_wise.errors import WiseError
from securo_wise.payload import account_id, number

INTERBALANCE = "INTERBALANCE"


def transaction_id(profile_id: Any, activity: Activity, suffix: str = "") -> str:
    """`wise:<profile>:<activity>` (`:in` for the credit side of a move). Fixed once shipped."""
    return f"wise:{profile_id}:{activity.id}{suffix}"


class Balances:
    """A profile's balances, looked up the ways the feed refers to them."""

    def __init__(self, profile_id: Any, balances: list[dict[str, Any]]) -> None:
        self.profile_id = profile_id
        self.standard = {b["currency"]: b for b in balances if b.get("type") == "STANDARD"}
        self.jars = [b for b in balances if b.get("type") == "SAVINGS"]

    def account(self, balance: dict[str, Any]) -> str:
        return account_id(self.profile_id, balance["id"])

    def standard_for(self, currency: str, activity: Activity) -> dict[str, Any]:
        balance = self.standard.get(currency)
        if balance is None:
            raise WiseError(f"A Wise {activity.type} activity moved {currency}, but the profile has no {currency} balance.")
        return balance

    def named(self, name: str, activity: Activity) -> dict[str, Any]:
        """The balance a move's title names: a Jar's name, or a currency code for that currency's balance."""
        jars = [b for b in self.jars if (b.get("name") or "").strip() == name]
        is_currency = name in self.standard
        if len(jars) + is_currency != 1:
            raise WiseError(
                f"A Wise {activity.type} activity names a balance ({len(jars)} Jars and "
                f"{int(is_currency)} currency balances match its name) this plugin can't tell apart."
            )
        return jars[0] if jars else self.standard[name]

    def other_in_currency(self, balance: dict[str, Any], activity: Activity) -> dict[str, Any]:
        """For a move within one currency: the one other balance in that currency."""
        others = [b for b in [*self.standard.values(), *self.jars]
                  if b["currency"] == balance["currency"] and b["id"] != balance["id"]]
        if len(others) != 1:
            raise WiseError(
                f"A Wise {activity.type} activity moved money within {balance['currency']}, which has "
                f"{len(others) + 1} balances: this plugin can't tell which one it came from."
            )
        return others[0]


#: Every move between the person's own balances is described with this prefix,
#: and the manifest's rule files those under Securo's Transfers category, which
#: keeps them out of spending and income. Securo pairs a transfer only when
#: both legs have the same amount, which a conversion's legs never do.
OWN_BALANCES_PREFIX = "Wise: "


def _label(balance: dict[str, Any]) -> str:
    if balance.get("type") == "SAVINGS":
        return f"{(balance.get('name') or 'Jar').strip()} Jar"
    return f"{balance['currency']} balance"


def own_balances_description(source: dict[str, Any], destination: dict[str, Any]) -> str:
    """"Wise: converted EUR to USD", "Wise: moved EUR balance to Holiday Jar"."""
    if source["currency"] != destination["currency"]:
        return f"{OWN_BALANCES_PREFIX}converted {source['currency']} to {destination['currency']}"
    return f"{OWN_BALANCES_PREFIX}moved {_label(source)} to {_label(destination)}"


def _record(external_id: str, activity: Activity, value, currency: str, kind: str, description: str) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "description": (description or activity.type or "Wise")[:500],
        "amount": number(value),
        "currency": currency,
        "date": activity.day.isoformat(),
        "type": kind,
        "status": activity.status,
        "provider_category": activity.type[:100] or None,
        "payee": activity.title[:200] or None,
        "raw_data": {"activity_type": activity.type, "resource_type": activity.resource_type},
    }


def transactions(activity: Activity, balances: Balances) -> list[tuple[str, dict[str, Any]]]:
    """(account external id, contract transaction) pairs for one activity; none when it moved no money."""
    if activity.status is None:
        return []
    if activity.type == INTERBALANCE:
        return _move(activity, balances)
    moved = activity.balance_amount
    balance = balances.standard_for(moved.currency, activity)
    kind = "credit" if activity.incoming else "debit"
    record = _record(transaction_id(balances.profile_id, activity), activity, moved.value, moved.currency, kind, activity.title)
    fee = _fee(activity)
    if fee is not None:
        record["raw_data"]["fee"] = number(fee)
    return [(balances.account(balance), record)]


def _fee(activity: Activity):
    """Wise's fee on a same-currency send: what left the balance less what was sent.

    One transaction keeps the whole amount (it is one movement of the balance);
    the fee goes in `raw_data` for reference. On a cross-currency send the
    two amounts are in different currencies, so the fee isn't known here.
    """
    if activity.incoming or activity.secondary is None or activity.secondary.currency != activity.primary.currency:
        return None
    fee = activity.secondary.value - activity.primary.value
    return fee if fee >= 0 else None


def _move(activity: Activity, balances: Balances) -> list[tuple[str, dict[str, Any]]]:
    if activity.incoming:
        raise WiseError("A Wise INTERBALANCE activity came marked as incoming, which this plugin hasn't seen.")
    name = activity.title.removeprefix("To ").strip()
    if name == activity.title:
        raise WiseError("A Wise INTERBALANCE activity has a title this plugin doesn't know.")
    destination = balances.named(name, activity)
    if activity.primary.currency != destination["currency"]:
        raise WiseError("A Wise INTERBALANCE activity arrived in a currency other than its destination's.")
    if activity.secondary is not None and activity.secondary.currency != activity.primary.currency:
        source = balances.standard_for(activity.secondary.currency, activity)  # a conversion
    else:
        source = balances.other_in_currency(destination, activity)  # into or out of a Jar
    left = activity.balance_amount
    description = own_balances_description(source, destination)
    out = _record(transaction_id(balances.profile_id, activity), activity, left.value, left.currency, "debit", description)
    arrived = _record(transaction_id(balances.profile_id, activity, ":in"), activity, activity.primary.value,
                      activity.primary.currency, "credit", description)
    for record in (out, arrived):
        record["payee"] = None  # money between the person's own balances has no payee
    return [(balances.account(source), out), (balances.account(destination), arrived)]
