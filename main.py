import os
import json
import re
import requests
from random import randint
import logging
import threading
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
OFFICIAL_GROUP_LINK = os.getenv("OFFICIAL_GROUP_LINK", "https://t.me/+pr4uQhCd3684OTE1")
OFFICIAL_GROUP_ID = int(os.getenv("OFFICIAL_GROUP_ID", "-1003720589363"))
PAID_MODE_DEFAULT = os.getenv("PAID_MODE", "off").lower() in ("1", "true", "on", "yes")

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
GROUP_CHAT_ID = OFFICIAL_GROUP_ID
vcf_state = {}  # Per-user VCF workflow state
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

def ensure_first_start_token(user_id):
    """Give exactly 1 free token on the user's first /start only."""
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {
            "credits": 1,
            "sub_expiry": None,
            "referrer": None,
            "referred_rewarded": False,
            "first_start": True,
        }
        save_data(data)
        return 1
    return data[uid].get("credits", 0)

def get_user_credits(user_id, referrer_id=None):
    data = load_data()
    uid = str(user_id)
    created = False
    if uid not in data:
        data[uid] = {
            "credits": 0,
            "used": 0,
            "referred_by": referrer_id,
            "sub_expiry": None,
            "referral_rewarded": False,
            "group_token_claimed": False
        }
        created = True
    else:
        data[uid].setdefault("credits", 0)
        data[uid].setdefault("used", 0)
        data[uid].setdefault("referred_by", referrer_id)
        data[uid].setdefault("sub_expiry", None)
        data[uid].setdefault("referral_rewarded", False)
        data[uid].setdefault("group_token_claimed", False)

    # In FREE mode, a successful referral gives the inviter 2 tokens once.
    if (not paid_mode_enabled()) and created and referrer_id and str(referrer_id) in data and str(referrer_id) != uid:
        ref_uid = str(referrer_id)
        if not data[ref_uid].get("referral_rewarded", False):
            data[ref_uid]["credits"] = data[ref_uid].get("credits", 0) + 2
            data[uid]["referred_by"] = ref_uid
            # This flag is kept on the inviter as a simple compatibility marker;
            # multiple different invitees are still rewarded because each new
            # invitee has a different uid.
            data[uid]["referral_rewarded"] = True

    save_data(data)
    return data[uid]

def paid_mode_enabled():
    data = load_data()
    return bool(data.get("_settings", {}).get("paid_mode", PAID_MODE_DEFAULT))

def set_paid_mode(enabled):
    data = load_data()
    data.setdefault("_settings", {})
    data["_settings"]["paid_mode"] = bool(enabled)
    save_data(data)

def require_paid_access(user_id):
    if user_id == ADMIN_ID or not paid_mode_enabled():
        return True
    return is_subscribed(user_id)

def send_paid_gate(chat_id, tool_name):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("💳 Buy Subscription", callback_data="buy_sub"))
    markup.add(InlineKeyboardButton("🤝 Invite Friends", callback_data="invite_friends"))
    markup.add(InlineKeyboardButton("📢 Join Group + Get 1 Token", url=OFFICIAL_GROUP_LINK))
    bot.send_message(
        chat_id,
        f"🔒 <b>{tool_name}</b> paid mode में है।\\n\\n"
        "आपके पास active subscription नहीं है।\\n"
        "Subscription खरीदें या Free mode में Admin से Paid System OFF करवाएँ।",
        parse_mode="HTML",
        reply_markup=markup
    )

def award_group_token(user_id):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        get_user_credits(user_id)
        data = load_data()
    if data[uid].get("group_token_claimed"):
        return False, data[uid].get("credits", 0)
    data[uid]["credits"] = data[uid].get("credits", 0) + 1
    data[uid]["group_token_claimed"] = True
    save_data(data)
    return True, data[uid]["credits"]

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
    buttons = [
        "📱 Number Detail", "🔎 Advance Info",
        "💳 Adhar Info", "🪙 TG ID TO NUM",
        "Paytm UPI Detail", "🏦 IFSC Detail",
        "📱 Number to UPI", "📍 Pincode Detail",
        "📸 Insta Detail", "💳 Pan Card Details",
        "💳 Buy Subscription", "🎁 Daily Free Points",
        "🤝 Invite Friends", "🎁 Group Join Token",
        "📁 टेक्स्ट से VCF", "📂 VCF से टेक्स्ट",
        "👑 Admin/Navy VCF", "✏️ VCF एडिटर",
        "🔄 फाइल जोड़ें (Merge)", "✂️ फाइल बांटें (Split)",
        "⚙️ नाम बदलें", "🔎 VCF जानकारी"
    ]
    for i in range(0, len(buttons), 2):
        markup.add(*(KeyboardButton(x) for x in buttons[i:i+2]))
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
    btn_time = KeyboardButton("⏰ Set Alert Time")
    btn_msg = KeyboardButton("📝 Change Alert Msg")
    btn_paid = KeyboardButton("💰 Paid System ON/OFF")
    btn_close = KeyboardButton("❌ Close Menu")
    
    markup.add(btn_stats, btn_broadcast)
    markup.add(btn_api, btn_upi)
    markup.add(btn_addcredit, btn_addsub)
    markup.add(btn_time, btn_msg)
    markup.add(btn_paid)
    markup.add(btn_close)
    return markup

# --- Helper for Feature Restriction / Token Message ---
def send_token_restriction_message(chat_id, tool_name):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(text="📢 Join Group for 1 Demo Token", url=OFFICIAL_GROUP_LINK),
        InlineKeyboardButton(text="💳 Buy Subscription", callback_data="buy_sub")
    )

    msg = (
        f"⚠️ <b>{tool_name}</b>\n\n"
        f"❌ <b>आपके पास अभी 0 टोकन (Token) हैं!</b>\n\n"
        f"🎁 1 डेमो (Demo) लेने के लिए नीचे दिए गए बटन पर क्लिक करके हमारा ऑफिशियल ग्रुप जॉइन करें, "
        f"या अनलिमिटेड एक्सेस के लिए सब्सक्रिप्शन खरीदें।"
    )
    bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=markup)

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
    ensure_first_start_token(message.from_user.id)
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
        mode = "🔒 Paid" if paid_mode_enabled() else "🆓 Free"
        sub_info = f"{mode} Mode | 🎟️ <b>Tokens:</b> {user_data['credits']}"

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

# --- Main Message Handler ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text if message.text else ""

    # Continue active VCF workflows before normal tool routing.
    if user_id in vcf_state:
        if text.strip() == "/done" and vcf_state[user_id].get("mode") == "merge":
            vcf_done_command(message)
            return
        vcf_followup_handler(message)
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
        elif text == "⏰ Set Alert Time":
            admin_state[user_id] = "WAITING_ALERT_TIME"
            bot.reply_to(message, "📝 Alert Interval **Hours** me likhein (e.g. `1` for 1 hour, `2` for 2 hours, `0.5` for 30 min):")
            return
        elif text == "📝 Change Alert Msg":
            admin_state[user_id] = "WAITING_ALERT_MSG"
            bot.reply_to(message, f"📝 Naya Group Alert Message HTML format me bhejein.\n\nCurrent Message:\n\n{ALERT_MSG}", parse_mode="HTML")
            return
        elif text == "💰 Paid System ON/OFF":
            state = "ON" if paid_mode_enabled() else "OFF"
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🟢 Turn ON", callback_data="paid_on"),
                InlineKeyboardButton("🔴 Turn OFF", callback_data="paid_off")
            )
            bot.reply_to(message, f"💰 <b>Paid System:</b> {state}", parse_mode="HTML", reply_markup=markup)
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
        if not require_paid_access(user_id):
            send_paid_gate(chat_id, text)
            return
        send_token_restriction_message(chat_id, text)
        return

    elif text == "💳 Buy Subscription":
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(text="⚡ 1 Week — ₹80", callback_data="plan_80"),
            InlineKeyboardButton(text="🔥 15 Days — ₹160", callback_data="plan_160"),
            InlineKeyboardButton(text="💎 1 Month — ₹500", callback_data="plan_500"),
            InlineKeyboardButton(text="👑 Lifetime — ₹2000", callback_data="plan_2000")
        )
        sub_text = (
            "💳 <b>Buy Subscription</b>\n\n"
            "⚡ <b>1 Week — ₹80</b>\n7 Days | Unlimited Lookups\n\n"
            "🔥 <b>15 Days — ₹160</b>\n15 Days | Unlimited Lookups\n\n"
            "💎 <b>1 Month — ₹500</b>\n30 Days | Unlimited Lookups\n\n"
            "👑 <b>Lifetime — ₹2000</b>\nForever ∞ | Unlimited Lookups\n\n"
            "👇 Select a plan:"
        )
        bot.send_message(chat_id, sub_text, parse_mode="HTML", reply_markup=markup)
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


    if text == "🎁 Group Join Token":
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Join Official Group", url=OFFICIAL_GROUP_LINK))
        markup.add(InlineKeyboardButton("🔄 Verify & Claim 1 Token", callback_data="claim_group_token"))
        bot.reply_to(
            message,
            "🎁 <b>Group Join Reward</b>\n\n"
            "Group join करें और verification के बाद <b>1 Free Token</b> claim करें।",
            parse_mode="HTML",
            reply_markup=markup
        )
        return

    # --- VCF TOOLS ---
    vcf_tools = {
        "📁 टेक्स्ट से VCF", "📂 VCF से टेक्स्ट", "👑 Admin/Navy VCF",
        "✏️ VCF एडिटर", "🔄 फाइल जोड़ें (Merge)", "✂️ फाइल बांटें (Split)",
        "⚙️ नाम बदलें", "🔎 VCF जानकारी"
    }
    if text in vcf_tools:
        if not require_paid_access(user_id):
            send_paid_gate(chat_id, text)
            return
        if text == "📁 टेक्स्ट से VCF":
            msg = bot.reply_to(message, "📥 नंबरों की .txt/.csv file भेजें या नंबरों को line-by-line भेजें:")
            bot.register_next_step_handler(msg, vcf_text_to_vcf_process)
            return
        if text == "📂 VCF से टेक्स्ट":
            msg = bot.reply_to(message, "📥 .vcf file भेजें:")
            bot.register_next_step_handler(msg, vcf_to_text_process)
            return
        if text == "👑 Admin/Navy VCF":
            msg = bot.reply_to(message, "📥 .vcf file भेजें। मैं contacts को साफ करके नई VCF file दूँगा:")
            bot.register_next_step_handler(msg, admin_navy_vcf_process)
            return
        if text == "✏️ VCF एडिटर":
            msg = bot.reply_to(message, "📥 .vcf file भेजें:")
            bot.register_next_step_handler(msg, vcf_editor_process)
            return
        if text == "🔄 फाइल जोड़ें (Merge)":
            vcf_state[user_id] = {"mode": "merge", "files": []}
            bot.reply_to(message, "📥 पहली .vcf file भेजें। सभी files भेजने के बाद /done लिखें।")
            return
        if text == "✂️ फाइल बांटें (Split)":
            msg = bot.reply_to(message, "📥 .vcf file भेजें:")
            bot.register_next_step_handler(msg, vcf_split_process)
            return
        if text == "⚙️ नाम बदलें":
            msg = bot.reply_to(message, "📥 .vcf file भेजें:")
            bot.register_next_step_handler(msg, vcf_rename_process)
            return
        if text == "🔎 VCF जानकारी":
            msg = bot.reply_to(message, "📥 .vcf file भेजें:")
            bot.register_next_step_handler(msg, vcf_info_process)
            return

    # --- SEARCH PROCESSOR ---
    if message.content_type == "text":
        if text.startswith("/"):
            return

        user_data = get_user_credits(user_id)

        if paid_mode_enabled() and not is_subscribed(user_id) and user_id != ADMIN_ID:
            if user_data["credits"] <= 0:
                send_paid_gate(chat_id, "Search")
                return

        query_id = randint(0, 9999999)
        bot.reply_to(message, "Searching...")

        report = generate_report(text, query_id)

        if not report:
            bot.send_message(chat_id, "Result nahi mila.", reply_to_message_id=message.message_id)
            return

        if paid_mode_enabled() and user_id != ADMIN_ID:
            deduct_credit(user_id)

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
    if call.data == "paid_on" and call.from_user.id == ADMIN_ID:
        set_paid_mode(True)
        bot.answer_callback_query(call.id, "Paid System ON")
        bot.edit_message_text("💰 <b>Paid System:</b> 🟢 ON", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML")
        return

    if call.data == "paid_off" and call.from_user.id == ADMIN_ID:
        set_paid_mode(False)
        bot.answer_callback_query(call.id, "Paid System OFF")
        bot.edit_message_text("💰 <b>Paid System:</b> 🔴 OFF — Tools Free + Token rewards active", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML")
        return

    if call.data == "claim_group_token":
        try:
            member = bot.get_chat_member(OFFICIAL_GROUP_ID, call.from_user.id)
            if member.status in ("left", "kicked"):
                bot.answer_callback_query(call.id, "❌ पहले Group join करें।", show_alert=True)
                return
            ok, balance = award_group_token(call.from_user.id)
            bot.answer_callback_query(call.id, "🎁 1 token added!" if ok else "Token already claimed.", show_alert=True)
            bot.send_message(call.message.chat.id, f"🎟️ <b>Token Balance:</b> {balance}", parse_mode="HTML")
        except Exception:
            bot.answer_callback_query(call.id, "❌ Group verification failed. Bot को group में add/admin करें।", show_alert=True)
        return

    if call.data == "buy_sub":
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(text="⚡ 1 Week — ₹80", callback_data="plan_80"),
            InlineKeyboardButton(text="🔥 15 Days — ₹160", callback_data="plan_160"),
            InlineKeyboardButton(text="💎 1 Month — ₹500", callback_data="plan_500"),
            InlineKeyboardButton(text="👑 Lifetime — ₹2000", callback_data="plan_2000")
        )
        sub_text = (
            "💳 <b>Buy Subscription</b>\n\n"
            "⚡ <b>1 Week — ₹80</b>\n7 Days | Unlimited Lookups\n\n"
            "🔥 <b>15 Days — ₹160</b>\n15 Days | Unlimited Lookups\n\n"
            "💎 <b>1 Month — ₹500</b>\n30 Days | Unlimited Lookups\n\n"
            "👑 <b>Lifetime — ₹2000</b>\nForever ∞ | Unlimited Lookups\n\n"
            "👇 Select a plan:"
        )
        bot.edit_message_text(sub_text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("plan_"):
        amount = call.data.split("_")[1]
        
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa={PAYMENT_UPI_ID}&pn=PowerOfSandip&am={amount}&cu=INR"
        
        pay_text = (
            f"💳 <b>Payment QR Code</b>\n\n"
            f"💰 <b>Amount:</b> ₹{amount}\n"
            f"📌 <b>UPI ID:</b> <code>{PAYMENT_UPI_ID}</code>\n\n"
            f"1. Is QR Code ko kisi bhi UPI app (Paytm/GPay/PhonePe) se scan karke payment karein.\n"
            f"2. Payment karne ke baad niche <b>'Send Screenshot to Admin'</b> par click karke screenshot bhej dein."
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text="💬 Send Screenshot to Admin", url=f"https://t.me/{ADMIN_USERNAME}"))

        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.send_photo(call.message.chat.id, photo=qr_url, caption=pay_text, parse_mode="HTML", reply_markup=markup)

    elif call.data == "invite_friends":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
        
        inv_text = (
            "🤝 <b>Invite Friends</b>\n\n"
            "Apne dosto ko share karein aur free search credits paayein!\n\n"
            "Har dost ke join karne par aapko <b>2 Free Search Credits</b> milenge.\n\n"
            f"🔗 <b>Aapka Referral Link:</b>\n<code>{ref_link}</code>"
        )
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Join Group + Get 1 Token", url=OFFICIAL_GROUP_LINK))
        markup.add(InlineKeyboardButton("🔄 Verify & Claim 1 Token", callback_data="claim_group_token"))
        bot.send_message(call.message.chat.id, inv_text, parse_mode="HTML", reply_markup=markup)

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


# ==================== VCF MODULE ====================
def _download_document(message):
    if not message.document:
        return None, None
    info = bot.get_file(message.document.file_id)
    data = bot.download_file(info.file_path)
    return data, message.document.file_name or "contacts.vcf"

def _send_temp_file(chat_id, path, caption):
    with open(path, "rb") as fh:
        bot.send_document(chat_id, fh, caption=caption)
    try:
        os.remove(path)
    except OSError:
        pass

def _vcf_cards(content):
    return [x.strip() for x in content.replace("\r\n", "\n").split("END:VCARD") if "BEGIN:VCARD" in x]

def vcf_text_to_vcf_process(message):
    data, filename = _download_document(message)
    if data is not None:
        lines = data.decode("utf-8", errors="ignore").splitlines()
    elif message.text:
        lines = message.text.splitlines()
    else:
        bot.reply_to(message, "❌ Text বা .txt/.csv file পাঠান।")
        return
    path = f"contacts_{message.from_user.id}.vcf"
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for idx, line in enumerate(lines, 1):
            value = line.strip()
            if not value:
                continue
            f.write(f"BEGIN:VCARD\nVERSION:3.0\nN:;Contact_{idx};;;\nFN:Contact_{idx}\nTEL;TYPE=CELL:{value}\nEND:VCARD\n")
            count += 1
    if count:
        _send_temp_file(message.chat.id, path, f"✅ VCF तैयार है\n📊 Contacts: {count}")
    else:
        bot.reply_to(message, "❌ कोई non-empty line नहीं मिली।")

def vcf_to_text_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    content = data.decode("utf-8", errors="ignore")
    numbers = []
    for line in content.splitlines():
        if line.upper().startswith("TEL"):
            value = line.split(":", 1)[-1].strip()
            if value:
                numbers.append(value)
    path = f"contacts_{message.from_user.id}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(numbers))
    _send_temp_file(message.chat.id, path, f"✅ Text तैयार है\n📊 Numbers: {len(numbers)}")

def admin_navy_vcf_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    content = data.decode("utf-8", errors="ignore").replace("\r\n", "\n")
    cards = _vcf_cards(content)
    path = f"cleaned_{message.from_user.id}.vcf"
    with open(path, "w", encoding="utf-8") as f:
        for card in cards:
            f.write(card + "\nEND:VCARD\n")
    _send_temp_file(message.chat.id, path, f"✅ VCF तैयार है\n📊 Contacts: {len(cards)}")

def vcf_editor_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    vcf_state[message.from_user.id] = {"mode": "edit", "data": data, "filename": filename}
    bot.reply_to(message, "✏️ अब prefix भेजें। उदाहरण: <code>Friend</code>\nनाम `Friend 1`, `Friend 2`... होगा।", parse_mode="HTML")

def vcf_rename_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    vcf_state[message.from_user.id] = {"mode": "rename", "data": data, "filename": filename}
    bot.reply_to(message, "📝 नया file name भेजें, बिना `.vcf` भी चलेगा।")

def vcf_split_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    cards = _vcf_cards(data.decode("utf-8", errors="ignore"))
    if not cards:
        bot.reply_to(message, "❌ कोई vCard नहीं मिला।")
        return
    vcf_state[message.from_user.id] = {"mode": "split", "cards": cards, "filename": filename}
    bot.reply_to(message, f"✂️ कुल {len(cards)} contacts हैं। कितने contacts प्रति file चाहिए? उदाहरण: <code>100</code>", parse_mode="HTML")

def vcf_info_process(message):
    data, filename = _download_document(message)
    if data is None or not filename.lower().endswith(".vcf"):
        bot.reply_to(message, "❌ केवल .vcf file भेजें।")
        return
    content = data.decode("utf-8", errors="ignore")
    cards = _vcf_cards(content)
    bot.reply_to(message, f"📄 <b>File:</b> <code>{filename}</code>\n📊 <b>Contacts:</b> {len(cards)}", parse_mode="HTML")

@bot.message_handler(commands=["done"])
def vcf_done_command(message):
    uid = message.from_user.id
    state = vcf_state.get(uid)
    if not state or state.get("mode") != "merge":
        bot.reply_to(message, "ℹ️ कोई Merge session active नहीं है।")
        return
    files = state.get("files", [])
    vcf_state.pop(uid, None)
    if not files:
        bot.reply_to(message, "❌ कोई VCF file नहीं मिली।")
        return
    path = f"merged_{uid}.vcf"
    with open(path, "wb") as out:
        for data, _name in files:
            out.write(data if data.endswith(b"\n") else data + b"\n")
    _send_temp_file(message.chat.id, path, f"✅ Merge complete\n📊 Files: {len(files)}")

@bot.message_handler(content_types=["document", "text"])
def vcf_followup_handler(message):
    uid = message.from_user.id
    state = vcf_state.get(uid)
    if not state:
        return
    mode = state.get("mode")

    if mode == "merge":
        data, filename = _download_document(message)
        if data is None or not filename.lower().endswith(".vcf"):
            bot.reply_to(message, "❌ Merge में .vcf file भेजें या /done लिखें।")
            return
        state["files"].append((data, filename))
        bot.reply_to(message, f"✅ {filename} add हो गई। अगली file भेजें या /done लिखें।")
        return

    if mode == "edit" and message.text:
        prefix = message.text.strip()
        data = state["data"]
        content = data.decode("utf-8", errors="ignore")
        cards = _vcf_cards(content)
        path = f"edited_{uid}.vcf"
        with open(path, "w", encoding="utf-8") as f:
            for i, card in enumerate(cards, 1):
                lines = []
                replaced = False
                for line in card.splitlines():
                    if line.startswith("FN:"):
                        lines.append(f"FN:{prefix} {i}")
                        replaced = True
                    elif line.startswith("N:"):
                        lines.append(f"N:;{prefix} {i};;;")
                    else:
                        lines.append(line)
                if not replaced:
                    lines.insert(2, f"FN:{prefix} {i}")
                f.write("\n".join(lines) + "\nEND:VCARD\n")
        vcf_state.pop(uid, None)
        _send_temp_file(message.chat.id, path, f"✅ Editor complete\n📊 Contacts: {len(cards)}")
        return

    if mode == "rename" and message.text:
        newname = re.sub(r"[^A-Za-z0-9._-]+", "_", message.text.strip()).strip("._") or "contacts"
        path = f"{newname}.vcf"
        if not path.lower().endswith(".vcf"):
            path += ".vcf"
        with open(path, "wb") as f:
            f.write(state["data"])
        vcf_state.pop(uid, None)
        _send_temp_file(message.chat.id, path, "✅ VCF renamed successfully.")
        return

    if mode == "split" and message.text:
        try:
            chunk = int(message.text.strip())
            if chunk <= 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Positive number भेजें।")
            return
        cards = state["cards"]
        base = os.path.splitext(state["filename"])[0]
        total = 0
        for idx in range(0, len(cards), chunk):
            path = f"{base}_part_{idx//chunk+1}.vcf"
            with open(path, "w", encoding="utf-8") as f:
                for card in cards[idx:idx+chunk]:
                    f.write(card + "\nEND:VCARD\n")
            with open(path, "rb") as fh:
                bot.send_document(message.chat.id, fh, caption=f"📦 Part {idx//chunk+1}")
            os.remove(path)
            total += 1
        vcf_state.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ Split complete: {total} files.")
        return

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
