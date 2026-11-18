"""Task 1 spike: sample what Wise returns for a read-only token, redacted.

    python3 -I scripts/sample.py [OUTPUT]      (default: ~/wise-samples.redacted.json)

Reads the token from the file named by WISE_TOKEN_FILE, or else from a
hidden prompt, and writes one JSON file OUTSIDE the
repo. Nothing real is kept:

- every digit becomes 9 (amounts keep their shape: "9,999.99 USD")
- ids become fakes (P1, B1, A1, T1...), the same real id always the same fake,
  so relations survive: an activity's resource and a transfer's balance ids
  can still be matched to the balances list
- free text becomes X, except tags, ENUM_VALUES, currency codes and a short
  list of interface words ("Sent", "to", "Spent"...), so the structure shows
  without names or merchants

The comparisons that need real values (does an activity's amount equal its
transfer's source or target amount? do since/until filter on createdOn?)
run here, on the real data, and only their verdicts are written.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: WISE_API=https://api.wise-sandbox.com samples a sandbox account instead.
API = os.environ.get("WISE_API", "https://api.wise.com").rstrip("/")
MAX_ACTIVITIES = int(os.environ.get("WISE_MAX_ACTIVITIES", 400))  # per profile: enough to see every type without walking years
MAX_DETAILS = 25  # transfer / card detail calls per profile

# Words Wise's interface uses around names; anything else in free text is masked.
KEEP_WORDS = {
    "sent", "send", "to", "from", "by", "you", "spent", "at", "added", "add", "received", "receive",
    "converted", "convert", "card", "payment", "fee", "fees", "for", "with", "money", "balance",
    "moved", "move", "into", "jar", "account", "details", "order", "ordered", "cashback", "refund",
    "refunded", "interest", "direct", "debit", "cancelled", "declined", "pending", "and", "your",
    "the", "a", "of", "on", "in", "transfer", "deposit", "topped", "up", "withdrawal", "atm",
    "paid", "charged", "exchange", "rate", "wise", "business", "personal", "self", "own",
}
ENUM = re.compile(r"^[A-Z][A-Z0-9_#.-]*$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
TAG = re.compile(r"(</?[a-z]+>)")
ID_KEY = re.compile(r"(^id$|Id$|_id$|^id_|uuid|token|reference|number|iban|account(?!Type)|^key$)", re.I)


class Fakes:
    """The same real id always maps to the same fake, per prefix."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str], str] = {}
        self.count: dict[str, int] = {}

    def __call__(self, prefix: str, real: object) -> str:
        key = (prefix, str(real))
        if key not in self.seen:
            self.count[prefix] = self.count.get(prefix, 0) + 1
            self.seen[key] = f"{prefix}{self.count[prefix]}"
        return self.seen[key]


FAKE = Fakes()


def mask_digits(text: str) -> str:
    return re.sub(r"\d", "9", text)


def mask_text(text: str) -> str:
    """Keep tags, enum-like tokens, currencies and interface words; X out the rest."""
    out = []
    for part in TAG.split(text):
        if TAG.fullmatch(part):
            out.append(part)
            continue
        words = []
        for token in re.split(r"(\s+)", part):
            bare = token.strip(".,:;!?()'\"")
            if not token.strip():
                words.append(token)
            elif bare.lower() in KEEP_WORDS or CURRENCY.fullmatch(bare) or re.fullmatch(r"[+\-−]?[\d.,]*", bare):
                words.append(mask_digits(token))
            else:
                words.append("X")
        out.append("".join(words))
    return re.sub(r"X(\s+X)+", "X", "".join(out))


def redact(value: object, key: str = "") -> object:
    """Redact any JSON value; `key` is the field name it sits under."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if ID_KEY.search(key) and not isinstance(value, (dict, list)):
        return FAKE("I", value)
    if isinstance(value, (int, float)):
        return mask_digits(str(value))
    text = str(value)
    if ENUM.fullmatch(text) or CURRENCY.fullmatch(text) or re.fullmatch(r"[a-z]+(_[a-z]+)+", text):
        return text  # enum values, including Wise's lower_snake transfer states
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?", text):
        return mask_digits(text[:4]) + text[4:7] + mask_digits(text[7:])  # keep the month only
    return mask_text(text)


def amount(text: str | None) -> tuple[Decimal, str] | None:
    """'<positive>+ 1,234.50 USD</positive>' -> (Decimal('1234.50'), 'USD')."""
    if not text:
        return None
    bare = TAG.sub("", text).replace("+", "").replace("−", "-").strip()
    match = re.fullmatch(r"-?\s*([\d,]+(?:\.\d+)?)\s+([A-Z]{3})", bare)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", "")), match.group(2)
    except InvalidOperation:
        return None


def relation(a: tuple[Decimal, str], b: tuple[Decimal, str]) -> str:
    """How amount `a` relates to `b`, without saying what either is."""
    if a[1] != b[1]:
        return "other currency"
    if a[0] == b[0]:
        return "equal"
    if a[0] == b[0].quantize(Decimal(1)) or a[0] == b[0].to_integral_value(rounding="ROUND_FLOOR"):
        return "equal when rounded to a whole number"
    return "smaller" if a[0] < b[0] else "larger"


def quote_fee(quote: dict) -> tuple[Decimal, str] | None:
    """The fee of the quote's selected (or first) payment option, in the source currency."""
    options = quote.get("paymentOptions") or []
    # A transfer paid from a Wise balance uses the BALANCE pay-in option.
    chosen = next((o for o in options if o.get("payIn") == "BALANCE" and o.get("payOut") == quote.get("payOut")), None)
    chosen = chosen or next((o for o in options if o.get("payIn") == "BALANCE"), None)
    for source in (chosen, options[0] if options else None, quote):
        fee = (source or {}).get("fee")
        if isinstance(fee, dict) and fee.get("total") is not None:
            return Decimal(str(fee["total"])), quote.get("sourceCurrency")
        if isinstance(fee, (int, float, str)) and source is quote:
            return Decimal(str(fee)), quote.get("sourceCurrency")
    return None


class Wise:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[dict] = []

    def get(self, path: str) -> tuple[int, dict, object]:
        req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                     # Cloudflare in front of the sandbox refuses Python's default (error 1010).
                                     "User-Agent": "securo-wise-sampler/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, headers, raw = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, raw = exc.code, dict(exc.headers), exc.read()
        try:
            body: object = json.loads(raw or b"null")
        except ValueError:
            body = raw.decode(errors="replace")[:200]
        # Every path segment holding a digit (ids, card tokens) is faked, except API versions.
        shown = re.sub(r"/(?!v\d+(?:/|$))[^/]*\d[^/]*", lambda m: "/" + FAKE("I", m.group(0)[1:]), path.split("?")[0])
        self.calls.append({"path": shown, "status": status, "sca": headers.get("x-2fa-approval-result")})
        return status, headers, body


def sample_profile(wise: Wise, profile: dict, now: datetime) -> dict:
    pid = profile["id"]
    out: dict = {"type": profile.get("type"), "profile_keys": sorted(profile)}

    status, _, balances = wise.get(f"/v4/profiles/{pid}/balances?types=STANDARD,SAVINGS")
    balances = balances if status == 200 else []
    out["balances"] = [redact(b) for b in balances]
    for b in balances:
        FAKE("I", b["id"])  # register so later references show as the same fake

    # Activities: walk pages of 100, keep up to MAX_ACTIVITIES.
    items: list[dict] = []
    cursor, pages = None, 0
    while len(items) < MAX_ACTIVITIES:
        path = f"/v1/profiles/{pid}/activities?size=100" + (f"&nextCursor={cursor}" if cursor else "")
        status, _, body = wise.get(path)
        if status != 200 or not isinstance(body, dict):
            out["activities_error"] = {"status": status, "body": redact(body)}
            break
        pages += 1
        items.extend(body.get("activities") or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    created = [a.get("createdOn") or "" for a in items]
    out["activities_paging"] = {
        "pages": pages,
        "count": len(items),
        "more_left": bool(cursor),
        "newest_first": created == sorted(created, reverse=True),
        "newest_updated_first": [a.get("updatedOn") or "" for a in items]
        == sorted((a.get("updatedOn") or "" for a in items), reverse=True),
        "oldest_month": min(created)[:7] if created else None,
    }

    unparsed = sorted({mask_digits(a.get(k) or "") for a in items for k in ("primaryAmount", "secondaryAmount")
                       if a.get(k) and amount(a.get(k)) is None})
    out["amount_formats_unparsed"] = unparsed
    out["amount_formats"] = sorted({mask_digits(a.get(k) or "") for a in items for k in ("primaryAmount", "secondaryAmount")})

    # One full redacted example of every (type, resource type, status, has secondary) combination.
    examples: dict[str, dict] = {}
    for a in items:
        sig = f"{a.get('type')}|{(a.get('resource') or {}).get('type')}|{a.get('status')}|secondary={bool(a.get('secondaryAmount'))}"
        examples.setdefault(sig, redact(a))
    out["activity_examples"] = examples
    out["activity_counts"] = {}
    for a in items:
        sig = f"{a.get('type')}|{(a.get('resource') or {}).get('type')}"
        out["activity_counts"][sig] = out["activity_counts"].get(sig, 0) + 1

    # since / until: do they filter, and on which timestamp?
    if len(items) >= 4:
        pivot = sorted(created)[len(created) // 2]
        status, _, body = wise.get(f"/v1/profiles/{pid}/activities?size=100&since={pivot}")
        got = (body.get("activities") or []) if status == 200 and isinstance(body, dict) else []
        out["since_filter"] = {
            "status": status,
            "returned": len(got),
            "all_created_on_or_after": all((a.get("createdOn") or "") >= pivot for a in got),
            "all_updated_on_or_after": all((a.get("updatedOn") or "") >= pivot for a in got),
            "expected_at_most": sum(1 for c in created if c >= pivot),
        }

    # Transfer details, compared with the activity that points at them.
    transfers = []
    for a in [a for a in items if (a.get("resource") or {}).get("type") == "TRANSFER"][:MAX_DETAILS]:
        rid = a["resource"]["id"]
        status, _, t = wise.get(f"/v1/transfers/{rid}")
        entry: dict = {"activity_type": a.get("type"), "status": status}
        if status == 200 and isinstance(t, dict):
            prim, sec = amount(a.get("primaryAmount")), amount(a.get("secondaryAmount"))
            src = (Decimal(str(t.get("sourceValue"))), t.get("sourceCurrency")) if t.get("sourceValue") is not None else None
            tgt = (Decimal(str(t.get("targetValue"))), t.get("targetCurrency")) if t.get("targetValue") is not None else None
            entry["primary_matches"] = "source" if prim == src else "target" if prim == tgt else "neither"
            entry["secondary_matches"] = None if sec is None else "source" if sec == src else "target" if sec == tgt else "neither"
            entry["same_currency"] = t.get("sourceCurrency") == t.get("targetCurrency")
            entry["positive_tag"] = "<positive>" in (a.get("primaryAmount") or "")
            if sec is not None and src is not None:
                entry["secondary_vs_source"] = relation(sec, src)
            if sec is not None and tgt is not None:
                entry["secondary_vs_target"] = relation(sec, tgt)
            if prim is not None and src is not None:
                entry["primary_vs_source"] = relation(prim, src)
            # The quote holds the fee: does the feed's amount equal the source with or without it?
            if t.get("quoteUuid"):
                qs, _, q = wise.get(f"/v3/profiles/{pid}/quotes/{t['quoteUuid']}")
                entry["quote_status"] = qs
                if qs == 200 and isinstance(q, dict):
                    fee = quote_fee(q)
                    entry["quote_fee_found"] = fee is not None
                    entry["quote_keys"] = sorted(q)
                    entry["quote_payment_option_keys"] = sorted((q.get("paymentOptions") or [{}])[0])
                    if fee is not None and src is not None and fee[1] == src[1]:
                        for name, value in (("primary", prim), ("secondary", sec)):
                            if value is not None and value[1] == src[1]:
                                entry[f"{name}_equals_source_minus_fee"] = value[0] == src[0] - fee[0]
                                entry[f"{name}_equals_source_plus_fee"] = value[0] == src[0] + fee[0]
            entry["redacted"] = redact(t)
        transfers.append(entry)
    out["transfers"] = transfers

    # Card transactions: is there a detail endpoint this token can read?
    cards = [a for a in items if (a.get("resource") or {}).get("type") == "CARD_TRANSACTION"]
    status, _, card_list = wise.get(f"/v3/spend/profiles/{pid}/cards")
    out["cards_list"] = {"status": status, "body": redact(card_list)}
    tokens = []
    if status == 200 and isinstance(card_list, dict):
        tokens = [c.get("token") for c in card_list.get("cards") or [] if c.get("token")]
    card_details = []
    for a in cards[:5]:
        rid = a["resource"]["id"]
        for path in [f"/v3/spend/profiles/{pid}/cards/{tok}/transactions/{rid}" for tok in tokens[:2]] + \
                    [f"/v4/spend/profiles/{pid}/cards/transactions/{rid}"]:
            status, _, body = wise.get(path)
            if status == 200:
                card_details.append({"status": status, "redacted": redact(body),
                                     "activity_primary": mask_digits(a.get("primaryAmount") or ""),
                                     "activity_secondary": mask_digits(a.get("secondaryAmount") or "")})
                break
        else:
            card_details.append({"status": status, "found": False})
    out["card_details"] = card_details
    return out


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "~/wise-samples.redacted.json").expanduser().resolve()
    repo = Path(__file__).resolve().parent.parent
    if repo in target.parents:
        sys.exit(f"Write the samples outside the repo ({repo}).")
    token_file = os.environ.get("WISE_TOKEN_FILE")
    token = Path(token_file).expanduser().read_text() if token_file else getpass.getpass("Wise read-only API token: ")
    wise = Wise(token.strip())
    status, _, profiles = wise.get("/v2/profiles")
    if status != 200 or not isinstance(profiles, list):
        sys.exit(f"profiles: HTTP {status}")
    now = datetime.now(timezone.utc)
    result = {"sampled_on": now.strftime("%Y-%m"), "profiles": [sample_profile(wise, p, now) for p in profiles]}
    result["calls"] = wise.calls
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {target} ({len(wise.calls)} API calls). Read it before sharing.")


if __name__ == "__main__":
    main()
