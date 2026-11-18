"""A fake Wise for tests: answers requests from a made-up fixture (D3)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from securo_wise.client import Response

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str = "wise_account.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeWise:
    """`Http` for `WiseClient`: each request path (with its query) is a key of `routes`.

    A route whose value is `{"_status": 403}` answers that error; a path with
    no route answers 404. Every request is recorded.
    """

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = load() if routes is None else routes
        self.requests: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        parts = urlsplit(url)
        # Fixture keys are written readably (types=STANDARD,SAVINGS) and without `since`:
        # like Wise, `since` filters the feed instead of naming another page.
        params = [p for p in unquote(parts.query).split("&") if p]
        since = next((p.split("=", 1)[1] for p in params if p.startswith("since=")), None)
        query = "&".join(p for p in params if not p.startswith("since="))
        key = parts.path + (f"?{query}" if query else "")
        self.requests.append(unquote(parts.path + (f"?{parts.query}" if parts.query else "")))
        if key not in self.routes:
            return Response(404, {}, b'{"code": "not_found"}')
        answer = self.routes[key]
        if isinstance(answer, dict) and "_status" in answer:
            return Response(answer["_status"], {}, b'{"error": "unauthorized"}')
        if since is not None and isinstance(answer, dict) and "activities" in answer:
            answer = {**answer, "activities": [a for a in answer["activities"] if a["createdOn"] >= since]}
        return Response(200, {}, json.dumps(answer).encode())
