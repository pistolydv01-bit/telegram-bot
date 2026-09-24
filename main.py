import os
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
LANG = "en"
LIMIT = 100

cash_reports = {}

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

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "Namaste! Main Search Bot hoon.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.content_type == "text":
        query_id = randint(0, 9999999)
        bot.reply_to(message, "Searching...")

        report = generate_report(message.text, query_id)

        if not report:
            bot.send_message(message.chat.id, "Result nahi mila.")
            return

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


# Start Health Check Server
logging.basicConfig(level=logging.INFO)

threading.Thread(
    target=run_dummy_server,
    daemon=True
).start()


# Start Telegram Bot
bot.polling(none_stop=True)
