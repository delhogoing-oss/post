#!/usr/bin/env python3
"""
Telegram Channel Post Bot
Python 3.13 + python-telegram-bot 22.8

.env:
    BOT_TOKEN=123:ABC
    CHANNEL_ID=-1001234567890
    ADMIN_IDS=123456789,987654321

The bot stores persistent settings in data.json.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ----------------------------- configuration ---------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")
if not CHANNEL_ID_RAW:
    raise RuntimeError("CHANNEL_ID is missing in .env")

try:
    CHANNEL_ID: int | str = int(CHANNEL_ID_RAW)
except ValueError:
    CHANNEL_ID = CHANNEL_ID_RAW

try:
    ADMIN_IDS = {
        int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()
    }
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS must contain Telegram numeric user IDs.") from exc

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing or empty in .env")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("channel_post_bot")

MAX_TITLE = 500
MAX_URL = 2048
MAX_BUTTON_TEXT = 60
MAX_BUTTONS = 12
BUTTON_STYLES = {
    "default": None,
    "red": "danger",
    "green": "success",
    "blue": "primary",
}

COLOR_LABELS = {
    "default": "⚪ Default",
    "red": "🔴 Red",
    "green": "🟢 Green",
    "blue": "🔵 Blue",
}


DEFAULT_DATA = {
    "next_post_number": 1,
    "default_photo_file_id": None,
    "default_buttons": [],
    "button_layout": "one_per_row",
}

data_lock = asyncio.Lock()

# ----------------------------- states ----------------------------------------

(
    POST_TITLE,
    POST_PREVIEW,
    POST_DOWNLOAD,
    POST_BUTTONS,
    POST_CUSTOM_BUTTON_COLOR,
    POST_CUSTOM_BUTTON_TEXT,
    POST_CUSTOM_BUTTON_URL,
    POST_LAYOUT,
    POST_MEDIA,
    POST_CONFIRM,
    PHOTO_WAIT,
    NUMBER_WAIT,
    BUTTON_MENU,
    BUTTON_ADD_COLOR,
    BUTTON_ADD_TEXT,
    BUTTON_ADD_URL,
    BUTTON_LAYOUT,
) = range(17)

# ----------------------------- persistence -----------------------------------

def valid_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > MAX_URL:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def atomic_write(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{DATA_FILE.name}.",
        suffix=".tmp",
        dir=str(DATA_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, DATA_FILE)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def normalize(raw) -> dict:
    d = deepcopy(DEFAULT_DATA)
    if not isinstance(raw, dict):
        return d

    try:
        n = int(raw.get("next_post_number", 1))
        d["next_post_number"] = max(1, n)
    except (TypeError, ValueError):
        pass

    photo = raw.get("default_photo_file_id")
    if isinstance(photo, str) and photo:
        d["default_photo_file_id"] = photo

    buttons = raw.get("default_buttons", [])
    if isinstance(buttons, list):
        clean = []
        for item in buttons[:MAX_BUTTONS]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()[:MAX_BUTTON_TEXT]
            url = str(item.get("url", "")).strip()[:MAX_URL]
            if text and valid_url(url):
                color = str(item.get("color", "default")).lower()
                if color not in BUTTON_STYLES:
                    color = "default"
                clean.append({"text": text, "url": url, "color": color})
        d["default_buttons"] = clean

    if raw.get("button_layout") in ("one_per_row", "two_per_row"):
        d["button_layout"] = raw["button_layout"]

    return d


def load_data() -> dict:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        atomic_write(DEFAULT_DATA)
        return deepcopy(DEFAULT_DATA)

    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return normalize(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot read {DATA_FILE}. Fix the JSON file before restarting."
        ) from exc


async def get_data() -> dict:
    async with data_lock:
        return deepcopy(load_data())


async def change_data(fn) -> dict:
    async with data_lock:
        d = load_data()
        fn(d)
        atomic_write(d)
        return deepcopy(d)

# ----------------------------- formatting ------------------------------------

def post_text(number: int, title: str, preview: str, download: str) -> str:
    return (
        f"#Post - {number}\n\n"
        "✨━━━━━━━━━━━━━━━✨\n\n"
        f"🎬 {title}\n\n"
        "➥ 𝙋𝙧𝙚𝙫𝙞𝙚𝙬\n"
        f"{preview}\n\n"
        "👨‍💻 ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ :\n\n"
        f"🖇️ {download}\n\n"
        "━━━━━━━━━━━━━━━\n"
        "❤️ Thanks for your support"
    )


def button_style(button: dict):
    color = button.get("color", "default")
    return BUTTON_STYLES.get(color)


def button_label(button: dict) -> str:
    return str(button.get("text", ""))


def keyboard_button(button: dict) -> InlineKeyboardButton:
    style = button_style(button)
    kwargs = {"text": button["text"], "url": button["url"]}
    if style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def keyboard_for(buttons: list[dict], layout: str):
    if not buttons:
        return None

    rows = []
    if layout == "one_per_row":
        rows = [[keyboard_button(b)] for b in buttons]
    else:
        row = []
        for b in buttons:
            row.append(keyboard_button(b))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    return InlineKeyboardMarkup(rows)


def layout_name(layout: str) -> str:
    return "One per row" if layout == "one_per_row" else "Two per row"


def title_clean(value: str) -> str:
    return " ".join(value.strip().split())[:MAX_TITLE]


# ----------------------------- admin helpers ---------------------------------

def admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


async def deny(update: Update) -> bool:
    if admin(update):
        return False
    try:
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot."
            )
    except TelegramError:
        pass
    return True


async def answer(query, text=None, alert=False):
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError:
        pass


def clear(context):
    context.user_data.clear()


# ----------------------------- dashboard --------------------------------------

def dashboard_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Post", callback_data="dash:post")],
        [
            InlineKeyboardButton("🖼️ Set Photo", callback_data="dash:photo"),
            InlineKeyboardButton("🗑️ Remove Photo", callback_data="dash:remove_photo"),
        ],
        [
            InlineKeyboardButton("🔘 Manage Buttons", callback_data="dash:buttons"),
            InlineKeyboardButton("🔢 Post Number", callback_data="dash:number"),
        ],
        [InlineKeyboardButton("⚙️ Status", callback_data="dash:status")],
    ])


async def dashboard_text():
    d = await get_data()
    return (
        "🛠️ <b>Channel Post Admin</b>\n\n"
        f"🔢 Next post number: <b>{d['next_post_number']}</b>\n"
        f"🖼️ Default photo: <b>{'Configured' if d['default_photo_file_id'] else 'Not set'}</b>\n"
        f"🔘 Default buttons: <b>{len(d['default_buttons'])}</b>\n"
        f"↔️ Button layout: <b>{html.escape(layout_name(d['button_layout']))}</b>\n\n"
        "Choose an action:"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return
    clear(context)
    await update.effective_message.reply_text(
        await dashboard_text(),
        reply_markup=dashboard_markup(),
        parse_mode="HTML",
    )


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    if not admin(update):
        await answer(q, "Not authorized.", True)
        return ConversationHandler.END

    await answer(q)
    action = q.data.split(":", 1)[1]

    if action == "post":
        clear(context)
        await q.edit_message_text(
            "➕ <b>Create Post</b>\n\n"
            "Step 1/5\n"
            "🎬 Send the title/content name.\n\n"
            "Use /cancel at any time.",
            parse_mode="HTML",
        )
        return POST_TITLE

    if action == "photo":
        clear(context)
        await q.edit_message_text(
            "🖼️ <b>Set Default Photo</b>\n\n"
            "Send a photo now. Telegram's file_id will be saved.",
            parse_mode="HTML",
        )
        return PHOTO_WAIT

    if action == "remove_photo":
        await change_data(lambda d: d.update(default_photo_file_id=None))
        await q.edit_message_text("🗑️ Default photo removed.")
        return ConversationHandler.END

    if action == "buttons":
        clear(context)
        return await show_button_menu(update, context, edit=True)

    if action == "number":
        clear(context)
        await q.edit_message_text(
            "🔢 Send the number to use for the next post.\nExample: <code>100</code>",
            parse_mode="HTML",
        )
        return NUMBER_WAIT

    if action == "status":
        await show_status(update, context, edit=True)
        return ConversationHandler.END

    return ConversationHandler.END


# ----------------------------- /post flow ------------------------------------

async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    clear(context)
    await update.effective_message.reply_text(
        "➕ <b>Create Post</b>\n\n"
        "Step 1/5\n🎬 Send the title/content name.\n\n"
        "Use /cancel at any time.",
        parse_mode="HTML",
    )
    return POST_TITLE


async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    text = title_clean(update.effective_message.text or "")
    if not text:
        await update.effective_message.reply_text("❌ Title cannot be empty.")
        return POST_TITLE
    context.user_data["title"] = text
    await update.effective_message.reply_text(
        "Step 2/5\n🔗 Send the Preview link.\n\nExample: https://example.com/preview"
    )
    return POST_PREVIEW


async def get_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    url = (update.effective_message.text or "").strip()
    if not valid_url(url):
        await update.effective_message.reply_text(
            "❌ Invalid Preview URL. Use http:// or https://."
        )
        return POST_PREVIEW
    context.user_data["preview"] = url
    await update.effective_message.reply_text(
        "Step 3/5\n🖇️ Send the Here is your link / Download link."
    )
    return POST_DOWNLOAD


async def get_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    url = (update.effective_message.text or "").strip()
    if not valid_url(url):
        await update.effective_message.reply_text(
            "❌ Invalid Download URL. Use http:// or https://."
        )
        return POST_DOWNLOAD

    context.user_data["download"] = url
    d = await get_data()
    # Saved default buttons are applied automatically for every new post.
    # The admin is not asked to choose them again. Use the Change Buttons
    # button on the media screen when a one-off override is needed.
    context.user_data["buttons"] = deepcopy(d["default_buttons"])
    context.user_data["layout"] = d["button_layout"]
    await update.effective_message.reply_text(
        "Step 4/5\n"
        f"🔘 Saved default buttons applied automatically ({len(d['default_buttons'])}).\n"
        "You can change them for this post from the next screen."
    )
    return await ask_media(update, context, edit=False)


async def post_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END

    await answer(q)
    action = q.data.split(":", 1)[1]

    if action == "cancel":
        clear(context)
        await q.edit_message_text("❌ Post cancelled.")
        return ConversationHandler.END

    if action == "back":
        return await ask_media(update, context, edit=True)

    if action == "default":
        d = await get_data()
        context.user_data["buttons"] = deepcopy(d["default_buttons"])
        context.user_data["layout"] = d["button_layout"]
        return await ask_layout(update, context, edit=True)

    if action == "none":
        context.user_data["buttons"] = []
        context.user_data["layout"] = "one_per_row"
        return await ask_media(update, context, edit=True)

    if action == "custom":
        context.user_data["buttons"] = []
        return await ask_custom_color(update, context, edit=True)

    return POST_BUTTONS


async def ask_custom_color(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚪ Default", callback_data="postcolor:default"), InlineKeyboardButton("🔴 Red", callback_data="postcolor:red")],
        [InlineKeyboardButton("🟢 Green", callback_data="postcolor:green"), InlineKeyboardButton("🔵 Blue", callback_data="postcolor:blue")],
    ])
    text = "🎨 <b>Choose color for the next button</b>"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return POST_CUSTOM_BUTTON_COLOR


async def custom_button_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)
    color = q.data.split(":", 1)[1]
    context.user_data["button_color"] = color
    await q.edit_message_text("✏️ Send button text for the next button. Use /done when finished.")
    return POST_CUSTOM_BUTTON_TEXT


async def custom_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    text = " ".join((update.effective_message.text or "").strip().split())
    if not text:
        await update.effective_message.reply_text("❌ Button text cannot be empty.")
        return POST_CUSTOM_BUTTON_TEXT
    if len(text) > MAX_BUTTON_TEXT:
        await update.effective_message.reply_text(
            f"❌ Button text must be <= {MAX_BUTTON_TEXT} characters."
        )
        return POST_CUSTOM_BUTTON_TEXT

    if len(context.user_data.get("buttons", [])) >= MAX_BUTTONS:
        return await custom_done(update, context)

    context.user_data["pending_button_text"] = text
    await update.effective_message.reply_text("🔗 Now send the URL for this button.")
    return POST_CUSTOM_BUTTON_URL


async def custom_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    url = (update.effective_message.text or "").strip()
    if not valid_url(url):
        await update.effective_message.reply_text("❌ Invalid button URL.")
        return POST_CUSTOM_BUTTON_URL

    buttons = context.user_data.setdefault("buttons", [])
    if len(buttons) >= MAX_BUTTONS:
        return await custom_done(update, context)

    text = context.user_data.pop("pending_button_text", None)
    if not text:
        await update.effective_message.reply_text("❌ Button state expired. Restart /post.")
        return ConversationHandler.END

    buttons.append({"text": text, "url": url, "color": context.user_data.get("button_color", "blue")})
    context.user_data.pop("button_color", None)
    await update.effective_message.reply_text(f"✅ Button {len(buttons)} added.")
    if len(buttons) >= MAX_BUTTONS:
        return await custom_done(update, context)
    return await ask_custom_color(update, context, edit=False)


async def custom_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    buttons = context.user_data.get("buttons", [])
    if not buttons:
        await update.effective_message.reply_text(
            "❌ Add at least one button before /done."
        )
        return POST_CUSTOM_BUTTON_TEXT
    return await ask_layout(update, context, edit=False)


async def ask_layout(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣ One per row", callback_data="postlayout:one"),
            InlineKeyboardButton("2️⃣ Two per row", callback_data="postlayout:two"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="postlayout:cancel")],
    ])
    text = "↔️ Choose button layout for this post:"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return POST_LAYOUT


async def post_layout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)

    action = q.data.split(":", 1)[1]
    if action == "cancel":
        clear(context)
        await q.edit_message_text("❌ Post cancelled.")
        return ConversationHandler.END

    context.user_data["layout"] = (
        "one_per_row" if action == "one" else "two_per_row"
    )
    return await ask_media(update, context, edit=True)


async def ask_media(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    d = await get_data()
    rows = []
    if d["default_photo_file_id"]:
        rows.append([
            InlineKeyboardButton("🖼️ Default Photo", callback_data="media:photo")
        ])
    rows.append([
        InlineKeyboardButton("🎥 Video", callback_data="media:video"),
        InlineKeyboardButton("📝 Text", callback_data="media:text"),
    ])
    rows.append([InlineKeyboardButton("🔘 Change Buttons", callback_data="media:buttons")])
    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data="media:cancel")
    ])
    text = "Step 5/5\n🎞️ Choose media for this post:"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows)
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows)
        )
    return POST_MEDIA


async def media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)

    action = q.data.split(":", 1)[1]

    if action == "cancel":
        clear(context)
        await q.edit_message_text("❌ Post cancelled.")
        return ConversationHandler.END

    if action == "buttons":
        await q.edit_message_text(
            "🔘 Change buttons for this post:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Saved Defaults", callback_data="postbtn:default"),
                    InlineKeyboardButton("✏️ Custom", callback_data="postbtn:custom"),
                ],
                [InlineKeyboardButton("🚫 None", callback_data="postbtn:none")],
                [InlineKeyboardButton("⬅️ Back", callback_data="postbtn:back")],
            ]),
        )
        return POST_BUTTONS

    if action == "photo":
        d = await get_data()
        if not d["default_photo_file_id"]:
            await q.edit_message_text("❌ Default photo is not configured.")
            return POST_MEDIA
        context.user_data["media_type"] = "photo"
        context.user_data["media_id"] = d["default_photo_file_id"]
        return await show_preview(update, context, edit=True)

    if action == "text":
        context.user_data["media_type"] = "text"
        context.user_data["media_id"] = None
        return await show_preview(update, context, edit=True)

    if action == "video":
        context.user_data["media_type"] = "video"
        context.user_data["media_id"] = None
        await q.edit_message_text(
            "🎥 Send the video now.\n\n"
            "Only this post will use the video."
        )
        return POST_MEDIA

    return POST_MEDIA


async def media_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    if not update.effective_message or not update.effective_message.video:
        await update.effective_message.reply_text("❌ Please send a Telegram video.")
        return POST_MEDIA

    context.user_data["media_id"] = update.effective_message.video.file_id
    return await show_preview(update, context, edit=False)


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    d = await get_data()
    number = d["next_post_number"]
    title = context.user_data["title"]
    preview = context.user_data["preview"]
    download = context.user_data["download"]
    buttons = context.user_data.get("buttons", [])
    layout = context.user_data.get("layout", "one_per_row")
    media_type = context.user_data.get("media_type", "text")

    text = post_text(number, title, preview, download)
    if media_type != "text" and len(text) > 1024:
        await send_or_edit(
            update,
            "❌ Photo/video captions are limited to 1024 characters. "
            "Shorten the title/links and try again.",
            edit,
        )
        return ConversationHandler.END

    if media_type == "text" and len(text) > 4096:
        await send_or_edit(update, "❌ Text message is too long.", edit)
        return ConversationHandler.END

    media_name = {
        "text": "Text only",
        "photo": "Default photo",
        "video": "Per-post video",
    }[media_type]

    # Use HTML only for the admin preview, not for the channel post.
    admin_preview = (
        "👀 <b>POST PREVIEW</b>\n\n"
        f"<pre>{html.escape(text)}</pre>\n\n"
        f"🎞️ Media: <b>{html.escape(media_name)}</b>\n"
        f"🔘 Buttons: <b>{len(buttons)}</b>\n"
        f"↔️ Layout: <b>{html.escape(layout_name(layout))}</b>\n\n"
        "Publish?"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data="confirm:publish"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm:cancel"),
    ]])

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            admin_preview, reply_markup=kb, parse_mode="HTML"
        )
    else:
        await update.effective_message.reply_text(
            admin_preview, reply_markup=kb, parse_mode="HTML"
        )
    return POST_CONFIRM


async def send_or_edit(update, text, edit):
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text)
            return
        except TelegramError:
            pass
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def confirm_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END

    action = q.data.split(":", 1)[1]
    if action == "cancel":
        await answer(q, "Cancelled.")
        clear(context)
        await q.edit_message_text("❌ Post cancelled. Number unchanged.")
        return ConversationHandler.END

    await answer(q, "Publishing…")

    try:
        number = await publish(context)
    except Exception as exc:
        log.exception("Publishing failed")
        clear(context)
        await q.edit_message_text(
            "❌ <b>Publishing failed</b>\n\n"
            f"{html.escape(publish_error(exc))}\n\n"
            "🔢 Post number was NOT incremented.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # The Telegram send succeeded. Now commit the next number.
    try:
        await change_data(lambda d: d.update(next_post_number=number + 1))
    except Exception as exc:
        log.exception("Post published but counter persistence failed")
        clear(context)
        await q.edit_message_text(
            "✅ Post published.\n\n"
            f"⚠️ Counter could not be saved: {html.escape(str(exc))}\n"
            f"Use /setnumber {number + 1} before the next post.",
        )
        return ConversationHandler.END

    clear(context)
    await q.edit_message_text(
        "✅ <b>Published successfully!</b>\n\n"
        f"🔢 Published: <b>#Post - {number}</b>\n"
        f"🔢 Next number: <b>{number + 1}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def publish(context: ContextTypes.DEFAULT_TYPE) -> int:
    d = await get_data()
    number = d["next_post_number"]

    text = post_text(
        number,
        context.user_data["title"],
        context.user_data["preview"],
        context.user_data["download"],
    )
    media_type = context.user_data.get("media_type", "text")
    media_id = context.user_data.get("media_id")
    buttons = context.user_data.get("buttons", [])
    layout = context.user_data.get("layout", "one_per_row")
    kb = keyboard_for(buttons, layout)

    bot = context.bot

    if media_type == "photo":
        if not media_id:
            raise ValueError("Default photo file_id is missing.")
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=media_id,
            caption=text,
            reply_markup=kb,
        )
    elif media_type == "video":
        if not media_id:
            raise ValueError("Video file_id is missing.")
        await bot.send_video(
            chat_id=CHANNEL_ID,
            video=media_id,
            caption=text,
            reply_markup=kb,
            supports_streaming=True,
        )
    else:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=kb,
            disable_web_page_preview=False,
        )

    return number


def publish_error(exc: Exception) -> str:
    if isinstance(exc, Forbidden):
        return (
            "Telegram denied the request. Make sure the bot is an administrator "
            "of the channel and can post messages."
        )
    if isinstance(exc, BadRequest):
        msg = str(exc)
        if "chat not found" in msg.lower():
            return "Channel not found. Check CHANNEL_ID."
        if "not enough rights" in msg.lower():
            return "The bot does not have permission to post in the channel."
        return f"Telegram rejected the request: {msg}"
    if isinstance(exc, NetworkError):
        return "Network error while contacting Telegram. Try again."
    if isinstance(exc, TelegramError):
        return f"Telegram error: {exc}"
    return str(exc) or "Unknown error."


# ----------------------------- photo management ------------------------------

async def photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    clear(context)
    await update.effective_message.reply_text(
        "🖼️ Send the new default photo now.\n"
        "Telegram's file_id will be stored; the image is not downloaded."
    )
    return PHOTO_WAIT


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    m = update.effective_message
    file_id = None

    if m.photo:
        file_id = m.photo[-1].file_id
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        file_id = m.document.file_id

    if not file_id:
        await m.reply_text("❌ Please send a photo.")
        return PHOTO_WAIT

    await change_data(lambda d: d.update(default_photo_file_id=file_id))
    clear(context)
    await m.reply_text("✅ Default photo updated.")
    return ConversationHandler.END


async def remove_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return
    await change_data(lambda d: d.update(default_photo_file_id=None))
    await update.effective_message.reply_text("🗑️ Default photo removed.")


# ----------------------------- number management -----------------------------

async def number_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    if context.args:
        return await set_number(update, context.args[0])

    clear(context)
    await update.effective_message.reply_text(
        "🔢 Send the next post number.\nExample: <code>100</code>",
        parse_mode="HTML",
    )
    return NUMBER_WAIT


async def set_number(update: Update, context_or_value):
    # Accept either ContextTypes context with args or a direct string.
    if isinstance(context_or_value, str):
        raw = context_or_value
    else:
        raw = " ".join(context_or_value.args).strip()

    try:
        n = int(raw)
    except ValueError:
        await update.effective_message.reply_text("❌ Number must be an integer.")
        return NUMBER_WAIT

    if n < 1:
        await update.effective_message.reply_text("❌ Number must be 1 or greater.")
        return NUMBER_WAIT

    await change_data(lambda d: d.update(next_post_number=n))
    await update.effective_message.reply_text(
        f"✅ Next post number is now {n}."
    )
    return ConversationHandler.END


async def number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    return await set_number(update, (update.effective_message.text or "").strip())


# ----------------------------- button management -----------------------------

async def show_button_menu(update, context, edit=False):
    d = await get_data()
    buttons = d["default_buttons"]

    if buttons:
        lines = ["🔘 <b>Default Buttons</b>", ""]
        for i, b in enumerate(buttons, 1):
            lines.append(
                f"{i}. {html.escape(COLOR_LABELS.get(b.get('color', 'default'), '⚪ Default'))} {html.escape(button_label(b))} → {html.escape(b['url'])}"
            )
        lines += [
            "",
            f"↔️ Layout: <b>{html.escape(layout_name(d['button_layout']))}</b>",
        ]
        text = "\n".join(lines)
    else:
        text = "🔘 <b>Default Buttons</b>\n\nNo default buttons configured."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Button", callback_data="btn:add")],
        [
            InlineKeyboardButton("↔️ Layout", callback_data="btn:layout"),
            InlineKeyboardButton("♻️ Reset", callback_data="btn:reset"),
        ],
        [InlineKeyboardButton("✅ Done", callback_data="btn:done")],
    ])

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML"
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=kb, parse_mode="HTML"
        )
    return BUTTON_MENU


async def buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    clear(context)
    return await show_button_menu(update, context)


async def button_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)

    action = q.data.split(":", 1)[1]

    if action == "add":
        await q.edit_message_text(
            "🎨 Choose button color/style:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚪ Default", callback_data="btncolor:default"), InlineKeyboardButton("🔴 Red", callback_data="btncolor:red")],
                [InlineKeyboardButton("🟢 Green", callback_data="btncolor:green"), InlineKeyboardButton("🔵 Blue", callback_data="btncolor:blue")],
                [InlineKeyboardButton("⬅️ Back", callback_data="btncolor:back")],
            ]),
        )
        return BUTTON_ADD_COLOR

    if action == "layout":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1️⃣ One per row", callback_data="btnlayout:one"),
                InlineKeyboardButton("2️⃣ Two per row", callback_data="btnlayout:two"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="btnlayout:back")],
        ])
        await q.edit_message_text("↔️ Choose default button layout:", reply_markup=kb)
        return BUTTON_LAYOUT

    if action == "reset":
        await change_data(lambda d: d.update(default_buttons=[]))
        await q.edit_message_text("♻️ Default buttons reset.")
        return ConversationHandler.END

    if action == "done":
        await q.edit_message_text(
            await dashboard_text(),
            reply_markup=dashboard_markup(),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    return BUTTON_MENU


async def button_add_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)
    action = q.data.split(":", 1)[1]
    if action == "back":
        return await show_button_menu(update, context, edit=True)
    context.user_data["button_color"] = action
    await q.edit_message_text("➕ Send button text.\nMaximum 60 characters.")
    return BUTTON_ADD_TEXT


async def button_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    d = await get_data()
    if len(d["default_buttons"]) >= MAX_BUTTONS:
        await update.effective_message.reply_text(
            f"❌ Maximum of {MAX_BUTTONS} buttons reached."
        )
        return await show_button_menu(update, context)

    text = " ".join((update.effective_message.text or "").strip().split())
    if not text:
        await update.effective_message.reply_text("❌ Button text cannot be empty.")
        return BUTTON_ADD_TEXT
    if len(text) > MAX_BUTTON_TEXT:
        await update.effective_message.reply_text(
            f"❌ Maximum {MAX_BUTTON_TEXT} characters."
        )
        return BUTTON_ADD_TEXT

    context.user_data["button_text"] = text
    await update.effective_message.reply_text("🔗 Send button URL.")
    return BUTTON_ADD_URL


async def button_add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END

    url = (update.effective_message.text or "").strip()
    if not valid_url(url):
        await update.effective_message.reply_text("❌ Invalid URL.")
        return BUTTON_ADD_URL

    text = context.user_data.pop("button_text", None)
    if not text:
        await update.effective_message.reply_text("❌ Button state expired.")
        return ConversationHandler.END

    def add(d):
        if len(d["default_buttons"]) < MAX_BUTTONS:
            d["default_buttons"].append({"text": text, "url": url, "color": context.user_data.get("button_color", "blue")})

    await change_data(add)
    context.user_data.pop("button_color", None)
    await update.effective_message.reply_text("✅ Default button added.")
    return await show_button_menu(update, context)


async def button_layout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not admin(update):
        if q:
            await answer(q, "Not authorized.", True)
        return ConversationHandler.END
    await answer(q)

    action = q.data.split(":", 1)[1]
    if action == "back":
        return await show_button_menu(update, context, edit=True)

    layout = "one_per_row" if action == "one" else "two_per_row"
    await change_data(lambda d: d.update(button_layout=layout))
    return await show_button_menu(update, context, edit=True)


async def reset_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return
    await change_data(lambda d: d.update(default_buttons=[]))
    await update.effective_message.reply_text("♻️ Default buttons reset.")


# ----------------------------- status ----------------------------------------

async def show_status(update, context, edit=False):
    d = await get_data()
    bot_id = None
    channel_status = "Unknown"

    try:
        me = await context.bot.get_me()
        bot_id = me.id
        member = await context.bot.get_chat_member(CHANNEL_ID, bot_id)
        if member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            channel_status = "✅ Bot is channel administrator"
        else:
            channel_status = f"❌ Bot status: {member.status}"
    except TelegramError as exc:
        channel_status = f"❌ Cannot verify: {exc}"

    text = (
        "⚙️ <b>Bot Status</b>\n\n"
        f"🔢 Next number: <b>{d['next_post_number']}</b>\n"
        f"🖼️ Photo: <b>{'Yes' if d['default_photo_file_id'] else 'No'}</b>\n"
        f"🔘 Buttons: <b>{len(d['default_buttons'])}</b>\n"
        f"↔️ Layout: <b>{html.escape(layout_name(d['button_layout']))}</b>\n"
        f"📣 Channel: <code>{html.escape(str(CHANNEL_ID))}</code>\n"
        f"🤖 {html.escape(channel_status)}"
    )

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=dashboard_markup()
        )
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return
    await show_status(update, context)


# ----------------------------- cancellation ----------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    clear(context)
    await update.effective_message.reply_text(
        "❌ Cancelled. Post number was not changed."
    )
    return ConversationHandler.END


# ----------------------------- conversations ---------------------------------

def post_conversation():
    return ConversationHandler(
        entry_points=[
            CommandHandler("post", post_start),
            CallbackQueryHandler(dashboard_callback, pattern=r"^dash:post$"),
        ],
        states={
            POST_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)
            ],
            POST_PREVIEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_preview)
            ],
            POST_DOWNLOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_download)
            ],
            POST_BUTTONS: [
                CallbackQueryHandler(
                    post_buttons_callback,
                    pattern=r"^postbtn:(default|custom|none|cancel)$",
                )
            ],
            POST_CUSTOM_BUTTON_COLOR: [
                CallbackQueryHandler(custom_button_color, pattern=r"^postcolor:(default|green|blue|red)$"),
            ],
            POST_CUSTOM_BUTTON_TEXT: [
                CommandHandler("done", custom_done),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, custom_button_text
                ),
            ],
            POST_CUSTOM_BUTTON_URL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, custom_button_url
                )
            ],
            POST_LAYOUT: [
                CallbackQueryHandler(
                    post_layout_callback,
                    pattern=r"^postlayout:(one|two|cancel)$",
                )
            ],
            POST_MEDIA: [
                CallbackQueryHandler(
                    media_callback,
                    pattern=r"^media:(photo|video|text|buttons|cancel)$",
                ),
                MessageHandler(filters.VIDEO, media_video),
            ],
            POST_CONFIRM: [
                CallbackQueryHandler(
                    confirm_post,
                    pattern=r"^confirm:(publish|cancel)$",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="post_flow",
    )


def photo_conversation():
    return ConversationHandler(
        entry_points=[
            CommandHandler("setphoto", photo_start),
            CallbackQueryHandler(dashboard_callback, pattern=r"^dash:photo$"),
        ],
        states={
            PHOTO_WAIT: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photo)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="photo_flow",
    )


def number_conversation():
    return ConversationHandler(
        entry_points=[
            CommandHandler("setnumber", number_start),
            CallbackQueryHandler(dashboard_callback, pattern=r"^dash:number$"),
        ],
        states={
            NUMBER_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, number_message)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="number_flow",
    )


def button_conversation():
    return ConversationHandler(
        entry_points=[
            CommandHandler("buttons", buttons_start),
            CallbackQueryHandler(dashboard_callback, pattern=r"^dash:buttons$"),
        ],
        states={
            BUTTON_MENU: [
                CallbackQueryHandler(
                    button_menu_callback,
                    pattern=r"^btn:(add|layout|reset|done)$",
                )
            ],
            BUTTON_ADD_COLOR: [
                CallbackQueryHandler(button_add_color, pattern=r"^btncolor:(default|green|blue|red|back)$"),
            ],
            BUTTON_ADD_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, button_add_text)
            ],
            BUTTON_ADD_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, button_add_url)
            ],
            BUTTON_LAYOUT: [
                CallbackQueryHandler(
                    button_layout_callback,
                    pattern=r"^btnlayout:(one|two|back)$",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="button_flow",
    )


# ----------------------------- application -----------------------------------

async def post_init(application: Application):
    async with data_lock:
        load_data()

    commands = [
        BotCommand("start", "Admin dashboard"),
        BotCommand("admin", "Admin dashboard"),
        BotCommand("post", "Create a channel post"),
        BotCommand("setphoto", "Set default photo"),
        BotCommand("removephoto", "Remove default photo"),
        BotCommand("buttons", "Manage default buttons"),
        BotCommand("resetbuttons", "Reset default buttons"),
        BotCommand("status", "Check bot/channel status"),
        BotCommand("setnumber", "Set next post number"),
        BotCommand("cancel", "Cancel current action"),
    ]
    await application.bot.set_my_commands(commands)

    try:
        me = await application.bot.get_me()
        member = await application.bot.get_chat_member(CHANNEL_ID, me.id)
        log.info("Channel membership: %s", member.status)
    except TelegramError as exc:
        log.warning("Channel check failed: %s", exc)

    log.info("Bot started. Channel=%r Admins=%s", CHANNEL_ID, sorted(ADMIN_IDS))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Unexpected error. Use /cancel and try again."
            )
        except TelegramError:
            pass


def build_app() -> Application:
    # PTB 22.x API. Do not create Updater manually.
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", start))
    app.add_handler(CommandHandler("removephoto", remove_photo))
    app.add_handler(CommandHandler("resetbuttons", reset_buttons))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(post_conversation())
    app.add_handler(photo_conversation())
    app.add_handler(number_conversation())
    app.add_handler(button_conversation())

    # Dashboard callbacks which are not conversation entry points.
    app.add_handler(
        CallbackQueryHandler(
            dashboard_callback,
            pattern=r"^dash:(remove_photo|status)$",
        )
    )

    app.add_handler(CommandHandler("cancel", cancel), group=10)
    app.add_error_handler(error_handler)
    return app


def main():
    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
