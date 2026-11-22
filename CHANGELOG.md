# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-11-22

### Changed

- The container now runs as a non-root `app` user (UID 1000) by default.

## [1.0.0] - 2026-11-22

First public release.

### Added

- Telegram control bot with inline panels for accounts, campaigns, posts,
  groups, timing, windows, status and logs.
- Telethon user-account sending with per-account sequential workers.
- Formatted HTML posts, rich-message conversion, custom emoji and per-group
  `{{vars}}`.
- Campaign scheduling with interval, delay, jitter, weekdays, active dates and
  overnight windows in a per-campaign timezone (IST default).
- Run once with a fixed 10 s gap, pause/enable, run/stop all.
- Post management: save via `/post`, replace, rename, delete.
- Group manager: add to a list or replace it; never joins groups.
- Status persistence across restarts and per-user event logs.
- Multi-user workspaces via `OWNER_ID` and `ALLOWED_USERS`.
- Docker image, Compose file, multi-arch release to GHCR.
- Offline self-check, CI and release workflows.

### Security

- `.env`, sessions, accounts/state and user posts are git-ignored and excluded
  from the image.
- No auto-join, DM, account-pool or limit-evasion features.
