"""Across syncs: reading a long history in parts, re-reading recent activity, pending that settles. Made up (D3)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from securo_wise import plugin as plugin_module
from securo_wise.plugin import Wise
from tests.fake_wise import FakeWise, load

CONTEXT = SimpleNamespace(secrets={"token": "0000-fake-token"}, config={}, securo={})
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
BUSINESS_FEED = "/v1/profiles/10000002/activities?size=100"


def run(routes: dict, state: dict | None = None, *, now=NOW, http=None, clock=None):
    fake = FakeWise(routes)
    wise = Wise(http=http or fake, now=lambda: now, **({"clock": clock} if clock else {}))
    result = wise.sync({"state": state or {}}, CONTEXT)
    return result["state"], result["data"], fake


def ids(data: dict) -> set[str]:
    return {t["external_id"] for a in data["accounts"] for t in a["transactions"]}


def paged() -> dict:
    routes = load()
    feed = routes.pop(BUSINESS_FEED)["activities"]
    routes[BUSINESS_FEED] = {"cursor": "page-2", "activities": feed[:3]}
    routes[BUSINESS_FEED + "&nextCursor=page-2"] = {"cursor": "page-3", "activities": feed[3:5]}
    routes[BUSINESS_FEED + "&nextCursor=page-3"] = {"cursor": None, "activities": feed[5:]}
    return routes


def test_a_history_read_to_the_end_records_when_its_read_began():
    state, data, _ = run(load())
    assert state["profiles"] == {"10000001": {"since": "2026-11-18T12:00:00.000Z"},
                                 "10000002": {"since": "2026-11-18T12:00:00.000Z"}}
    assert state["pending"] == {}
    assert len(ids(data)) == 8


def test_later_syncs_reread_only_the_recent_window():
    state, _, _ = run(load())
    _, data, fake = run(load(), state, now=LATER)
    feeds = [r for r in fake.requests if "/activities" in r]
    assert feeds == [f"/v1/profiles/{p}/activities?size=100&since=2026-10-19T12:00:00.000Z" for p in ("10000001", "10000002")]
    assert ids(data) == {"wise:10000001:activity-0001", "wise:10000001:activity-0002",  # Oct 19 and 20
                         "wise:10000002:activity-0003", "wise:10000002:activity-0004", "wise:10000002:activity-0005"}


def _deadline_after(page_marker: str):
    """A clock that jumps past the deadline once a request containing `page_marker` was answered."""
    now = [0.0]
    fake = FakeWise(paged())

    def http(url, headers, timeout):
        answer = fake(url, headers, timeout)
        if page_marker in url:
            now[0] = 1000.0
        return answer
    return http, (lambda: now[0]), fake


def test_a_long_history_continues_where_the_last_sync_stopped():
    http, clock, fake = _deadline_after("10000002/activities?size=100&nextCursor=page-2")
    state, first, _ = run(paged(), http=http, clock=clock)
    assert state["profiles"]["10000002"] == {"started": "2026-11-18T12:00:00.000Z", "cursor": "page-3"}

    state, second, fake2 = run(paged(), state, now=LATER)
    business = [r for r in fake2.requests if "10000002/activities" in r]
    assert business == [BUSINESS_FEED + "&nextCursor=page-3"]  # no page read twice
    assert state["profiles"]["10000002"] == {"since": "2026-11-18T12:00:00.000Z"}  # when the read began
    _, whole, _ = run(load())
    assert ids(first) | ids(second) == ids(whole)
    business = {i for i in ids(second) if i.startswith("wise:10000002:")}
    assert not business & ids(first)  # the backfill never sent a business activity twice
    # (the personal profile, already read, re-reads its recent window on purpose)


def test_a_reply_stops_before_securos_transaction_cap(monkeypatch):
    monkeypatch.setattr(plugin_module, "MAX_TRANSACTIONS", 4)
    state, first, _ = run(paged())
    assert "cursor" in state["profiles"]["10000002"] and len(ids(first)) <= 4 + 2 * 100
    seen, rounds = set(ids(first)), 0
    while "since" not in state["profiles"]["10000002"]:
        state, data, _ = run(paged(), state)
        seen |= ids(data)
        rounds += 1
        assert rounds < 5
    _, whole, _ = run(load())
    assert seen == ids(whole)


def test_a_saved_cursor_wise_no_longer_takes_reads_from_the_start():
    state = {"profiles": {"10000001": {"since": "2026-11-18T12:00:00.000Z"},
                          "10000002": {"started": "2026-11-18T12:00:00.000Z", "cursor": "expired"}}}
    state, data, fake = run(paged(), state)
    business = [r for r in fake.requests if "10000002/activities" in r]
    assert business[0].endswith("nextCursor=expired") and business[1] == BUSINESS_FEED
    assert state["profiles"]["10000002"] == {"since": "2026-11-18T12:00:00.000Z"}


def _with_status(status: str) -> dict:
    routes = load()
    feed = routes["/v1/profiles/10000002/activities?size=100"]["activities"]
    card = next(a for a in feed if a["id"] == "activity-0003")
    card["status"] = status
    return routes


def test_pending_activity_settles_into_the_same_transaction():
    state, data, _ = run(_with_status("IN_PROGRESS"))
    card = {t["external_id"]: t for a in data["accounts"] for t in a["transactions"]}["wise:10000002:activity-0003"]
    assert card["status"] == "pending"
    assert state["pending"] == {"wise:10000002:activity-0003": ["wise:10000002:50000021", "debit", "USD", "Example Coffee"]}

    state, data, _ = run(_with_status("COMPLETED"), state, now=LATER)
    card = {t["external_id"]: t for a in data["accounts"] for t in a["transactions"]}["wise:10000002:activity-0003"]
    assert card["status"] == "posted" and card["amount"] == "42.9"
    assert state["pending"] == {}


def test_pending_activity_wise_cancels_is_zeroed():
    state, _, _ = run(_with_status("IN_PROGRESS"))
    state, data, _ = run(_with_status("CANCELLED"), state, now=LATER)
    card = {t["external_id"]: t for a in data["accounts"] for t in a["transactions"]}["wise:10000002:activity-0003"]
    assert (card["amount"], card["status"], card["description"]) == ("0", "posted", "Example Coffee (cancelled)")
    assert state["pending"] == {}


def test_a_cancelled_activity_never_sent_as_pending_stays_out():
    _, data, _ = run(_with_status("CANCELLED"))
    assert "wise:10000002:activity-0003" not in ids(data)
