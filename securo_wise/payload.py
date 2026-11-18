"""Build what `sync` returns to Securo: records in the provider plugin contract (1).

Securo writes these through its bank sync pipeline, the same code its native
connections use, matching every record by `external_id`. The external-id
formats never change once shipped: a new format would sync everything again
as new records beside the old ones.

- One account per Wise balance, in every profile the token can see: each
  currency's STANDARD balance and each Jar (SAVINGS), with Wise's balance (D6).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

CONTRACT = 1
#: A key of the manifest's `logos`: Securo serves the file from the package, so
#: showing it never sends anyone's browser to a host the manifest doesn't declare (D14).
INSTITUTION_LOGO = "wise"
#: The contract takes at most 8 decimals; Securo stores 2 for money.
_PLACES = Decimal("0.00000001")

_PROFILE_LABEL = {"PERSONAL": "Wise", "BUSINESS": "Wise Business"}


def number(value: Decimal | int | str) -> str:
    """A decimal as the contract wants it: a plain string, no exponent, at most 8 decimals."""
    return format(Decimal(str(value)).quantize(_PLACES).normalize(), "f")


def account_id(profile_id: int | str, balance_id: int | str) -> str:
    """`wise:<profile>:<balance>`. Fixed once shipped (D4)."""
    return f"wise:{profile_id}:{balance_id}"


def account_name(profile: dict[str, Any], balance: dict[str, Any]) -> str:
    """"Wise USD", "Wise Business EUR", or a Jar's own name: "Wise Jar: Holiday (EUR)"."""
    label = _PROFILE_LABEL.get(str(profile.get("type")), "Wise")
    currency = balance["currency"]
    if balance.get("type") == "SAVINGS":
        name = str(balance.get("name") or "").strip() or "Jar"
        return f"{label} Jar: {name} ({currency})"[:255]
    return f"{label} {currency}"


def account(profile: dict[str, Any], balance: dict[str, Any],
            transactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One balance as a contract account, with the transactions fetched for it (None: not fetched)."""
    return {
        "external_id": account_id(profile["id"], balance["id"]),
        "name": account_name(profile, balance),
        "type": "savings" if balance.get("type") == "SAVINGS" else "checking",
        "balance": number(balance["amount"]["value"]),
        "currency": balance["currency"],
        # The logo alone, no institution name: Securo would show the name in
        # place of each account's own, and every balance would read "Wise".
        "institution_logo": INSTITUTION_LOGO,
        # None: not fetched, Securo keeps what it has. A list is upserted by
        # external id; transactions missing from it are left alone.
        "transactions": transactions,
    }


def build(
    profiles: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    transactions: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """The contract payload: each profile's balances, with `transactions` keyed by account external id.

    Without `transactions`, accounts carry none (not fetched). With it, an
    account that had no activity gets `[]`.
    """
    return {
        "contract": CONTRACT,
        "accounts": [
            account(profile, balance,
                    None if transactions is None else transactions.get(account_id(profile["id"], balance["id"]), []))
            for profile, balances in profiles for balance in balances
        ],
    }
