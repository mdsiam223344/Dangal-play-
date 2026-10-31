import os
import json
import logging
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dangal-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8535271767:AAGASCq-ncUEGY5IYnszNs_WOGQ8xlewOoY").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "1686274364").split(",")
    if x.strip().isdigit()
}
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004427032466").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
FEED_URL = os.getenv("FEED_URL", "").strip()
FEED_INTERVAL = max(60, int(os.getenv("FEED_INTERVAL", "300")))
DB_PATH = os.getenv("DB_PATH", "dangal.db")
WEBHOOK_PATH = "/telegram"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is required (comma-separated Telegram user IDs)")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required")
if not WEBHOOK_URL.startswith("https://"):
    raise RuntimeError("WEBHOOK_URL must be your public Render HTTPS URL")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET must be at least 16 characters")

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series TEXT NOT NULL,
            season TEXT NOT NULL,
            episode TEXT NOT NULL,
            release_date TEXT DEFAULT '',
            description TEXT DEFAULT '',
            poster TEXT DEFAULT '',
            link TEXT DEFAULT '',
            source TEXT DEFAULT 'admin',
            created_at TEXT NOT NULL,
            UNIQUE(series, season, episode)
        )
        """)
        conn.commit()

def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)

def safe_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")

def save_episode(item: dict, source: str = "admin") -> tuple[bool, int | None]:
    required = ("series", "season", "episode")
    if any(not str(item.get(k, "")).strip() for k in required):
        raise ValueError("series, season and episode are required")
    values = (
        str(item["series"]).strip(),
        str(item["season"]).strip(),
        str(item["episode"]).strip(),
        str(item.get("release_date", "")).strip(),
        str(item.get("description", "")).strip(),
        str(item.get("poster", "")).strip(),
        str(item.get("link", "")).strip(),
        source,
        datetime.now(timezone.utc).isoformat(),
    )
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO episodes
            (series, season, episode, release_date, description, poster, link, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)
        conn.commit()
        if cur.rowcount == 0:
            return False, None
        return True, cur.lastrowid

def get_episode(eid: int):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()

def format_caption(row) -> str:
    caption = (
        "🆕 DANGAL PLAY — NEW EPISODE\n\n"
        f"📺 Serial: {row['series']}\n"
        f"📁 Season: {row['season']}\n"
        f"🎬 Episode: {row['episode']}\n"
        f"📅 Release: {row['release_date'] or 'Not provided'}\n\n"
        f"📝 {row['description'] or 'New episode update'}\n\n"
        "🔔 Follow the channel for more OTT updates."
    )
    return caption[:1024]

async def publish_episode(bot, row):
    keyboard = None
    if safe_url(row["link"] or ""):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Watch on Official Platform", url=row["link"])
        ]])
    if safe_url(row["poster"] or ""):
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=row["poster"],
            caption=format_caption(row),
            reply_markup=keyboard,
        )
    else:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=format_caption(row),
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

async def publish_saved(application: Application, episode_id: int):
    row = get_episode(episode_id)
    if row:
        await publish_episode(application.bot, row)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 Welcome to Dangal Play OTT Updates!\n\n"
        "এখানে নতুন সিরিয়াল ও এপিসোডের আপডেট পাবেন।\n"
        "/latest — সর্বশেষ এপিসোড\n"
        "/help — সাহায্য"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 User commands:\n/latest — সর্বশেষ ১০টি এপিসোড\n\n"
        "🔐 Admin commands:\n"
        "/add SERIES|SEASON|EPISODE|DATE|DESCRIPTION|POSTER_URL|OFFICIAL_URL\n"
        "/delete EPISODE_ID\n"
        "/feedcheck — এখনই Feed চেক করুন\n"
        "/help — Help"
    )
    await update.effective_message.reply_text(text)

async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("এখনও কোনো এপিসোড যোগ করা হয়নি।")
        return
    lines = ["🎬 LATEST DANGAL PLAY EPISODES\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} • {r['series']}\n"
            f"Season {r['season']} | Episode {r['episode']}\n"
            f"📅 {r['release_date'] or 'N/A'}\n"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML"
    )

async def add_episode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 7:
        await update.effective_message.reply_text(
            "ব্যবহার:\n/add SERIES|SEASON|EPISODE|DATE|DESCRIPTION|POSTER_URL|OFFICIAL_URL\n\n"
            "URL না থাকলে - লিখুন।"
        )
        return
    keys = ["series", "season", "episode", "release_date",
            "description", "poster", "link"]
    item = dict(zip(keys, parts))
    for key in ("poster", "link"):
        if item[key] == "-":
            item[key] = ""
        elif item[key] and not safe_url(item[key]):
            await update.effective_message.reply_text(f"{key} অবশ্যই http:// অথবা https:// URL হতে হবে, অথবা - দিন।")
            return
    try:
        inserted, eid = save_episode(item, "admin")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if not inserted:
        await update.effective_message.reply_text("⚠️ এই সিরিয়ালের Season/Episode আগে যোগ করা হয়েছে।")
        return
    row = get_episode(eid)
    try:
        await publish_episode(context.bot, row)
        await update.effective_message.reply_text(f"✅ Episode #{eid} যোগ হয়েছে এবং Channel-এ পোস্ট করা হয়েছে।")
    except Exception:
        log.exception("Channel publish failed for episode %s", eid)
        await update.effective_message.reply_text(
            f"⚠️ Episode #{eid} ডেটাবেসে সেভ হয়েছে, কিন্তু Channel-এ পোস্ট হয়নি। "
            "Bot-কে Channel Admin করুন এবং CHANNEL_ID যাচাই করুন।"
        )

async def delete_episode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("ব্যবহার: /delete EPISODE_ID")
        return
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM episodes WHERE id=?", (int(context.args[0]),))
        conn.commit()
    await update.effective_message.reply_text(
        "🗑️ Episode deleted." if cur.rowcount else "Episode ID পাওয়া যায়নি।"
    )

async def fetch_feed_items():
    if not FEED_URL:
        return []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        response = await client.get(FEED_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("episodes", [])
    if not isinstance(payload, list):
        raise ValueError("Feed JSON must be a list or an object with an 'episodes' list")
    return payload

async def check_feed(application: Application):
    if not FEED_URL:
        return
    try:
        items = await fetch_feed_items()
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = {
                "series": item.get("series", ""),
                "season": item.get("season", ""),
                "episode": item.get("episode", ""),
                "release_date": item.get("release_date", ""),
                "description": item.get("description", ""),
                "poster": item.get("poster", ""),
                "link": item.get("link", ""),
            }
            if normalized["poster"] and not safe_url(str(normalized["poster"])):
                normalized["poster"] = ""
            if normalized["link"] and not safe_url(str(normalized["link"])):
                normalized["link"] = ""
            try:
                inserted, eid = save_episode(normalized, "feed")
            except (ValueError, TypeError):
                log.warning("Skipping invalid feed item: %r", item)
                continue
            if inserted:
                row = get_episode(eid)
                try:
                    await publish_episode(application.bot, row)
                    added += 1
                except Exception:
                    log.exception("Could not publish feed episode %s", eid)
        log.info("Feed check complete. New episodes published: %s", added)
    except Exception:
        log.exception("Feed check failed")

async def feedcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if not FEED_URL:
        await update.effective_message.reply_text("FEED_URL সেট করা নেই।")
        return
    await update.effective_message.reply_text("🔎 Feed চেক করা হচ্ছে...")
    await check_feed(context.application)
    await update.effective_message.reply_text("✅ Feed check শেষ। লগে ফলাফল দেখুন।")

async def scheduled_feed_check(context: ContextTypes.DEFAULT_TYPE):
    await check_feed(context.application)

application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("latest", latest))
application.add_handler(CommandHandler("add", add_episode_command))
application.add_handler(CommandHandler("delete", delete_episode))
application.add_handler(CommandHandler("feedcheck", feedcheck_command))

@asynccontextmanager
async def lifespan(api: FastAPI):
    init_db()
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )
    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_feed_check, interval=FEED_INTERVAL, first=15,
            name="authorized-json-feed-poller"
        )
    log.info("Dangal Play bot started. Webhook: %s%s", WEBHOOK_URL, WEBHOOK_PATH)
    yield
    await application.bot.delete_webhook()
    await application.stop()
    await application.shutdown()

api = FastAPI(title="Dangal Play OTT Notification Bot", lifespan=lifespan)

@api.get("/")
async def home():
    return {"status": "running", "service": "Dangal Play OTT Notification Bot"}

@api.get("/health")
async def health():
    return {"ok": True, "bot": "Dangal Play OTT Notification Bot"}

@api.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        data = await request.json()
        telegram_update = Update.de_json(data, application.bot)
        await application.process_update(telegram_update)
        return JSONResponse({"ok": True})
    except Exception:
        log.exception("Webhook update processing failed")
        raise HTTPException(status_code=400, detail="Update processing failed")
