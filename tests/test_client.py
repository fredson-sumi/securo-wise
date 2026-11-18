"""How the client talks to Wise and maps its failures. A fake transport only (D3)."""
from __future__ import annotations

import json

import pytest

from securo_wise import __version__
from securo_wise.client import MIN_REQUEST_TIME, OutOfTime, Response, WiseClient, endpoint
from securo_wise.errors import NeedsAuth, RateLimited, WiseError, WiseUnavailable

TOKEN = "00000000-fake-4000-8000-tokentoken00"


class FakeHttp:
    """Answers each request with the next queued response, and records what was asked."""

    def __init__(self, *responses: Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append((url, headers, timeout))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(data: object) -> Response:
    return Response(200, {}, json.dumps(data).encode())


def fail(status: int, body: object = b"", **headers: str) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Response(status, {k.replace("_", "-"): v for k, v in headers.items()}, raw)


def client(*responses: Response | Exception, **kwargs) -> tuple[WiseClient, FakeHttp]:
    http = FakeHttp(*responses)
    return WiseClient(TOKEN, http=http, **kwargs), http


def test_a_get_sends_the_token_a_user_agent_and_the_query():
    wise, http = client(ok([{"id": 1}]))
    assert wise.get("/v1/profiles/10000001/activities", {"size": 100, "nextCursor": None}) == [{"id": 1}]
    (url, headers, timeout), = http.requests
    assert url == "https://api.wise.com/v1/profiles/10000001/activities?size=100"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["User-Agent"] == f"securo-wise-plugin/{__version__}"  # Cloudflare refuses Python's default
    assert timeout == 20.0


def test_no_params_means_no_query_string():
    wise, http = client(ok([]), ok([]))
    wise.get("/v2/profiles")
    wise.get("/v2/profiles", {"cursor": None})
    assert [r[0] for r in http.requests] == ["https://api.wise.com/v2/profiles"] * 2


@pytest.mark.parametrize(
    ("response", "error", "words"),
    [
        (fail(401, {"error": "invalid_token"}), NeedsAuth, "refused the API token (invalid_token)"),
        (fail(403, {"code": "forbidden"}, x_2fa_approval="ott", x_2fa_approval_result="REJECTED"), NeedsAuth, "extra verification (SCA)"),
        (fail(403, {"error": "unauthorized"}), NeedsAuth, "didn't let this token read /v2/profiles (unauthorized)"),
        (fail(403, b"error code: 1010"), NeedsAuth, "(error code 1010)"),
        (fail(429, retry_after="30"), RateLimited, "Wise asked to wait 30 s"),
        (fail(503, b"<html>down</html>"), WiseUnavailable, "HTTP 503"),
        (fail(404, {"code": "not_found"}), WiseError, "HTTP 404 (not_found)"),
        (Response(200, {}, b"<html>"), WiseError, "isn't JSON"),
    ],
)
def test_wise_failures_map_onto_what_securo_understands(response, error, words):
    wise, _ = client(response)
    with pytest.raises(error, match=words.replace("(", r"\(").replace(")", r"\)")):
        wise.get("/v2/profiles")


def test_the_sca_and_token_failures_carry_codes_securo_can_show():
    wise, _ = client(fail(401), fail(403, x_2fa_approval="ott"))
    with pytest.raises(NeedsAuth) as first:
        wise.get("/v2/profiles")
    with pytest.raises(NeedsAuth) as second:
        wise.get("/v2/profiles")
    assert (first.value.code, second.value.code) == ("token_refused", "sca_required")


def test_an_optional_detail_wise_hides_is_none_but_sca_still_fails():
    wise, _ = client(fail(403, {"error": "unauthorized"}), fail(404), fail(403, x_2fa_approval="ott"))
    assert wise.get("/v1/transfers/90000005", optional=True) is None
    assert wise.get("/v1/transfers/90000006", optional=True) is None
    with pytest.raises(NeedsAuth, match="SCA"):
        wise.get("/v1/transfers/90000007", optional=True)


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ConnectionResetError(), OSError("no route")])
def test_a_network_failure_is_temporary(failure):
    wise, _ = client(failure)
    with pytest.raises(WiseUnavailable, match=r"/v1/profiles/\{id\}/activities"):
        wise.get("/v1/profiles/10000001/activities")


def test_messages_never_carry_the_token_the_ids_or_the_body():
    echoed = {"error": TOKEN, "message": f"token {TOKEN} for profile 10000001"}
    for response in (fail(401, echoed), fail(403, echoed), fail(500, echoed), fail(418, echoed)):
        wise, _ = client(response)
        with pytest.raises(Exception) as raised:
            wise.get("/v1/profiles/10000001/balances")
        message = str(raised.value)
        assert TOKEN not in message and "10000001" not in message and "message" not in message


def test_endpoints_are_shown_with_their_ids_masked():
    assert endpoint("/v1/profiles/10000001/activities") == "/v1/profiles/{id}/activities"
    assert endpoint("/v4/profiles/10000001/balances") == "/v4/profiles/{id}/balances"
    assert endpoint("/v1/transfers/90000005") == "/v1/transfers/{id}"


def test_no_request_starts_once_the_deadline_is_close():
    now = [100.0]
    wise, http = client(ok([]), deadline=110.0, clock=lambda: now[0])
    wise.get("/v2/profiles")
    assert http.requests[0][2] == pytest.approx(9.0)  # the request's timeout shrinks to fit
    now[0] = 110.0 - MIN_REQUEST_TIME + 0.5
    with pytest.raises(OutOfTime):
        wise.get("/v2/profiles")
    assert len(http.requests) == 1
