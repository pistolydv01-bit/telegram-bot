import os
import json
import logging
import requests
import atexit
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    ContextTypes
)

# Load Environment Variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)

DATA_FILE = "users_data.json"

# --- HEALTH CHECK DUMMY SERVER & CLEANUP ---
def cleanup_all_sessions():
    logging.info("🧹 Cleaning up sessions before shutdown...")

atexit.register(cleanup_all_sessions)

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
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"🌐 Health Check Server Running on Port {port}")
    server.serve_forever()

# --- DATA STORAGE MANAGEMENT ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"approved": {}, "trial_used": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_data()

# --- HELPER FUNCTIONS ---
def is_user_approved(user_id):
    str_id = str(user_id)
    if str_id in db["approved"]:
        expiry_str = db["approved"][str_id]
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
        if datetime.now() < expiry_date:
            return True, expiry_str
        else:
            del db["approved"][str_id]
            save_data(db)
            return False, "Expired"
    return False, "Not Authorized"

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    str_id = str(user_id)

    if user_id == ADMIN_ID:
        await update.message.reply_text(
            f" Welcome Admin ({user.first_name})!\n\nUse /admin to open the Control Panel."
        )
        return

    is_approved, status = is_user_approved(user_id)

    if is_approved:
        await update.message.reply_text(
            f" Welcome back!\n\nYour Subscription is active until: `{status}`\n\n"
            "Send number to lookup:\n`/lookup +91XXXXXXXXXX`",
            parse_mode="Markdown"
        )
    elif str_id in db["trial_used"]:
        await update.message.reply_text(
            " Aapka 1-Time Free Trial khatam ho chuka hai.\n\n"
            "Bot ko aage use karne ke liye Admin se contact karein aur access approve karwayein.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(" Contact Admin", url=f"tg://user?id={ADMIN_ID}")]
            ])
        )
    else:
        await update.message.reply_text(
            f" Welcome {user.first_name}!\n\n"
            "Aapko 1-Time Free Trial mil raha hai. Aap ek number lookup kar sakte hain.\n\n"
            "Usage:\n`/lookup +91XXXXXXXXXX`",
            parse_mode="Markdown"
        )

async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    str_id = str(user_id)

    if not context.args:
        await update.message.reply_text(" Number format invalid! Example:\n`/lookup +91XXXXXXXXXX`", parse_mode="Markdown")
        return

    is_approved, _ = is_user_approved(user_id)
    is_admin = (user_id == ADMIN_ID)
    is_trial = False

    if not is_admin and not is_approved:
        if str_id not in db["trial_used"]:
            is_trial = True
        else:
            await update.message.reply_text(
                " Aapka Trial khatam ho gaya hai! Admin se contact karke approval lein.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(" Contact Admin", url=f"tg://user?id={ADMIN_ID}")]
                ])
            )
            return

    phone_number = context.args[0]
    url = f"http://apilayer.net/api/validate?access_key={NUMVERIFY_API_KEY}&number={phone_number}"

    try:
        response = requests.get(url).json()

        if response.get("valid"):
            msg = (
                f" **Phone Lookup Result**\n\n"
                f" **Number:** {response.get('number')}\n"
                f" **Country:** {response.get('country_name')} ({response.get('country_code')})\n"
                f" **Location:** {response.get('location')}\n"
                f" **Carrier:** {response.get('carrier')}\n"
                f" **Line Type:** {response.get('line_type')}"
            )
            if is_trial:
                db["trial_used"].append(str_id)
                save_data(db)
                msg += "\n\n **Aapka 1-Time Free Trial khatam ho gaya hai. Agli baar use karne ke liye Admin se access lein.**"
        else:
            msg = " Invalid Phone Number ya lookup failed!"

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception:
        await update.message.reply_text(" API request error! Kuch der baad try karein.")

# --- ADMIN PANEL ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(" Access Denied! Sirf Admin is panel ko open kar sakta hai.")
        return

    keyboard = [
        [InlineKeyboardButton(" Approve User Access", callback_data="admin_add")],
        [InlineKeyboardButton(" List Active Users", callback_data="admin_list")],
        [InlineKeyboardButton(" Remove User Access", callback_data="admin_remove")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(" **Admin Control Panel**", reply_markup=reply_markup, parse_mode="Markdown")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "admin_add":
        await query.message.reply_text(
            " User ko permission dene ke liye ye command bhein:\n\n"
            "`/add <CHAT_ID> <DAYS>`\n\n"
            "Example (30 Days): `/add 123456789 30`",
            parse_mode="Markdown"
        )
    elif query.data == "admin_remove":
        await query.message.reply_text(
            " User ki permission remove karne ke liye ye command bhejin:\n\n"
            "`/remove <CHAT_ID>`",
            parse_mode="Markdown"
        )
    elif query.data == "admin_list":
        if not db["approved"]:
            await query.message.reply_text(" Abhi koi active approved users nahi hain.")
            return

        text = " **Active Approved Users:**\n\n"
        for uid, expiry in db["approved"].items():
            text += f" User ID: `{uid}` | Expiry: `{expiry}`\n"

        await query.message.reply_text(text, parse_mode="Markdown")

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        target_id = str(context.args[0])
        days = int(context.args[1])

        expiry_date = datetime.now() + timedelta(days=days)
        db["approved"][target_id] = expiry_date.strftime("%Y-%m-%d %H:%M:%S")
        save_data(db)

        await update.message.reply_text(
            f" User `{target_id}` successfully approved for **{days} days**!\n"
            f"Expiry Date: `{db['approved'][target_id]}`",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(" Invalid format! Correct format: `/add <CHAT_ID> <DAYS>`", parse_mode="Markdown")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        target_id = str(context.args[0])
        if target_id in db["approved"]:
            del db["approved"][target_id]
            save_data(db)
            await update.message.reply_text(f" User `{target_id}` ki permission remove kar di gayi hai.", parse_mode="Markdown")
        else:
            await update.message.reply_text(" Yeh User list me nahi hai.")
    except Exception:
        await update.message.reply_text(" Invalid format! Correct format: `/remove <CHAT_ID>`", parse_mode="Markdown")

# --- MAIN RUNNER ---
if __name__ == '__main__':
    # Start Dummy Web Server for Cloud Hosting / Health Checks (Render / Koyeb etc.)
    threading.Thread(target=run_dummy_server, daemon=True).start()

    logging.info(" Telegram Bot initialize ho raha hai...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lookup", lookup))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CallbackQueryHandler(button_click))

    print("Bot is running...")
    app.run_polling()
