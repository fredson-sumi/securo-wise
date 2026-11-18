"""Build what `sync` returns to Securo: records in the provider plugin contract (1).

Securo writes these through its bank sync pipeline, the same code its native
connections use, matching every record by `external_id`. The external-id
formats never change once shipped: a new format would sync everything again
as new records beside the old ones.

- One account per Wise balance, in every profile the token can see: each
  currency's STANDARD balance and each Jar (SAVINGS), with Wise's balance.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

CONTRACT = 1
#: The contract takes at most 8 decimals; Securo stores 2 for money.
_PLACES = Decimal("0.00000001")

_PROFILE_LABEL = {"PERSONAL": "Wise", "BUSINESS": "Wise Business"}


def number(value: Decimal | int | str) -> str:
    """A decimal as the contract wants it: a plain string, no exponent, at most 8 decimals."""
    return format(Decimal(str(value)).quantize(_PLACES).normalize(), "f")


def account_id(profile_id: int | str, balance_id: int | str) -> str:
    """`wise:<profile>:<balance>`. Fixed once shipped."""
    return f"wise:{profile_id}:{balance_id}"


def account_name(profile: dict[str, Any], balance: dict[str, Any]) -> str:
    """"Wise USD", "Wise Business EUR", or a Jar's own name: "Wise Jar: Holiday (EUR)"."""
    label = _PROFILE_LABEL.get(str(profile.get("type")), "Wise")
    currency = balance["currency"]
    if balance.get("type") == "SAVINGS":
        name = str(balance.get("name") or "").strip() or "Jar"
        return f"{label} Jar: {name} ({currency})"[:255]
    return f"{label} {currency}"


def account(profile: dict[str, Any], balance: dict[str, Any]) -> dict[str, Any]:
    """One balance as a contract account; transactions are added separately."""
    return {
        "external_id": account_id(profile["id"], balance["id"]),
        "name": account_name(profile, balance),
        "type": "savings" if balance.get("type") == "SAVINGS" else "checking",
        "balance": number(balance["amount"]["value"]),
        "currency": balance["currency"],
        # Not fetched yet (null keeps what Securo has); `[]` would say there are none.
        "transactions": None,
    }


def build(profiles: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any]:
    """The contract payload for each profile with its balances."""
    return {
        "contract": CONTRACT,
        "accounts": [account(profile, balance) for profile, balances in profiles for balance in balances],
    }
