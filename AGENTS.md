# AGENTS.md

Guidance for coding agents and contributors working on this repository.

## What this is

A single-process, self-hosted Telegram broadcasting control bot. A Telegram bot
receives owner commands through inline panels; one Telethon **user** client per
sender account delivers HTML posts to a list of groups on a schedule.

Stack: Python 3.10+, [Telethon](https://docs.telethon.dev/) 1.45+, JSON files.
No database, no web server, no queue, no external service. `app.py` is the whole
application.

## Layout

```
app.py                 everything: config, panels, callbacks, scheduler, delivery, login
posts/default.html     sample post (safe to ship)
accounts.json          runtime config (git-ignored, created on first run)
state.json             runtime state/results/logs (git-ignored)
sessions/              Telethon sessions (git-ignored, full account access)
tests via --self-check
```

## Commands

```bash
just sync                 uv sync
just check                uv run python app.py --self-check
just dry                  uv run python app.py --dry-run
just login alice          uv run python app.py --login alice
just run                  uv run python app.py
just image                docker build -t tg-broadcaster:local .
```

`--self-check` must pass before every commit. It is offline: no network, no
Telegram credentials.

## Invariants

1. **One worker per sender account.** Campaigns of one account run
   sequentially; accounts are isolated so one FloodWait does not stall another.
2. **One control-bot session**, fixed at `ROOT/sessions/control`.
3. **Per-user isolation.** `CFG`, `ACCOUNTS`, `STATE`, `RESULTS`, `UI`, `POSTS`
   and `SESSIONS` are proxies over the current user via `CURRENT` (ContextVar).
   Any new code that touches them must run with `CURRENT` set by
   `on_message`/`on_callback`/`worker`. Never bypass the proxy with
   `USERS[...]` unless you are loading/saving users.
4. **Callback data ≤ 64 bytes.** Names are at most 24 chars (`NAME`) and post
   slugs 30 (`SLUG`) so composed payloads fit. Add a size check when adding a
   new callback shape.
5. **Message limit 4096**, measured on parsed plain text in UTF-16 units
   (`telegram_len`). Never split.
6. **HTML posts** are stored as Telegram HTML. Use `telethon.extensions.html`
   (`tg_html`) for parse/unparse; do not hand-roll entity offsets.
7. **No joining, no DMs, no evasion.** Do not add auto-join, auto-reply,
   account pools/rotation, proxy rotation, or features whose purpose is to
   evade Telegram limits. They are out of scope and will be rejected.
8. **Owner data stays private.** `.env`, `sessions/`, `accounts.json`,
   `state.json` and non-sample posts must never be tracked by git or copied into
   the image.

## Adding a screen

- Build a `*_panel(...)` returning `(html_text, rows)`, register it in
  `PANELS`, and add a `callback` branch in `on_callback`.
- Use `open_screen` for in-place edits. Use `send_panel` only when a new message
  is intended (e.g. after a pending prompt).
- Colored buttons: `Button.inline(..., style="success"|"danger")`.
- Keep callback data short and split on `:`; names cannot contain `:`.

## Adding a pending text prompt

1. Set `UI["pending"] = (kind, ...args)` **after** showing the prompt
   (`dispatch_callback` clears pending at the start).
2. Handle `kind` in `on_message` before the generic settings branch.
3. On invalid input, reply and keep pending; on success clear it and send the
   relevant panel.
4. Cancel is `/cancel` and a back button pointing at the parent screen.

## State and persistence

- `accounts.json`: `{"users": {"<uid>": {"accounts": {...}, "campaigns": {...}}}}`.
  Migrate old flat files through `migrate_config`.
- `state.json`: per user, keyed by campaign `{last, next, result?, sample?}` plus
  `_log`. `migrate_state` preserves `_` keys.
- `save_config()`/`save_state()` merge `USERS` into the file; they preserve
  users not currently loaded. Do not write the files directly.
- `RESULTS` is hydrated from `state.json` at startup and persisted via
  `record_result`.

## Time

- Display timestamps with `fmt_ist` (IST, `dd/mm/yy`, 12-hour AM/PM).
- Window hours stay 24-hour (`HH:MM`) and are evaluated in the campaign's
  `window.tz`, default `Asia/Kolkata`.
- Scheduling uses epoch seconds; do not store naive datetimes.

## Tests

- `self_check()` covers render, windows/day wrap, jitter, due, rich text, length,
  migrations, rename/create/delete helpers, callback sizes, and the login state
  machine.
- Add asserts for new pure logic. Network/Telegram behaviour is out of scope for
  the offline check; state it as manually verified instead of claiming a test.
- Run `uv run python app.py --self-check` and `docker build` before pushing.

## Commits and PRs

- Conventional commits (`feat:`, `fix:`, `docs:`, `ci:`).
- Keep changes focused; do not reformat unrelated code.
- Update README/CHANGELOG when behaviour, configuration or setup changes.
- CI must be green: self-check, private-data guard, Docker build.

## Release

1. Bump `__version__` in `app.py` and `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Commit, tag `vX.Y.Z`, push the tag.
4. The release workflow builds `linux/amd64,linux/arm64` images for GHCR and
   creates the GitHub release.

Never commit credentials. If you suspect a leak, follow `SECURITY.md`.
