import os
import json
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
    ReplyKeyboardRemove
)
from dotenv import load_dotenv, set_key

load_dotenv()

# --- Config & Defaults ---
ENV_PATH = ".env"
URL = "https://leakosintapi.com/"

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6874667015"))               # Yahan apni Telegram Numeric ID dalein
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ooooooo929")       # Aapka Telegram Username
PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "sandeepkumar960148.rzp@rxairtel") # Default UPI ID

LANG = "en"
LIMIT = 100
DATA_FILE = "users.json"

cash_reports = {}
admin_state = {}  # Admin state tracker

# --- Helper to Update .env File ---
def update_env_variable(key, value):
    global API_TOKEN, PAYMENT_UPI_ID
    if key == "API_TOKEN":
        API_TOKEN = value
    elif key == "PAYMENT_UPI_ID":
        PAYMENT_UPI_ID = value

    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w") as f:
            f.write("")

    set_key(ENV_PATH, key, value)

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

# --- Admin Reply Keyboard Setup ---
def get_admin_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_stats = KeyboardButton("📊 Bot Stats")
    btn_api = KeyboardButton("🔑 Change API Key")
    btn_upi = KeyboardButton("💳 Change UPI ID")
    btn_broadcast = KeyboardButton("📢 Broadcast")
    btn_addcredit = KeyboardButton("➕ Add Credits")
    btn_addsub = KeyboardButton("👑 Add Subscription")
    btn_close = KeyboardButton("❌ Close Menu")
    
    markup.add(btn_stats, btn_broadcast)
    markup.add(btn_api, btn_upi)
    markup.add(btn_addcredit, btn_addsub)
    markup.add(btn_close)
    return markup

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
                        address_text.append(f"• <b>{key}:</b> {report_data[key]}")
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

# --- Start Handler ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    args = message.text.split()
    referrer_id = args[1] if len(args) > 1 and args[1].isdigit() else None

    user_data = get_user_credits(message.from_user.id, referrer_id)
    
    if is_subscribed(message.from_user.id):
        sub_info = f"👑 <b>Subscription:</b> Active ({user_data['sub_expiry']})"
    else:
        sub_info = f"💳 <b>Credits:</b> {user_data['credits']} search credit(s)"

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(text="💳 Buy Subscription", callback_data="buy_sub"),
        InlineKeyboardButton(text="🤝 Invite Friends", callback_data="invite_friends")
    )

    msg = (
        f"Namaste <b>{message.from_user.first_name}</b>!\n\n"
        f"{sub_info}\n\n"
        "Search karne ke liye direct text message bhejein."
    )
    bot.reply_to(message, msg, parse_mode="HTML", reply_markup=markup)

# --- Admin Panel Command ---
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Aapko is command ka access nahi hai.")
        return

    text = (
        "<b>👑 Admin Control Panel</b>\n\n"
        f"📌 <b>Current UPI ID:</b> <code>{PAYMENT_UPI_ID}</code>\n"
        f"🔑 <b>Current API Key:</b> <code>{API_TOKEN[:8] + '...' if API_TOKEN else 'Not Set'}</code>\n\n"
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
        f"Total Users: {total_users}\n"
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

# --- Main Message Handler & Reply Keyboard Logic ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id

    # Admin Reply Keyboard Buttons Handler
    if user_id == ADMIN_ID:
        text = message.text

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

        elif text == "❌ Close Menu":
            bot.reply_to(message, "Menu closed.", reply_markup=ReplyKeyboardRemove())
            return

        # State Handling for Admin Inputs
        if user_id in admin_state:
            state = admin_state.pop(user_id)
            if state == "WAITING_API":
                new_api = message.text.strip()
                update_env_variable("API_TOKEN", new_api)
                bot.reply_to(message, f"✅ <b>API Key updated & saved to .env!</b>\n\n<code>{new_api}</code>", parse_mode="HTML")
                return

            elif state == "WAITING_UPI":
                new_upi = message.text.strip()
                update_env_variable("PAYMENT_UPI_ID", new_upi)
                bot.reply_to(message, f"✅ <b>UPI ID updated & saved to .env!</b>\n\n<code>{new_upi}</code>", parse_mode="HTML")
                return

            elif state == "WAITING_BROADCAST":
                text_to_send = message.text.strip()
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
                return

            elif state == "WAITING_ADDCREDIT":
                try:
                    args = message.text.split()
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
                    args = message.text.split()
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

    # Regular User Search Request
    if message.content_type == "text":
        if message.text.startswith("/"):
            return

        user_data = get_user_credits(user_id)

        if not is_subscribed(user_id) and user_data["credits"] <= 0 and user_id != ADMIN_ID:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="💳 Buy Subscription", callback_data="buy_sub"))
            markup.add(InlineKeyboardButton(text="🤝 Invite Friends", callback_data="invite_friends"))

            bot.reply_to(
                message,
                "❌ <b>Not enough credits!</b>\n\n"
                "Aage search karne ke liye plan purchase karein ya friends ko invite karein.",
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        query_id = randint(0, 9999999)
        bot.reply_to(message, "Searching...")

        report = generate_report(message.text, query_id)

        if not report:
            bot.send_message(message.chat.id, "Result nahi mila.", reply_to_message_id=message.message_id)
            return

        if user_id != ADMIN_ID:
            deduct_credit(user_id)

        markup = create_inline_keyboard(query_id, 0, len(report))
        try:
            bot.send_message(
                message.chat.id,
                report[0],
                parse_mode="HTML",
                reply_markup=markup,
                reply_to_message_id=message.message_id
            )
        except Exception:
            bot.send_message(
                message.chat.id,
                report[0],
                reply_markup=markup,
                reply_to_message_id=message.message_id
            )

# --- Callbacks ---
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call: CallbackQuery):
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
        
        # Dynamic UPI QR Code
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

bot.polling(none_stop=True)
