# tg-broadcaster

[![CI](https://github.com/vaproh/tg-broadcaster/actions/workflows/ci.yml/badge.svg)](https://github.com/vaproh/tg-broadcaster/actions/workflows/ci.yml)
[![Release](https://github.com/vaproh/tg-broadcaster/actions/workflows/release.yml/badge.svg)](https://github.com/vaproh/tg-broadcaster/actions/workflows/release.yml)
[![Container](https://img.shields.io/badge/ghcr.io-vaproh%2Ftg--broadcaster-2496ED?logo=docker&logoColor=white)](https://github.com/vaproh/tg-broadcaster/pkgs/container/tg-broadcaster)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A self-hosted Telegram control bot that sends **already formatted** messages
from **your own Telegram accounts** to groups you choose, on a schedule you set.

You write the post once, pick the groups, and the bot delivers it from a real
user account — not a bot account — so it keeps Telegram formatting, links,
custom emoji, and the account's normal identity. A campaign is one post + one
group list + its own timing.

> Use this only in groups where posting is permitted. The tool never joins
> groups, never sends unsolicited DMs, and has no limit-evasion features. See
> [Acceptable use](#acceptable-use).

## Features

- **User-account sending** via MTProto/Telethon, with a Telegram control bot.
- **Formatted posts**: bold, italic, underline, strike, spoiler, code, links,
  blockquotes, lists and animated custom emoji are stored as HTML and re-sent
  as-is.
- **Campaigns**: independent post, groups, interval, delay, time window, weekdays,
  start/end dates and enabled state.
- **Per-group text**: `{{title}}`, `{{username}}`, or your own `{{vars}}`.
- **Human-ish scheduling**: every wait is randomised between 60% and 100% of
  the configured value; run-once uses a fixed 10 s gap.
- **Several sender accounts**, each with its own campaign list. Accounts never
  share work or flood limits.
- **Multi-user**: every allowed Telegram id gets an isolated workspace of
  accounts, campaigns, posts, sessions and logs.
- **Manage from Telegram**: create/rename/delete accounts, campaigns and posts;
  pick groups; set windows; run once; pause; inspect status and logs.
- **No web UI, no database, no external service**: one Python process and JSON
  files.

## How it works

```
Telegram control bot  ──►  scheduler  ──►  Telethon user client  ──►  groups
 (only allowed IDs)                     (one per sender account)
```

- One control-bot session receives your commands.
- One Telethon client per sender account stays connected.
- A worker per account runs its campaigns one at a time. A cycle sends the post
  to every group, then schedules the next cycle.
- State and results live in `state.json`; configuration in `accounts.json`.
- Posts are HTML files in `posts/`.

## Quick start with Docker (recommended)

Requirements: Docker with Compose v2, a Telegram account, and a Telegram bot.

```bash
git clone https://github.com/vaproh/tg-broadcaster.git
cd tg-broadcaster

cp .env.example .env
$EDITOR .env                 # fill the four values described below

mkdir -p sessions posts
touch accounts.json state.json

TG_UID="$(id -u)" TG_GID="$(id -g)" docker compose up -d --build
docker compose logs -f
```

Open your bot in Telegram, press **Start**, then **＋ Add account** and log in a
sender account. Or use the safer terminal login:

```bash
docker compose stop
docker compose run --rm tg-broadcaster python app.py --login alice
docker compose up -d
```

Do not run a login in a second container while the bot is connected to the same
session; stop it first.

The image is also published:

```bash
docker pull ghcr.io/vaproh/tg-broadcaster:latest
```

Image tags: `latest`, `1.0.1`, `1.0` (the `v` prefix is only on the git tag).

Compose uses the published image name and can build the same image locally.
`TG_UID`/`TG_GID` must match the owner of the mounted files; the default is
`1000:1000`.

## Telegram setup

### 1. API credentials

1. Go to <https://my.telegram.org> → **API development tools**.
2. Create an application. Copy the **api_id** and **api_hash**.

These belong to the **user account** that will send messages, not to the bot.

### 2. Control bot

1. Message [@BotFather](https://t.me/botfather) → `/newbot`.
2. Choose a name and username. Copy the **bot token**.

### 3. Your numeric id

Message [@userinfobot](https://t.me/userinfobot) and copy your numeric id.
That is `OWNER_ID`. To let other people use the same instance, add their ids to
`ALLOWED_USERS` (comma-separated).

### 4. `.env`

```dotenv
TG_API_ID=123456
TG_API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
OWNER_ID=123456789
ALLOWED_USERS=
```

| Variable | Required | Meaning |
|---|---|---|
| `TG_API_ID` | yes | `api_id` from my.telegram.org |
| `TG_API_HASH` | yes | `api_hash` from my.telegram.org |
| `BOT_TOKEN` | yes | token from @BotFather |
| `OWNER_ID` | yes | your numeric Telegram id |
| `ALLOWED_USERS` | no | extra numeric ids, comma or space separated |

Never commit `.env`. It is ignored by git and excluded from the Docker build.

## Run from source

Python 3.10+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/vaproh/tg-broadcaster.git
cd tg-broadcaster
cp .env.example .env
$EDITOR .env

just sync
just login alice        # phone, code, 2FA in the terminal
just run
```

`just` is optional; the equivalent commands are `uv sync`,
`uv run python app.py --login alice` and `uv run python app.py`.

Add more sender accounts with `just login bob` or **＋ Add account** in the bot.
Adding an account from the bot works immediately. Accounts added with
`--login` need a restart of the process.

## Using the panel

`/start` opens the account list → campaign list → campaign screen.

### Accounts

- **＋ Add account** asks for a label, then phone → code → 2FA. The label is
  only for the panel.
- **🏷 Rename account** renames the label. Campaigns move with it; the Telegram
  session keeps working.
- **▶️ Run all / ⏸ Stop all** toggles every campaign owned by that account.
- An account with no valid session shows **⚠️ not logged in**.

Prefer the terminal login for an account you care about: Telegram can reject a
code pasted into a chat with “this code was previously shared by your account”.
The panel deletes the code/password messages after reading them, but the
terminal never puts them in Telegram at all.

### Campaigns

- **＋ New campaign** asks for a name.
- **Account** moves the campaign to another sender. Its last/next schedule is
  reset.
- **Post** picks a saved post, or **✍️ Send a new ad** (asks for a name, then the
  message). Reusing a name replaces that post and attaches it.
- **Interval** is the time between cycles. Quick picks 5/10/20/30/60/120 min, or
  a custom value.
- **Delay** is the gap between groups inside one cycle. Quick picks
  15/30/45/60/120/300 s, minimum 15 s.
- **Window** offers 24/7, 09:00–22:00, 10:00–20:00, 18:00–23:00, or a custom
  `HH:MM-HH:MM` (24-hour). Windows may cross midnight. Evaluated in the
  campaign's timezone, IST by default.
- **Days** is `all` or `mon,tue,wed,thu,fri`.
- **Groups** opens the group manager: **➕ Add more** appends, **🆕 New list**
  replaces.
- **▶️ enable / ⏸ pause** controls the scheduler. **▶️ run once** fires
  immediately even while paused, with a fixed 10 s gap.
- **🗑 delete** removes the campaign.

Every scheduled wait is randomised between 60% and 100% of the configured value
(`JITTER_FLOOR = 0.6` in `app.py`). A 10 min interval fires after 6–10 min.

### Groups

Accepted forms, comma-separated:

- `@username`
- `t.me/+invite` — only if the sending account is **already a member**
- `-1001234567890` — numeric channel/supergroup id

The bot never joins a group. An invite link for a group the account is not in
will fail. Numeric ids resolve from the account's dialog cache; `@username` is
the most reliable choice. Message links such as `t.me/c/123/456` are not group
ids.

### Posts

- **📝 Posts** lists saved posts. Tap one to **✍️ Replace content** (send the new
  message, name unchanged), **🏷 Rename**, or **🗑 Delete**. Deleting detaches the
  post from campaigns instead of leaving a broken reference.
- Without the panel, reply to any formatted message with `/post <name>` to save
  it as `posts/<name>.html`, formatting and custom emoji included.

### Status and logs

- **Status** shows each campaign's last result: sent/failed/skipped counts, time,
  and a short preview of the last delivered text. Results survive restarts.
- **📜 Logs** shows the last 25 events per user: sends, skips, resolve errors,
  flood waits. The state file keeps 200.
- All timestamps are **IST, 12-hour with AM/PM** (`22/11/26 9:30 PM`). Window
  hours are the only 24-hour values.

### Per-group text

Stop the bot before hand-editing `accounts.json`. The current shape is nested per
user:

```json
{
  "users": {
    "123456789": {
      "accounts": {
        "alice": { "session": "alice" }
      },
      "campaigns": {
        "winter": {
          "account": "alice",
          "post": "winter.html",
          "groups": [
            "@group_a",
            { "chat": "@group_b", "vars": { "price": "49€" } },
            { "chat": "-1001234567890", "vars": { "price": "39€" } }
          ],
          "interval_min": 60,
          "delay_s": 30,
          "window": { "start": "09:00", "end": "22:00", "tz": "Asia/Kolkata" },
          "days": ["mon", "tue", "wed", "thu", "fri"],
          "active_from": null,
          "active_until": null,
          "enabled": false
        }
      }
    }
  }
}
```

In a post, `{{title}}` and `{{username}}` are filled from the group, and every
key in `vars` becomes a placeholder. Values are HTML-escaped; the rest of the
file passes through as Telegram HTML, so `<b>`, `<a href="…">` and
`<tg-emoji emoji-id="…">` work.

The old flat `{"accounts": …, "campaigns": …}` shape is migrated automatically
to the owner on startup.

## Multi-user

Each id in `OWNER_ID` + `ALLOWED_USERS` gets a separate workspace:

- own accounts, campaigns, posts, sessions, logs and status;
- no access to another user's campaign, even by crafting callback data;
- the owner keeps the root `posts/` and `sessions/`; other users use
  `posts/<id>/` and `sessions/<id>/`;
- config and state stay in the same files, nested under `users.<id>`.

Add an id to `ALLOWED_USERS`, restart, and that person can message the bot and
add their own account. Log in from the terminal for another user with:

```bash
uv run python app.py --login alice --user 123456789
```

## Data and backups

| Path | Contents | Sensitive |
|---|---|---|
| `.env` | API credentials, bot token, ids | **yes** |
| `sessions/*.session` | MTProto login keys — full account access | **yes** |
| `accounts.json` | users, accounts, campaigns, group lists | **yes** |
| `state.json` | schedules, results, logs | yes |
| `posts/*.html` | message content | user data |

All five are git-ignored except the sample `posts/default.html`. Back them up
together; restoring a session file restores the login. Encrypt backups. Losing
`sessions/` only means logging in again.

Run **one process per session directory**. Two copies sharing a session fight
over the account.

## Docker details

The compose file mounts `sessions/`, `posts/`, `accounts.json` and `state.json`
from the current directory. Create the two JSON files before the first start;
otherwise Docker creates **directories** with those names.

```bash
mkdir -p sessions posts
touch accounts.json state.json
```

Update:

```bash
docker compose pull
docker compose up -d
```

or, building from source:

```bash
docker compose up -d --build
```

`restart: unless-stopped` brings it back after a reboot. Watchtower is disabled
for the container.

## CLI

```text
python app.py                 start the bot
python app.py --login NAME    log a sender account in (terminal)
python app.py --user ID       use ID for --login
python app.py --self-check    offline logic checks
python app.py --dry-run       show what would fire now
python app.py --version       version
```

`--dry-run` is read-only.

## Limits and behaviour worth knowing

- **4096 characters** per message, measured on the visible text. Longer posts
  are skipped, never split.
- Animated **custom emoji require Telegram Premium on the sending account**.
  Free accounts can use them only in Saved Messages; elsewhere the fallback
  emoji is shown.
- `FloodWaitError`: the cycle sleeps if the wait is at most one hour, otherwise
  it aborts.
- `PEER_FLOOD`: the campaign is disabled and the reason is logged. The tool does
  not try to evade account penalties.
- `SlowModeWaitError` skips that group for the cycle. `ChatWriteForbidden`,
  `UserBannedInChannel` and private-channel errors skip the target.
- The minimum scheduled gap between groups is 15 s; run-once uses 10 s.
- Numeric ids only resolve for chats the account has in its dialog list.
- Session files are not encrypted at rest. Protect the host.

## Troubleshooting

**“This code was previously shared by your account.”**
Telegram refuses codes pasted into a chat. Use `--login` in the terminal.

**A campaign says `could not find the input entity` / `ValueError`.**
Use `@username`, make sure the account is a member and allowed to post, or send
one message in the group so the id is cached.

**“over 4096 chars”.**
The rendered text is too long. Shorten it; splitting is intentionally not
automatic.

**Custom emoji show as normal emoji.**
The sender account needs Premium.

**`ChatWriteForbiddenError`.**
The account cannot post there: not an admin, no permission, or the group
restricts posting.

**`PEER_FLOOD`.**
The account is limited by Telegram. Stop that campaign, wait, and do not try to
work around it.

**Docker permission errors.**
Run with `TG_UID="$(id -u)" TG_GID="$(id -g)" docker compose up -d`.

**`accounts.json` is a directory.**
Docker created it because the file was missing. Stop, remove the directory, and
`touch accounts.json state.json`.

**The window does not fire.**
Check the window, weekdays, `active_from/until`, and that the campaign is
enabled. Windows use the campaign timezone, IST by default.

## Development

```bash
just sync
just check          # offline self-check
just dry            # what would fire now
just image          # docker build -t tg-broadcaster:local .
```

The application is a single `app.py` (Telethon + argparse + JSON files). There is
no database and no web server. CI runs the self-check and builds the image;
tagging `v*` publishes a multi-arch image to GHCR and creates a GitHub release.
See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and
[PRD.md](PRD.md).

## Security and acceptable use

- Only post in groups where you have permission.
- No unsolicited DMs, auto-joining, account farming, or ban/limit evasion. Those
  features are out of scope and will not be merged.
- Sessions grant full access to the account. Keep `.env`, `sessions/`,
  `accounts.json` and `state.json` private.
- Report vulnerabilities privately per [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
