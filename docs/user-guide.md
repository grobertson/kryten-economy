# Kryten Economy User Guide

Audience: regular channel users

This guide explains how to use the economy bot from private messages (PM), with examples you can copy.

## How to talk to the bot

1. Open a PM to the economy bot in your channel.
2. Send commands like `balance`, `spin`, or `search movie title`.
3. Read the bot response in PM.

If you are not sure what to do next, send:

```text
help
```

## Quick start

Try these first:

```text
balance
rewards
rank
top
```

Then try one game:

```text
spin
flip 100
```

## Core commands

## Account and progress

```text
balance
bal
rewards
rank
profile
achievements
top
leaderboard
lb
history
about
help
```

What they do:

- `balance` or `bal`: show your current balance.
- `rewards`: show your earning sources.
- `rank`: show your rank progress.
- `profile`: show your economy profile summary.
- `achievements`: show achievement progress.
- `top`, `leaderboard`, or `lb`: show leaderboard.
- `history`: show your recent economy activity.
- `about`: show bot/version info.
- `help`: show the command list.

## Gambling and duels

```text
spin
spin 250
flip 100
challenge @user 500
accept
decline
heist 250
stats
gambling
```

Notes:

- `spin` with no wager uses your free daily spin (if enabled).
- `challenge` sends a duel request to another user.
- `accept` or `decline` responds to a pending duel.
- `stats` and `gambling` show your personal gambling stats.
- `heist` may be disabled depending on channel config.

## Queue and media purchases

```text
search your query here
queue 1
playnext 1
forcenow 1
status
eventstatus
events
multipliers
```

How queue flow works:

1. Run `search <query>`.
2. Bot returns numbered results.
3. Send the number you want, for example `1`.
4. Bot sends a price confirmation.
5. Reply `YES` to buy, anything else cancels.

Queue command notes:

- `queue <id>` buys a normal queue slot.
- `playnext <id>` buys higher priority placement.
- `forcenow <id>` is usually expensive and may require admin approval.
- `status` and `eventstatus` show queue availability and event timing.
- `events` and `multipliers` show active earning multipliers.

Event window behavior:

- During active event windows, queue and search actions are temporarily disabled.
- A pre-event lockout may also apply shortly before an event starts.
- Use `status` to check when queueing reopens.

## Shop and social actions

```text
shop
buy greeting Your custom greeting text
buy title Your custom title
buy color red
buy gif https://example.com/clip.gif
buy shoutout Your message here
shoutout Your message here
fortune
buy rename Credits
tip @user 250
```

Notes:

- `shop` shows available vanity items and your current owned items.
- `fortune` is typically limited to once per day.
- `tip` transfers currency to another user (limits may apply).

## Notifications

```text
quiet
mute
unquiet
unmute
```

- `quiet` and `mute` turn off optional economy PM notifications.
- `unquiet` and `unmute` turn notifications back on.

## Common errors and fixes

- Unknown command: send `help` and use exact command spelling.
- Insufficient funds: use `balance` and try a lower wager or cheaper action.
- Queue unavailable: run `status` and wait for the event window to end.
- Search not configured: your channel may not have media search enabled.
- Slow down warning: wait a moment and retry (PM rate limit protection).

## Tips for power users

- Keep `status` and `events` handy during event nights.
- Use `history` to track your recent spending and wins.
- Use `rewards` plus `rank` to optimize progression.
- If a command fails repeatedly, copy the exact bot reply for an admin.

## Not in this guide

Admin-only commands such as grants, rain, bans, and config reload are documented in the admin guide.
