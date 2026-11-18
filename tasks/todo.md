# Tasks: Wise for Securo

See [plan.md](plan.md) for the decisions behind these. Tests: `uv run --python 3.14 --group dev pytest -q`.

## Task 1: Wise API spike on the author's account

**Description:** Answer the mapping questions with real data before writing the mapping. The task adds a sampler script, `scripts/sample.py`. The author runs it with their read-only token. It writes redacted samples outside the repo:

- digits become 9
- names and descriptions become placeholders
- ids become sequential fakes

From those samples, the task writes the findings to `docs/wise-api.md` and builds made-up fixtures in `tests/fixtures/`.

**Acceptance criteria:**
- [x] `docs/wise-api.md` answers:
  - which balance each activity type hit, and whether the activity or its resource names it;
  - what `primaryAmount` and `secondaryAmount` hold for a foreign-currency card payment;
  - how a conversion, a fee, `BANK_DETAILS_ORDER` and a deposit appear;
  - whether `/v1/transfers/{id}` gives exact source and target amounts and fees;
  - whether a card-transaction detail endpoint accepts the token;
  - how activities paginate and whether `since`/`until` filter on `createdOn`.
- [x] Every amount format seen is in the fixtures. No real value appears in the repo.
- Not answered, because the account has none: conversions between own balances, foreign-currency card payments, pending items, Jars, multi-page feeds. Listed under "Not seen yet" in `docs/wise-api.md`.

**Verification:**
- [ ] Manual check: the author reads `docs/wise-api.md` and the fixtures and confirms nothing real leaked.

**Dependencies:** None

**Files likely touched:** `scripts/sample.py`, `docs/wise-api.md`, `tests/fixtures/*.json`

**Estimated scope:** Medium

---

## Checkpoint 0: Spike reviewed
- [ ] The author agrees with the mapping in `docs/wise-api.md` before Phase 1.

---

## Task 2: Repo skeleton and manifest

**Description:** Set up the repo in securo-ibkr's shape:
- `pyproject.toml` (Python 3.14, dev group pytest, no runtime dependencies)
- `securo-plugin.json`: integration `token`, `auth: "credentials"`, one secret field `token`, `provides: ["accounts", "transactions"]`, `sync_minutes: 60`, network host `api.wise.com`, a logo, en and pt-BR strings
- the `securo_wise` package with a `plugin.py` stub
- `errors.py` with `NeedsAuth`, `RateLimited` and a transient error
- `.gitignore`, `LICENSE` (MIT) and `icon.png`

**Acceptance criteria:**
- [x] The manifest passes Securo's own parser. Run `parse_manifest` from the Securo backend's virtualenv against it.
- [x] `sync` with no token raises `NeedsAuth`.
- [x] `icon.png` and `logos/wise.png`: Wise's own 256 px app icon from wise.com, named by every account (`institution_logo: "wise"`).

**Verification:**
- [ ] Tests pass.
- [ ] Manual check: the manifest is parsed with `~/Codes/securo/backend`'s `app.plugins.manifest`.

**Dependencies:** T1 (for the logo and wording only; can start in parallel)

**Files likely touched:** `pyproject.toml`, `securo-plugin.json`, `securo_wise/{__init__,plugin,errors}.py`, `tests/test_plugin.py`

**Estimated scope:** Small

---

## Task 3: HTTP client and error mapping

**Description:** Add `securo_wise/client.py`. It sends `Authorization: Bearer` with a JSON accept header, uses an injectable transport for tests, and enforces a per-call deadline. It maps errors as follows:
- 401 → `NeedsAuth`
- 403 with `x-2fa-approval` → `NeedsAuth`, with a message saying Wise requires extra verification for this endpoint
- 429 → `RateLimited`, respecting `retry-after`
- 5xx and timeouts → transient

Error messages name the endpoint and never contain the token.

**Acceptance criteria:**
- [x] Each mapping is covered by a test using a fake transport. Also smoke-tested on real Wise: optional transfer details 403 → None, statements → `NeedsAuth(sca_required)`, a wrong token → `NeedsAuth(token_refused)`.
- [x] A token never appears in an exception message (covered by a test, even when Wise echoes it).
- Added beyond the plan: a `User-Agent` (Cloudflare error 1010), no redirects, a 10 MB response cap, `optional=True` for details Wise hides, and `OutOfTime` once the call's deadline is near (for T8).

**Verification:**
- [ ] Tests pass.

**Dependencies:** T2

**Files likely touched:** `securo_wise/client.py`, `tests/test_client.py`

**Estimated scope:** Small

---

## Task 4: Balances become accounts

**Description:** Make `sync` list the profiles and their balances, and return a contract-1 payload with one account per balance:
- external id `wise:<profileId>:<balanceId>`
- name such as "Wise USD" or "Wise Business USD", and the Jar's name for Jars
- type `checking` for STANDARD balances and `savings` for Jars (to confirm)
- Wise's balance and currency
- `institution_logo: "wise"`
- `transactions: null`, because transactions aren't fetched yet

Amounts go out as strings with at most 8 decimals.

**Acceptance criteria:**
- [x] The payload passes Securo's `parse_payload`: checked by hand with upstream Securo's parser on the author's real account (5 accounts) and the sandbox (16, including a Jar).
- [x] Two profiles with the same currency produce two distinct accounts.
- [x] Every balance becomes an account, hidden (`visible: false`) or empty ones included: it is money Wise holds. Record this in `DESIGN.md` (T10).
- Decided: STANDARD balances are `checking` and Jars are `savings`. Names are "Wise USD", "Wise Business USD" and "Wise Jar: Holiday (EUR)". The client parses JSON numbers as `Decimal`.

**Verification:**
- [ ] Tests pass.
- [ ] Manual check (Checkpoint A): install the plugin in the author's Securo, connect it, and compare every balance with the Wise app.

**Dependencies:** T3

**Files likely touched:** `securo_wise/plugin.py`, `securo_wise/payload.py`, `tests/test_accounts.py`

**Estimated scope:** Medium

---

## Checkpoint A: Accounts in a real Securo
- [x] All tests pass.
- [x] Both profiles' balances appear in Securo under Bank Connections, with the Wise logo and the correct amounts.
- [x] Review with the author before continuing.

---

## Task 5: Activity parsing

**Description:** Add `securo_wise/activity.py`. It turns one feed item into a parsed activity:
- amount (`Decimal`), currency, direction (`<positive>` and `+` mean money in)
- the title with its markup tags removed, and the description
- the date from `createdOn`
- the status: COMPLETED → posted, IN_PROGRESS/UPCOMING/REQUIRES_ATTENTION → pending, CANCELLED → dropped
- the resource type and id

Parsing is strict: an amount it doesn't recognise raises an error naming the format with its digits masked. It never guesses.

**Acceptance criteria:**
- [x] Every format in the T1 fixtures parses, including thousands separators, no decimals, and the tagged positive form.
- [x] An unknown format fails with a masked message.

**Verification:**
- [ ] Tests pass.

**Dependencies:** T1, T3

**Files likely touched:** `securo_wise/activity.py`, `tests/test_activity.py`

**Estimated scope:** Small

---

## Task 6: Activities become transactions

**Description:** Page through each profile's activities over a recent window and turn each one into a contract transaction on the right balance's account, using the rule decided in T1:
- external id `wise:<profileId>:<activityId>`
- description and payee, with `payee` taken from the title
- a positive amount, with `type` set to debit or credit
- `currency` and `amount_in_account_currency` when they differ, for card payments abroad
- `provider_category` from the activity type
- `status`

An activity the plugin can't place on an account fails the sync with a clear message instead of being dropped silently.

**Acceptance criteria:**
- [x] Card payments, deposits, transfers and `BANK_DETAILS_ORDER` from the fixtures land on the expected accounts with the expected signs. Conversions and Jar moves (`INTERBALANCE`) become a debit plus a `:in` credit; unfunded and cancelled transfers are left out.
- [x] On the author's real account, each account's transactions add up exactly to its Wise balance (24 transactions). In the sandbox, the Jar's 108 moves add up exactly too.
- Decided: `amount` is what the balance moved, in the account's currency (no `amount_in_account_currency`), and the date is the UTC day of `createdOn`.
- [x] The same feed mapped twice gives identical external ids, and an item repeated across pages counts once.

**Verification:**
- [ ] Tests pass.

**Dependencies:** T4, T5

**Files likely touched:** `securo_wise/mapping.py`, `securo_wise/payload.py`, `tests/test_mapping.py`

**Estimated scope:** Medium

---

## Task 7: Conversions as transfers, and fees

**Description (revised after T1):** The feed already gives exact amounts, so transfer details are no longer needed for them. What was left:
- **Conversions and Jar moves** must not count as spending and income. Securo pairs transfers by exact amount, which pairs Jar moves but never a conversion's two legs.
- **The fee** inside a send should be recorded somewhere.

**Acceptance criteria:**
- [x] Moves between the person's own balances are described `Wise: converted EUR to USD` / `Wise: moved EUR balance to Holiday Jar`. The manifest ships the rule `wise-own-balances` (description starts with `Wise: ` → Securo's built-in `transfers` category, which has `treat_as_transfer`). Upstream Securo and the QA instance both accept the manifest.
- [x] No other transaction gets the `Wise: ` prefix (covered by a test).
- [x] A same-currency send stays one transaction for the whole amount that left the balance. The fee (`secondaryAmount − primaryAmount`) is kept in `raw_data.fee`. A cross-currency send's fee isn't comparable from the feed, so it is left out rather than fetched from the quote, which Wise sometimes refuses.

**Dependencies:** T6

---

## Checkpoint B: Recent transactions correct
- [x] All tests pass.
- [x] For the last 30 days, every Wise activity in both profiles appears once in Securo, with the right account, sign, amount and date.
- [x] Review with the author before continuing.

---

## Task 8: Backfill across syncs

**Description:** The first sync walks the feed back to the oldest activity within a budget of about 40 s and fewer than 20,000 transactions per reply. It keeps the paging cursor in `state["backfill"]` and continues on the next sync until the feed is exhausted. Records from each batch are sent as they are fetched.

**Acceptance criteria:**
- [x] With a fake clock and transport, a long history is fetched over several calls with nothing missed or repeated.
- [x] A sync interrupted before its records are written resumes from the previous cursor. Securo only keeps `state` after a successful write, and a test covers this.

**Verification:**
- [x] Tests pass.

- Checked live: in the sandbox, a read capped after page 1 resumed from Wise's own cursor, and the two syncs equal one full read (223 transactions). A saved cursor Wise refuses restarts the read from the start.

**Dependencies:** T7

**Files likely touched:** `securo_wise/plugin.py`, `tests/test_backfill.py`

**Estimated scope:** Medium

---

## Task 9: Incremental syncs and pending updates

**Description:** Once the backfill is complete, each sync re-reads activities from Securo's `since` minus an overlap margin, so that pending activities that complete or are cancelled get updated. A cancelled activity that was already synced as pending is handled as decided in `DESIGN.md`, for example sent as posted with zero amount, or left for Securo to expire.

**Acceptance criteria:**
- [x] A pending activity that later completes ends up as one posted transaction.
- [x] Running the same sync twice returns the same records.

**Verification:**
- [x] Tests pass.

- Built: later syncs re-read from the last complete read less 30 days, using Wise's `since` filter, which was checked live on the real account. Pending transactions are tracked in `state.pending`; one that Wise cancels is sent again as a posted 0 with "(cancelled)" appended, because the contract can't delete.

**Dependencies:** T8

**Files likely touched:** `securo_wise/plugin.py`, `tests/test_incremental.py`

**Estimated scope:** Small

---

## Task 10: README and DESIGN

**Description:**
- **README:** what a sync brings in; how to create a read-only token, and that this needs a Wise business account; the three-way coverage table; what the plugin can reach (`api.wise.com`); and how to run the tests.
- **DESIGN.md:** a numbered decisions table (D-numbers), like securo-ibkr's, covering the external-id formats, the activities-not-statements choice, conversions, pending handling and backfill.

**Acceptance criteria:**
- [x] Every decision the code depends on has a D-number, and the code cites it.
- [x] The README contains no real data.

**Verification:**
- [x] Manual check: the author reads both files.

**Dependencies:** T9

**Files likely touched:** `README.md`, `DESIGN.md`

**Estimated scope:** Small

---

## Task 11: End-to-end on the author's account

**Description:** Install the finished plugin in the author's Securo, connect it with a fresh read-only token, and let the backfill complete. Then compare against the Wise app. Fix any discrepancies with a test for each.

**Acceptance criteria:**
- [ ] Every balance of both profiles matches Wise.
- [ ] The transaction count and totals per account match Wise's activity list for the whole history.
- [ ] A second "Sync now" adds and changes nothing.

**Verification:**
- [ ] Manual check by the author.

**Dependencies:** T10

**Files likely touched:** whichever files a discrepancy points to, plus their tests

**Estimated scope:** Small to Medium

---

## Checkpoint C: Complete
- [ ] All acceptance criteria met.
- [ ] Tests pass.
- [ ] Ready to publish a release, if the author wants to.
