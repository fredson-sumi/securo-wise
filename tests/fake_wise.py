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
        query = unquote(parts.query)  # fixture keys are written readably: types=STANDARD,SAVINGS
        key = parts.path + (f"?{query}" if query else "")
        self.requests.append(key)
        if key not in self.routes:
            return Response(404, {}, b'{"code": "not_found"}')
        answer = self.routes[key]
        if isinstance(answer, dict) and "_status" in answer:
            return Response(answer["_status"], {}, b'{"error": "unauthorized"}')
        return Response(200, {}, json.dumps(answer).encode())
