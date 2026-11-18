"""Securo calls `plugin.sync(params, context)` in its plugin runner.

- `context.secrets["token"]`: the person's read-only Wise API token

`sync` returns `{"state": ..., "data": ...}`: `data` holds the records for
Securo to write as a connected bank. Securo keeps the returned `state` only
once it has written the records, so a sync that fails reads the same
activity again.

`params["state"]` is what the last sync returned (`{}` the first time):

- `profiles[<id>]`, one per Wise profile:
  - while its history is first read: `started` (when that began) and
    `cursor` (the feed page to continue from; absent means the start);
  - once read to the end: `since`, the time of the last complete read.
    Later syncs re-read the feed from `since` less `OVERLAP`, which is
    how pending activity that completed or was cancelled is seen again.
- `pending`: transactions sent as pending, by external id, as
  `[account, type, currency, description]`, so one Wise cancels can be
  sent again as a posted zero (the contract has no way to delete).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from securo_wise import activity, mapping, payload
from securo_wise.client import API, Http, OutOfTime, WiseClient
from securo_wise.errors import NeedsAuth, WiseError

#: Wise tokens are short; anything this long is a paste gone wrong.
MAX_TOKEN_LENGTH = 4096
#: Seconds of Wise requests per call: Securo stops a plugin call at 60.
CALL_BUDGET = 45.0
BALANCE_TYPES = "STANDARD,SAVINGS"
ACTIVITY_PAGE = 100
#: Securo refuses a reply with more than 20,000 transactions; an activity makes at most 2.
MAX_TRANSACTIONS = 20_000 - 2 * ACTIVITY_PAGE
#: How far back a later sync re-reads, so pending activity is seen settling or cancelled.
OVERLAP = timedelta(days=30)


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


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Run:
    """One call's reading of the feed: what it found, and where each profile got to."""

    def __init__(self, wise: WiseClient, state: dict[str, Any], now: datetime) -> None:
        self.wise = wise
        self.now = now
        self.profiles: dict[str, dict[str, Any]] = dict(state.get("profiles") or {})
        self.pending: dict[str, list] = dict(state.get("pending") or {})
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.count = 0
        self.stopped = False  # out of time, or the reply is full

    def state(self) -> dict[str, Any]:
        return {"profiles": self.profiles, "pending": self.pending}

    def add(self, account_key: str, record: dict[str, Any]) -> None:
        records = self.records.setdefault(account_key, {})
        if record["external_id"] not in records:
            self.count += 1
        # A page boundary can repeat an item; Securo refuses a list with the same id twice.
        records[record["external_id"]] = record
        if record["status"] == "pending":
            self.pending[record["external_id"]] = [account_key, record["type"], record["currency"], record["description"]]
        else:
            self.pending.pop(record["external_id"], None)

    def cancel(self, profile_id: Any, item: activity.Activity) -> None:
        """An activity that moved no money: if it was sent as pending, send it again as a posted zero."""
        for external_id in mapping.transaction_ids(profile_id, item):
            sent = self.pending.pop(external_id, None)
            if sent is None:
                continue
            account_key, kind, currency, description = sent
            self.records.setdefault(account_key, {})[external_id] = {
                "external_id": external_id,
                "description": f"{description} (cancelled)"[:500],
                "amount": "0",
                "currency": currency,
                "date": item.day.isoformat(),
                "type": kind,
                "status": "posted",
                "provider_category": item.type[:100] or None,
                "payee": item.title[:200] or None,
                "raw_data": {"activity_type": item.type, "resource_type": item.resource_type, "cancelled": True},
            }
            self.count += 1

    def page(self, profile_id: Any, cursor: str | None, since: str | None) -> dict[str, Any]:
        """One page of the feed, or `OutOfTime` once the call's time or the reply's room runs out."""
        if self.count >= MAX_TRANSACTIONS:
            raise OutOfTime
        page = self.wise.get(f"/v1/profiles/{profile_id}/activities",
                             {"size": ACTIVITY_PAGE, "since": since, "nextCursor": cursor})
        if not isinstance(page, dict) or not isinstance(page.get("activities"), list):
            raise WiseError("Wise answered /v1/profiles/{id}/activities with something this plugin doesn't know.")
        return page

    def read(self, profile: dict[str, Any], balances: list[dict[str, Any]]) -> None:
        """Read one profile's feed as far as this call allows, and record where it got to."""
        key = str(profile["id"])
        lookup = mapping.Balances(profile["id"], balances)
        progress = dict(self.profiles.get(key) or {})
        backfilling = "since" not in progress
        if backfilling:
            started = progress.get("started") or _iso(self.now)
            cursor, since = progress.get("cursor"), None
        else:
            started = _iso(self.now)
            cursor, since = None, _iso(datetime.fromisoformat(progress["since"].replace("Z", "+00:00")) - OVERLAP)
        resumed = cursor is not None
        while True:
            try:
                page = self.page(profile["id"], cursor, since)
            except OutOfTime:
                self.stopped = True
                if backfilling:  # continue from this page next time
                    self.profiles[key] = {"started": started, **({"cursor": cursor} if cursor else {})}
                return  # an incremental read keeps its `since`: the whole window again next time
            except WiseError:
                if not resumed:
                    raise
                cursor, resumed = None, False  # a saved cursor Wise no longer takes: read from the start
                continue
            resumed = False
            for item in page["activities"]:
                parsed = activity.parse(item)
                if parsed.status is None:
                    self.cancel(profile["id"], parsed)
                for account_key, record in mapping.transactions(parsed, lookup):
                    self.add(account_key, record)
            cursor = page.get("cursor")
            if not cursor:
                self.profiles[key] = {"since": started}
                return


class Wise:
    def __init__(
        self,
        *,
        http: Http | None = None,
        api: str = API,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._http = http
        self._api = api
        self._clock = clock
        self._now = now

    def client(self, context: Any) -> WiseClient:
        return WiseClient(token(context), http=self._http, api=self._api,
                          deadline=self._clock() + CALL_BUDGET, clock=self._clock)

    def sync(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
        wise = self.client(context)
        profiles = profiles_with_balances(wise)
        run = _Run(wise, dict(params.get("state") or {}), self._now())
        for profile, balances in profiles:
            if run.stopped:
                break  # the next sync continues with this profile
            run.read(profile, balances)
        transactions = {key: list(records.values()) for key, records in run.records.items()}
        return {"state": run.state(), "data": payload.build(profiles, transactions)}


plugin = Wise()
