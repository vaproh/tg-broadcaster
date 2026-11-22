# Product Requirements — tg-broadcaster

Status: v1.0.0
Owner: vaproh

## 1. Summary

tg-broadcaster is a self-hosted Telegram bot for sending **pre-written,
formatted** messages from a user's own Telegram account(s) to a chosen set of
groups, each with independent frequency and time-window settings.

It is a scheduling and delivery tool, not a growth or spam tool. It never joins
groups, discovers audiences, sends DMs, or works around Telegram limits.

## 2. Problem

People who run communities, announce launches, or post recurring notices in
several groups currently either:

- repeat the same message by hand in every group, at unpredictable times;
- use a bot account, losing the human identity and some formatting/emoji
  behaviour;
- use bulk tools that join groups, blast DMs, and get accounts limited.

They need a small, auditable, self-hosted tool that sends a message they wrote,
from an account they control, only where they are allowed to post.

## 3. Goals

1. Send already-formatted Telegram posts (including animated custom emoji) from
   real user accounts.
2. Let one instance manage multiple sender accounts, each with separate
   campaigns.
3. Give each campaign its own groups, interval, delay, time window, weekdays and
   active dates.
4. Make setup possible without editing code: configure and operate from the
   Telegram panel.
5. Be safe by default: owner allow-list, sequential sending, visible errors,
   no limit evasion.
6. Be trivial to self-host: one process, JSON files, a container image.

## 4. Non-goals

- Auto-joining groups or scraping audiences.
- Unsolicited DMs, auto-replies, or user targeting.
- Account farming, rented accounts, proxy rotation, or ban evasion.
- Guaranteed delivery or bypassing Telegram rate limits.
- A web dashboard, multi-tenant SaaS, or billing.
- Scheduling arbitrary Telegram API actions beyond sending a post.

## 5. Users

| User | Need |
|---|---|
| Community/announcement owner | Post the same notice to several groups on a schedule. |
| Small team | Several sender accounts with separate campaign lists under one control bot. |
| Self-hoster | Run it on a VPS with Docker, keep credentials local. |

## 6. Glossary

- **Sender account**: a Telegram user account logged in through Telethon.
- **Campaign**: one post + one group list + one schedule.
- **Cycle**: one send of the post to every group in a campaign.
- **Window**: allowed hours, in the campaign timezone.
- **Run once**: manual immediate cycle, regardless of paused state.
- **Workspace**: all data belonging to one allowed Telegram id.

## 7. Functional requirements

### Accounts

- FR-1: Log in sender accounts by phone, code and 2FA from the panel or CLI.
- FR-2: Accounts have a user-chosen label, can be renamed, and can be paused
  individually or all at once.
- FR-3: An account's campaigns are independent from every other account's.
- FR-4: A broken/unauthorised session is shown as “not logged in” without
  stopping other accounts.

### Campaigns

- FR-5: Create, rename, delete and move campaigns between accounts.
- FR-6: A campaign has a post, groups, interval, delay, window, weekdays,
  optional active-from/until and enabled state.
- FR-7: A cycle sends to all groups sequentially; the next cycle is scheduled
  after the cycle with randomised jitter (60–100%).
- FR-8: Run once sends immediately, even while paused, with a fixed 10 s gap.
- FR-9: The campaign never sends when disabled, outside its window/days/dates,
  or without a post or groups.

### Posts

- FR-10: Save a post by replying `/post <name>` to any formatted message.
- FR-11: Create, replace, rename and delete posts from the panel.
- FR-12: Preserve Telegram HTML: bold, italic, underline, strike, spoiler, code,
  links, blockquotes, lists and custom emoji.
- FR-13: Fill `{{title}}`, `{{username}}` and per-group `vars`; HTML-escape
  values.
- FR-14: Skip messages whose visible text exceeds 4096 characters; never split.

### Groups

- FR-15: Accept `@username`, `t.me/+invite` (already joined) and numeric ids.
- FR-16: Add groups to an existing list or replace the list.
- FR-17: Never join a group. Never send to a group the account cannot access.

### Scheduling and time

- FR-18: Per-campaign timezone, default `Asia/Kolkata` (IST).
- FR-19: Windows support overnight ranges and 24/7.
- FR-20: Weekday filters and optional active dates.
- FR-21: Display all timestamps in IST, 12-hour AM/PM; only window hours use
  24-hour time.

### Operations

- FR-22: Status shows the last result per campaign (sent/failed/skipped, time,
  text preview) and survives restarts.
- FR-23: Logs show recent events per user (sends, skips, resolve errors, flood
  waits), capped and persisted.
- FR-24: Handle `FloodWait` (sleep ≤1 h), `PEER_FLOOD` (disable the campaign),
  slow mode and permission errors by skipping the target, with visible reasons.
- FR-25: Multiple allowed Telegram ids each get an isolated workspace.
- FR-26: The control bot ignores unknown senders.

## 8. Non-functional requirements

- NFR-1: Single Python process; no database, broker or web server.
- NFR-2: Docker image, multi-arch (amd64/arm64), published to GHCR on release.
- NFR-3: Offline self-check runs in CI without credentials.
- NFR-4: Secrets (`.env`, sessions, accounts/state) never enter git or the image.
- NFR-5: Callback payloads fit Telegram's 64-byte limit.
- NFR-6: State writes are merge-safe so one user's update does not drop another
  user's data.
- NFR-7: Errors are visible in the panel/log instead of silently retrying.

## 9. UX flows

1. **First run**: fill `.env` → start → `/start` → Add account → name → phone →
   code → 2FA → account ready.
2. **First campaign**: account → New campaign → name → Post (new ad) → Groups →
   Interval/Delay/Window/Days → enable.
3. **Test**: Run once → read Status/Logs → fix groups/permissions.
4. **Edit**: open Posts → Replace/Rename/Delete; or open the campaign → rename,
   move account, change groups/timing.
5. **Invite a teammate**: add their id to `ALLOWED_USERS` → restart → they add
   their own account.

## 10. Data model

`accounts.json`

```json
{"users": {"<telegram-id>": {
  "accounts": {"<label>": {"session": "<session file>"}},
  "campaigns": {"<name>": {
    "account": "<label>", "post": "<slug>.html",
    "groups": ["@name", {"chat": "-100...", "vars": {}}],
    "interval_min": 60, "delay_s": 30,
    "window": {"start": "09:00", "end": "22:00", "tz": "Asia/Kolkata"},
    "days": ["all"], "active_from": null, "active_until": null,
    "enabled": false
  }}
}}}
```

`state.json`: per user, `{"<campaign>": {"last": 0, "next": 0, "result": {...}, "sample": "..."}, "_log": [...]}`.

## 11. Success criteria for v1

- Two accounts can run separate campaigns without sharing schedules.
- A formatted post with custom emoji is re-sent unchanged (Premium sender).
- Run once sends to all groups with a 10 s gap and the result is visible after a
  restart.
- A second allowed user sees only their own workspace.
- `--self-check` passes; the image builds and pulls from GHCR.
- No secrets are tracked in git.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Telegram limits / PEER_FLOOD | Sequential sends, jitter, visible errors, no evasion. |
| Account loss via session leak | Git-ignore sessions, warn, document encrypted backups. |
| User posts where not allowed | Docs and acceptable-use policy; no auto-join. |
| 4096 limit | Measure visible text, skip with a clear reason. |
| Multi-user data bleed | ContextVar proxies + per-user paths + tests/manual checks. |
| Docker permissions | `TG_UID`/`TG_GID`, documented. |

## 13. Future (not committed)

- Per-group send history.
- Post scheduling calendar.
- Optional webhook/metrics endpoint.
- Encrypted session storage.
- Import/export of a campaign bundle.
