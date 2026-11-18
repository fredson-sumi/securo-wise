"""Securo calls `plugin.sync(params, context)` in its plugin runner.

- `context.secrets["token"]`: the person's read-only Wise API token

`sync` returns `{"state": ..., "data": ...}`: `data` holds the records for
Securo to write as a connected bank. Securo keeps the returned `state` only
once it has written the records, so a sync that fails fetches the same
activity again.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from securo_wise import payload
from securo_wise.client import API, Http, WiseClient
from securo_wise.errors import NeedsAuth, WiseError

#: Wise tokens are short; anything this long is a paste gone wrong.
MAX_TOKEN_LENGTH = 4096
#: Seconds of Wise requests per call: Securo stops a plugin call at 60.
CALL_BUDGET = 45.0
BALANCE_TYPES = "STANDARD,SAVINGS"


def token(context: Any) -> str:
    """The token from the connection's secrets, or `NeedsAuth` saying what is wrong with it."""
    value = str((getattr(context, "secrets", None) or {}).get("token") or "").strip()
    if not value:
        raise NeedsAuth("The Wise API token is missing.")
    if len(value) > MAX_TOKEN_LENGTH or any(ch.isspace() for ch in value):
        raise NeedsAuth("That doesn't look like a Wise API token: paste the token alone.")
    return value


def profiles_with_balances(wise: WiseClient) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every profile the token can see, each with its balances."""
    profiles = wise.get("/v2/profiles")
    if not isinstance(profiles, list):
        raise WiseError("Wise answered /v2/profiles with something other than a list.")
    result = []
    for profile in profiles:
        balances = wise.get(f"/v4/profiles/{profile['id']}/balances", {"types": BALANCE_TYPES})
        if not isinstance(balances, list):
            raise WiseError("Wise answered /v4/profiles/{id}/balances with something other than a list.")
        result.append((profile, balances))
    return result


class Wise:
    def __init__(self, *, http: Http | None = None, api: str = API, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = http
        self._api = api
        self._clock = clock

    def client(self, context: Any) -> WiseClient:
        return WiseClient(token(context), http=self._http, api=self._api,
                          deadline=self._clock() + CALL_BUDGET, clock=self._clock)

    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        wise = self.client(context)
        state = dict(params.get("state") or {})
        # Transactions arrive in later tasks (T6): accounts carry `transactions: null` until then.
        return {"state": state, "data": payload.build(profiles_with_balances(wise))}


plugin = Wise()
