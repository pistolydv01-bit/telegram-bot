import os
import json
import re
import requests
from random import randint
import logging
import threading
import io
import zipfile
from pathlib import Path
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    ChatPermissions
)
from dotenv import load_dotenv, set_key
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# --- Config & Defaults ---
ENV_PATH = ".env"
URL = "https://leakosintapi.com/"

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6874667015"))               # Yahan apni Telegram Numeric ID dalein
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ooooooo929")       # Aapka Telegram Username
PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "sandeepkumar960148.rzp@rxairtel") # Default UPI ID
OFFICIAL_GROUP_LINK = "https://t.me/+pr4uQhCd3684OTE1"           # Aapka Telegram Group Link

# Dynamic Alert Config
ALERT_INTERVAL_HOURS = float(os.getenv("ALERT_INTERVAL_HOURS", "1.0")) # Default 1 Hour
DEFAULT_ALERT_MSG = (
    "🚀 <b>Looking for Quick & Accurate Details?</b>\n\n"
    "📱 <b>Number Detail</b> | 🔎 <b>Advance Info</b> | 💳 <b>Other Tools</b>\n\n"
    "⚠️ <i>Note: Group me spam mat karein. Direct Bot ko DM me start karke free search ya subscription use karein!</i>\n\n"
    "👇 <b>Niche Button Par Click Karke DM Me Aayein:</b>"
)
ALERT_MSG = os.getenv("ALERT_MSG", DEFAULT_ALERT_MSG)

LANG = "en"
LIMIT = 100
DATA_FILE = "users.json"

cash_reports = {}
admin_state = {}  # Admin state tracker
vcf_state = {}    # Per-user VCF workflow state
VCF_TMP_DIR = Path("vcf_tmp")
VCF_TMP_DIR.mkdir(exist_ok=True)
GROUP_CHAT_ID = -1003720589363
# Link Detection Regex
LINK_REGEX = re.compile(r'(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+|[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}/[^\s]*)', re.IGNORECASE)

# --- Helper to Update .env File ---
def update_env_variable(key, value):
    global API_TOKEN, PAYMENT_UPI_ID, ALERT_INTERVAL_HOURS, ALERT_MSG
    if key == "API_TOKEN":
        API_TOKEN = value
    elif key == "PAYMENT_UPI_ID":
        PAYMENT_UPI_ID = value
    elif key == "ALERT_INTERVAL_HOURS":
        ALERT_INTERVAL_HOURS = float(value)
    elif key == "ALERT_MSG":
        ALERT_MSG = value

    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w") as f:
            f.write("")

    set_key(ENV_PATH, key, str(value))

# --- JSON Database Helpers ---
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_user_credits(user_id, referrer_id=None):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {
            "credits": 1, 
            "used": 0, 
            "referred_by": referrer_id,
            "sub_expiry": None
        }
        save_data(data)
        
        # Referrer reward logic
        if referrer_id and str(referrer_id) in data and str(referrer_id) != uid:
            data[str(referrer_id)]["credits"] += 2
            save_data(data)
    
    if "sub_expiry" not in data[uid]:
        data[uid]["sub_expiry"] = None
        save_data(data)
        
    return data[uid]

def is_subscribed(user_id):
    data = load_data()
    uid = str(user_id)
    if uid in data and data[uid].get("sub_expiry"):
        expiry_str = data[uid]["sub_expiry"]
        if expiry_str == "LIFETIME":
            return True
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expiry_date:
                return True
        except Exception:
            pass
    return False

def add_subscription(user_id, days):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        get_user_credits(user_id)
        data = load_data()

    if days == -1:
        data[uid]["sub_expiry"] = "LIFETIME"
    else:
        new_expiry = datetime.now() + timedelta(days=days)
        data[uid]["sub_expiry"] = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    
    save_data(data)
    return data[uid]["sub_expiry"]

def deduct_credit(user_id):
    if is_subscribed(user_id):
        return True

    data = load_data()
    uid = str(user_id)
    if uid in data and data[uid]["credits"] > 0:
        data[uid]["credits"] -= 1
        data[uid]["used"] += 1
        save_data(data)
        return True
    return False

def add_credits_to_user(user_id, count):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        get_user_credits(user_id)
        data = load_data()
    data[uid]["credits"] += count
    save_data(data)
    return data[uid]["credits"]

# --- User Main Tools Reply Keyboard Setup ---
def get_main_user_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    
    btn1 = KeyboardButton("📱 Number Detail")
    btn2 = KeyboardButton("🔎 Advance Info")
    btn3 = KeyboardButton("💳 Adhar Info")
    btn4 = KeyboardButton("🪙 TG ID TO NUM")
    btn5 = KeyboardButton("Paytm UPI Detail")
    btn6 = KeyboardButton("🏦 IFSC Detail")
    btn7 = KeyboardButton("📱 Number to UPI")
    btn8 = KeyboardButton("📍 Pincode Detail")
    btn9 = KeyboardButton("📸 Insta Detail")
    btn10 = KeyboardButton("💳 Pan Card Details")
    btn_v1 = KeyboardButton("📁 टेक्स्ट से VCF")
    btn_v2 = KeyboardButton("📂 VCF से टेक्स्ट")
    btn_v3 = KeyboardButton("👑 Admin/Navy VCF")
    btn_v4 = KeyboardButton("✏️ VCF एडिटर")
    btn_v5 = KeyboardButton("🔄 फाइल जोड़ें (Merge)")
    btn_v6 = KeyboardButton("✂️ फाइल बांटें (Split)")
    btn_v7 = KeyboardButton("⚙️ नाम बदलें")
    btn_v8 = KeyboardButton("🔎 VCF जानकारी")
    btn12 = KeyboardButton("🎁 Daily Free Points")
    btn13 = KeyboardButton("🤝 Invite Friends")

    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn5, btn6)
    markup.add(btn7, btn8)
    markup.add(btn9, btn10)
    markup.add(btn_v1, btn_v2)
    markup.add(btn_v3, btn_v4)
    markup.add(btn_v5, btn_v6)
    markup.add(btn_v7, btn_v8)
    markup.add(btn12)
    markup.add(btn13)
    return markup

# --- Admin Reply Keyboard Setup ---
def get_admin_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_stats = KeyboardButton("📊 Bot Stats")
    btn_api = KeyboardButton("🔑 Change API Key")
    btn_upi = KeyboardButton("💳 Change UPI ID")
    btn_broadcast = KeyboardButton("📢 Broadcast")
    btn_addcredit = KeyboardButton("➕ Add Credits")
    btn_addsub = KeyboardButton("👑 Add Subscription")
    btn_payment = KeyboardButton("💳 Payment Settings")
    btn_time = KeyboardButton("⏰ Set Alert Time")
    btn_msg = KeyboardButton("📝 Change Alert Msg")
    btn_close = KeyboardButton("❌ Close Menu")
    
    markup.add(btn_stats, btn_broadcast)
    markup.add(btn_api, btn_upi)
    markup.add(btn_addcredit, btn_addsub)
    markup.add(btn_payment)
    markup.add(btn_time, btn_msg)
    markup.add(btn_close)
    return markup

# --- Helper for Feature Restriction / Token Message ---
def send_token_restriction_message(chat_id, tool_name):
    # User-facing tools are free. Keep this helper for compatibility with
    # the existing menu handlers and show a simple free-access message.
    bot.send_message(
        chat_id,
        f"✅ <b>{tool_name}</b>\n\n"
        "🆓 यह फीचर अभी Free है।\n"
        "आप इसे इस्तेमाल कर सकते हैं।",
        parse_mode="HTML"
    )

# --- Formatted Data Generator ---
def generate_report(query, query_id):
    data = {
        "token": API_TOKEN,
        "request": query.split("\n")[0],
        "limit": LIMIT,
        "lang": LANG
    }

    try:
        response = requests.post(URL, json=data, timeout=30).json()
    except Exception:
        return None

    if "Error code" in response:
        return None

    cash_reports[str(query_id)] = []

    if "List" in response:
        for database_name in response["List"].keys():
            if database_name == "No results found":
                continue

            text = [f"📊 <b>Database: {database_name}</b>\n"]
            data_list = response["List"][database_name].get("Data", [])

            if not data_list:
                info_leak = response["List"][database_name].get("InfoLeak", "")
                if info_leak:
                    text.append(f"ℹ️ {info_leak}\n")

            for report_data in data_list:
                text.append("👤 <b>PERSONAL INFORMATION</b>")
                if "FullName" in report_data:
                    text.append(f"• <b>Full Name:</b> {report_data['FullName']}")
                if "FatherName" in report_data:
                    text.append(f"• <b>Father Name:</b> {report_data['FatherName']}")
                if "DocNumber" in report_data:
                    text.append(f"• <b>Doc Number:</b> {report_data['DocNumber']}")
                text.append("")

                has_contact = False
                contact_text = ["📞 <b>CONTACT DETAILS</b>"]
                for key in ["Phone", "Phone2", "Phone3", "Phone4", "Phone5"]:
                    if key in report_data and report_data[key]:
                        contact_text.append(f"• <b>{key}:</b> {report_data[key]}")
                        has_contact = True
                if "Region" in report_data and report_data["Region"]:
                    contact_text.append(f"• <b>Region:</b> {report_data['Region']}")
                    has_contact = True
                if "Email" in report_data and report_data["Email"]:
                    contact_text.append(f"• <b>Email:</b> {report_data['Email']}")
                    has_contact = True

                if has_contact:
                    text.extend(contact_text)
                    text.append("")

                has_address = False
                address_text = ["🏠 <b>ADDRESS DETAILS</b>"]
                for key in ["Address", "Address2", "Address3", "Address4"]:
                    if key in report_data and report_data[key]:
                        address_text.append(f"• <b>Address:</b> {report_data[key]}")
                        has_address = True

                if has_address:
                    text.extend(address_text)
                    text.append("")

                known_keys = {
                    "FullName", "FatherName", "DocNumber", 
                    "Phone", "Phone2", "Phone3", "Phone4", "Phone5", 
                    "Region", "Email", "Address", "Address2", "Address3", "Address4"
                }
                extra_keys = [k for k in report_data.keys() if k not in known_keys]
                if extra_keys:
                    text.append("📋 <b>OTHER INFORMATION</b>")
                    for k in extra_keys:
                        text.append(f"• <b>{k}:</b> {report_data[k]}")
                    text.append("")

                text.append("────────────────────")

            text.append("💻 <b>Developer:</b> Sandip Yadav")
            text.append("👑 <b>Owner:</b> Sandip & Nitish\n")

            full_text = "\n".join(text)
            if len(full_text) > 3500:
                full_text = full_text[:3500] + "\n\n...[Data truncated]"

            cash_reports[str(query_id)].append(full_text)

    return cash_reports.get(str(query_id))

def create_inline_keyboard(query_id, page_id, count_page):
    markup = InlineKeyboardMarkup()
    if count_page <= 1:
        return markup

    markup.row_width = 3
    markup.add(
        InlineKeyboardButton(text="<<", callback_data=f"/page {query_id} {page_id - 1}"),
        InlineKeyboardButton(text=f"{page_id + 1}/{count_page}", callback_data="page_list"),
        InlineKeyboardButton(text=">>", callback_data=f"/page {query_id} {page_id + 1}")
    )
    return markup

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

bot = telebot.TeleBot(BOT_TOKEN)

# --- BACKGROUND SCHEDULER FOR GROUP ALERTS ---
scheduler = BackgroundScheduler(daemon=True)

def send_group_alert_job():
    data = load_data()
    bot_username = bot.get_me().username
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🤖 Start Bot In DM", url=f"https://t.me/{bot_username}?start=group_alert"))

    # Send alert to all active group/supergroup chats recorded in user/bot data or admin broadcast
    # Broadcast alert message
    for uid in data.keys():
        try:
            chat_id = int(uid)
            if chat_id < 0: # Indicates Group / Supergroup ID
                bot.send_message(chat_id, ALERT_MSG, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            pass

def reset_alert_scheduler(hours):
    global scheduler
    scheduler.remove_all_jobs()
    scheduler.add_job(send_group_alert_job, 'interval', hours=hours)

# Start scheduler with initial default or env hours
scheduler.add_job(send_group_alert_job, 'interval', hours=ALERT_INTERVAL_HOURS)
scheduler.start()

# --- AUTO APPROVE CHAT JOIN REQUESTS ---
@bot.chat_join_request_handler()
def auto_approve_join_request(chat_join_request):
    user_id = chat_join_request.from_user.id
    chat_id = chat_join_request.chat.id

    try:
        # Approve Request Automatically
        bot.approve_chat_join_request(chat_id, user_id)
        
        # Track Group in database for alerts
        get_user_credits(chat_id)

        # Welcome message in user's DM
        welcome_text = (
            f"🎉 <b>Welcome {chat_join_request.from_user.first_name}!</b>\n\n"
            f"Aapki group join request accept kar li gayi hai.\n"
            f"Bot ka upyog karne ke liye niche दिए गए buttons se kisi bhi tool ko select karein!"
        )
        bot.send_message(user_id, welcome_text, parse_mode="HTML", reply_markup=get_main_user_keyboard())
    except Exception as e:
        logging.error(f"Auto-approve error: {e}")

# --- Group Welcome Handler ---
@bot.message_handler(content_types=["new_chat_members"])
def welcome_new_member(message):
    for member in message.new_chat_members:
        if member.id == bot.get_me().id:
            get_user_credits(message.chat.id) # Track group ID for alerts
            continue

        first_name = member.first_name
        last_name = member.last_name if member.last_name else ""
        full_name = f"{first_name} {last_name}".strip()
        username = f"@{member.username}" if member.username else "No Username"
        user_id = member.id

        welcome_text = (
            f"🎉 <b>Welcome to the Group!</b>\n\n"
            f"👤 <b>Name:</b> {full_name}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🌐 <b>Username:</b> {username}\n"
            f"✨ <b>Mention:</b> <a href='tg://user?id={user_id}'>{first_name}</a>\n\n"
            f"Group me rules ko follow karein aur kisi bhi tarah ka spam ya link share na karein."
        )

        bot.send_message(
            message.chat.id, 
            welcome_text, 
            parse_mode="HTML"
        )

# --- Start Handler ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    if message.chat.type != "private":
        get_user_credits(message.chat.id) # Save group ID
        bot.reply_to(message, "Mujhe Private PM me start karein.")
        return

    args = message.text.split()
    referrer_id = args[1] if len(args) > 1 and args[1].isdigit() else None

    user_data = get_user_credits(message.from_user.id, referrer_id)
    
    if is_subscribed(message.from_user.id):
        sub_info = f"👑 <b>Subscription:</b> Active ({user_data['sub_expiry']})"
    else:
        sub_info = "🆓 <b>Mode:</b> Free Access"

    msg = (
        f"Namaste <b>{message.from_user.first_name}</b>!\n\n"
        f"{sub_info}\n\n"
        "Niche दिए गए Buttons से Tools सेलेक्ट करें या direct text message भेजकर सर्च करें।"
    )
    bot.reply_to(message, msg, parse_mode="HTML", reply_markup=get_main_user_keyboard())

# --- Admin Panel Command ---
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Aapko is command ka access nahi hai.")
        return

    text = (
        "<b>👑 Admin Control Panel</b>\n\n"
        f"📌 <b>Current UPI ID:</b> <code>{PAYMENT_UPI_ID}</code>\n"
        f"🔑 <b>Current API Key:</b> <code>{API_TOKEN[:8] + '...' if API_TOKEN else 'Not Set'}</code>\n"
        f"⏰ <b>Alert Interval:</b> <code>{ALERT_INTERVAL_HOURS} Hours</code>\n\n"
        "Niche दिए गए Buttons से डायरेक्ट एक्शन लें 👇"
    )
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=get_admin_reply_keyboard())

# --- Command Handlers ---
@bot.message_handler(commands=["broadcast"])
def broadcast_command(message):
    if message.from_user.id != ADMIN_ID:
        return

    text_to_send = message.text.replace("/broadcast", "").strip()
    if not text_to_send:
        bot.reply_to(message, "Usage: <code>/broadcast Aapka message...</code>", parse_mode="HTML")
        return

    data = load_data()
    success, failed = 0, 0
    bot.reply_to(message, "📢 Broadcasting message...")

    for uid in data.keys():
        try:
            bot.send_message(int(uid), text_to_send, parse_mode="HTML")
            success += 1
        except Exception:
            failed += 1

    bot.send_message(message.chat.id, f"✅ <b>Broadcast Completed!</b>\n\n• Success: {success}\n• Failed: {failed}", parse_mode="HTML")

@bot.message_handler(commands=["addsub"])
def add_sub_command(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        args = message.text.split()
        target_user = args[1]
        days = int(args[2])

        expiry = add_subscription(target_user, days)
        bot.reply_to(message, f"✅ User <code>{target_user}</code> ko Membership de di gayi hai!\nExpiry: <b>{expiry}</b>", parse_mode="HTML")

        try:
            bot.send_message(
                int(target_user),
                f"🎉 Congratulation! Admin ne aapki Membership Activate kar di hai.\nExpiry: <b>{expiry}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        bot.reply_to(message, "Usage: <code>/addsub <user_id> <days></code> (-1 for Lifetime)", parse_mode="HTML")

@bot.message_handler(commands=["payment"])
def payment_settings_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(
        message,
        "💳 <b>Payment Settings</b>\n\n"
        f"UPI ID: <code>{PAYMENT_UPI_ID}</code>\n"
        "User tools: 🆓 Free\n\n"
        "Payment processing can be added later without changing the free user flow.",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["stats"])
def admin_stats(message):
    if message.from_user.id != ADMIN_ID:
        return

    data = load_data()
    total_users = len(data)
    total_searches = sum(u.get("used", 0) for u in data.values())

    msg = (
        f"<b>📊 Bot Statistics</b>\n\n"
        f"Total Users/Chats: {total_users}\n"
        f"Total Searches Executed: {total_searches}"
    )
    bot.reply_to(message, msg, parse_mode="HTML")

@bot.message_handler(commands=["addcredit"])
def add_credit_command(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        args = message.text.split()
        target_user = args[1]
        amount = int(args[2])

        new_total = add_credits_to_user(target_user, amount)
        bot.reply_to(message, f"Success! User <code>{target_user}</code> ke pass ab {new_total} credits hain.", parse_mode="HTML")

        try:
            bot.send_message(
                int(target_user),
                f"🎉 Admin ne aapko {amount} extra search credits add kar diye hain!"
            )
        except Exception:
            pass
    except Exception:
        bot.reply_to(message, "Usage: <code>/addcredit <user_id> <amount></code>", parse_mode="HTML")


# ==================== VCF TOOLS ====================
def _safe_filename(name, default="contacts"):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    name = name.strip("._")
    return name or default

def _parse_vcards(content):
    """Return individual VCARD blocks from text."""
    return re.findall(r"BEGIN:VCARD.*?END:VCARD", content, flags=re.I | re.S)

def _extract_vcf_contacts(content):
    contacts = []
    for block in _parse_vcards(content):
        name = ""
        phone = ""
        for raw in block.splitlines():
            line = raw.strip()
            upper = line.upper()
            if upper.startswith("FN:"):
                name = line.split(":", 1)[1].strip()
            elif upper.startswith("N:") and not name:
                parts = line.split(":", 1)[1].split(";")
                name = " ".join([p for p in parts if p]).strip()
            elif upper.startswith("TEL"):
                phone = line.split(":", 1)[-1].strip()
                break
        contacts.append((name or "Contact", phone))
    return contacts

def _numbers_from_text(raw):
    nums = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # CSV-friendly: use the first non-empty field.
        if "," in line:
            line = next((x.strip() for x in line.split(",") if x.strip()), "")
        # Ignore obvious header rows.
        if line.lower() in {"phone", "phone number", "mobile", "number", "name"}:
            continue
        nums.append(line)
    return nums

def _build_vcf(numbers, name_prefix="Contact"):
    out = []
    for idx, number in enumerate(numbers, 1):
        out.extend([
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"N:;{name_prefix}_{idx};;;",
            f"FN:{name_prefix}_{idx}",
            f"TEL;TYPE=CELL:{number}",
            "END:VCARD",
            ""
        ])
    return "\n".join(out)

def _send_bytes_as_document(chat_id, filename, content, caption):
    bio = io.BytesIO(content if isinstance(content, bytes) else content.encode("utf-8"))
    bio.name = filename
    bot.send_document(chat_id, bio, caption=caption)

def _clear_vcf_state(user_id):
    state = vcf_state.pop(user_id, None)
    if state:
        for p in state.get("files", []):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

def _handle_vcf_document(message):
    user_id = message.from_user.id
    state = vcf_state.get(user_id)
    if not state:
        return False

    doc = message.document
    if not doc:
        return False

    file_info = bot.get_file(doc.file_id)
    raw = bot.download_file(file_info.file_path)
    mode = state.get("mode")

    if mode in {"text_to_vcf", "admin_navy_vcf"}:
        text_data = raw.decode("utf-8", errors="ignore")
        numbers = _numbers_from_text(text_data)
        if not numbers:
            bot.reply_to(message, "❌ फाइल में कोई नंबर नहीं मिला।")
            _clear_vcf_state(user_id)
            return True
        prefix = "NavyContact" if mode == "admin_navy_vcf" else "Contact"
        content = _build_vcf(numbers, prefix)
        filename = "navy_contacts.vcf" if mode == "admin_navy_vcf" else "contacts.vcf"
        _send_bytes_as_document(
            message.chat.id, filename, content,
            f"✅ VCF तैयार है!\nकुल Contacts: {len(numbers)}"
        )
        _clear_vcf_state(user_id)
        return True

    if mode == "vcf_to_text":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ कृपया `.vcf` फाइल भेजें।")
            return True
        contacts = _extract_vcf_contacts(raw.decode("utf-8", errors="ignore"))
        lines = [f"{name} - {phone}" if phone else name for name, phone in contacts]
        _send_bytes_as_document(
            message.chat.id, "contacts.txt", "\n".join(lines),
            f"✅ VCF से Text तैयार!\nकुल Contacts: {len(contacts)}"
        )
        _clear_vcf_state(user_id)
        return True

    if mode == "vcf_info":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ कृपया `.vcf` फाइल भेजें।")
            return True
        content = raw.decode("utf-8", errors="ignore")
        contacts = _parse_vcards(content)
        unique_phones = {p for _, p in _extract_vcf_contacts(content) if p}
        bot.reply_to(
            message,
            f"📄 <b>VCF जानकारी</b>\n\n"
            f"नाम: <code>{doc.file_name}</code>\n"
            f"Contacts: <b>{len(contacts)}</b>\n"
            f"Phone Numbers: <b>{len(unique_phones)}</b>\n"
            f"File Size: <b>{len(raw)/1024:.1f} KB</b>",
            parse_mode="HTML"
        )
        _clear_vcf_state(user_id)
        return True

    if mode == "rename":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ कृपया `.vcf` फाइल भेजें।")
            return True
        state["rename_bytes"] = raw
        state["rename_ext"] = ".vcf"
        bot.reply_to(message, "✏️ अब नया नाम भेजें (extension लिखने की जरूरत नहीं)।")
        state["mode"] = "rename_wait_name"
        return True

    if mode == "split":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ कृपया `.vcf` फाइल भेजें।")
            return True
        state["content"] = raw.decode("utf-8", errors="ignore")
        bot.reply_to(message, "✂️ हर output file में कितने contacts चाहिए? उदाहरण: <code>100</code>", parse_mode="HTML")
        state["mode"] = "split_wait_count"
        return True

    if mode == "merge":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ केवल `.vcf` फाइल भेजें।")
            return True
        path = VCF_TMP_DIR / f"{user_id}_{len(state.get('files', []))}.vcf"
        path.write_bytes(raw)
        state.setdefault("files", []).append(str(path))
        bot.reply_to(
            message,
            f"📥 फाइल {len(state['files'])} मिल गई।\n"
            "और VCF भेजें या <code>/done</code> लिखें।",
            parse_mode="HTML"
        )
        return True

    if mode == "editor":
        if not (doc.file_name or "").lower().endswith(".vcf"):
            bot.reply_to(message, "❌ कृपया `.vcf` फाइल भेजें।")
            return True
        state["content"] = raw.decode("utf-8", errors="ignore")
        bot.reply_to(
            message,
            "✏️ Editor options:\n"
            "• <code>/prefix NAME</code> — सभी contact names के आगे prefix\n"
            "• <code>/done</code> — बिना बदलाव के file वापस भेजें",
            parse_mode="HTML"
        )
        return True

    return False

def _start_vcf_mode(message, mode, prompt):
    vcf_state[message.from_user.id] = {"mode": mode, "files": []}
    bot.reply_to(message, prompt, parse_mode="HTML")


# --- Main Message Handler ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text if message.text else ""

    # VCF workflows are intentionally free for all users.
    if message.content_type == "document" and _handle_vcf_document(message):
        return

    # Save Group Chat ID automatically when messages are sent in group
    if message.chat.type in ["group", "supergroup"]:
        get_user_credits(chat_id)

    # --- ANTI-LINK & AUTO-MUTE IN GROUPS ---
    if message.chat.type in ["group", "supergroup"]:
        chat_member = bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ["creator", "administrator"] and user_id != ADMIN_ID:
            if text and LINK_REGEX.search(text):
                try:
                    bot.delete_message(chat_id, message.message_id)
                    bot.restrict_chat_member(
                        chat_id, 
                        user_id, 
                        permissions=ChatPermissions(can_send_messages=False)
                    )
                    bot.send_message(
                        chat_id,
                        f"🚫 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> ko Group me link bhejne ki वजह से **MUTE** कर दिया गया है!",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logging.error(f"Failed to mute user: {e}")
                return

    # --- ADMIN REPLY KEYBOARD LOGIC ---
    if user_id == ADMIN_ID:
        if text == "📊 Bot Stats":
            admin_stats(message)
            return
        elif text == "🔑 Change API Key":
            admin_state[user_id] = "WAITING_API"
            bot.reply_to(message, "📝 Kripya apni **Nayi API Key** reply me bhejein:")
            return
        elif text == "💳 Change UPI ID":
            admin_state[user_id] = "WAITING_UPI"
            bot.reply_to(message, "📝 Kripya apni **Nayi UPI ID** (e.g. `9871742130@paytm`) reply me bhejein:")
            return
        elif text == "📢 Broadcast":
            admin_state[user_id] = "WAITING_BROADCAST"
            bot.reply_to(message, "📢 Jo message sabhi users ko bhejna hai, wo type karke bhejein:")
            return
        elif text == "➕ Add Credits":
            admin_state[user_id] = "WAITING_ADDCREDIT"
            bot.reply_to(message, "📝 Kripya is format me bhejein:\n<code>USER_ID AMOUNT</code>\n\nExample: <code>123456789 10</code>", parse_mode="HTML")
            return
        elif text == "👑 Add Subscription":
            admin_state[user_id] = "WAITING_ADDSUB"
            bot.reply_to(message, "📝 Kripya is format me bhejein:\n<code>USER_ID DAYS</code> (-1 for Lifetime)\n\nExample: <code>123456789 30</code>", parse_mode="HTML")
            return
        elif text == "💳 Payment Settings":
            admin_state[user_id] = "WAITING_PAYMENT_UPI"
            bot.reply_to(
                message,
                "💳 <b>Payment Settings</b>\n\n"
                f"Current UPI: <code>{PAYMENT_UPI_ID}</code>\n\n"
                "नई UPI ID भेजें। इससे payment system बाद में enable/configure किया जा सकता है।",
                parse_mode="HTML"
            )
            return
        elif text == "⏰ Set Alert Time":
            admin_state[user_id] = "WAITING_ALERT_TIME"
            bot.reply_to(message, "📝 Alert Interval **Hours** me likhein (e.g. `1` for 1 hour, `2` for 2 hours, `0.5` for 30 min):")
            return
        elif text == "📝 Change Alert Msg":
            admin_state[user_id] = "WAITING_ALERT_MSG"
            bot.reply_to(message, f"📝 Naya Group Alert Message HTML format me bhejein.\n\nCurrent Message:\n\n{ALERT_MSG}", parse_mode="HTML")
            return
        elif text == "❌ Close Menu":
            bot.reply_to(message, "Admin Menu Closed.", reply_markup=get_main_user_keyboard())
            return

        if user_id in admin_state:
            state = admin_state.pop(user_id)
            if state == "WAITING_API":
                new_api = text.strip()
                update_env_variable("API_TOKEN", new_api)
                bot.reply_to(message, f"✅ <b>API Key updated & saved to .env!</b>\n\n<code>{new_api}</code>", parse_mode="HTML")
                return
            elif state == "WAITING_UPI":
                new_upi = text.strip()
                update_env_variable("PAYMENT_UPI_ID", new_upi)
                bot.reply_to(message, f"✅ <b>UPI ID updated & saved to .env!</b>\n\n<code>{new_upi}</code>", parse_mode="HTML")
                return
            elif state == "WAITING_ALERT_TIME":
                try:
                    new_hours = float(text.strip())
                    update_env_variable("ALERT_INTERVAL_HOURS", new_hours)
                    reset_alert_scheduler(new_hours)
                    bot.reply_to(message, f"✅ <b>Alert Interval successfully updated to {new_hours} Hours!</b>", parse_mode="HTML")
                except Exception:
                    bot.reply_to(message, "❌ Invalid input. Kripya number (e.g. 1 ya 2) bhejein.")
                return
            elif state == "WAITING_ALERT_MSG":
                new_msg = text.strip()
                update_env_variable("ALERT_MSG", new_msg)
                bot.reply_to(message, f"✅ <b>Group Alert Message Updated!</b>\n\nNaya Alert Text:\n{new_msg}", parse_mode="HTML")
                return
            elif state == "WAITING_BROADCAST":
                text_to_send = text.strip()
                data = load_data()
                success, failed = 0, 0
                bot.reply_to(message, "📢 Broadcasting message...")

                for uid in data.keys():
                    try:
                        bot.send_message(int(uid), text_to_send, parse_mode="HTML")
                        success += 1
                    except Exception:
                        failed += 1

                bot.send_message(chat_id, f"✅ <b>Broadcast Completed!</b>\n\n• Success: {success}\n• Failed: {failed}", parse_mode="HTML")
                return
            elif state == "WAITING_ADDCREDIT":
                try:
                    args = text.split()
                    target_user, amount = args[0], int(args[1])
                    new_total = add_credits_to_user(target_user, amount)
                    bot.reply_to(message, f"Success! User <code>{target_user}</code> ke pass ab {new_total} credits hain.", parse_mode="HTML")
                    try:
                        bot.send_message(int(target_user), f"🎉 Admin ne aapko {amount} extra search credits add kar diye hain!")
                    except Exception:
                        pass
                except Exception:
                    bot.reply_to(message, "Invalid Format. Try again with <code>USER_ID AMOUNT</code>", parse_mode="HTML")
                return
            elif state == "WAITING_ADDSUB":
                try:
                    args = text.split()
                    target_user, days = args[0], int(args[1])
                    expiry = add_subscription(target_user, days)
                    bot.reply_to(message, f"✅ User <code>{target_user}</code> ko Membership de di gayi hai!\nExpiry: <b>{expiry}</b>", parse_mode="HTML")
                    try:
                        bot.send_message(int(target_user), f"🎉 Congratulation! Admin ne aapki Membership Activate kar di hai.\nExpiry: <b>{expiry}</b>", parse_mode="HTML")
                    except Exception:
                        pass
                except Exception:
                    bot.reply_to(message, "Invalid Format. Try again with <code>USER_ID DAYS</code>", parse_mode="HTML")
                return
            elif state == "WAITING_PAYMENT_UPI":
                new_upi = text.strip()
                if not new_upi:
                    bot.reply_to(message, "❌ UPI ID खाली नहीं हो सकती।")
                    return
                update_env_variable("PAYMENT_UPI_ID", new_upi)
                bot.reply_to(
                    message,
                    f"✅ <b>Payment UPI updated.</b>\n\n"
                    f"New UPI: <code>{new_upi}</code>\n\n"
                    "ℹ️ User side अभी भी Free रहेगा; यह setting सिर्फ future payment activation के लिए रखी गई है.",
                    parse_mode="HTML"
                )
                return

    # --- VCF BUTTON HANDLERS ---
    if text == "📁 टेक्स्ट से VCF":
        _start_vcf_mode(
            message, "text_to_vcf",
            "📁 <b>Text to VCF</b>\n"
            "नंबरों की `.txt`/`.csv` फाइल भेजें या नीचे हर लाइन में एक नंबर भेजें।"
        )
        return

    if text == "📂 VCF से टेक्स्ट":
        _start_vcf_mode(
            message, "vcf_to_text",
            "📂 <b>VCF से Text</b>\nकृपया `.vcf` फाइल भेजें।"
        )
        return

    if text == "👑 Admin/Navy VCF":
        _start_vcf_mode(
            message, "admin_navy_vcf",
            "👑 <b>Admin/Navy VCF</b>\n"
            "नंबरों की `.txt`/`.csv` फाइल भेजें। यह अलग नाम-prefix के साथ VCF बनाएगा।"
        )
        return

    if text == "✏️ VCF एडिटर":
        _start_vcf_mode(
            message, "editor",
            "✏️ <b>VCF Editor</b>\nकृपया `.vcf` फाइल भेजें।"
        )
        return

    if text == "🔄 फाइल जोड़ें (Merge)":
        _start_vcf_mode(
            message, "merge",
            "🔄 <b>VCF Merge</b>\nपहली `.vcf` फाइल भेजें। उसके बाद बाकी VCF भेजते रहें और अंत में <code>/done</code> लिखें।"
        )
        return

    if text == "✂️ फाइल बांटें (Split)":
        _start_vcf_mode(
            message, "split",
            "✂️ <b>VCF Split</b>\nपहले `.vcf` फाइल भेजें। फिर हर output में contacts की संख्या बताएं।"
        )
        return

    if text == "⚙️ नाम बदलें":
        _start_vcf_mode(
            message, "rename",
            "⚙️ <b>VCF Rename</b>\nकृपया `.vcf` फाइल भेजें।"
        )
        return

    if text == "🔎 VCF जानकारी":
        _start_vcf_mode(
            message, "vcf_info",
            "🔎 <b>VCF जानकारी</b>\nकृपया `.vcf` फाइल भेजें।"
        )
        return

    # --- USER TOOLS BUTTON HANDLERS ---
    if text == "🔎 Advance Info":
        bot.reply_to(message, "🔍 Send details (Phone Number, Email, etc.) to perform an Advance Search:")
        return

    elif text in [
        "📱 Number Detail", "💳 Adhar Info", "🪙 TG ID TO NUM", 
        "Paytm UPI Detail", "🏦 IFSC Detail", "📱 Number to UPI", 
        "📍 Pincode Detail", "📸 Insta Detail", "💳 Pan Card Details", 
        "🎁 Daily Free Points"
    ]:
        send_token_restriction_message(chat_id, text)
        return

    elif text == "🤝 Invite Friends":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        
        inv_text = (
            "🤝 <b>Invite Friends</b>\n\n"
            "Apne dosto ko share karein aur free search credits paayein!\n\n"
            "Har dost ke join karne par aapko <b>2 Free Search Credits</b> milenge.\n\n"
            f"🔗 <b>Aapka Referral Link:</b>\n<code>{ref_link}</code>"
        )
        bot.send_message(chat_id, inv_text, parse_mode="HTML")
        return

    # --- VCF TEXT WORKFLOWS ---
    state = vcf_state.get(user_id)
    if state:
        mode = state.get("mode")

        if mode == "text_to_vcf":
            numbers = _numbers_from_text(text)
            if not numbers:
                bot.reply_to(message, "❌ कोई नंबर नहीं मिला। हर लाइन में एक नंबर भेजें।")
                return
            content = _build_vcf(numbers)
            _send_bytes_as_document(chat_id, "contacts.vcf", content, f"✅ VCF तैयार!\nकुल Contacts: {len(numbers)}")
            _clear_vcf_state(user_id)
            return

        if mode == "admin_navy_vcf":
            numbers = _numbers_from_text(text)
            if not numbers:
                bot.reply_to(message, "❌ कोई नंबर नहीं मिला।")
                return
            content = _build_vcf(numbers, "NavyContact")
            _send_bytes_as_document(chat_id, "navy_contacts.vcf", content, f"✅ Navy VCF तैयार!\nकुल Contacts: {len(numbers)}")
            _clear_vcf_state(user_id)
            return

        if mode == "rename_wait_name":
            new_name = _safe_filename(text, "renamed_contacts")
            raw = state.get("rename_bytes", b"")
            _send_bytes_as_document(chat_id, new_name + ".vcf", raw, f"✅ नाम बदल दिया: {new_name}.vcf")
            _clear_vcf_state(user_id)
            return

        if mode == "split_wait_count":
            try:
                per_file = int(text.strip())
                if per_file <= 0 or per_file > 100000:
                    raise ValueError
            except ValueError:
                bot.reply_to(message, "❌ कृपया 1 से 100000 के बीच संख्या भेजें।")
                return

            blocks = _parse_vcards(state.get("content", ""))
            if not blocks:
                bot.reply_to(message, "❌ VCF में कोई contact नहीं मिला।")
                _clear_vcf_state(user_id)
                return

            total_parts = (len(blocks) + per_file - 1) // per_file
            for idx in range(total_parts):
                chunk = blocks[idx * per_file:(idx + 1) * per_file]
                content = "\n".join(chunk) + "\n"
                _send_bytes_as_document(
                    chat_id, f"split_{idx+1}.vcf", content,
                    f"✂️ Part {idx+1}/{total_parts} • Contacts: {len(chunk)}"
                )
            _clear_vcf_state(user_id)
            return

        if mode == "editor":
            if text == "/done":
                content = state.get("content", "")
                _send_bytes_as_document(chat_id, "edited_contacts.vcf", content, "✅ VCF वापस भेज दिया गया।")
                _clear_vcf_state(user_id)
                return
            if text.startswith("/prefix "):
                prefix = text.split(" ", 1)[1].strip()
                if not prefix:
                    bot.reply_to(message, "❌ Prefix खाली है।")
                    return
                blocks = _parse_vcards(state.get("content", ""))
                edited = []
                for idx, block in enumerate(blocks, 1):
                    lines = []
                    changed = False
                    for line in block.splitlines():
                        if line.upper().startswith("FN:"):
                            line = "FN:" + prefix + " " + line.split(":", 1)[1].strip()
                            changed = True
                        elif line.upper().startswith("N:"):
                            parts = line.split(":", 1)
                            if len(parts) == 2:
                                fields = parts[1].split(";")
                                if len(fields) > 1:
                                    fields[1] = prefix + " " + fields[1]
                                    line = "N:" + ";".join(fields)
                                    changed = True
                        lines.append(line)
                    edited.append("\n".join(lines))
                state["content"] = "\n".join(edited) + ("\n" if edited else "")
                bot.reply_to(message, "✅ Prefix apply हो गया। अब <code>/done</code> लिखें।", parse_mode="HTML")
                return

        if mode == "merge" and text == "/done":
            files = state.get("files", [])
            if len(files) < 2:
                bot.reply_to(message, "❌ Merge के लिए कम से कम 2 VCF फाइलें भेजें।")
                return
            merged_blocks = []
            seen = set()
            for path in files:
                try:
                    content = Path(path).read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for block in _parse_vcards(content):
                    key = block.strip()
                    if key not in seen:
                        seen.add(key)
                        merged_blocks.append(block)
            content = "\n".join(merged_blocks) + ("\n" if merged_blocks else "")
            _send_bytes_as_document(
                chat_id, "merged_contacts.vcf", content,
                f"✅ Merge complete!\nUnique Contacts: {len(merged_blocks)}"
            )
            _clear_vcf_state(user_id)
            return

    # --- VCF CANCEL ---
    if text == "/cancel" and user_id in vcf_state:
        _clear_vcf_state(user_id)
        bot.reply_to(message, "❌ VCF operation cancelled.")
        return

    # --- SEARCH PROCESSOR ---
    if message.content_type == "text":
        if text.startswith("/"):
            return

        user_data = get_user_credits(user_id)

        # Free mode: searches are available without subscription/credit checks.
        query_id = randint(0, 9999999)
        bot.reply_to(message, "Searching...")

        report = generate_report(text, query_id)

        if not report:
            bot.send_message(chat_id, "Result nahi mila.", reply_to_message_id=message.message_id)
            return

        # Free mode: do not deduct user credits.
        markup = create_inline_keyboard(query_id, 0, len(report))
        try:
            bot.send_message(
                chat_id,
                report[0],
                parse_mode="HTML",
                reply_markup=markup,
                reply_to_message_id=message.message_id
            )
        except Exception:
            bot.send_message(
                chat_id,
                report[0],
                reply_markup=markup,
                reply_to_message_id=message.message_id
            )

# --- Callbacks ---
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call: CallbackQuery):
    if call.data == "invite_friends":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
        
        inv_text = (
            "🤝 <b>Invite Friends</b>\n\n"
            "Apne dosto ko share karein aur free search credits paayein!\n\n"
            "Har dost ke join karne par aapko <b>2 Free Search Credits</b> milenge.\n\n"
            f"🔗 <b>Aapka Referral Link:</b>\n<code>{ref_link}</code>"
        )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, inv_text, parse_mode="HTML")

    elif call.data.startswith("/page "):
        _, query_id, page_id = call.data.split(" ")
        page_id = int(page_id)

        if query_id not in cash_reports:
            bot.answer_callback_query(call.id, "Expired.")
            return

        report = cash_reports[query_id]
        total_pages = len(report)
        page_id = page_id % total_pages

        markup = create_inline_keyboard(query_id, page_id, total_pages)
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=report[page_id],
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception:
            pass

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthCheckHandler
    )

    logging.info(
        f"🌐 Health Check Server Running on Port {port}"
    )

    server.serve_forever()

logging.basicConfig(level=logging.INFO)

threading.Thread(
    target=run_dummy_server,
    daemon=True
).start()

bot.polling(none_stop=True, allowed_updates=["message", "callback_query", "chat_join_request"])
