# Implementation Plan: Wise for Securo

## Overview

A Securo provider plugin, in the style of [securo-ibkr](https://github.com/fredson-sumi/securo-ibkr), that syncs Wise as a connected bank. The user pastes one **read-only Wise API token**. Each sync brings in:

- One account for each currency balance in every profile the token can see (personal and business), with Wise's exact balance.
- Transactions from Wise's **activities feed**, back to the oldest activity. Where the transfers endpoint (or a card endpoint, if one accepts the token) gives exact figures, those replace the feed's formatted amounts.
- No duplicates on later syncs: every record has a stable `external_id`.

**Done** means it syncs the author's own Wise personal and business profiles correctly in their Securo. Listing it in the catalog is not part of this work.

## Who it covers

| Wise user | Route into Securo |
|---|---|
| Has a Wise business account (its token also sees the personal profile) | This plugin |
| Personal account in the EU/UK | Securo's built-in Enable Banking connector, if it lists Wise (unverified) |
| Personal account anywhere else | Export a QIF/CSV statement from Wise and use Securo's import |

The README explains all three.

## What the API allows (probed 2026-11-18 on the author's account)

Wise's docs understate what a personal token can reach, so these probe results take precedence over the docs.

| Endpoint | Result | Use |
|---|---|---|
| `GET /v2/profiles` | 200: PERSONAL and BUSINESS | profiles |
| `GET /v4/profiles/{id}/balances?types=STANDARD,SAVINGS` | 200 | accounts and balances |
| `GET /v1/profiles/{id}/activities` | 200: TRANSFER, CARD_PAYMENT, BALANCE_DEPOSIT, BANK_DETAILS_ORDER | transaction history |
| `GET /v1/transfers?profile={id}` | 200 | exact transfer figures |
| `GET /v1/profiles/{id}/balance-statements/...` | **403, SCA** on both profiles | not used |

**Ruled out:**
- **Signing the SCA challenge with the user's own RSA key:** rejected (403) even after the public key was uploaded.
- **The newer one-time-token SCA:** Wise returns `ott_not_found` for this challenge.
- **Webhooks:** they need a public HTTPS Securo and carry no history.
- **OAuth:** needs a Wise partner agreement.

## Architecture decisions

- **Activities feed as the source of history.** Statements are blocked by SCA in most regions. The feed works with a read-only token, but its amounts are formatted text (`"9,999.99 USD"`, `<positive>+ 999 USD</positive>`). Those strings get parsed, and exact figures replace them wherever a detail endpoint provides them.
- **Standard library only, no lockfile**, the same as securo-ibkr's D7. With RSA signing gone, there is no need for `cryptography`.
- **Stable external ids, fixed once shipped:**
  - accounts: `wise:<profileId>:<balanceId>`
  - transactions: `wise:<profileId>:<activityId>`, plus a suffix for the second side of a two-sided activity such as a conversion

  Securo matches every record by external id, so changing a format later would duplicate what's already synced.
- **History is backfilled across several syncs.** A plugin call is stopped at 60 s and a reply holds at most 20,000 transactions. The plugin walks the feed back in time within a budget of about 40 s per call, keeps a cursor in `state`, and continues on the next sync. Securo keeps `state` only once the records are written, so a failed sync repeats safely.
- **Incremental syncs re-read an overlap window**, so that pending activities that later complete or are cancelled get updated. The overlap uses Securo's `since` plus a margin.
- **Error mapping:**
  - 401 → `NeedsAuth` (a revoked or wrong token)
  - 429 → `RateLimited`, respecting `retry-after`
  - 5xx and network failures → a transient failure that Securo retries
  - a 403 from SCA on any endpoint the plugin uses → `NeedsAuth` with a clear message
- **Only `api.wise.com` is reachable.** The manifest declares it as the plugin's only network host.

## Dependency graph

```
T1 API spike (redacted samples → fixtures + notes)
 │
 ├── T2 Repo skeleton + manifest
 │     └── T3 HTTP client + error mapping
 │           └── T4 Slice: balances → accounts  ── Checkpoint A (real Securo)
 │                 └── T5 Activity parsing (amounts, sign, status, dates)
 │                       └── T6 Slice: activities → transactions
 │                             └── T7 Transfer enrichment + conversions ── Checkpoint B
 │                                   ├── T8 Backfill across syncs
 │                                   └── T9 Incremental + pending updates
 │                                         └── T10 README + DESIGN
 │                                               └── T11 End-to-end on the author's account ── Checkpoint C
```

T1 comes first because it carries the most risk. If the feed can't tell which balance an activity hit, or can't give amounts for foreign-currency card payments, then the mapping in T5–T7 changes.

## Task list

The full tasks, with acceptance criteria, are in [`todo.md`](todo.md).

### Phase 0: Spike
- [ ] T1: Wise API spike on the author's account

### Checkpoint 0: Spike reviewed
- [ ] The mapping questions in T1 are answered and recorded in `docs/wise-api.md`. The author agrees with the mapping before Phase 1 starts.

### Phase 1: Foundation and accounts
- [ ] T2: Repo skeleton and manifest
- [ ] T3: HTTP client and error mapping
- [ ] T4: Balances become accounts

### Checkpoint A: Accounts in a real Securo
- [ ] Tests pass. The plugin installs in the author's Securo, connects, and shows every balance of both profiles with Wise's exact amounts.

### Phase 2: Transactions
- [ ] T5: Activity parsing
- [ ] T6: Activities become transactions
- [ ] T7: Transfer enrichment and conversions

### Checkpoint B: Recent transactions correct
- [ ] Recent transactions in Securo match the Wise app, with amounts, signs, dates and the account each landed in all correct.

### Phase 3: History and incremental sync
- [ ] T8: Backfill across syncs
- [ ] T9: Incremental syncs and pending updates

### Phase 4: Docs and verification
- [ ] T10: README and DESIGN
- [ ] T11: End-to-end on the author's account

### Checkpoint C: Complete
- [ ] Full history from both profiles is in Securo, balances match, and a second sync adds nothing new.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The feed doesn't say which balance an activity hit | High | T1 checks this. Fall back to the currency of the amount, and use transfer details for conversions. Jars are ambiguous, so flag that if the author has none to test with |
| Foreign-currency card payments show the merchant's currency, not the amount charged | High | T1 compares `primaryAmount` and `secondaryAmount` on a real foreign card payment |
| The format of formatted amounts varies (locale, separators, tags) | Med | T5 parses strictly and fails loudly on an unknown format instead of guessing. Fixtures cover every format seen |
| Activity ids or contents change after posting | Med | T9's overlap window re-reads recent activities. External ids use the activity id only |
| Wise undocumented behaviour changes (these endpoints aren't documented for personal tokens) | Med | The README states this. Errors name the endpoint so a break is easy to diagnose |
| Full history is slow on old accounts (one transfer-detail call per transfer) | Low | T8's time budget and cursor. The rate limits (900/min) are not the bottleneck |
| Real data leaking into the public repo | High | Follow securo-ibkr's D3: fixtures are made up, and the spike script redacts and writes outside the repo |

## Open questions

- How does Securo turn `provider_category` into a category for plugin transactions, and can a conversion be marked as a transfer (via the plugin's own `categories` with `treat_as_transfer`, or a rule on `provider_category` from Securo #1594)? Settle this in T7.
- Is there a card-transaction detail endpoint that accepts a personal token (merchant, exact amounts)? T1 checks; if not, card payments keep the feed's data.
- How will the author install a development build into their Securo: the same way securo-ibkr was tested (a GitHub release with the `securo-plugin` topic, or a local index)? Confirm before Checkpoint A.
- What account `type` should balances use: `checking` for STANDARD and `savings` for Jars? Confirm in T4.
