"""Create test activity in a Wise SANDBOX account, for the sampler to read.

    WISE_TOKEN_FILE=path/to/sandbox-token python3 -I scripts/sandbox.py STEP [ARGS...]

Sandbox only: the host is fixed to api.wise-sandbox.com, where money is fake.
Needs a full-access sandbox token. Steps print status codes and ids of the
sandbox objects they make, never the token.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.wise-sandbox.com"  # never production
TOKEN = Path(os.environ["WISE_TOKEN_FILE"]).expanduser().read_text().strip()


def call(method: str, path: str, body: object = None, headers: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "securo-wise-sandbox/0.1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw[:300]


def idem() -> dict:
    return {"X-idempotence-uuid": str(uuid.uuid4())}


def profiles() -> dict[str, int]:
    _, body = call("GET", "/v2/profiles")
    return {p["type"]: p["id"] for p in body}


def balances(pid: int) -> list[dict]:
    _, body = call("GET", f"/v4/profiles/{pid}/balances?types=STANDARD,SAVINGS")
    return body


def balance(pid: int, currency: str, kind: str = "STANDARD") -> dict:
    return next(b for b in balances(pid) if b["currency"] == currency and b["type"] == kind)


def topup(pid: int, currency: str, amount: float) -> None:
    b = balance(pid, currency)
    status, body = call("POST", "/v1/simulation/balance/topup",
                        {"profileId": pid, "balanceId": b["id"], "currency": currency, "amount": amount})
    print("topup", currency, amount, status, body if status >= 300 else "")


def convert(pid: int, source: str, target: str, amount: float) -> None:
    status, quote = call("POST", f"/v3/profiles/{pid}/quotes",
                         {"sourceCurrency": source, "targetCurrency": target, "sourceAmount": amount, "payOut": "BALANCE"})
    if status >= 300:
        print("quote", status, quote)
        return
    status, body = call("POST", f"/v2/profiles/{pid}/balance-movements", {"quoteId": quote["id"]}, idem())
    print("convert", source, target, amount, status, body if status >= 300 else body.get("id"))


def jar(pid: int, currency: str, name: str) -> int | None:
    existing = [b for b in balances(pid) if b["type"] == "SAVINGS" and b["currency"] == currency]
    if existing:
        return existing[0]["id"]
    status, body = call("POST", f"/v4/profiles/{pid}/balances", {"currency": currency, "type": "SAVINGS", "name": name}, idem())
    print("jar", currency, status, body if status >= 300 else body.get("id"))
    return body.get("id") if isinstance(body, dict) else None


def move(pid: int, currency: str, amount: float, into_jar: bool) -> None:
    std, savings = balance(pid, currency), balance(pid, currency, "SAVINGS")
    source, target = (std, savings) if into_jar else (savings, std)
    status, body = call("POST", f"/v2/profiles/{pid}/balance-movements",
                        {"amount": {"value": amount, "currency": currency},
                         "sourceBalanceId": source["id"], "targetBalanceId": target["id"]}, idem())
    print("move", "into jar" if into_jar else "out of jar", currency, amount, status, body if status >= 300 else "")


RECIPIENTS = {
    # Wise's own sandbox examples: made-up accounts.
    "GBP": {"currency": "GBP", "type": "sort_code", "accountHolderName": "Test Recipient",
            "details": {"legalType": "PRIVATE", "sortCode": "231470", "accountNumber": "28821822"}},
    "EUR": {"currency": "EUR", "type": "iban", "accountHolderName": "Test Recipient",
            "details": {"legalType": "PRIVATE", "IBAN": "DE89370400440532013000"}},
}


def recipient(pid: int, currency: str) -> int | None:
    status, body = call("POST", "/v1/accounts", {"profile": pid, **RECIPIENTS[currency]})
    if status >= 300:
        print("recipient", currency, status, body)
        return None
    return body["id"]


def transfer(pid: int, source: str, target: str, amount: float, *, fund: bool, simulate: list[str]) -> int | None:
    account = recipient(pid, target)
    if account is None:
        return None
    status, quote = call("POST", f"/v3/profiles/{pid}/quotes",
                         {"sourceCurrency": source, "targetCurrency": target, "sourceAmount": amount, "targetAccount": account})
    if status >= 300:
        print("quote", status, quote)
        return None
    status, t = call("POST", "/v1/transfers", {"targetAccount": account, "quoteUuid": quote["id"],
                                               "customerTransactionId": str(uuid.uuid4()), "details": {"reference": "test"}})
    if status >= 300:
        print("transfer", status, t)
        return None
    print("transfer", source, target, amount, "id", t["id"], t["status"])
    if fund:
        status, body = call("POST", f"/v3/profiles/{pid}/transfers/{t['id']}/payments", {"type": "BALANCE"})
        print("  fund", status, body if status >= 300 else body.get("status"))
    for state in simulate:
        status, body = call("GET", f"/v1/simulation/transfers/{t['id']}/{state}")
        if status == 405:
            status, body = call("POST", f"/v1/simulation/transfers/{t['id']}/{state}")
        print("  simulate", state, status, body.get("status") if isinstance(body, dict) else body)
    return t["id"]


def cancel(transfer_id: int) -> None:
    status, body = call("PUT", f"/v1/transfers/{transfer_id}/cancel")
    print("  cancel", status, body.get("status") if isinstance(body, dict) else body)


def main() -> None:
    step, *args = sys.argv[1:]
    p = profiles()
    personal, business = p["PERSONAL"], p["BUSINESS"]
    if step == "basics":
        for pid in (personal, business):
            topup(pid, "GBP", 1000)
            topup(pid, "EUR", 500)
        convert(personal, "GBP", "EUR", 100)
        convert(personal, "EUR", "USD", 50)
        jar(personal, "EUR", "Holiday")
        move(personal, "EUR", 40, into_jar=True)
        move(personal, "EUR", 15, into_jar=False)
    elif step == "transfers":
        # Paying from a balance is refused here (403, like UK/EU personal tokens in production),
        # so these transfers are never funded from a balance: simulations move them along anyway.
        # Completed, same currency and cross-currency.
        transfer(personal, "GBP", "GBP", 120, fund=True, simulate=["processing", "funds_converted", "outgoing_payment_sent"])
        transfer(personal, "GBP", "EUR", 80, fund=True, simulate=["processing", "funds_converted", "outgoing_payment_sent"])
        # Funded but left in progress: should show as pending.
        transfer(personal, "GBP", "GBP", 33, fund=True, simulate=["processing"])
        # Created, never funded.
        transfer(personal, "GBP", "GBP", 21, fund=False, simulate=[])
        # Funded, then cancelled before it went out.
        tid = transfer(personal, "GBP", "GBP", 44, fund=True, simulate=[])
        if tid:
            cancel(tid)
        # Business: one completed send.
        transfer(business, "EUR", "EUR", 70, fund=True, simulate=["processing", "funds_converted", "outgoing_payment_sent"])
    elif step == "gbp":
        transfer(personal, "GBP", "GBP", 120, fund=False, simulate=["processing", "funds_converted", "outgoing_payment_sent"])
        transfer(personal, "GBP", "GBP", 33, fund=False, simulate=["processing"])
        transfer(personal, "GBP", "GBP", 21, fund=False, simulate=[])
        tid = transfer(personal, "GBP", "GBP", 44, fund=False, simulate=[])
        if tid:
            cancel(tid)
    elif step == "many":
        # Enough activity to need a second page of the feed (pages hold 100).
        for i in range(int(args[0]) if args else 60):
            move(personal, "EUR", 1, into_jar=True)
            move(personal, "EUR", 1, into_jar=False)
    elif step == "balances":
        for name, pid in p.items():
            print(name, [(b["type"], b["currency"], b["amount"]["value"]) for b in balances(pid)])
    else:
        sys.exit(f"unknown step {step}")


if __name__ == "__main__":
    main()
