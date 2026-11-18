"""How a sync reports trouble to Securo.

Securo reads the exception's class name: `NeedsAuth` means the person has to
fix the connection (and pauses scheduled syncs until they do), `RateLimited`
means try again at the usual interval without counting a failure, and any
other exception is a failure Securo retries with backoff.
"""
from __future__ import annotations

HELP_URL = "https://wise.com/your-account/integrations-and-tools/api-tokens"


class NeedsAuth(Exception):
    """The token is missing, wrong or revoked, or Wise wants something only the person can give."""

    def __init__(self, message: str, *, code: str = "credentials_invalid") -> None:
        super().__init__(f"{message} API tokens: {HELP_URL}")
        self.code = code


class RateLimited(Exception):
    """Wise is throttling: nothing is wrong, the next sync tries again."""


class WiseError(Exception):
    """Wise answered something the plugin can't use; Securo retries with backoff."""


class WiseUnavailable(WiseError):
    """A temporary Wise or network failure; Securo retries with backoff."""
