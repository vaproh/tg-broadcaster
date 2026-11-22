# Security policy

## Supported versions

The latest release (currently `1.x`) receives fixes. Older tags are not
maintained.

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability, a leaked session, or
credentials. Use GitHub's private vulnerability reporting:

<https://github.com/vaproh/tg-broadcaster/security/advisories/new>

Include the affected version, reproduction steps, impact, and any suggested fix.
You will get an acknowledgement as soon as possible.

## What is sensitive

- `.env` — API id/hash, bot token.
- `sessions/*.session` — MTProto authorization keys; possession grants full
  access to the Telegram account.
- `accounts.json` / `state.json` — accounts, group lists, schedules, logs.
- `posts/*.html` — message content.

These are git-ignored, excluded from the Docker build, and must never be pasted
into issues, screenshots, or logs.

## If a credential leaks

1. Bot token: revoke it in @BotFather (`/revoke`) and issue a new one.
2. API hash: reset it at <https://my.telegram.org> if you believe it is exposed.
3. Session: terminate the session in Telegram → Settings → Devices, delete the
   `.session` file, and log in again.
4. Rotate anything else that shared the host or backup.

Removing a file from a git commit is not enough once it was pushed; rotate first.

## Deployment hardening

- Run as an unprivileged user; the compose file supports `TG_UID`/`TG_GID`.
- Keep the host and Docker patched.
- Restrict SSH and the Docker socket.
- Encrypt backups of `sessions/` and `.env`.
- Run one process per session directory.
- Do not expose the bot process to the network; it only makes outbound
  connections.
