"""
Byte Clipper V3 - Telegram poller (runs every ~2 min via GitHub Actions cron)

Flow
----
1. Poll getUpdates using the offset persisted in .github/tg_offset.txt.
2. YouTube / Google Drive link received -> reply immediately with 1-click buttons:
       [🎬 3 Viral Shorts]  [🎬 5 Viral Shorts]
3. Button tapped (callback_query):
       a. answerCallbackQuery FIRST (kills the Telegram spinner instantly),
       b. edit the message -> "Processing started! 5-7 minutes...",
       c. repository_dispatch `byte_clip` with EXACTLY the keys clip.yml/clip.py read:
              { "url": ..., "num_clips": "3"|"5", "chat_id": ... }
4. Offset + pending-link map (.github/tg_links.json) are written back; poll.yml
   commits them (permissions: contents: write) under a concurrency group so
   cron runs never race on the state files.

Why tg_links.json exists: Telegram callback_data is hard-capped at 64 bytes,
so the full video URL is stashed in-repo under a sha1 short id instead.
"""
import hashlib
import html
import json
import os
import re
import time
import traceback
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GH_TOKEN = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("GITHUB_REPOSITORY")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = Path(".github")
OFFSET_FILE = STATE_DIR / "tg_offset.txt"
LINKS_FILE = STATE_DIR / "tg_links.json"

LINK_TTL_SECONDS = 6 * 3600        # forget a pending link if no button was tapped within 6h
MAX_PENDING_LINKS = 300            # hard cap so the state file can't grow forever
DISPATCH_GUARD_SECONDS = 90        # ignore double-taps on the same button

MAX_NUM_CLIPS = 10


def _parse_chat_ids(raw):
    ids = set()
    for tok in re.split(r"[,\s;]+", raw or ""):
        tok = tok.strip()
        if re.fullmatch(r"-?\d+", tok):
            ids.add(int(tok))
    return ids


# Optional repo secret ALLOWED_CHAT_IDS = comma-separated chat ids.
# Empty = anyone talking to the bot may dispatch jobs (public bot mode).
ALLOWED_CHAT_IDS = _parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))

LINK_RE = re.compile(
    r"(https?://(?:www\.|m\.)?"
    r"(?:youtu\.be/[\w\-]+"
    r"|youtube\.com/(?:watch\?v=|shorts/|live/|embed/)[\w\-]+"
    r"|drive\.google\.com/(?:file/d/[\w\-]+|[^\s]*?[?&]id=[\w\-]+))"
    r"[^\s]*)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- Telegram API
def get_updates(offset=None, timeout=10):
    params = {
        "timeout": timeout,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    try:
        res = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=timeout + 15).json()
        if not res.get("ok"):
            print("getUpdates not ok:", res)
            return []
        return res.get("result", [])
    except Exception as e:
        print("getUpdates failed:", e)
        return []


def send_msg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=20)
    except Exception as e:
        print("sendMessage failed:", e)


def edit_msg(chat_id, message_id, text):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=20)
        if not r.ok:
            # e.g. message too old / identical text - fall back to a plain new message
            send_msg(chat_id, text)
    except Exception as e:
        print("editMessageText failed:", e)
        send_msg(chat_id, text)


def answer_callback(callback_query_id, text=None):
    """Must be called EXACTLY ONCE per callback, as early as possible."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json=payload, timeout=20)
    except Exception as e:
        print("answerCallbackQuery failed:", e)


def trigger_github(url, num_clips, chat_id):
    """Dispatch byte_clip with the EXACT keys clip.yml / clip.py read:
       url, num_clips, chat_id."""
    if not GH_TOKEN:
        print("dispatch skipped: GH_PAT / GITHUB_TOKEN dono missing hain")
        return False, None
    if not REPO:
        print("dispatch skipped: GITHUB_REPOSITORY missing hai")
        return False, None

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "event_type": "byte_clip",
        "client_payload": {
            "url": url,
            "num_clips": str(num_clips),
            "chat_id": str(chat_id),
        },
    }
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/dispatches",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if r.status_code != 204:
            print("dispatch failed:", r.status_code, r.text[:300])
        return r.status_code == 204, r
    except Exception as e:
        print("dispatch failed:", e)
        return False, None


# ---------------------------------------------------------------- persisted state
def load_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return None


def save_offset(offset):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset))


def load_links():
    try:
        return json.loads(LINKS_FILE.read_text())
    except Exception:
        return {}


def save_links(links):
    now = time.time()
    links = {k: v for k, v in links.items() if now - v.get("ts", 0) < LINK_TTL_SECONDS}
    if len(links) > MAX_PENDING_LINKS:
        newest = sorted(links.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        links = dict(newest[:MAX_PENDING_LINKS])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LINKS_FILE.write_text(json.dumps(links))
    return links


def short_id(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def chat_allowed(chat_id):
    if not ALLOWED_CHAT_IDS:
        return True
    try:
        return int(chat_id) in ALLOWED_CHAT_IDS
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- update handlers
def handle_callback(cb, links):
    cb_id = cb.get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    parts = data.split("|")
    ok_shape = (
        len(parts) == 3
        and parts[0] == "clip"
        and parts[2].isdigit()
        and 1 <= int(parts[2]) <= MAX_NUM_CLIPS
    )
    sid = parts[1] if ok_shape else ""
    num_clips = parts[2] if ok_shape else ""
    entry = links.get(sid) if ok_shape else None
    url = (entry or {}).get("url") or ""

    # 1) ack EXACTLY ONCE, before any other network call -> spinner dies instantly
    if chat_id is None or message_id is None:
        answer_callback(cb_id, "Is message par ab click nahi kar sakte.")
        return
    if not chat_allowed(chat_id):
        answer_callback(cb_id, "Ye bot abhi invite-only hai.")
        return
    if not ok_shape:
        answer_callback(cb_id, "Unknown action.")
        return
    if not GH_TOKEN or not REPO:
        answer_callback(cb_id, "Server ka GitHub token set nahi hai.")
        return
    if not entry or not url:
        answer_callback(cb_id)
        edit_msg(chat_id, message_id, "Ye link expire ho gaya hai. Link dubara bhej dein.")
        return
    if time.time() - float(entry.get("dispatched_at") or 0) < DISPATCH_GUARD_SECONDS:
        answer_callback(cb_id, "Ye clip pehle se chal rahi hai...")
        return

    answer_callback(cb_id)

    # 2) confirm execution (edit also strips the keyboard -> no re-taps)
    edit_msg(
        chat_id,
        message_id,
        f"🚀 <b>Processing started!</b> {num_clips} viral shorts ban rahe hain - "
        f"5-7 minutes me yahan aa jayengi...",
    )

    # 3) fire repository_dispatch with EXACT keys clip.yml / clip.py read
    entry["dispatched_at"] = time.time()
    ok, r = trigger_github(url, num_clips, chat_id)
    if not ok:
        entry.pop("dispatched_at", None)  # allow a retry after cooldown
        status = getattr(r, "status_code", "?")
        edit_msg(
            chat_id,
            message_id,
            f"GitHub dispatch fail hua ({status}). Thodi der baad try karein.",
        )


def handle_message(msg, links):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if (msg.get("from") or {}).get("is_bot"):
        return  # never reply to other bots (loop safety)

    text = (msg.get("text") or "").strip()

    if text in ("/start", "/help"):
        if not chat_allowed(chat_id):
            send_msg(chat_id, "Ye bot abhi invite-only hai.")
            return
        send_msg(
            chat_id,
            "👋 <b>Byte Clipper V3 Bot Active!</b>\n\n"
            "Mujhe koi YouTube ya Google Drive link bhejein, phir 1-click button se "
            "3 ya 5 viral shorts choose karein. Shorts 5-7 minutes me yahin aa jayengi.",
        )
        return

    m = LINK_RE.search(text) if text else None
    if m:
        if not chat_allowed(chat_id):
            send_msg(chat_id, "Ye bot abhi invite-only hai.")
            return
        url = m.group(1).rstrip(".,;:!?)]'\"")
        sid = short_id(url)
        links[sid] = {"url": url, "ts": time.time()}
        keyboard = {
            "inline_keyboard": [[
                {"text": "🎬 3 Viral Shorts", "callback_data": f"clip|{sid}|3"},
                {"text": "🎬 5 Viral Shorts", "callback_data": f"clip|{sid}|5"},
            ]]
        }
        shown = html.escape(url if len(url) <= 90 else url[:87] + "...")
        send_msg(
            chat_id,
            "🎯 <b>Video link mil gaya!</b>\n\n"
            f"<code>{shown}</code>\n\n"
            "Niche kisi ek button par click karein:",
            reply_markup=keyboard,
        )
        return

    # polite nudge only in private chats so groups don't get spammed
    if text and not text.startswith("/") and chat.get("type") == "private":
        send_msg(chat_id, "Mujhe sirf YouTube ya Google Drive ka link bhejein 🙂")


# ---------------------------------------------------------------- main loop
def process():
    offset = load_offset()
    links = load_links()

    if offset is None:
        # Fresh/corrupt state file: confirm the existing backlog WITHOUT acting on
        # old messages, then start polling from the tip.
        updates = get_updates()
        if updates:
            save_offset(updates[-1]["update_id"] + 1)
            print("offset primed at", updates[-1]["update_id"] + 1)
        save_links(links)
        return

    updates = get_updates(offset=offset)
    if not updates:
        save_links(links)  # still prune expired entries even on an empty poll
        return

    last_id = None
    for u in updates:
        last_id = u.get("update_id", last_id)
        try:
            if "callback_query" in u:
                handle_callback(u["callback_query"], links)
            elif "message" in u:
                handle_message(u["message"], links)
        except Exception:
            # One bad update must never poison the offset loop (no stuck reprocessing).
            print("failed to handle update", u.get("update_id"))
            print(traceback.format_exc())

    save_links(links)
    if last_id is not None:
        save_offset(last_id + 1)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing hai (repo secret set karo).")
    process()
