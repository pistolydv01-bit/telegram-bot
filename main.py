import os
import json
import requests
from random import randint
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv

load_dotenv()

URL = "https://leakosintapi.com/"
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

LANG = "en"
LIMIT = 100
DATA_FILE = "users.json"

cash_reports = {}

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

def get_user_credits(user_id):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        # Default 1 free use
        data[uid] = {"credits": 1, "used": 0}
        save_data(data)
    return data[uid]

def deduct_credit(user_id):
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
        data[uid] = {"credits": 0, "used": 0}
    data[uid]["credits"] += count
    save_data(data)
    return data[uid]["credits"]

# --- Bot Functions ---
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
            text = [f"<b>{database_name}</b>\n"]
            text.append(response["List"][database_name].get("InfoLeak", "") + "\n")

            if database_name != "No results found":
                for report_data in response["List"][database_name].get("Data", []):
                    for column_name in report_data.keys():
                        text.append(f"<b>{column_name}</b>: {report_data[column_name]}")
                    text.append("")

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

if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is missing")

bot = telebot.TeleBot(BOT_TOKEN)

# --- Start Handler ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_data = get_user_credits(message.from_user.id)
    credits = user_data["credits"]
    
    msg = (
        f"Namaste {message.from_user.first_name}!\n\n"
        f"Aapke pass <b>{credits}</b> free search credit baaki hai.\n\n"
        "Search karne ke liye direct text message bhejein."
    )
    bot.reply_to(message, msg, parse_mode="HTML")

# --- Admin Commands ---
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Aapko is command ka access nahi hai.")
        return

    text = (
        "<b>👑 Admin Panel</b>\n\n"
        "Commands:\n"
        "• <code>/stats</code> - User stats dekhein\n"
        "• <code>/addcredit &lt;user_id&gt; &lt;amount&gt;</code> - User ko search credits dein\n"
        "• <code>/reset &lt;user_id&gt;</code> - User record reset karein\n"
    )
    bot.reply_to(message, text, parse_mode="HTML")

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

# --- Main Message Handler ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.content_type == "text":
        user_id = message.from_user.id
        user_data = get_user_credits(user_id)

        # Free Credit Check
        if user_data["credits"] <= 0 and user_id != ADMIN_ID:
            markup = InlineKeyboardMarkup()
            contact_btn = InlineKeyboardButton(
                text="💬 Contact Admin",
                url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}"
            )
            markup.add(contact_btn)

            bot.reply_to(
                message,
                "⚠️ <b>Aapka Free Limit Khatam Ho Gaya Hai!</b>\n\n"
                "Aage search karne ke liye Admin se contact karke plan purchase/renew karwayen.",
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        query_id = randint(0, 9999999)
        bot.reply_to(message, "Searching...")

        report = generate_report(message.text, query_id)

        if not report:
            bot.send_message(message.chat.id, "Result nahi mila.")
            return

        # Deduct Credit on successful search
        if user_id != ADMIN_ID:
            deduct_credit(user_id)

        markup = create_inline_keyboard(query_id, 0, len(report))
        try:
            bot.send_message(
                message.chat.id,
                report[0],
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception:
            bot.send_message(
                message.chat.id,
                report[0],
                reply_markup=markup
            )

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call: CallbackQuery):
    if call.data.startswith("/page "):
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
