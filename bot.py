import os
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, JobQueue
)

# ============================================================
# DANGAL PLAY OTT NOTIFICATION BOT - ADVANCED V3
# Polling edition: NO Render Webhook / NO FastAPI
# Deploy on Render as a Background Worker.
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("dangal-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
FEED_URL = os.getenv("FEED_URL", "").strip()
FEED_INTERVAL = max(60, int(os.getenv("FEED_INTERVAL", "300")))
DB_PATH = os.getenv("DB_PATH", "dangal.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is required. Use your numeric Telegram user ID.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required. Example: @YourChannel")

def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with db_connect() as connection:
        connection.execute("""
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
        connection.commit()

def is_admin(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id in ADMIN_IDS
    )

def valid_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")

def save_episode(item: dict, source: str = "admin"):
    required = ("series", "season", "episode")
    if any(not str(item.get(key, "")).strip() for key in required):
        raise ValueError("Serial, season and episode are required.")

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

    with db_connect() as connection:
        cursor = connection.execute("""
            INSERT OR IGNORE INTO episodes
            (series, season, episode, release_date, description,
             poster, link, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)
        connection.commit()

        if cursor.rowcount == 0:
            return False, None
        return True, cursor.lastrowid

def get_episode(episode_id: int):
    with db_connect() as connection:
        return connection.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()

def make_caption(row) -> str:
    return (
        "🆕 DANGAL PLAY — NEW EPISODE\n\n"
        f"📺 Serial: {row['series']}\n"
        f"📁 Season: {row['season']}\n"
        f"🎬 Episode: {row['episode']}\n"
        f"📅 Release: {row['release_date'] or 'Not provided'}\n\n"
        f"📝 {row['description'] or 'New episode update'}\n\n"
        "🔔 Follow the channel for more OTT updates."
    )

async def publish_episode(bot, row):
    caption = make_caption(row)
    keyboard = None

    if valid_url(row["link"] or ""):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "▶️ Watch on Official Platform",
                url=row["link"]
            )
        ]])

    poster = row["poster"] or ""
    if valid_url(poster):
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=poster,
            caption=caption[:1024],
            reply_markup=keyboard,
        )
    else:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

# -------------------- USER COMMANDS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 Welcome to Dangal Play OTT Updates!\n\n"
        "নতুন সিরিয়াল ও এপিসোডের আপডেট পেতে আমাদের Channel-এ যুক্ত থাকুন।\n\n"
        "/latest — সর্বশেষ ১০টি এপিসোড\n"
        "/help — সাহায্য"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📌 USER COMMANDS\n"
        "/start — Start Bot\n"
        "/latest — Latest Episodes\n"
        "/help — Help\n\n"
        "🔐 ADMIN COMMANDS\n"
        "/add SERIES|SEASON|EPISODE|DATE|DESCRIPTION|POSTER_URL|OFFICIAL_URL\n"
        "/delete EPISODE_ID\n"
        "/feedcheck — Check authorized JSON feed"
    )

async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT 10"
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "এখনও কোনো Episode যোগ করা হয়নি।"
        )
        return

    messages = ["🎬 LATEST DANGAL PLAY EPISODES\n"]
    for row in rows:
        messages.append(
            f"#{row['id']} • {row['series']}\n"
            f"Season {row['season']} | Episode {row['episode']}\n"
            f"📅 {row['release_date'] or 'N/A'}\n"
        )

    await update.effective_message.reply_text("\n".join(messages))

# -------------------- ADMIN COMMANDS --------------------

async def add_episode_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return

    raw = " ".join(context.args)
    parts = [part.strip() for part in raw.split("|")]

    if len(parts) != 7:
        await update.effective_message.reply_text(
            "এই ফরম্যাটে পাঠান:\n\n"
            "/add Serial Name|1|12|2026-10-31|Episode description|-|https://www.dangalplay.com/\n\n"
            "ক্রম: SERIES|SEASON|EPISODE|DATE|DESCRIPTION|POSTER_URL|OFFICIAL_URL\n"
            "Poster বা link না থাকলে - লিখুন।"
        )
        return

    keys = [
        "series", "season", "episode", "release_date",
        "description", "poster", "link"
    ]
    item = dict(zip(keys, parts))

    for key in ("poster", "link"):
        if item[key] == "-":
            item[key] = ""
        elif item[key] and not valid_url(item[key]):
            await update.effective_message.reply_text(
                f"{key} URL অবশ্যই http:// অথবা https:// দিয়ে শুরু হতে হবে।"
            )
            return

    try:
        inserted, episode_id = save_episode(item, "admin")
    except ValueError as error:
        await update.effective_message.reply_text(str(error))
        return

    if not inserted:
        await update.effective_message.reply_text(
            "⚠️ এই Serial-এর Season/Episode আগে যোগ করা হয়েছে।"
        )
        return

    row = get_episode(episode_id)
    try:
        await publish_episode(context.bot, row)
        await update.effective_message.reply_text(
            f"✅ Episode #{episode_id} সেভ হয়েছে এবং Channel-এ পোস্ট হয়েছে।"
        )
    except Exception:
        log.exception("Channel publish failed for episode %s", episode_id)
        await update.effective_message.reply_text(
            f"⚠️ Episode #{episode_id} Database-এ সেভ হয়েছে, "
            "কিন্তু Channel-এ পোস্ট হয়নি। Bot-কে Channel Admin করুন।"
        )

async def delete_episode(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "ব্যবহার: /delete EPISODE_ID"
        )
        return

    with db_connect() as connection:
        cursor = connection.execute(
            "DELETE FROM episodes WHERE id = ?",
            (int(context.args[0]),)
        )
        connection.commit()

    await update.effective_message.reply_text(
        "🗑️ Episode deleted."
        if cursor.rowcount
        else "এই Episode ID পাওয়া যায়নি।"
    )

# -------------------- AUTHORIZED JSON FEED --------------------

async def fetch_feed_items():
    if not FEED_URL:
        return []

    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True
    ) as client:
        response = await client.get(
            FEED_URL,
            headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict):
        payload = payload.get("episodes", [])

    if not isinstance(payload, list):
        raise ValueError(
            "Feed must be a JSON list or an object containing episodes[]"
        )
    return payload

async def check_feed(application: Application):
    if not FEED_URL:
        return

    try:
        items = await fetch_feed_items()
        published = 0

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

            for key in ("poster", "link"):
                if normalized[key] and not valid_url(str(normalized[key])):
                    normalized[key] = ""

            try:
                inserted, episode_id = save_episode(normalized, "feed")
            except (ValueError, TypeError):
                log.warning("Skipping invalid feed entry: %r", item)
                continue

            if inserted:
                row = get_episode(episode_id)
                try:
                    await publish_episode(application.bot, row)
                    published += 1
                except Exception:
                    log.exception(
                        "Could not publish feed episode %s", episode_id
                    )

        log.info("Feed check complete. New posts: %s", published)

    except Exception:
        log.exception("Authorized feed check failed")

async def feedcheck_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Admin only.")
        return

    if not FEED_URL:
        await update.effective_message.reply_text(
            "FEED_URL সেট করা নেই।"
        )
        return

    await update.effective_message.reply_text(
        "🔎 Authorized feed চেক করা হচ্ছে..."
    )
    await check_feed(context.application)
    await update.effective_message.reply_text(
        "✅ Feed check শেষ। বিস্তারিত Render Logs-এ দেখুন।"
    )

async def scheduled_feed_check(context: ContextTypes.DEFAULT_TYPE):
    await check_feed(context.application)

async def post_init(application: Application):
    # Remove any old webhook configuration before polling starts.
    # The bot will receive updates using long polling from now on.
    await application.bot.delete_webhook(drop_pending_updates=True)

    if FEED_URL:
        application.job_queue.run_repeating(
            scheduled_feed_check,
            interval=FEED_INTERVAL,
            first=15,
            name="authorized-json-feed-check"
        )
        log.info("Authorized JSON feed polling enabled.")
    else:
        log.info("FEED_URL is empty. Automatic feed checks are disabled.")

def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("latest", latest))
    app.add_handler(CommandHandler("add", add_episode_command))
    app.add_handler(CommandHandler("delete", delete_episode))
    app.add_handler(CommandHandler("feedcheck", feedcheck_command))

    log.info("Dangal Play OTT Notification Bot starting with polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
