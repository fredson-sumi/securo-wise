# What the Wise API gives a read-only token

These findings come from Task 1: `scripts/sample.py` was run on 2026-11-18 against the author's account, which has two profiles:

- PERSONAL: USD and BRL balances, 8 activities, oldest from 2026-08.
- BUSINESS: USD, BRL and EUR balances, 16 activities, oldest from 2026-06.

Where this file disagrees with Wise's docs, trust this file. The docs say a personal token reaches only quotes, recipients and transfers, but it also reads profiles, balances and activities.

The samples were redacted: every digit became 9, every id became a fake such as `I3`, and all text became `X`. Comparisons that needed real values ran inside the script, and only their verdicts are recorded here.

## Endpoints

| Call | Status | Notes |
|---|---|---|
| `GET /v2/profiles` | 200 | Both profiles. `type` is `PERSONAL` or `BUSINESS`. |
| `GET /v4/profiles/{p}/balances?types=STANDARD,SAVINGS` | 200 | See [Balances](#balances). |
| `GET /v1/profiles/{p}/activities?size=100[&nextCursor=…][&since=…]` | 200 | See [Activities](#activities). |
| `GET /v1/transfers/{id}` | 200, sometimes 403 | 6 of 13 business transfers returned 403, probably payments other people sent. The plugin must work without this call. |
| `GET /v3/profiles/{p}/quotes/{quoteUuid}` | 200 | Holds the fee in `paymentOptions[]`, under the option whose `payIn` is `BALANCE`. |
| `GET /v3/spend/profiles/{p}/cards` | 403 | No card endpoint accepts a read-only personal token. |
| `GET /v4/spend/profiles/{p}/cards/transactions/{id}` | 403 | Same. |
| `GET /2026Q4/spend/profiles/{p}/cards[/transactions/{id}]` | 403 (`unauthorized`, `forbidden`) | The documented path. Its reference says `Security: UserToken` (partner OAuth) and only covers the last 90 days. That reference also lists the fields this token can't reach: merchant category (MCC), merchant city and country, original amount and currency, and fees. |
| `GET /v1/profiles/{p}/balance-statements/…` | 403, SCA | Blocked; see `tasks/plan.md`. |

## Balances

Each balance has these fields:
- `id`
- `currency`
- `type` (`STANDARD`; Jars would be `SAVINGS`)
- `name` (null for STANDARD)
- `amount`, `cashAmount`, `reservedAmount` and `totalWorth`, each as `{value, currency}`
- `investmentState`
- `creationTime`, `modificationTime`
- `visible`, `primary`
- `groupId`
- `recipientId`

`amount.value` is a JSON number, not a string. Convert it with `Decimal(str(value))`.

**`recipientId` identifies a balance from the outside.** An incoming transfer's `targetAccount` equals the receiving balance's `recipientId`. This was an exact match in the business USD balance.

## Activities

Each activity has these fields:
- `id`
- `type`
- `resource`: `{type, id}`
- `title`: may contain markup tags
- `description`
- `primaryAmount`, `secondaryAmount`: formatted text
- `status`
- `createdOn`, `updatedOn`: ISO 8601 with milliseconds and `Z`

**Types seen:**

| `type` | `resource.type` | Example `title` / `description` | Direction |
|---|---|---|---|
| `TRANSFER` | `TRANSFER` | `<strong>{recipient}</strong>` / `Sent` | out |
| `TRANSFER` | `TRANSFER` | `<strong>{sender}</strong>` / `""` | in (`<positive>`) |
| `CARD_PAYMENT` | `CARD_TRANSACTION` | `<strong>{merchant}</strong>` / `""` | out |
| `BALANCE_DEPOSIT` | `TRANSFER` | `To <strong>{CCY}</strong>` / `Added` | in |
| `BANK_DETAILS_ORDER` | `BANK_DETAILS_ORDER` | `For <strong>your account details</strong>` / `""` | out (the account-details fee) |

Every status seen was `COMPLETED`. The docs also list `REQUIRES_ATTENTION`, `IN_PROGRESS`, `UPCOMING` and `CANCELLED`.

**Amount formats seen.** All parsed:

```
9,999.99 USD      999 BRL      99.99 USD      (no decimals when whole)
<positive>+ 9,999.99 USD</positive>      <positive>+ 999 BRL</positive>
""                                         (secondaryAmount only)
```

The format is the number with `,` thousands separators and `.` decimals, then a space and the currency code. Money coming in is wrapped in `<positive>+ … </positive>`. No negative sign was seen: outgoing amounts are unsigned.

### What the amounts mean

These rules were checked against the transfer details and quotes of every transfer the token could read: 8 personal and 8 business.

1. **The balance movement is `secondaryAmount` when it is present, and otherwise `primaryAmount`.**
   - **Outgoing with a `secondaryAmount`:** that amount equals the transfer's `sourceValue` plus the quote's BALANCE fee, which is the total taken from the balance. This held in 5 of 5 cases, including one cross-currency send.
   - **Outgoing without a `secondaryAmount`:** `primaryAmount` equals `sourceValue` and the fee is 0. This held in 5 of 5 cases.
   - **Incoming:** `primaryAmount` equals `sourceValue`, and there is never a `secondaryAmount`.
2. **On an outgoing send, `primaryAmount` is what the recipient got.** For a same-currency send that is `sourceValue`. For a cross-currency send it is the `targetValue` in the target currency: a USD→BRL send showed `primaryAmount` in BRL and `secondaryAmount` in USD.
3. **The balance is the one whose currency matches the balance-side amount.** Outgoing transfers have `sourceAccount: null`, so the currency is the only link. Incoming transfers can also be checked against `targetAccount` = `recipientId`. With one STANDARD balance per currency, the currency is enough. Jars would make it ambiguous.
4. **The fee is `secondaryAmount − primaryAmount` on a same-currency send.** On a cross-currency send it is the quote's BALANCE fee. Both are optional detail; the balance movement doesn't need them.

**Check on the whole account.** For each currency, the script added up the balance-side amount of every activity, as a credit when it had `<positive>` and as a debit otherwise. The total equalled Wise's `amount.value` exactly for all 5 balances across both profiles. This covers every activity type seen, including the card payment, the account-details fee and the deposit. So the feed alone gives the transactions, and running balances can be checked against Wise's own figures.

### Paging and filtering

- **Order:** newest `updatedOn` first. The personal feed was also newest-first by `createdOn`. The business feed wasn't, because an account-details order created in June was updated in August.
- **Paging:** `size` goes up to 100, followed by `nextCursor`. Confirmed in the sandbox: 115 activities came back over 2 pages, and the last page's `cursor` was null.
- **Order is not reliable.** In production the feed looked newest-first by `updatedOn`. In the sandbox it was sorted by neither `createdOn` nor `updatedOn`, even across pages. So the plugin pages through the whole window and never stops early because of dates.
- **`since`:** it filters. Every result from `since=<mid-point>` had both `createdOn` and `updatedOn` on or after it, so it is unclear which of the two it filters on. Incremental syncs re-read an overlap window regardless (plan T9).

## Sandbox findings (2026-11-18)

These come from a sandbox account (`api.wise-sandbox.com`, fake money). `scripts/sandbox.py` created the activity with the author's full-access sandbox token, and `scripts/sample.py` read it back. The sandbox can differ from production, so production is what Checkpoints B and C test.

- **Set a `User-Agent`.** Cloudflare in front of the sandbox refuses Python's default with `403 error code: 1010`. The plugin's client sends its own `User-Agent` so production never fails the same way.
- **Conversions and Jar moves are one `INTERBALANCE` activity each** (`resource.type` is `BALANCE_TRANSACTION`). Each one touches two balances, so it becomes two transactions. The description is always `Moved`. The title names the balance the money went **to**:

  | Movement | Title | `primaryAmount` | `secondaryAmount` |
  |---|---|---|---|
  | EUR balance → Jar "Holiday" | `To <strong>Holiday</strong>` | the amount, in EUR | empty |
  | Jar → EUR balance | `To <strong>EUR</strong>` | the amount, in EUR | empty |
  | EUR → USD conversion | `To <strong>USD</strong>` | what arrived, in USD | what left, in EUR |

  Neither direction carries `<positive>`.

  **Working out the two balances:**
  - **Destination:** the balance the title names. It is either a Jar's `name`, or a currency code meaning that currency's STANDARD balance.
  - **Source:** for a conversion, the STANDARD balance in `secondaryAmount`'s currency. For a move without `secondaryAmount`, the *other* balance in the same currency.

  If the source or destination can't be told apart, for example a Jar named like a currency code or two Jars in the same currency, the sync fails with a clear message instead of guessing.

  No endpoint looks up a `BALANCE_TRANSACTION`: `/v1/…/balance-transactions/{id}`, `/v2/…/balance-movements/{id}` and `/v1/…/balance-movements/{id}` all return 404.
- **Transfer states that move no money:**

  | `status` | `description` | What happened |
  |---|---|---|
  | `REQUIRES_ATTENTION` | `Waiting for you to pay` | created, never funded |
  | `REQUIRES_ATTENTION` | `Sending paused` | unfunded, but pushed into processing by the simulation |
  | `CANCELLED` | `Cancelled` | cancelled before it was funded |
  | `COMPLETED` | `Sent` | sent; when funded from a balance, this is the debit |

  These keep the `primaryAmount`/`secondaryAmount` shape of a send. `REQUIRES_ATTENTION` and `CANCELLED` move no balance money, so the plugin leaves them out. `IN_PROGRESS` (a funded transfer still on its way) wasn't seen, because paying from a balance is refused in the sandbox (`POST /v3/profiles/{p}/transfers/{id}/payments` returned 403, matching Wise's rule for UK/EU personal tokens).
- **Top-ups from the simulation (`POST /v1/simulation/balance/topup`) never appear in the feed.** Sandbox balances also start at 1,000,000, so feed totals there don't add up to the balances. The exact-sum check is a production result only.
- **Card endpoints:** 403 in the sandbox too.

## Not seen yet

These are unverified. The code must refuse anything that doesn't match a known shape, with a message naming the masked format, rather than guess.

- Foreign-currency card payments: which amount is the merchant's and which was charged.
- `IN_PROGRESS` and `UPCOMING` activities, refunds, card cashback, interest, direct debits.
- Amounts of 1,000,000 or more (two thousands separators) in the feed, and negative signs.
- Converting into or out of a Jar directly (not through the STANDARD balance).
