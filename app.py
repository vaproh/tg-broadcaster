#!/usr/bin/env python3
"""Broadcaster: a control bot that posts formatted messages from your accounts.

Run `python app.py` for the bot, `--login NAME` to add a sender account,
`--self-check` for offline logic tests, `--dry-run` to see what would fire.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import Button, TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PasswordHashInvalidError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    SlowModeWaitError,
    UserBannedInChannelError,
)
from telethon.extensions import html as tg_html

__version__ = "1.0.0"

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "accounts.json"
STATE_PATH = ROOT / "state.json"

TICK = 20
MAX_LEN = 4096
# Randomised waits: uniform(floor * value, value). 0.6 makes a 5 min delay 3-5 min.
JITTER_FLOOR = 0.6
SLUG = re.compile(r"^[a-z0-9_-]{1,30}$")
NAME = re.compile(r"^[a-z0-9_-]{1,24}$")
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

DEFAULT_CAMPAIGN = {
    "post": None,
    "groups": [],
    "interval_min": 60,
    "delay_s": 30,
    "window": {"start": "09:00", "end": "22:00", "tz": "Asia/Kolkata"},
    "days": ["all"],
    "active_from": None,
    "active_until": None,
    "enabled": False,
}

WINDOW_PRESETS = [
    ("24/7", "00:00", "24:00"),
    ("09:00–22:00", "09:00", "22:00"),
    ("10:00–20:00", "10:00", "20:00"),
    ("18:00–23:00", "18:00", "23:00"),
]

HINTS = {
    "interval": "Send the interval in minutes (minimum 5).",
    "delay": "Send the delay between groups in seconds (minimum 15).",
    "window": "Send the window as HH:MM-HH:MM, e.g. 09:00-22:00.",
    "days": "Send days as mon,tue,wed,thu,fri,sat,sun or all.",
    "groups": ("Send the full new list. This replaces the current groups. "
               "Comma-separated @name, t.me/+invite (already joined), or -100… id."),
    "addgroups": ("Send groups to add, comma-separated. @name, t.me/+invite "
                  "(already joined), or -100… id. Existing ones are skipped."),
}

log = logging.getLogger("broadcaster")


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN") or ""

OWNER_ID = int(os.environ.get("OWNER_ID") or 0)
ALLOWED: set[int] = {OWNER_ID}
for _part in re.split(r"[,\s]+", os.environ.get("ALLOWED_USERS") or ""):
    if _part.isdigit():
        ALLOWED.add(int(_part))

TASKS: list = []
CURRENT: ContextVar[int] = ContextVar("current_user", default=-1)
USERS: dict[int, dict] = {}


def make_user(uid: int) -> dict:
    if uid == OWNER_ID:
        posts, sessions = ROOT / "posts", ROOT / "sessions"
    else:
        posts = ROOT / "posts" / str(uid)
        sessions = ROOT / "sessions" / str(uid)
    return {
        "cfg": {"accounts": {}, "campaigns": {}},
        "accounts": {},
        "state": {},
        "results": {},
        "ui": {"panel": None, "screen": None, "pending": None, "login": None},
        "posts": posts,
        "sessions": sessions,
    }


class _UserDict(MutableMapping):
    def __init__(self, key: str) -> None:
        self.key = key

    def _d(self) -> dict:
        return USERS[CURRENT.get()][self.key]

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v) -> None:
        self._d()[k] = v

    def __delitem__(self, k) -> None:
        del self._d()[k]

    def __iter__(self):
        return iter(self._d())

    def __len__(self) -> int:
        return len(self._d())

    def __contains__(self, k) -> bool:
        return k in self._d()

    def get(self, k, default=None):
        return self._d().get(k, default)

    def setdefault(self, k, default=None):
        return self._d().setdefault(k, default)

    def pop(self, k, *args):
        return self._d().pop(k, *args)

    def clear(self) -> None:
        self._d().clear()


class _UserPath:
    def __init__(self, key: str) -> None:
        self.key = key

    def _p(self) -> Path:
        return USERS[CURRENT.get()][self.key]

    def __truediv__(self, other) -> Path:
        return self._p() / other

    def glob(self, pattern: str):
        return self._p().glob(pattern)

    def mkdir(self, **kwargs):
        return self._p().mkdir(**kwargs)

    def __fspath__(self) -> str:
        return str(self._p())

    def __str__(self) -> str:
        return str(self._p())


CFG = _UserDict("cfg")
ACCOUNTS = _UserDict("accounts")
STATE = _UserDict("state")
RESULTS = _UserDict("results")
UI = _UserDict("ui")
POSTS = _UserPath("posts")
SESSIONS = _UserPath("sessions")


# ---------------------------------------------------------------- storage

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # bind-mounted single file: its directory may be read-only and
        # renaming over the mount point fails
        tmp.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


def _split_users(raw) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("users"), dict):
        return raw["users"]
    return {str(OWNER_ID): raw if isinstance(raw, dict) else {}}


def load_users() -> None:
    raw = load_json(CONFIG_PATH, {})
    per = _split_users(raw)
    per_state = _split_users(load_json(STATE_PATH, {}))
    for uid in sorted(ALLOWED):
        user = USERS.setdefault(uid, make_user(uid))
        user["cfg"] = per.get(str(uid)) or {"accounts": {}, "campaigns": {}}
        migrate_config(user["cfg"])
        user["state"] = migrate_state(per_state.get(str(uid)) or {})
        user["results"] = {
            cname: entry["result"] for cname, entry in user["state"].items()
            if isinstance(entry, dict) and isinstance(entry.get("result"), dict)
        }
        user["posts"].mkdir(parents=True, exist_ok=True)
        user["sessions"].mkdir(parents=True, exist_ok=True)
    save_config()
    save_state()


def save_config() -> None:
    per = _split_users(load_json(CONFIG_PATH, {}))
    for uid, user in USERS.items():
        per[str(uid)] = user["cfg"]
    save_json(CONFIG_PATH, {"users": per})


def save_state() -> None:
    per = _split_users(load_json(STATE_PATH, {}))
    for uid, user in USERS.items():
        per[str(uid)] = user["state"]
    save_json(STATE_PATH, {"users": per})


def ensure_account(name: str) -> None:
    CFG.setdefault("campaigns", {})
    CFG.setdefault("accounts", {}).setdefault(name, {"session": name})
    save_config()


def campaigns() -> dict:
    return CFG.setdefault("campaigns", {})


def account_campaigns(name: str) -> dict:
    return {c: camp for c, camp in campaigns().items() if camp.get("account") == name}


def campaign_of(cname: str) -> dict | None:
    return campaigns().get(cname)


def campaign_owner(cname: str) -> str | None:
    camp = campaign_of(cname)
    return camp.get("account") if camp else None


def migrate_config(cfg: dict) -> bool:
    """Move campaigns nested under accounts to the flat campaigns table."""
    changed = False
    cfg.setdefault("accounts", {})
    cfg.setdefault("campaigns", {})
    for name, conf in cfg["accounts"].items():
        nested = conf.pop("campaigns", None)
        if nested is not None:
            changed = True
            for cname, camp in nested.items():
                camp.setdefault("account", name)
                cfg["campaigns"].setdefault(cname, camp)
    for name, conf in cfg["accounts"].items():
        conf.setdefault("session", name)
    return changed


def migrate_state(state: dict) -> dict:
    """Flatten {account: {campaign: ts}} into {campaign: {last, next}}."""
    data = {k: v for k, v in state.items() if not str(k).startswith("_")}
    if not data:
        return state
    if all(isinstance(v, dict) and ("last" in v or "next" in v) for v in data.values()):
        return state
    flat: dict = {k: v for k, v in state.items() if str(k).startswith("_")}
    for key, value in data.items():
        if isinstance(value, dict) and ("last" in value or "next" in value):
            flat[key] = value
        elif isinstance(value, dict):
            for cname, sub in value.items():
                if isinstance(sub, dict):
                    flat[cname] = sub
                elif isinstance(sub, (int, float)):
                    flat[cname] = {"last": sub, "next": None}
        elif isinstance(value, (int, float)):
            flat[key] = {"last": value, "next": None}
    return flat


class Account:
    def __init__(self, name: str, session: str | None = None):
        self.name = name
        self.session = session or name
        self.client: TelegramClient | None = None
        self.ready = False
        self.abort = asyncio.Event()
        self.run_once: set[str] = set()


# ---------------------------------------------------------------- schedule

def _hm(value: str):
    hour, minute = value.split(":")
    return int(hour), int(minute)


def in_window(camp: dict, now: datetime) -> bool:
    window = camp.get("window") or {}
    try:
        tz = ZoneInfo(window.get("tz") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)

    start, end = window.get("start"), window.get("end")
    if start and end:
        try:
            sh, sm = _hm(start)
            eh, em = _hm(end)
        except ValueError:
            return False
        current = (local.hour, local.minute)
        if (sh, sm) == (eh, em):
            pass  # 00:00-00:00 means all day
        elif (sh, sm) <= (eh, em):
            if not (sh, sm) <= current < (eh, em):
                return False
        elif not (current >= (sh, sm) or current < (eh, em)):
            return False

    days = camp.get("days") or ["all"]
    if "all" not in days and local.strftime("%a").lower()[:3] not in days:
        return False

    for key, after in (("active_from", False), ("active_until", True)):
        value = camp.get(key)
        if not value:
            continue
        try:
            bound = datetime.fromisoformat(value)
        except ValueError:
            continue
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=tz)
        if after and local > bound:
            return False
        if not after and local < bound:
            return False
    return True


def jittered(seconds: float) -> float:
    return random.uniform(JITTER_FLOOR, 1.0) * seconds


IST = ZoneInfo("Asia/Kolkata")


def fmt_clock(dt: datetime, seconds: bool = False) -> str:
    hour = dt.hour % 12 or 12
    clock = f"{hour}:{dt:%M}" + (f":{dt:%S}" if seconds else "")
    return f"{clock} {dt:%p}"


def fmt_ist(ts: float, seconds: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, IST)
    return f"{dt:%d/%m/%y} {fmt_clock(dt, seconds)}"


def cycle_delay(camp: dict, fixed: int | None = None) -> float:
    if fixed is not None:
        return fixed
    return jittered(max(15, int(camp.get("delay_s") or 30)))


def campaign_state(cname: str, camp: dict | None = None) -> dict:
    raw = STATE.get(cname)
    if isinstance(raw, dict):
        return raw
    entry: dict = {}
    if isinstance(raw, (int, float)):
        entry["last"] = raw
        interval = max(5, int((camp or {}).get("interval_min") or 60)) * 60
        entry["next"] = raw + interval
    return entry


def log_event(text: str) -> None:
    entries = STATE.setdefault("_log", [])
    entries.append({"at": time.time(), "text": text[:200]})
    del entries[:-200]
    save_state()


def record_result(cname: str, result: dict, sample: str | None = None) -> None:
    RESULTS[cname] = result
    entry = STATE.setdefault(cname, {})
    entry["result"] = result
    if sample is not None:
        entry["sample"] = sample
    save_state()


def mark_cycle(cname: str, camp: dict) -> None:
    now = time.time()
    interval = max(5, int(camp.get("interval_min") or 60)) * 60
    STATE[cname] = {"last": now, "next": now + jittered(interval)}
    save_state()


def due(camp: dict, next_ts: float | None, now: datetime) -> bool:
    if not camp.get("enabled"):
        return False
    if not camp.get("groups") or not camp.get("post"):
        return False
    if not in_window(camp, now):
        return False
    return next_ts is None or now.timestamp() >= next_ts


def next_run_text(camp: dict, next_ts: float | None) -> str:
    if not camp.get("enabled"):
        return "paused"
    now = datetime.now().astimezone()
    if not in_window(camp, now):
        return "outside window"
    if next_ts is None:
        return "next tick"
    if next_ts <= now.timestamp():
        return "due"
    return fmt_ist(next_ts)


# ---------------------------------------------------------------- render

def render(template: str, values: dict) -> str:
    def sub(match):
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return html.escape(str(values[key]))

    return PLACEHOLDER.sub(sub, template)


def rich_text(node) -> str:
    """Render Telegram's RichMessage inline text as HTML."""
    if isinstance(node, str):
        return html.escape(node)
    name = type(node).__name__
    if name == "TextPlain":
        return html.escape(node.text or "")
    if name == "TextConcat":
        return "".join(rich_text(part) for part in node.texts or [])
    if name == "TextBold":
        return f"<b>{rich_text(node.text)}</b>"
    if name == "TextItalic":
        return f"<i>{rich_text(node.text)}</i>"
    if name == "TextUnderline":
        return f"<u>{rich_text(node.text)}</u>"
    if name == "TextStrike":
        return f"<s>{rich_text(node.text)}</s>"
    if name == "TextSpoiler":
        return f"<tg-spoiler>{rich_text(node.text)}</tg-spoiler>"
    if name == "TextFixed":
        return f"<code>{rich_text(node.text)}</code>"
    if name == "TextUrl":
        href = html.escape(node.url or "", quote=True)
        return f'<a href="{href}">{rich_text(node.text)}</a>'
    if name == "TextEmail":
        href = html.escape(getattr(node, "email", "") or "", quote=True)
        return f'<a href="mailto:{href}">{rich_text(node.text)}</a>'
    if name == "TextCustomEmoji":
        return (f'<tg-emoji emoji-id="{node.document_id}">'
                f'{html.escape(node.alt or "")}</tg-emoji>')
    if name == "TextEmpty":
        return ""
    if name == "TextImage":
        return ""
    inner = getattr(node, "text", None)
    return rich_text(inner) if inner is not None else ""


def rich_to_html(rich) -> str:
    parts: list[str] = []
    for block in rich.blocks or []:
        name = type(block).__name__
        if name == "PageBlockDivider":
            parts.append("──────────")
        elif name in ("PageBlockBlockquote", "PageBlockPullquote"):
            caption = getattr(block, "caption", None)
            body = rich_text(block.text)
            if caption is not None:
                body += rich_text(caption)
            parts.append(f"<blockquote>{body}</blockquote>")
        elif name == "PageBlockPreformatted":
            parts.append(f"<pre>{rich_text(block.text)}</pre>")
        elif name in ("PageBlockList", "PageBlockOrderedList"):
            for item in block.items or []:
                chunks = getattr(item, "text", []) or []
                if isinstance(chunks, list):
                    parts.append("· " + "".join(rich_text(c) for c in chunks))
                else:
                    parts.append("· " + rich_text(chunks))
        elif name.startswith("PageBlockHeading") or name in (
            "PageBlockTitle", "PageBlockSubtitle", "PageBlockKicker",
            "PageBlockHeader", "PageBlockSubheader",
        ):
            parts.append(f"<b>{rich_text(block.text)}</b>")
        else:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(rich_text(text))
    return "\n\n".join(part for part in parts if part.strip())


def telegram_len(body: str) -> int:
    """Telegram's 4096 limit counts plain text, not HTML markup."""
    try:
        text, _ = tg_html.parse(body)
    except Exception:
        text = body
    return len(text.encode("utf-16-le")) // 2


def message_html(msg) -> str:
    if (msg.message or "").strip():
        return tg_html.unparse(msg.message, msg.entities or [])
    rich = getattr(msg, "rich_message", None)
    if rich:
        return rich_to_html(rich)
    return ""


def split_group(entry):
    if isinstance(entry, str):
        chat, extra = entry, {}
    else:
        chat, extra = entry.get("chat", ""), entry.get("vars") or {}
    if isinstance(chat, str) and re.fullmatch(r"-?\d+", chat.strip()):
        chat = int(chat.strip())
    return chat, extra


# ---------------------------------------------------------------- sending

async def deliver(acc: Account, entity, body: str):
    for attempt in (0, 1):
        try:
            await acc.client.send_message(
                entity, body, parse_mode="html", link_preview=False
            )
            return "sent", ""
        except FloodWaitError as exc:
            if attempt or exc.seconds > 3600:
                return "abort", f"flood wait {exc.seconds}s"
            await asyncio.sleep(exc.seconds + 2)
        except PeerFloodError:
            return "abort", "PEER_FLOOD"
        except SlowModeWaitError as exc:
            return "skipped", f"slow mode {exc.seconds}s"
        except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError) as exc:
            return "skipped", type(exc).__name__
        except Exception as exc:  # network, entity, formatting
            return "failed", f"{type(exc).__name__}: {exc}"[:120]
    return "failed", "unknown"


async def run_cycle(acc: Account, cname: str, camp: dict,
                    fixed_delay: int | None = None) -> None:
    acc.abort.clear()
    notes: list[str] = []
    sent = failed = skipped = 0
    post_name = camp.get("post") or ""
    post_path = POSTS / post_name
    if not post_name or not post_path.exists():
        mark_cycle(cname, camp)
        record_result(cname, {
            "at": time.time(), "sent": 0, "failed": 0, "skipped": 0,
            "notes": [f"post missing: {post_name or '—'}"],
        })
        log_event(f"{acc.name}/{cname}: post missing ({post_name or '—'})")
        return

    template = post_path.read_text(encoding="utf-8")
    groups = camp.get("groups") or []
    sample: str | None = None

    for index, entry in enumerate(groups):
        if acc.abort.is_set():
            break
        chat, extra = split_group(entry)
        if not chat:
            continue
        try:
            entity = await acc.client.get_entity(chat)
        except Exception as exc:
            failed += 1
            notes.append(f"{chat}: resolve {type(exc).__name__}")
            log_event(f"{acc.name}/{cname} · {chat}: resolve {type(exc).__name__}")
            continue

        values = {
            "title": getattr(entity, "title", None) or getattr(entity, "first_name", "") or chat,
            "username": getattr(entity, "username", "") or "",
            **extra,
        }
        body = render(template, values)
        if telegram_len(body) > MAX_LEN:
            skipped += 1
            notes.append(f"{chat}: over {MAX_LEN} chars")
            log_event(f"{acc.name}/{cname} · {chat}: over {MAX_LEN} chars")
            continue

        status, detail = await deliver(acc, entity, body)
        if status == "sent":
            sent += 1
            try:
                sample = tg_html.parse(body)[0][:200]
            except Exception:
                sample = body[:200]
        elif status == "skipped":
            skipped += 1
            notes.append(f"{chat}: {detail}")
        elif status == "abort":
            skipped += 1
            notes.append(f"{chat}: {detail} — cycle stopped")
            if detail == "PEER_FLOOD":
                camp["enabled"] = False
                save_config()
                notes.append("campaign disabled; account is peer-flooded")
            break
        else:
            failed += 1
            notes.append(f"{chat}: {detail}")

        if status != "sent":
            log_event(f"{acc.name}/{cname} · {chat}: {detail}")

        if index < len(groups) - 1 and not acc.abort.is_set():
            await asyncio.sleep(cycle_delay(camp, fixed_delay))

    mark_cycle(cname, camp)
    record_result(cname, {
        "at": time.time(), "sent": sent, "failed": failed,
        "skipped": skipped, "notes": notes[:5],
    }, sample)
    log_event(f"{acc.name}/{cname}: {sent} sent, {failed} failed, {skipped} skipped")
    log.info("%s/%s: %d sent, %d failed, %d skipped", acc.name, cname, sent, failed, skipped)


async def worker(uid: int, acc: Account) -> None:
    CURRENT.set(uid)
    while True:
        try:
            now = datetime.now().astimezone()
            for cname, camp in list(account_campaigns(acc.name).items()):
                forced = cname in acc.run_once
                entry = campaign_state(cname, camp)
                if forced or due(camp, entry.get("next"), now):
                    acc.run_once.discard(cname)
                    await run_cycle(acc, cname, camp, 10 if forced else None)
        except Exception:
            log.exception("worker %s failed", acc.name)
        await asyncio.sleep(TICK)


def create_campaign(cname: str, account: str) -> bool:
    if not NAME.match(cname) or cname in campaigns() or account not in ACCOUNTS:
        return False
    camp = json.loads(json.dumps(DEFAULT_CAMPAIGN))
    camp["account"] = account
    campaigns()[cname] = camp
    save_config()
    return True


def rename_campaign(old: str, new: str) -> bool:
    if old not in campaigns() or new in campaigns() or not NAME.match(new):
        return False
    campaigns()[new] = campaigns().pop(old)
    if old in RESULTS:
        RESULTS[new] = RESULTS.pop(old)
    if old in STATE:
        STATE[new] = STATE.pop(old)
        save_state()
    for acc in ACCOUNTS.values():
        acc.run_once.discard(old)
    save_config()
    return True


def rename_account(old: str, new: str) -> bool:
    if old not in ACCOUNTS or new in ACCOUNTS or not NAME.match(new):
        return False
    CFG["accounts"][new] = CFG["accounts"].pop(old)
    for camp in campaigns().values():
        if camp.get("account") == old:
            camp["account"] = new
    acc = ACCOUNTS.pop(old)
    acc.name = new
    ACCOUNTS[new] = acc
    save_config()
    return True


def move_campaign(cname: str, dst: str) -> bool:
    camp = campaign_of(cname)
    if not camp or dst not in ACCOUNTS:
        return False
    camp["account"] = dst
    save_config()
    return True


# ---------------------------------------------------------------- panels

def esc(value) -> str:
    return html.escape(str(value))


def home_panel():
    rows = []
    camps_total = active = 0
    for name, acc in ACCOUNTS.items():
        camps = account_campaigns(name)
        running = sum(1 for camp in camps.values() if camp.get("enabled"))
        camps_total += len(camps)
        active += running
        state = "not logged in" if not acc.ready else (f"▶️ {running}" if running else "⏸")
        rows.append([Button.inline(f"{name} · {len(camps)} campaigns · {state}",
                                   f"acc:{name}".encode())])
    rows.append([Button.inline("＋ Add account", b"addacc", style="success")])
    rows.append([Button.inline("📝 Posts", b"posts"), Button.inline("📊 Status", b"status")])
    rows.append([Button.inline("📜 Logs", b"logs")])
    rows.append([Button.inline("⏹ Stop all", b"stopall", style="danger")])
    text = (f"🔊 <b>Broadcaster</b>\n"
            f"Accounts: {len(ACCOUNTS)} · campaigns: {camps_total} · active: {active}")
    if not ACCOUNTS:
        uid = CURRENT.get()
        cmd = "python app.py --login alice" + (
            f" --user {uid}" if uid != OWNER_ID else "")
        text = ("🔊 <b>Broadcaster</b>\n\nNo accounts yet. Add one with:\n"
                f"<code>{esc(cmd)}</code>")
    return text, rows


def account_panel(name: str):
    acc = ACCOUNTS[name]
    camps = account_campaigns(name)
    rows = []
    for cname, camp in camps.items():
        mark = "▶️" if camp.get("enabled") else "⏸"
        rows.append([Button.inline(
            f"{mark} {cname} · {len(camp.get('groups') or [])} groups",
            f"camp:{cname}".encode())])
    rows.append([Button.inline("▶️ Run all", f"accrun:{name}".encode(), style="success"),
                 Button.inline("⏸ Stop all", f"accstop:{name}".encode(), style="danger")])
    rows.append([Button.inline("＋ New campaign", f"new:{name}".encode())])
    rows.append([Button.inline("🏷 Rename account", f"racc:{name}".encode()),
                 Button.inline("‹ back", b"home")])
    text = f"<b>{esc(name)}</b>\n{len(camps)} campaigns"
    if not acc.ready:
        cmd = f"python app.py --login {name}" + (
            f" --user {CURRENT.get()}" if CURRENT.get() != OWNER_ID else "")
        text += "\n⚠️ not logged in — run: <code>" + esc(cmd) + "</code>"
    return text, rows


def campaign_panel(cname: str):
    camp = campaign_of(cname)
    if not camp:
        return "That campaign no longer exists.", [[Button.inline("‹ home", b"home")]]
    owner = camp.get("account") or "—"
    window = camp.get("window") or {}
    next_ts = campaign_state(cname, camp).get("next")
    result = RESULTS.get(cname)
    lines = [
        f"<b>{esc(owner)} / {esc(cname)}</b>",
        f"{'▶️ enabled' if camp.get('enabled') else '⏸ paused'}",
        f"Ad: {esc(camp.get('post')) + ' ✅' if camp.get('post') else 'not set 🔴'}",
        f"Groups: {len(camp.get('groups') or [])}",
        f"Interval: {camp.get('interval_min')} min (random "
        f"{round(camp.get('interval_min') * JITTER_FLOOR)}–{camp.get('interval_min')}) "
        f"· delay {camp.get('delay_s')} s (random "
        f"{round(camp.get('delay_s') * JITTER_FLOOR)}–{camp.get('delay_s')} s)",
        f"Window: {esc(window_text(window))} {esc(window.get('tz', ''))}",
        f"Days: {esc(','.join(camp.get('days') or ['all']))}",
        f"Next: {next_run_text(camp, next_ts)}",
    ]
    if result:
        lines.append(
            f"Last: {result['sent']} sent / {result['failed']} failed / "
            f"{result['skipped']} skipped"
        )
        for note in result["notes"]:
            lines.append(f"· {esc(note)}")

    rows = [
        [Button.inline(f"Account: {owner}", f"acctsel:{cname}".encode())],
        [Button.inline(f"Post: {camp.get('post') or 'choose'}",
                       f"postsel:{cname}".encode())],
        [Button.inline(f"Interval: {camp.get('interval_min')} min",
                       f"quick:interval:{cname}".encode()),
         Button.inline(f"Delay: {camp.get('delay_s')} s",
                       f"quick:delay:{cname}".encode())],
        [Button.inline(f"Window: {window_text(window)}",
                       f"win:{cname}".encode())],
        [Button.inline(f"Days: {','.join(camp.get('days') or ['all'])}",
                       f"pend:days:{cname}".encode())],
        [Button.inline(f"Groups: {len(camp.get('groups') or [])}",
                       f"groups:{cname}".encode())],
        [Button.inline("⏸ pause" if camp.get("enabled") else "▶️ enable",
                       f"toggle:{cname}".encode(),
                       style="danger" if camp.get("enabled") else "success"),
         Button.inline("▶️ run once (10 s)", f"run:{cname}".encode(), style="success")],
        [Button.inline("🗑 delete", f"del:{cname}".encode(), style="danger"),
         Button.inline("🏷 rename", f"rcamp:{cname}".encode())],
        [Button.inline("‹ back", f"acc:{owner}".encode())],
    ]
    return "\n".join(lines), rows


def account_picker(cname: str):
    owner = campaign_owner(cname)
    rows = []
    for other, acc in ACCOUNTS.items():
        if other == owner:
            continue
        mark = "" if acc.ready else " ⚠️"
        rows.append([Button.inline(f"{other}{mark}",
                                   f"moveto:{cname}:{other}".encode())])
    rows.append([Button.inline("‹ back", f"camp:{cname}".encode())])
    text = ("Choose the account that sends this campaign.\n"
            "Moving it does not copy it.")
    if len(ACCOUNTS) < 2:
        text = "No other accounts yet. Add one from the home screen."
    return text, rows


def posts_panel(cname: str | None = None):
    files = sorted(POSTS.glob("*.html"))
    rows = []
    if cname:
        for path in files:
            rows.append([Button.inline(path.stem, f"post:{cname}:{path.stem}".encode())])
        rows.append([Button.inline("✍️ Send a new ad", f"newad:{cname}".encode(),
                                   style="success")])
        rows.append([Button.inline("‹ back", f"camp:{cname}".encode())])
        text = "Choose a post for this campaign, or send a new ad."
    else:
        for path in files:
            rows.append([Button.inline(f"{path.stem} · {path.stat().st_size}b",
                                       f"pview:{path.stem}".encode())])
        rows.append([Button.inline("‹ back", b"home")])
        text = ("<b>Posts</b>\n" + ("\n".join(f"· {esc(p.stem)}" for p in files) or "none")
                + "\n\nAdd one: reply to a message with /post &lt;slug&gt;")
    return text, rows


def post_detail_panel(slug: str):
    path = POSTS / f"{slug}.html"
    if not path.exists():
        return "That post no longer exists.", [[Button.inline("‹ back", b"posts")]]
    body = path.read_text(encoding="utf-8")
    preview = body[:280].replace("\n", " ")
    text = (f"<b>{esc(slug)}</b> · {len(body)} bytes\n\n"
            f"<code>{esc(preview)}</code>")
    rows = [
        [Button.inline("✍️ Replace content", f"epost:{slug}".encode(), style="success"),
         Button.inline("🏷 Rename", f"rpost:{slug}".encode())],
        [Button.inline("🗑 Delete", f"dpost:{slug}".encode(), style="danger")],
        [Button.inline("‹ back", b"posts")],
    ]
    return text, rows


def logs_panel():
    entries = STATE.get("_log") or []
    lines = ["📜 <b>Logs</b>", ""]
    if not entries:
        lines.append("no activity yet")
    for entry in reversed(entries[-25:]):
        lines.append(f"{fmt_ist(entry['at'], True)} · {esc(entry['text'][:120])}")
    return "\n".join(lines), [[Button.inline("‹ back", b"home")]]


def status_panel():
    lines = ["📊 <b>Status</b>", ""]
    for name, acc in ACCOUNTS.items():
        if not acc.ready:
            lines.append(f"<b>{esc(name)}</b> — not logged in")
            continue
        for cname, camp in account_campaigns(name).items():
            result = RESULTS.get(cname)
            mark = "▶️" if camp.get("enabled") else "⏸"
            if result:
                stamp = fmt_ist(result["at"])
                lines.append(
                    f"{mark} <b>{esc(name)}/{esc(cname)}</b> — {result['sent']} sent, "
                    f"{result['failed']} failed, {result['skipped']} skipped · {stamp}"
                )
                sample = STATE.get(cname, {}).get("sample")
                if sample:
                    lines.append(f"   <i>{esc(sample[:100])}</i>")
            else:
                lines.append(f"{mark} <b>{esc(name)}/{esc(cname)}</b> — not run yet")
    rows = [[Button.inline("‹ back", b"home")]]
    return "\n".join(lines), rows


def window_text(window: dict) -> str:
    start, end = window.get("start"), window.get("end")
    return "24/7" if start == "00:00" and end == "24:00" else f"{start}–{end}"


def window_panel(cname: str):
    camp = campaign_of(cname) or {}
    window = camp.get("window") or {}
    text = (f"🕒 <b>Window</b> · {esc(window_text(window))}\n"
            f"Timezone: {esc(window.get('tz', 'Asia/Kolkata'))}\n\n"
            "Window hours are 24-hour; every other time in the bot is IST AM/PM.")
    rows, row = [], []
    for index, (label, _, _) in enumerate(WINDOW_PRESETS):
        row.append(Button.inline(label, f"wset:{cname}:{index}".encode()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("✏️ Custom", f"wcustom:{cname}".encode(), style="success")])
    rows.append([Button.inline("‹ back", f"camp:{cname}".encode())])
    return text, rows


def groups_panel(cname: str):
    camp = campaign_of(cname) or {}
    groups = camp.get("groups") or []
    lines = [f"<b>Groups</b> · {len(groups)}", ""]
    for entry in groups[:10]:
        lines.append(f"· {esc(split_group(entry)[0])}")
    if len(groups) > 10:
        lines.append(f"… +{len(groups) - 10} more")
    if not groups:
        lines.append("none yet")
    rows = [
        [Button.inline("➕ Add more", f"gadd:{cname}".encode(), style="success"),
         Button.inline("🆕 New list", f"gnew:{cname}".encode())],
        [Button.inline("‹ back", f"camp:{cname}".encode())],
    ]
    return "\n".join(lines), rows


def quickset_panel(kind: str, cname: str):
    camp = campaign_of(cname) or {}
    if kind == "interval":
        current = f"{camp.get('interval_min')} min"
        text = (
            "⏱ <b>Set broadcast cycle interval</b>\n\n"
            f"Current: {current}\n\n"
            "Recommended:\n"
            "· 5 min — aggressive 🔴\n"
            "· 10 min — balanced 🟡\n"
            "· 20 min — conservative 🟢\n\n"
            f"Randomised: {round(camp.get('interval_min') * JITTER_FLOOR)}–"
            f"{camp.get('interval_min')} min.\n\n"
            "Send a number of minutes (min 5), or tap one below.\n"
            "Short intervals raise the risk of account limits."
        )
        options = [5, 10, 20, 30, 60, 120]
        unit = "min"
    else:
        current = f"{camp.get('delay_s')} s"
        text = (
            "⏸ <b>Set delay between groups</b>\n\n"
            f"Current: {current} (random {round(camp.get('delay_s') * JITTER_FLOOR)}–"
            f"{camp.get('delay_s')} s)\n\n"
            "Send a number of seconds (min 15), or tap one below.\n"
            "Every wait is randomised between 60% and 100% of the value."
        )
        options = [15, 30, 45, 60, 120, 300]
        unit = "s"
    rows, row = [], []
    for option in options:
        if kind == "delay" and option >= 60 and option % 60 == 0:
            label = f"{option // 60} min"
        else:
            label = f"{option} {unit}"
        row.append(Button.inline(label,
                                 f"set:{kind}:{cname}:{option}".encode()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("‹ back", f"camp:{cname}".encode())])
    return text, rows


def pending_panel(kind: str):
    rows = [[Button.inline("‹ cancel", b"home")]]
    return HINTS[kind], rows


# ---------------------------------------------------------------- ui plumbing

async def show(event, text: str, rows) -> None:
    await event.edit(text, buttons=rows, parse_mode="html")
    UI["panel"] = event.message_id


async def refresh_panel(client) -> None:
    screen, args = UI.get("screen") or ("home", ())
    text, rows = PANELS[screen](*args)
    if UI.get("panel"):
        try:
            await client.edit_message(CURRENT.get(), UI["panel"], text,
                                      buttons=rows, parse_mode="html")
            return
        except Exception:
            pass
    sent = await client.send_message(CURRENT.get(), text, buttons=rows, parse_mode="html")
    UI["panel"] = sent.id


PANELS = {
    "home": home_panel,
    "account": account_panel,
    "campaign": campaign_panel,
    "quickset": quickset_panel,
    "groups": groups_panel,
    "window": window_panel,
    "accountpick": account_picker,
    "posts": posts_panel,
    "postpick": posts_panel,
    "postdetail": post_detail_panel,
    "status": status_panel,
    "logs": logs_panel,
}


async def open_screen(event, screen: str, *args) -> None:
    UI["screen"] = (screen, args)
    text, rows = PANELS[screen](*args)
    await show(event, text, rows)


async def send_panel(event, screen: str, *args) -> None:
    text, rows = PANELS[screen](*args)
    sent = await event.reply(text, buttons=rows, parse_mode="html")
    UI["screen"] = (screen, args)
    UI["panel"] = sent.id


# ---------------------------------------------------------------- handlers

async def on_callback(event) -> None:
    if event.sender_id not in ALLOWED:
        await event.answer("Not allowed.", alert=True)
        return
    CURRENT.set(event.sender_id)
    await event.answer()
    try:
        await dispatch_callback(event)
    except Exception:
        log.exception("callback %r failed", event.data)


async def dispatch_callback(event) -> None:
    UI["pending"] = None
    data = event.data.decode()
    parts = data.split(":")

    if data == "home":
        await open_screen(event, "home")
    elif data == "posts":
        await open_screen(event, "posts")
    elif data == "status":
        await open_screen(event, "status")
    elif data == "logs":
        await open_screen(event, "logs")
    elif data == "addacc":
        await start_login(event)
    elif data == "logincancel":
        await close_login()
        await open_screen(event, "home")
    elif data == "stopall":
        for acc in ACCOUNTS.values():
            acc.abort.set()
            acc.run_once.clear()
        for camp in campaigns().values():
            camp["enabled"] = False
        save_config()
        await open_screen(event, "home")
    elif parts[0] == "acc":
        await open_screen(event, "account", parts[1])
    elif parts[0] == "camp":
        await open_screen(event, "campaign", parts[1])
    elif parts[0] in ("accrun", "accstop"):
        name = parts[1]
        running = parts[0] == "accrun"
        for camp in account_campaigns(name).values():
            camp["enabled"] = running
        if not running:
            ACCOUNTS[name].abort.set()
            ACCOUNTS[name].run_once.clear()
        save_config()
        await open_screen(event, "account", name)
    elif parts[0] == "acctsel":
        await open_screen(event, "accountpick", parts[1])
    elif parts[0] == "moveto":
        if move_campaign(parts[1], parts[2]):
            await open_screen(event, "campaign", parts[1])
        else:
            await open_screen(event, "home")
    elif parts[0] == "new":
        name = parts[1]
        text = (f"Send a name for the new campaign on <b>{esc(name)}</b> "
                "(a-z, 0-9, _ or -).")
        rows = [[Button.inline("‹ cancel", f"acc:{name}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("newcamp", name)
    elif parts[0] == "rcamp":
        text = (f"Send the new name for <b>{esc(parts[1])}</b> "
                "(a-z, 0-9, _ or -).")
        rows = [[Button.inline("‹ cancel", f"camp:{parts[1]}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("renamecamp", parts[1])
    elif parts[0] == "racc":
        text = (f"Send the new name for account <b>{esc(parts[1])}</b> "
                "(a-z, 0-9, _ or -). Campaigns move with it.")
        rows = [[Button.inline("‹ cancel", f"acc:{parts[1]}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("renameacc", parts[1])
    elif parts[0] == "pview":
        await open_screen(event, "postdetail", parts[1])
    elif parts[0] == "epost":
        text = ("✍️ Send the replacement text for this post. Formatting and "
                "custom emoji are kept; the name stays the same.")
        rows = [[Button.inline("‹ cancel", f"pview:{parts[1]}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("editpost", parts[1])
    elif parts[0] == "rpost":
        text = "Send the new post name (a-z, 0-9, _ or -)."
        rows = [[Button.inline("‹ cancel", f"pview:{parts[1]}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("renamepost", parts[1])
    elif parts[0] == "dpost":
        text = (f"Delete <b>{esc(parts[1])}</b>? Campaigns using it will "
                "lose their ad.")
        rows = [
            [Button.inline("🗑 yes, delete", f"dpost2:{parts[1]}".encode())],
            [Button.inline("‹ cancel", f"pview:{parts[1]}".encode())],
        ]
        await show(event, text, rows)
    elif parts[0] == "dpost2":
        delete_post(parts[1])
        await open_screen(event, "posts")
    elif parts[0] == "toggle":
        camp = campaign_of(parts[1])
        if camp:
            camp["enabled"] = not camp.get("enabled")
            save_config()
        await open_screen(event, "campaign", parts[1])
    elif parts[0] == "run":
        owner = campaign_owner(parts[1])
        if owner in ACCOUNTS:
            ACCOUNTS[owner].run_once.add(parts[1])
        await open_screen(event, "campaign", parts[1])
    elif parts[0] == "del":
        text = f"Delete campaign <b>{esc(parts[1])}</b> for good?"
        rows = [
            [Button.inline("🗑 yes, delete", f"del2:{parts[1]}".encode())],
            [Button.inline("‹ cancel", f"camp:{parts[1]}".encode())],
        ]
        await show(event, text, rows)
    elif parts[0] == "del2":
        camp = campaigns().pop(parts[1], None)
        owner = (camp or {}).get("account")
        RESULTS.pop(parts[1], None)
        if STATE.pop(parts[1], None) is not None:
            save_state()
        save_config()
        if owner in ACCOUNTS:
            await open_screen(event, "account", owner)
        else:
            await open_screen(event, "home")
    elif parts[0] == "postsel":
        await open_screen(event, "postpick", parts[1])
    elif parts[0] == "post":
        camp = campaign_of(parts[1])
        if camp:
            camp["post"] = f"{parts[2]}.html"
            save_config()
        await open_screen(event, "campaign", parts[1])
    elif parts[0] == "delpost":
        (POSTS / f"{parts[1]}.html").unlink(missing_ok=True)
        await open_screen(event, "posts")
    elif parts[0] == "quick":
        await open_screen(event, "quickset", parts[1], parts[2])
        UI["pending"] = (parts[1], parts[2])
    elif parts[0] == "set":
        camp = campaign_of(parts[2])
        value = int(parts[3])
        if camp:
            if parts[1] == "interval":
                camp["interval_min"] = max(5, value)
            else:
                camp["delay_s"] = max(15, value)
            save_config()
        await open_screen(event, "campaign", parts[2])
    elif parts[0] == "newad":
        text = ("✍️ Send a name for this ad (a-z, 0-9, _ or -).\n\n"
                "Reusing an existing name replaces that post and attaches it "
                "to this campaign.")
        rows = [[Button.inline("‹ back", f"camp:{parts[1]}".encode())]]
        await show(event, text, rows)
        UI["pending"] = ("adname", parts[1])
    elif parts[0] == "win":
        await open_screen(event, "window", parts[1])
    elif parts[0] == "wset":
        camp = campaign_of(parts[1])
        try:
            _, start, end = WINDOW_PRESETS[int(parts[2])]
        except (ValueError, IndexError):
            await open_screen(event, "window", parts[1])
            return
        if camp:
            camp["window"] = {"start": start, "end": end, "tz": "Asia/Kolkata"}
            save_config()
        await open_screen(event, "window", parts[1])
    elif parts[0] == "wcustom":
        rows = [[Button.inline("‹ cancel", f"win:{parts[1]}".encode())]]
        await show(event, HINTS["window"], rows)
        UI["pending"] = ("window", parts[1])
    elif parts[0] == "groups":
        await open_screen(event, "groups", parts[1])
    elif parts[0] == "gadd":
        rows = [[Button.inline("‹ cancel", f"groups:{parts[1]}".encode())]]
        await show(event, HINTS["addgroups"], rows)
        UI["pending"] = ("addgroups", parts[1])
    elif parts[0] == "gnew":
        rows = [[Button.inline("‹ cancel", f"groups:{parts[1]}".encode())]]
        await show(event, HINTS["groups"], rows)
        UI["pending"] = ("groups", parts[1])
    elif parts[0] == "pend":
        text, rows = pending_panel(parts[1])
        await show(event, text, rows)
        UI["pending"] = (parts[1], parts[2])


# ---------------------------------------------------------------- bot login

LOGIN_CANCEL = [[Button.inline("‹ Back", b"logincancel", style="danger")]]


async def close_login() -> None:
    state = UI.get("login")
    if state and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    UI["login"] = None


async def start_login(event) -> None:
    await close_login()
    UI["login"] = {"stage": "name", "attempts": 0}
    text = ("➕ <b>Add account</b>\n\n"
            "Send a name for this account (a-z, 0-9, _ or -).\n"
            "It is only a label for the panel, not the Telegram name.")
    await event.edit(text, buttons=LOGIN_CANCEL, parse_mode="html")


async def finish_login(event) -> None:
    state = UI["login"]
    name = state["name"]
    client = state["client"]
    try:
        me = await client.get_me()
    finally:
        await client.disconnect()

    CFG.setdefault("campaigns", {})
    CFG.setdefault("accounts", {}).setdefault(name, {"session": name})
    save_config()
    old = ACCOUNTS.get(name)
    if old and old.client:
        try:
            await old.client.disconnect()
        except Exception:
            pass
    acc = old or Account(name)
    fresh = await start_user_client(SESSIONS / name)
    if fresh:
        acc.client = fresh
        acc.ready = True
        TASKS.append(asyncio.create_task(worker(CURRENT.get(), acc)))
    else:
        acc.ready = False
    ACCOUNTS[name] = acc
    UI["login"] = None

    if not fresh:
        await event.reply("Session was created but the client did not connect; restart the bot.")
    await event.reply(
        f"✅ Logged in <b>{esc(name)}</b> as {esc(me.first_name)} "
        f"(@{esc(me.username or '—')}). Logout of other copies of this session "
        "if Telegram asks.",
        parse_mode="html",
    )
    text, rows = account_panel(name)
    sent = await event.reply(text, buttons=rows, parse_mode="html")
    UI["screen"] = ("account", (name,))
    UI["panel"] = sent.id


async def handle_login(event, text: str) -> None:
    state = UI["login"]
    stage = state["stage"]

    if text == "/cancel":
        await close_login()
        await event.reply("Login cancelled.")
        return

    try:
        if stage == "name":
            name = text.strip().lower()
            if not NAME.match(name):
                await event.reply("Name must be a-z, 0-9, _ or - (max 24).",
                                  buttons=LOGIN_CANCEL)
                return
            if name in ACCOUNTS:
                await event.reply("That account name is already in use.",
                                  buttons=LOGIN_CANCEL)
                return
            state["name"] = name
            state["stage"] = "phone"
            await event.reply(
                "Enter the phone number with country code:\n<code>+1234567890</code>",
                buttons=LOGIN_CANCEL, parse_mode="html",
            )

        elif stage == "phone":
            phone = text.strip().replace(" ", "")
            if not phone.startswith("+") or not phone[1:].isdigit():
                await event.reply("That is not a phone number. Use +4915112345678.",
                                  buttons=LOGIN_CANCEL)
                return
            SESSIONS.mkdir(exist_ok=True)
            client = TelegramClient(str(SESSIONS / state["name"]), API_ID, API_HASH)
            await client.connect()
            sent = await client.send_code_request(phone)
            state.update(client=client, phone=phone,
                         code_hash=sent.phone_code_hash, stage="code")
            await event.reply(
                "Telegram sent a login code to your Telegram app. Send it here — "
                "the message is deleted after use.\n\n"
                f"Account will be saved as <code>{esc(state['name'])}</code>.",
                buttons=LOGIN_CANCEL, parse_mode="html",
            )

        elif stage == "code":
            try:
                await event.delete()
            except Exception:
                pass
            code = text.strip().replace(" ", "")
            state["attempts"] += 1
            if state["attempts"] > 3:
                await close_login()
                await event.reply("Too many wrong codes. Start again with ＋ Add account.")
                return
            try:
                await state["client"].sign_in(
                    phone=state["phone"], code=code,
                    phone_code_hash=state["code_hash"],
                )
            except SessionPasswordNeededError:
                state["stage"] = "password"
                await event.reply(
                    "Two-step verification is on. Send the password — the message "
                    "is deleted after use.",
                    buttons=LOGIN_CANCEL,
                )
                return
            except PhoneCodeInvalidError:
                await event.reply("Wrong code, try again.", buttons=LOGIN_CANCEL)
                return
            except PhoneCodeExpiredError:
                await close_login()
                await event.reply("The code expired. Start again with ＋ Add account.")
                return
            await finish_login(event)

        elif stage == "password":
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await state["client"].sign_in(password=text)
            except PasswordHashInvalidError:
                await event.reply("Wrong password, try again.", buttons=LOGIN_CANCEL)
                return
            await finish_login(event)

    except FloodWaitError as exc:
        await close_login()
        await event.reply(f"Telegram asked to wait {exc.seconds}s. Start again later.")
    except PhoneNumberInvalidError:
        await close_login()
        await event.reply("That phone number was rejected. Start again with ＋ Add account.")
    except Exception as exc:
        log.exception("login failed")
        await close_login()
        await event.reply(f"Login failed: {type(exc).__name__}. Start again with ＋ Add account.")


def rename_post(old: str, new: str) -> bool:
    if not SLUG.match(new):
        return False
    src, dst = POSTS / f"{old}.html", POSTS / f"{new}.html"
    if not src.exists() or dst.exists():
        return False
    src.rename(dst)
    for camp in campaigns().values():
        if camp.get("post") == f"{old}.html":
            camp["post"] = f"{new}.html"
    save_config()
    return True


def delete_post(slug: str) -> None:
    (POSTS / f"{slug}.html").unlink(missing_ok=True)
    changed = False
    for camp in campaigns().values():
        if camp.get("post") == f"{slug}.html":
            camp["post"] = None
            changed = True
    if changed:
        save_config()


async def replace_post(event, slug: str) -> bool:
    body = message_html(event.message)
    if not body.strip():
        await event.reply("That message has no readable text.")
        return False
    (POSTS / f"{slug}.html").write_text(body, encoding="utf-8")
    await event.reply(f"✅ Updated posts/{slug}.html")
    text, rows = post_detail_panel(slug)
    sent = await event.reply(text, buttons=rows, parse_mode="html")
    UI["screen"] = ("postdetail", (slug,))
    UI["panel"] = sent.id
    return True


async def save_post(event) -> None:
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2 or not SLUG.match(parts[1].strip()):
        await event.reply("Usage: reply to a message with /post <slug> (a-z, 0-9, _-)")
        return
    if not event.is_reply:
        await event.reply("Reply to the message you want to save.")
        return
    msg = await event.get_reply_message()
    body = message_html(msg)
    if not body.strip():
        await event.reply("That message has no text.")
        return
    slug = parts[1].strip()
    (POSTS / f"{slug}.html").write_text(body, encoding="utf-8")
    await event.reply(f"Saved posts/{slug}.html ({len(body)} chars)")


async def save_ad(event, cname: str, slug: str | None = None) -> bool:
    camp = campaign_of(cname)
    if not camp:
        await event.reply("That campaign no longer exists.")
        return True
    msg = event.message
    body = message_html(msg)
    if not body.strip():
        await event.reply(
            "That message arrived with no readable text. Paste it as a normal "
            "text message — the prompt stays open."
        )
        return False
    if slug is None:
        index = 1
        while (POSTS / f"ad-{index}.html").exists():
            index += 1
        slug = f"ad-{index}"
    (POSTS / f"{slug}.html").write_text(body, encoding="utf-8")
    camp["post"] = f"{slug}.html"
    save_config()
    await event.reply(f"✅ Advertisement saved as posts/{slug}.html")
    text, rows = campaign_panel(cname)
    sent = await event.reply(text, buttons=rows, parse_mode="html")
    UI["screen"] = ("campaign", (cname,))
    UI["panel"] = sent.id
    return True


def apply_pending(kind: str, cname: str, raw: str) -> str | None:
    camp = campaign_of(cname)
    if not camp:
        return "That campaign no longer exists."
    value = raw.strip()
    try:
        if kind == "interval":
            camp["interval_min"] = max(5, int(value))
        elif kind == "delay":
            camp["delay_s"] = max(15, int(value))
        elif kind == "window":
            match = WINDOW_RE.match(value)
            if not match:
                return "Window must look like 09:00-22:00."
            camp["window"] = {
                "start": f"{int(match.group(1)):02d}:{match.group(2)}",
                "end": f"{int(match.group(3)):02d}:{match.group(4)}",
                "tz": (camp.get("window") or {}).get("tz", "Asia/Kolkata"),
            }
        elif kind == "days":
            items = [d.strip().lower()[:3] for d in value.split(",") if d.strip()]
            if not items or any(d not in DAYS and d != "all" for d in items):
                return "Days must be mon..sun or all."
            camp["days"] = items
        elif kind == "groups":
            items = [g.strip() for g in value.split(",") if g.strip()]
            if not items:
                return "No groups given."
            camp["groups"] = items
        elif kind == "addgroups":
            items = [g.strip() for g in value.split(",") if g.strip()]
            if not items:
                return "No groups given."
            existing = camp.setdefault("groups", [])
            seen = {str(split_group(g)[0]) for g in existing}
            for item in items:
                key = str(split_group(item)[0])
                if key not in seen:
                    existing.append(item)
                    seen.add(key)
        else:
            return "Unknown setting."
    except ValueError:
        return "That is not a number."
    save_config()
    return None


async def on_message(event) -> None:
    if event.sender_id not in ALLOWED:
        return
    CURRENT.set(event.sender_id)

    text = event.raw_text.strip()
    if UI.get("login"):
        await handle_login(event, text)
        return
    if text.startswith("/post"):
        await save_post(event)
        return
    if text.startswith("/start"):
        UI["screen"] = ("home", ())
        text, rows = home_panel()
        sent = await event.reply(text, buttons=rows, parse_mode="html")
        UI["panel"] = sent.id
        return

    pending = UI.get("pending")
    if pending:
        kind = pending[0]
        if text == "/cancel":
            UI["pending"] = None
            await event.reply("Cancelled.")
            return

        if kind == "newcamp":
            cname = text.strip().lower()
            if not create_campaign(cname, pending[1]):
                await event.reply("Name must be a-z, 0-9, _- and not already used.")
                return
            UI["pending"] = None
            await send_panel(event, "campaign", cname)
            return

        if kind in ("renamecamp", "renameacc"):
            new_name = text.strip().lower()
            if kind == "renamecamp":
                ok = rename_campaign(pending[1], new_name)
                screen = "campaign"
            else:
                ok = rename_account(pending[1], new_name)
                screen = "account"
            if not ok:
                await event.reply("Name must be a-z, 0-9, _- and not already used.")
                return
            UI["pending"] = None
            await send_panel(event, screen, new_name)
            return

        if kind == "renamepost":
            new_name = text.strip().lower()
            if not rename_post(pending[1], new_name):
                await event.reply("Name must be a-z, 0-9, _-, free, and the post must exist.")
                return
            UI["pending"] = None
            await send_panel(event, "postdetail", new_name)
            return

        if kind == "adname":
            name = text.strip().lower()
            if not SLUG.match(name):
                await event.reply("Name must be a-z, 0-9, _- (max 30).")
                return
            UI["pending"] = ("ad", pending[1], name)
            await event.reply(
                "Send the ad message now. Formatting and animated emoji are kept."
            )
            return

        if kind == "ad":
            slug = pending[2] if len(pending) > 2 else None
            if await save_ad(event, pending[1], slug):
                UI["pending"] = None
            return

        if kind == "editpost":
            if await replace_post(event, pending[1]):
                UI["pending"] = None
            return

        UI["pending"] = None
        cname = pending[1]
        if cname not in campaigns():
            await event.reply("That campaign no longer exists.")
            return
        error = apply_pending(kind, cname, text)
        if error:
            await event.reply(f"⚠️ {error}")
        UI["screen"] = ("campaign", (cname,))
        await refresh_panel(event.client)
        return

    await event.reply(
        "Reply to a message with /post <slug> to save it, or /start for the panel."
    )


# ---------------------------------------------------------------- commands

def require_api() -> None:
    if not (API_ID and API_HASH):
        sys.exit("Set TG_API_ID and TG_API_HASH in .env (see .env.example)")


def require_bot() -> None:
    require_api()
    if not BOT_TOKEN or not OWNER_ID:
        sys.exit("Set BOT_TOKEN and OWNER_ID in .env (see .env.example)")


async def start_user_client(session_path: Path):
    client = TelegramClient(
        str(session_path), API_ID, API_HASH, receive_updates=False
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
    except Exception:
        log.exception("cannot connect %s", session_path)
        return None
    try:
        # cache peers so bare -100… ids resolve without a prior chat lookup
        await client.get_dialogs(limit=None)
    except Exception:
        log.exception("cannot preload dialogs for %s", session_path)
    return client


async def cmd_login(name: str, uid: int = OWNER_ID) -> None:
    require_api()
    if not NAME.match(name):
        sys.exit("Account name must be a-z, 0-9, _ or -")
    if uid not in ALLOWED:
        sys.exit(f"User {uid} is not in ALLOWED_USERS")
    load_users()
    CURRENT.set(uid)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(SESSIONS / name), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    ensure_account(name)
    log.info("logged in %s as %s (@%s)", name, me.first_name, me.username)


async def run_bot() -> None:
    require_bot()
    load_users()
    (ROOT / "sessions").mkdir(exist_ok=True)
    total = 0
    for uid, user in USERS.items():
        for name, conf in user["cfg"]["accounts"].items():
            if not NAME.match(name):
                log.warning("user %s: skipping account %r", uid, name)
                continue
            user["accounts"][name] = Account(name, conf.get("session", name))
        for acc in user["accounts"].values():
            acc.client = await start_user_client(user["sessions"] / acc.session)
            if acc.client:
                acc.ready = True
                total += 1
                TASKS.append(asyncio.create_task(worker(uid, acc)))
            else:
                log.error(
                    "user %s: %s is not logged in: python app.py --login %s --user %s",
                    uid, acc.name, acc.session, uid,
                )

    bot = TelegramClient(str(ROOT / "sessions" / "control"), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    bot.add_event_handler(on_message, events.NewMessage())
    bot.add_event_handler(on_callback, events.CallbackQuery())
    me = await bot.get_me()
    log.info("control bot @%s is up (%d users, %d accounts)",
             me.username, len(USERS), total)
    CURRENT.set(OWNER_ID)
    log_event(f"bot up · {len(USERS)} users · {total} accounts")
    await bot.run_until_disconnected()


def dry_run(uid: int = OWNER_ID) -> None:
    load_users()
    CURRENT.set(uid)
    cfg = CFG
    state = STATE
    now = datetime.now().astimezone()
    fired = False
    for cname, camp in cfg.get("campaigns", {}).items():
        entry = state.get(cname) or {}
        next_ts = entry.get("next")
        if not due(camp, next_ts, now):
            continue
        fired = True
        name = camp.get("account") or "—"
        path = POSTS / (camp.get("post") or "")
        template = path.read_text(encoding="utf-8") if path.exists() else ""
        print(f"[{name}/{cname}] due now → {len(camp.get('groups') or [])} groups")
        for entry in camp.get("groups") or []:
            chat, extra = split_group(entry)
            body = render(template, {"title": "<title>", "username": "", **extra})
            print(f"  → {chat}: {body[:120]!r}")
    if not fired:
        print("Nothing due right now.")


async def _login_check() -> None:
    from types import SimpleNamespace

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.disconnected = False
            self.password = None

        async def connect(self):
            pass

        async def send_code_request(self, phone):
            assert phone == "+4915112345678"
            return SimpleNamespace(phone_code_hash="hash")

        async def sign_in(self, **kwargs):
            if "password" in kwargs:
                self.password = kwargs["password"]
                return
            assert kwargs["phone_code_hash"] == "hash"
            if kwargs["code"] != "111":
                raise PhoneCodeInvalidError(request=None)
            raise SessionPasswordNeededError(request=None)

        async def get_me(self):
            return SimpleNamespace(first_name="Alice", username="alice")

        async def disconnect(self):
            self.disconnected = True

    class FakeEvent:
        def __init__(self):
            self.replies = []

        async def reply(self, text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(id=1)

        async def delete(self):
            pass

        async def edit(self, *args, **kwargs):
            pass

    global TelegramClient, start_user_client, worker, TASKS, CONFIG_PATH

    test_uid = 424243
    saved = (TelegramClient, start_user_client, worker, TASKS, CONFIG_PATH)
    event = FakeEvent()
    root = Path(tempfile.mkdtemp())
    token = CURRENT.set(test_uid)
    USERS[test_uid] = make_user(test_uid)
    user = USERS[test_uid]
    user["sessions"] = root / "sessions"
    user["sessions"].mkdir(parents=True)
    CONFIG_PATH = root / "accounts.json"
    TASKS = []
    TelegramClient = FakeClient  # type: ignore[assignment]

    async def fake_start(path):
        return FakeClient()

    async def fake_worker(uid, acc):
        return

    start_user_client = fake_start  # type: ignore[assignment]
    worker = fake_worker  # type: ignore[assignment]

    try:
        UI["login"] = {"stage": "name", "attempts": 0}
        await handle_login(event, "acc1")
        assert UI["login"]["stage"] == "phone"
        await handle_login(event, "+49 151 12345678")
        assert UI["login"]["stage"] == "code"
        await handle_login(event, "999")
        assert UI["login"]["stage"] == "code", "bad code must keep the stage"
        await handle_login(event, "111")
        assert UI["login"]["stage"] == "password"
        await handle_login(event, "hunter2")
        assert UI["login"] is None
        assert ACCOUNTS["acc1"].ready
        assert CFG["accounts"]["acc1"] == {"session": "acc1"}
        assert any("Logged in" in r for r in event.replies)
    finally:
        (TelegramClient, start_user_client, worker, TASKS, CONFIG_PATH) = saved
        CURRENT.reset(token)
        USERS.pop(test_uid, None)


def self_check() -> None:
    tz = ZoneInfo("Europe/Berlin")
    friday = datetime(2026, 11, 13, 10, 0, tzinfo=tz)
    night = datetime(2026, 11, 13, 23, 0, tzinfo=tz)
    camp = {
        "enabled": True, "post": "x.html", "groups": ["@a"], "interval_min": 60,
        "delay_s": 30, "window": {"start": "09:00", "end": "22:00", "tz": "Europe/Berlin"},
        "days": ["all"], "active_from": None, "active_until": None,
    }
    assert in_window(camp, friday)
    assert not in_window(camp, night)

    wrap = {**camp, "window": {"start": "22:00", "end": "02:00", "tz": "Europe/Berlin"}}
    assert in_window(wrap, night)
    assert not in_window(wrap, friday)

    assert in_window({**camp, "days": ["fri"]}, friday)
    assert not in_window({**camp, "days": ["mon"]}, friday)

    assert due(camp, (friday - timedelta(minutes=1)).timestamp(), friday)
    assert not due(camp, (friday + timedelta(minutes=1)).timestamp(), friday)
    assert not due({**camp, "enabled": False}, None, friday)
    assert not due({**camp, "groups": []}, None, friday)
    assert due(camp, None, friday)
    for _ in range(100):
        assert 180 <= jittered(300) <= 300
    ts = datetime(2026, 11, 13, 3, 20, tzinfo=ZoneInfo("UTC")).timestamp()
    assert fmt_ist(ts) == "13/11/26 8:50 AM"
    assert fmt_ist(ts, True) == "13/11/26 8:50:00 AM"
    assert len(f"moveto:{'c' * 24}:{'a' * 24}".encode()) <= 64
    assert len(f"post:{'c' * 24}:{'s' * 30}".encode()) <= 64
    assert cycle_delay(camp, 10) == 10
    for _ in range(50):
        assert 18 <= cycle_delay(camp) <= 30

    assert render("Hi {{title}} {{missing}}", {"title": "A & B"}) == "Hi A &amp; B {{missing}}"
    assert render("<b>{{n}}</b>", {"n": "<x>"}) == "<b>&lt;x&gt;</b>"
    assert render("<tg-emoji emoji-id=\"1\">🔥</tg-emoji>", {}) == \
        '<tg-emoji emoji-id="1">🔥</tg-emoji>'

    assert split_group("@a") == ("@a", {})
    assert split_group({"chat": "@b", "vars": {"p": 1}}) == ("@b", {"p": 1})
    assert split_group("-1002106442789") == (-1002106442789, {})
    assert split_group({"chat": "-100123", "vars": {"p": 1}}) == (-100123, {"p": 1})

    from types import SimpleNamespace as NS
    from telethon.tl import types as tl
    rich = tl.RichMessage(
        blocks=[
            tl.PageBlockParagraph(tl.TextConcat([
                tl.TextBold(tl.TextPlain("A & B")), tl.TextPlain(" x"),
            ])),
            tl.PageBlockDivider(),
            tl.PageBlockBlockquote(tl.TextPlain("q"), tl.TextEmpty(), collapsed=False),
            tl.PageBlockParagraph(tl.TextCustomEmoji(1, "\U0001f525")),
        ],
        photos=[], documents=[],
    )
    html_body = ("<b>A &amp; B</b> x\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
                 "\n\n<blockquote>q</blockquote>\n\n"
                 "<tg-emoji emoji-id=\"1\">\U0001f525</tg-emoji>")
    assert rich_to_html(rich) == html_body
    assert message_html(NS(message="", entities=[], rich_message=rich)) == html_body
    assert telegram_len("<b>hi</b>") == 2
    assert telegram_len('<tg-emoji emoji-id="1">\U0001f525</tg-emoji>') == 2

    global CONFIG_PATH, STATE_PATH
    test_uid = 424242
    root = Path(tempfile.mkdtemp())
    (root / "posts").mkdir()
    saved_paths = (CONFIG_PATH, STATE_PATH)
    saved_user = USERS.get(test_uid)
    token = CURRENT.set(test_uid)
    USERS[test_uid] = make_user(test_uid)
    user = USERS[test_uid]
    user["posts"] = root / "posts"
    user["sessions"] = root / "sessions"
    user["accounts"] = {"a": Account("a", "a"), "b": Account("b", "b")}
    user["cfg"] = {
        "accounts": {"a": {"session": "a"}, "b": {"session": "b"}},
        "campaigns": {"c": {"account": "a", "post": "old.html"}},
    }
    user["results"] = {"c": {"sent": 1}}
    user["state"] = {"c": {"last": 1, "next": 2}}
    try:
        (root / "posts" / "old.html").write_text("x", encoding="utf-8")
        CONFIG_PATH = root / "accounts.json"
        STATE_PATH = root / "state.json"

        assert move_campaign("c", "b")
        assert CFG["campaigns"]["c"]["account"] == "b"
        assert not move_campaign("nope", "b")
        assert not move_campaign("c", "missing")

        assert create_campaign("two", "a")
        assert not create_campaign("two", "a")
        assert not create_campaign("bad:name", "a")
        assert rename_campaign("c", "renamed")
        assert "c" not in CFG["campaigns"] and "renamed" in STATE
        assert "renamed" in RESULTS

        assert rename_account("b", "z")
        assert CFG["campaigns"]["renamed"]["account"] == "z"
        assert ACCOUNTS["z"].session == "b" and "b" not in ACCOUNTS
        assert not rename_account("z", "a")

        assert apply_pending("addgroups", "renamed", "@a, -100123") is None
        assert CFG["campaigns"]["renamed"]["groups"] == ["@a", "-100123"]
        assert apply_pending("addgroups", "renamed", "@a, @b") is None
        assert CFG["campaigns"]["renamed"]["groups"] == ["@a", "-100123", "@b"]

        text, rows = window_panel("renamed")
        assert any(b.text == "24/7" for row in rows for b in row)
        for row in rows:
            for b in row:
                assert len(b.type.data) <= 64

        user["state"] = {}
        log_event("hello")
        assert STATE["_log"][-1]["text"] == "hello"
        assert "hello" in logs_panel()[0]
        flat = migrate_state({"_log": [{"at": 1, "text": "x"}],
                              "c": {"last": 1, "next": 2}})
        assert "_log" in flat and flat["c"]["next"] == 2

        assert rename_post("old", "new")
        assert CFG["campaigns"]["renamed"]["post"] == "new.html"
        assert not rename_post("old", "new")
        delete_post("new")
        assert CFG["campaigns"]["renamed"]["post"] is None
        assert not (root / "posts" / "new.html").exists()

        nested = {"accounts": {"z": {"session": "z",
                                     "campaigns": {"n": {"post": "p"}}}}}
        assert migrate_config(nested)
        assert nested["campaigns"]["n"]["account"] == "z"
        assert "campaigns" not in nested["accounts"]["z"]

        assert migrate_state({"z": {"n": 5}}) == {"n": {"last": 5, "next": None}}
        assert migrate_state({"n": {"last": 1, "next": 2}}) == \
            {"n": {"last": 1, "next": 2}}
    finally:
        CONFIG_PATH, STATE_PATH = saved_paths
        CURRENT.reset(token)
        if saved_user is None:
            USERS.pop(test_uid, None)
        else:
            USERS[test_uid] = saved_user

    asyncio.run(_login_check())
    print("self-check: OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram broadcaster")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--login", metavar="ACCOUNT", help="log a sender account in")
    parser.add_argument("--self-check", action="store_true", help="run offline checks")
    parser.add_argument("--dry-run", action="store_true", help="show due campaigns")
    parser.add_argument("--user", type=int, default=OWNER_ID,
                        help="user id for --login/--dry-run (default: owner)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_check:
        self_check()
    elif args.login:
        asyncio.run(cmd_login(args.login, args.user))
    elif args.dry_run:
        dry_run(args.user)
    else:
        asyncio.run(run_bot())


if __name__ == "__main__":
    main()
