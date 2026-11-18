"""Read-only GETs to the Wise API, with Wise's failures mapped onto what Securo understands.

Every request carries the token as a bearer header and a `User-Agent` of its
own: Cloudflare in front of Wise refuses Python's default (error 1010).
Requests go through the runner's `HTTPS_PROXY`, which urllib reads from the
environment, and redirects are never followed, so the token can't be sent
to a host the manifest doesn't declare.

No exception message carries the token, a URL with its query, or a response
body: only the endpoint with its ids masked and Wise's short error code.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from securo_wise import __version__
from securo_wise.errors import NeedsAuth, RateLimited, WiseError, WiseUnavailable

API = "https://api.wise.com"
USER_AGENT = f"securo-wise-plugin/{__version__}"
#: Seconds one request may take; a call's deadline can make it shorter.
REQUEST_TIMEOUT = 20.0
#: Below this many seconds left, a request isn't started at all.
MIN_REQUEST_TIME = 3.0
#: An activity page is a few hundred KB at most; anything near this is wrong.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


#: (url, headers, timeout) -> Response. Raises `OSError` when the request never got an answer.
Http = Callable[[str, dict[str, str], float], Response]


class OutOfTime(Exception):
    """The call's time budget is spent: stop fetching and return what there is."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_http(url: str, headers: dict[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status, response_headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        status, response_headers = exc.code, dict(exc.headers or {})
        body = exc.read(64 * 1024)
        exc.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise WiseError("Wise sent a response larger than 10 MB.")
    return Response(status, {k.lower(): v for k, v in response_headers.items()}, body)


def endpoint(path: str) -> str:
    """`/v1/profiles/123/activities` -> `/v1/profiles/{id}/activities`: safe to show."""
    return re.sub(r"/(?!v\d+(?:/|$))[^/]*\d[^/]*", "/{id}", path)


def _error_code(body: bytes) -> str:
    """Wise's short error code, if the body is JSON that has one; never the body itself."""
    try:
        data = json.loads(body)
    except ValueError:
        text = body[:200].decode(errors="replace")
        match = re.search(r"error code: (\d{3,5})", text)  # Cloudflare's plain-text refusals
        return f"error code {match.group(1)}" if match else ""
    if not isinstance(data, dict):
        return ""
    for key in ("code", "error"):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            return value
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        value = errors[0].get("code")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            return value
    return ""


class WiseClient:
    def __init__(
        self,
        token: str,
        *,
        http: Http | None = None,
        api: str = API,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._http = http or urllib_http
        self._api = api.rstrip("/")
        self._deadline = deadline
        self._clock = clock

    def time_left(self) -> float | None:
        return None if self._deadline is None else self._deadline - self._clock()

    def get(self, path: str, params: dict[str, Any] | None = None, *, optional: bool = False) -> Any:
        """The decoded JSON of a GET.

        With `optional`, a 403 or 404 returns None: for details Wise shows only
        to some tokens (a transfer someone else sent, a card transaction).
        """
        left = self.time_left()
        if left is not None and left < MIN_REQUEST_TIME:
            raise OutOfTime
        timeout = REQUEST_TIMEOUT if left is None else min(REQUEST_TIMEOUT, left - 1.0)
        given = {k: v for k, v in (params or {}).items() if v is not None}
        query = f"?{urllib.parse.urlencode(given)}" if given else ""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        where = endpoint(path)
        try:
            response = self._http(f"{self._api}{path}{query}", headers, timeout)
        except TimeoutError:
            raise WiseUnavailable(f"Wise didn't answer in time ({where}).") from None
        except OSError as exc:
            raise WiseUnavailable(f"Couldn't reach Wise ({where}): {type(exc).__name__}.") from None
        return self._decode(response, where, optional)

    def _decode(self, response: Response, where: str, optional: bool) -> Any:
        status, code = response.status, ""
        if status >= 300:
            code = _error_code(response.body).replace(self._token, "***")
        detail = f" ({code})" if code else ""
        if status == 401:
            raise NeedsAuth(f"Wise refused the API token{detail}: it may be revoked or mistyped.", code="token_refused")
        if status == 403 and response.headers.get("x-2fa-approval"):
            raise NeedsAuth(
                f"Wise asks for extra verification (SCA) to read {where}, which an API token can't give.",
                code="sca_required",
            )
        if status in (403, 404) and optional:
            return None
        if status == 403:
            raise NeedsAuth(f"Wise didn't let this token read {where}{detail}.", code="forbidden")
        if status == 429:
            wait = response.headers.get("retry-after", "")
            after = f" Wise asked to wait {wait} s." if wait.isdigit() else ""
            raise RateLimited(f"Wise is limiting requests ({where}).{after}")
        if status >= 500:
            raise WiseUnavailable(f"Wise had a problem answering {where} (HTTP {status}).")
        if status >= 300:
            raise WiseError(f"Wise answered {where} with HTTP {status}{detail}.")
        try:
            # Decimals, not floats: amounts are money, and a float loses cents on the way.
            return json.loads(response.body, parse_float=Decimal)
        except ValueError:
            raise WiseError(f"Wise answered {where} with something that isn't JSON.") from None
