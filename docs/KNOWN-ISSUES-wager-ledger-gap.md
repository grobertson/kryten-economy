# Known Defect: wagers debit balance without a ledger entry

**Status:** **FIXED** — 2026-09-26, shipped in 0.16.1
**Severity:** accounting integrity (not data loss)
**Discovered:** 2026-09-26, during the Sprint 12 PostgreSQL migration rehearsal
**Component:** `EconomyDatabase.atomic_debit` / `EconomyDatabasePg.atomic_debit`

> **This document is kept for provenance.** The behaviour described below is
> historical. For what the fix does and the invariant it now maintains, see
> **§ What was changed**. Do not act on the "Recommended fix" section — it is
> already done.

---

## Summary

`atomic_debit()` moves a user's balance but writes **no `transactions` row**. Every wager placed
through the gambling, blackjack, trivia, and race engines therefore reduces the real balance
while leaving no trace in the transaction ledger.

The consequence is that **`SUM(transactions.amount)` is not a valid measure of currency in
existence.** The true float is `SUM(accounts.balance)`.

---

## The evidence

Measured against a point-in-time copy of the production database (2026-09-26, 2,436 accounts,
5,724,336 transactions):

| Measure | Value |
| --- | --- |
| **Float — `SUM(accounts.balance)`** | **917,526,036** |
| `SUM(transactions.amount)` | 7,752,462,733 |
| Overstatement | **6,834,936,697 (745% of the float)** |
| Cumulative `lifetime_earned` | 10,561,866,666 |
| Cumulative `lifetime_spent` | 2,809,403,933 |
| `lifetime_earned - lifetime_spent` | 7,752,462,733 |

The last row is the tell: `lifetime_earned - lifetime_spent` equals the ledger sum **exactly**,
because both are maintained by the same credited/debited paths. Neither is a currency-supply
measure, because `atomic_debit` moves `balance` without incrementing `lifetime_spent`.

### There is no ledger type that can represent a wager

Every negative-amount row in production is accounted for by an explicit debit call:

| Type | Rows | Total |
| --- | --- | --- |
| `decay` | 31,483 | −2,550,317,808 |
| `spend` | 1,672 | −250,459,601 |
| `tip_send` | 319 | −5,686,810 |
| `admin_deduct` | 5 | −2,773,214 |
| `bounty_create` | 6 | −166,500 |

There is **no** `gamble_loss`, `gamble_wager`, `blackjack_wager`, `trivia_wager`, or `race_wager`
type. The wagers are simply absent — 70,140 of them by `gambling_stats` alone.

---

## Root cause

`kryten_economy/database.py`:

```python
async def atomic_debit(self, username, channel, amount) -> bool:
    """Debit balance atomically; return True if succeeded, False if insufficient."""
    ...
    cursor = conn.execute(
        "UPDATE accounts SET balance = balance - ? "
        "WHERE username = ? AND channel = ? AND balance >= ?",
        (amount, username, channel, amount),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        return False
    conn.commit()
    return True          # <-- no INSERT INTO transactions
```

The name is the tell. `debit()` **does** write a ledger row; `atomic_debit()` does not. The
`atomic_` prefix was presumably added to give gambling a single-statement balance check that
could not be interleaved, and the ledger insert was dropped along the way.

**14 call sites** depend on this behaviour, across four engines:

- `gambling_engine.py` — spins, flips, challenges, heists, duels (6 sites)
- `blackjack_engine.py` — blackjack hands (2 sites)
- `trivia_engine.py` — trivia wagers (2 sites)
- `race_engine.py` — race entries (1 site)

Plus the two store implementations and the `EconomyStore` protocol declaration.

---

## Why this was not caught

- Every existing test asserts on **balances** and on win-side ledger rows, never on the
  absence of a loss-side row.
- The sum of credits minus sum of debits is never asserted anywhere.
- The defect predates the PostgreSQL work and is **identical on both backends**, so parity
  testing could not surface it — the PostgreSQL store faithfully reproduces the gap.

---

## Impact

1. **Any audit, report, or "how much Z exists" query built on `transactions` is wrong by
   6.8 billion.** `econ:stats` and inflation reporting should be checked for this.
2. **Player-facing history is incomplete.** A user's `!history` shows winnings but not the
   wagers that funded them, so a session's net effect looks far more positive than it was.
3. **The float itself is correct.** `SUM(accounts.balance)` is authoritative and is exactly
   what the ETL verifies on both sides of the migration. No currency is lost or duplicated.
4. **The migration is unaffected.** Both stores carry the same rows, so the ETL copies the
   ledger faithfully — including its gap.

---

## Recommended fix

Make `atomic_debit` log a ledger row inside the same transaction, mirroring `debit()`:

- Add a `tx_type` (and optionally `reason` / `metadata`) parameter to `atomic_debit`.
- Insert the `transactions` row in the same transaction as the balance `UPDATE`, so a wager
  can never move the balance without a ledger entry, nor vice versa.
- Increment `lifetime_spent` in the same statement, so the two agree going forward.
- Update **both** `database.py` and `db/database_pg.py`, and the `EconomyStore` protocol.
- Pass a meaningful `tx_type` from each of the 14 call sites (`gamble_wager`, `blackjack_wager`,
  `trivia_wager`, `race_wager`).

**This is a schema-visible change** — new `type` values appear in `transactions`, which any
reporting that enumerates types will need to handle. It is additive and requires no Alembic
revision, since `type` is already a free-text column.

### Do NOT backfill historical wagers

The historical wagers cannot be reconstructed exactly: `gambling_stats` stores counts and
net results, not per-wager amounts or timestamps. Any backfill would be a fabrication. The
honest options are to start recording wagers from the point of the fix and note the boundary
in reporting, or to reconstruct approximate volume from `gambling_stats` clearly labelled as
an estimate. **Recommendation: do not backfill; begin recording forward.**

### Relationship to the PostgreSQL migration

Independent. The migration should proceed on its own merits and **must not** be blocked on
this fix. The defect is identical in both stores, so cutting over changes nothing about it.
Folding a behavioural change into the cutover would put two unrelated risks in one maintenance
window — the opposite of what a reversible migration wants. Ship the cutover, then fix the
ledger on its own.

---

## What was changed

The cutover completed first, exactly as recommended above. The fix then shipped separately.

### The invariant

`atomic_debit` now performs a complete accounting operation, and the following holds from the
fix forward:

```
balance == lifetime_earned - lifetime_spent          (per account)
SUM(accounts.balance) == SUM(transactions.amount)    (per channel)
```

### The change

`atomic_debit` gained optional `tx_type`, `reason`, `trigger_id` and `metadata` parameters, and
in **both** stores the balance decrement, the `lifetime_spent` increment, and the
`transactions` insert now happen in a single transaction. A wager can no longer move the
balance without leaving a ledger row, nor the reverse.

On PostgreSQL the conditional `UPDATE` and the insert share one `con.transaction()`; an
insufficient balance raises a private `_InsufficientFunds` sentinel purely to trigger the
rollback, which is caught and converted to the store's `False` contract.

### Transaction types

Each call site now names the game, so the ledger is queryable:

| Type | Source |
| --- | --- |
| `wager_spin` | `gambling_engine.spin` |
| `wager_flip` | `gambling_engine.flip` |
| `wager_challenge` | challenge issued / accepted |
| `wager_heist` | heist start / join |
| `wager_blackjack` | blackjack deal |
| `wager_blackjack_double` | double down |
| `wager_trivia` | trivia start / bet |
| `wager_race` | race bet |
| `wager` | default, if a caller omits `tx_type` |

### Refunds no longer masquerade as wins

Five escrow-refund paths (challenge declined, challenge expired, bulk expired-challenge cleanup,
heist cancelled, heist push) previously called `credit(tx_type="gamble_win")`, which both
inflated apparent gambling winnings and incremented `lifetime_earned` for money that was never
won. They now call `refund()`, which returns the balance **and** decrements `lifetime_spent`,
so `lifetime_earned` means "actually earned".

The heist push refunds less than the wager. Only the returned portion goes through `refund()`, so
the fee remains counted as spent — which is correct, since it genuinely was.

### Historical gap — not backfilled

The 6.8 billion historical overstatement **cannot be reconstructed**: `gambling_stats` stores
counts and net results, not per-wager amounts or timestamps. No backfill was written, because a
fabricated ledger is worse than a known-incomplete one.

The new `wager_*` rows begin at the fix's deployment. For any report that spans that boundary,
compare against `transactions.id` and treat rows before it as not containing wagers.

### Tests

`tests/test_wager_ledger.py` — 10 tests (4 of them `postgres`-marked) asserting the invariant,
the refund behaviour, and that a declined wager writes nothing.

**These tests were verified to fail against the old code**: with the pre-fix `atomic_debit`
reverted, 9 of the 10 fail. They are genuine regression guards, not restatements of the
implementation.
