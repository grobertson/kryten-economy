# Configuration Migration Guide

This guide covers the configuration changes introduced between **v0.8.10** and the
current release (**v0.9.2**). It is written for operators upgrading an existing
`config.yaml` so you can see exactly what changed, what (if anything) you must do,
and what is purely optional.

For the complete annotated reference, see [`config.example.yaml`](../config.example.yaml).
For operator setup and the full field-by-field reference, see the
[Admin Guide](admin-guide.md).

---

## Read this first: upgrades are non-breaking

The config loader is intentionally forgiving, so **your existing `config.yaml` will
keep loading after an upgrade without any edits**:

- **Every new section has a safe default.** If you don't add the new keys, the
  service uses the documented defaults. Nothing new is enabled in a way that
  requires you to opt out.
- **Unknown / removed keys are ignored.** Leftover fields from older versions
  (for example `service.name`) do not cause a validation error — they are simply
  ignored.
- **Environment-variable substitution still works** the same way: `${VAR}` and
  `${VAR:-default}`.

Because of this, the "Action required" items below are about **recommended cleanup**
and **opt-in behavior changes** — there are no hard breaking changes to the config
schema in this range.

### Validate before you restart

After editing your config, validate it without starting the service:

```bash
python -m kryten_economy --validate-config --config config.yaml
```

It prints `Config is valid.` on success or the validation error and a non-zero exit
code on failure. You can also hot-reload a running instance with the admin `reload`
PM command.

---

## At a glance

| Version | Section | Change | Action |
| --- | --- | --- | --- |
| 0.8.10 | `service` | `name` / `version` are now auto-managed | Remove them (optional cleanup) |
| 0.8.10 | `retention.inactivity_nudge` | Dropped from the example | Remove (optional; still accepted) |
| 0.8.15 | `mediacms` | New `web_queue_redirect`, `web_queue_url` | Review — changes default user flow |
| 0.9.0 | `gambling` | New `spectacle_cooldown_seconds` | Optional |
| 0.9.0 | `gambling.race` | New section (Race Betting) | Optional opt-in / tuning |
| 0.9.0 | `gambling.trivia` | New section (Trivia Gamble) | Optional opt-in / tuning |
| 0.9.0 | `gambling.blackjack` | New section (Blackjack Lite) | Optional opt-in / tuning |
| 0.9.0 | `gambling.race.commentary` | Static / LLM / hybrid commentary | Optional |
| 0.9.0→0.9.2 | _(database)_ | `gambling_stats` columns added | Automatic on startup |

---

## 0.8.10 — Service identity is now auto-managed

**What changed:** Service identity (`service.name` and `service.version`) is now
injected by the application at config-load time. `service.name` is always `economy`
and `service.version` always matches the installed package version. Earlier examples
asked you to set these by hand, which let the reported version drift away from the
deployed package.

**Action required:** None — but you should delete the two fields for clarity. The
lifecycle toggles remain configurable.

```diff
 service:
-  name: economy
-  version: "1.0.0"
   enable_lifecycle: true
   enable_heartbeat: true
   heartbeat_interval: 30
```

If you leave `name` / `version` in place, they are silently overridden, so this is
cleanup only.

**Also in 0.8.10:**

- `retention.inactivity_nudge` was removed from the example (there is no reliable
  way to contact absent/offline users). The field is still accepted for backward
  compatibility, but you can delete it.
- The example's `metrics.port` value changed from `28286` to `28290`. This is just
  the example default — keep whatever port your deployment already uses.
- The `bounties` section was restored to the example with schema defaults; no action
  needed if you already had it.

---

## 0.8.15 — Search / queue / playnext moved to the web queue

**What changed:** The `search`, `queue`, and `playnext` PM commands are now disabled
by default and instead point users at the kryten-webqueue instance. This is
controlled by two new `mediacms` fields:

```yaml
mediacms:
  base_url: "https://media.example.com"
  api_token: "${MEDIACMS_TOKEN:-your-token-here}"
  search_results_limit: 10
  # New in 0.8.15:
  web_queue_redirect: true                    # default
  web_queue_url: "https://queue.dropsugar.co/"
```

**Action required:** Decide which flow you want.

- **Keep the new default** (`web_queue_redirect: true`) — the three PM commands and
  the `help` Media section link users to `web_queue_url`. Set `web_queue_url` to your
  own web-queue address.
- **Restore the legacy in-PM flow** — set `web_queue_redirect: false` to re-enable
  in-PM search/queue/playnext.

The underlying spend/queue engine and the `forcenow` command are unchanged either way.

---

## 0.9.0 — Spectacle games (Race, Trivia, Blackjack)

**What changed:** Three new gambling games were added, plus a shared cooldown so only
one "spectacle" game (heist, race, or trivia) runs in a channel at a time. All three
are **enabled by default** but only fire when the parent `gambling` system is enabled.
Each game appears in the PM `help` output only when it is enabled in config.

**Action required:** None to start — defaults are sensible. Add the sections below
only if you want to tune them or disable a game.

### Shared spectacle cooldown

```yaml
gambling:
  enabled: true
  min_account_age_minutes: 60
  # New in 0.9.0 — shared post-game cooldown across heist/race/trivia:
  spectacle_cooldown_seconds: 60
```

### Race Betting — `gambling.race`

```yaml
  race:
    enabled: true
    betting_window_seconds: 20
    tick_interval_seconds: 1.5
    finish_distance: 20.0          # must be > 0
    min_bet: 10
    max_bet: 5000
    house_rake_pct: 0.05
    odds_mode: "pool"              # "fixed" or "pool" (pari-mutuel)
    announce_public: true
    live_betting:
      enabled: true
      cutoff_pct: 0.75             # close live betting at 75% of finish
    random_events:
      enabled: true
      chance_per_tick: 0.08
    traits:
      enabled: true
    commentary:
      mode: "static"              # see "Race commentary" below
      max_lines_per_race: 3
```

> Note: an early 0.9.0 preview used a `racer_count` key under `race`. The shipped
> release replaced it with per-race `odds_profiles` (built-in defaults are provided),
> so you do **not** need `racer_count`. If it is present it is harmless and ignored.

### Trivia Gamble — `gambling.trivia`

```yaml
  trivia:
    enabled: true
    min_wager: 10
    max_wager: 1000
    answer_window_seconds: 30
    betting_window_seconds: 15
    difficulty: "random"          # easy, medium, hard, or random
    payout_multipliers:
      easy: 1.5
      medium: 2.0
      hard: 3.0
    category: null                # null = random, or an OpenTDB category ID
    question_cache_size: 20
    announce_public: true
```

Trivia questions are fetched from the Open Trivia DB; the min-account-age gate
applies to both starting and joining a round.

### Blackjack Lite — `gambling.blackjack`

```yaml
  blackjack:
    enabled: true
    min_wager: 20
    max_wager: 2000
    cooldown_seconds: 10
    daily_limit: 50
    timeout_seconds: 120
    timeout_warning_seconds: 90
    dealer_hits_soft_17: true
    blackjack_payout: 1.5         # natural blackjack pays 3:2
```

### Race commentary: static, LLM, or hybrid

`gambling.race.commentary.mode` selects how race lines are generated:

- `static` (default) — uses the built-in narrative library. No external calls.
- `llm` — generates a themed commentary set once per race from an OpenAI-compatible
  endpoint, and falls back to the static pools on any failure.
- `hybrid` — tries the LLM first, falls back to static on timeout/error.

For `llm` or `hybrid`, add an `llm` block (defaults target a local Ollama server):

```yaml
    commentary:
      mode: "hybrid"
      max_lines_per_race: 3
      llm:
        endpoint: "http://localhost:11434/v1/chat/completions"
        api_key: ""               # Bearer token (blank for local Ollama)
        model: "llama3"
        temperature: 1.0
        max_tokens: 400
        timeout_seconds: 10
        max_retries: 1
```

Leave `mode: "static"` (or omit `commentary` entirely) if you do not want any
external LLM dependency.

---

## Database migration (0.9.0 → 0.9.2)

This is **not** a config change, but it matters when upgrading a database created
before 0.9.0. Version 0.9.0 added `total_races`, `total_trivias`, and
`total_blackjacks` columns to the `gambling_stats` table. On databases created before
0.9.0 these columns were missing, which crashed the first race/trivia/blackjack
resolution (most visibly in Blackjack).

**Action required:** None. As of **0.9.2** a startup migration adds the missing
columns automatically. Just upgrade to 0.9.2 or later and restart — no manual SQL and
no config edits are needed.

---

## Upgrade checklist

1. Back up your `config.yaml` and your economy database file.
2. (Optional cleanup) Remove `service.name`, `service.version`, and
   `retention.inactivity_nudge` from `config.yaml`.
3. Set `mediacms.web_queue_redirect` and `mediacms.web_queue_url` to match the user
   flow you want (new default redirects to the web queue).
4. (Optional) Add/tune the `gambling.race`, `gambling.trivia`, and
   `gambling.blackjack` sections; choose a race `commentary.mode`.
5. Validate: `python -m kryten_economy --validate-config --config config.yaml`.
6. Restart (or use the admin `reload` command). The 0.9.x database migration runs
   automatically on startup.
