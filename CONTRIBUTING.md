# Contributing

Thanks for helping. This project is intentionally small: one process, one Python
file, JSON state. Please keep it that way unless a change genuinely needs more.

## Setup

```bash
git clone https://github.com/vaproh/tg-broadcaster.git
cd tg-broadcaster
uv sync
uv run python app.py --self-check
```

You do not need Telegram credentials for the self-check. For manual testing,
create a test bot and a test sender account; never use a production session.

## Before opening a PR

1. `uv run python app.py --self-check` passes.
2. `docker build -t tg-broadcaster:test .` succeeds if you touched the image.
3. No secrets or user data are tracked:
   `git status --ignored` should show `.env`, `sessions/`, `accounts.json`,
   `state.json` and non-sample posts as ignored.
4. Docs changed if behaviour, config, commands or setup changed.
5. The change is focused; unrelated reformatting is out.

## Scope

Welcome: bug fixes, reliability, docs, accessibility of the panel, tests,
Docker/CI improvements, new scheduling options that respect the limits.

Out of scope: auto-join, DM blasting, account pools/rotation, proxy rotation,
ban/limit evasion, scraping audiences, or anything whose purpose is unsolicited
messaging. PRs for those will be closed.

## Style

- Python 3.10+, standard library where possible.
- Keep the single-file structure; use the existing panels/pending/proxy patterns.
- No new dependency for something the standard library or Telethon already does.
- Comment only the “why” that the code cannot show.
- Callback data must stay ≤ 64 bytes.

## Commits

Conventional commits: `feat:`, `fix:`, `docs:`, `ci:`, `refactor:`, `chore:`.
One logical change per commit. Explain user-visible behaviour in the body.

## Pull requests

Describe what changed, why, and how you verified it. Include the self-check
output and, for panel changes, a screenshot with personal data removed. Note any
Telegram behaviour you could not test offline.

## Security

Do not open a public issue for a vulnerability or a leaked credential. Follow
[SECURITY.md](SECURITY.md).
