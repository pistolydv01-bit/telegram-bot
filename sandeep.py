# ===================================================================
# Python Telegram Bot Source Code (Updated with Admin Panel & Permissions)
# ===================================================================

import os
import re
import time
import json
import shutil
import logging
import atexit
import threading
import telebot
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from telebot import custom_filters
from telebot.handler_backends import State, StatesGroup
from telebot.storage import StateMemoryStorage
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
    NoAlertPresentException,
    WebDriverException,
    JavascriptException,
)

# ===================================================================
# Safe Logging & Sensitive Data Masking Helper
# ===================================================================

def mask_sensitive_data(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r'\b\d{12}\b', '[ID_REDACTED]', text)
    text = re.sub(r'\b\d{10}\b', '[MOBILE_REDACTED]', text)
    text = re.sub(r'\b\d{6}\b', '[OTP_REDACTED]', text)
    text = re.sub(r'https?://\S+', '[URL_REDACTED]', text)
    return text

class MaskingFormatter(logging.Formatter):
    def format(self, record):
        original = super().format(record)
        return mask_sensitive_data(original)

handler = logging.StreamHandler()
handler.setFormatter(MaskingFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ Error: TELEGRAM_TOKEN .env फ़ाइल में नहीं मिला!")

# ===================================================================
# USER PERMISSION SYSTEM & ADMIN PANEL SETUP
# ===================================================================

ADMIN_USER_IDS_ENV = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = set()

if ADMIN_USER_IDS_ENV.strip():
    for uid in ADMIN_USER_IDS_ENV.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ADMIN_USER_IDS.add(int(uid))

PERMISSIONS_FILE = os.getenv("PERMISSIONS_FILE", "permissions.json")
permissions_lock = threading.RLock()

DEFAULT_PERMISSIONS = {
    "enabled": True,
    "use_bot": True,
    "RESIDENCE": True,
    "CASTE": True,
    "INCOME": True
}

def load_permissions():
    """Load users/permissions from JSON file."""
    if not os.path.exists(PERMISSIONS_FILE):
        return {}
    try:
        with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logging.error(f"Permission file load error: {e}")
        return {}

USER_PERMISSIONS = load_permissions()

def save_permissions():
    """Atomically save permission data."""
    temp_file = PERMISSIONS_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(USER_PERMISSIONS, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, PERMISSIONS_FILE)
    except Exception as e:
        logging.error(f"Permission file save error: {e}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass
        raise

def is_admin(user_id):
    """Check whether Telegram user is an admin."""
    try:
        return int(user_id) in ADMIN_USER_IDS
    except Exception:
        return False

def ensure_user_permission(user_id):
    """Create default permission record if user does not already have one."""
    user_id = str(user_id)
    with permissions_lock:
        if user_id not in USER_PERMISSIONS:
            USER_PERMISSIONS[user_id] = DEFAULT_PERMISSIONS.copy()
            save_permissions()
        else:
            changed = False
            for key, value in DEFAULT_PERMISSIONS.items():
                if key not in USER_PERMISSIONS[user_id]:
                    USER_PERMISSIONS[user_id][key] = value
                    changed = True
            if changed:
                save_permissions()
        return USER_PERMISSIONS[user_id]

def user_has_permission(user_id, permission):
    """Central permission checker. Admins automatically have full access."""
    if is_admin(user_id):
        return True
    user_id = str(user_id)
    with permissions_lock:
        perms = USER_PERMISSIONS.get(user_id)
        if not perms:
            return False
        if not perms.get("enabled", False):
            return False
        return bool(perms.get(permission, False))

def add_permission_user(user_id):
    """Add a user with all permissions disabled except use_bot."""
    user_id = int(user_id)
    with permissions_lock:
        USER_PERMISSIONS[str(user_id)] = {
            "enabled": True,
            "use_bot": True,
            "RESIDENCE": False,
            "CASTE": False,
            "INCOME": False
        }
        save_permissions()

def remove_permission_user(user_id):
    """Remove user completely."""
    user_id = str(user_id)
    with permissions_lock:
        if user_id in USER_PERMISSIONS:
            del USER_PERMISSIONS[user_id]
            save_permissions()
            return True
    return False

def toggle_user_permission(user_id, permission):
    """Toggle one permission and return new state."""
    user_id = str(user_id)
    with permissions_lock:
        if user_id not in USER_PERMISSIONS:
            return None
        current = bool(USER_PERMISSIONS[user_id].get(permission, False))
        USER_PERMISSIONS[user_id][permission] = not current
        save_permissions()
        return not current

state_storage = StateMemoryStorage()
bot = telebot.TeleBot(TELEGRAM_TOKEN, state_storage=state_storage)
bot.add_custom_filter(custom_filters.StateFilter(bot))

SESSION_TIMEOUT_SECONDS = 900
MAX_SESSION_LIFETIME_SECONDS = 2700
MAX_CAPTCHA_ATTEMPTS = 3
MAX_OTP_ATTEMPTS = 4  
MAX_FILE_SIZE_MB = 5

user_locks = defaultdict(threading.Lock)
session_lock = threading.Lock()
active_user_sessions = {}

class RTPSState(StatesGroup):
    service_type = State()
    user_details = State()
    photo_upload = State()
    document_upload = State()
    mobile_otp_input = State()
    email_otp_input = State()
    captcha_input = State()
    aadhaar_otp_input = State()

def get_user_dir(chat_id):
    sanitized_chat_id = re.sub(r'[^0-9_-]', '', str(chat_id))
    user_dir = os.path.abspath(f"user_data_{sanitized_chat_id}")
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def cleanup_user_files(chat_id):
    sanitized_chat_id = re.sub(r'[^0-9_-]', '', str(chat_id))
    user_dir = os.path.abspath(f"user_data_{sanitized_chat_id}")
    if os.path.exists(user_dir):
        try:
            shutil.rmtree(user_dir, ignore_errors=True)
        except Exception as e:
            logging.error(f"Cleanup error occurred for chat_id {chat_id}: {e}")

# ===================================================================
# Centralized Error Screenshot Helper Function (With Masking)
# ===================================================================

def send_error_screenshot(chat_id, driver, error_message="Automation Error", exception=None):
    try:
        if not driver:
            logging.warning("Screenshot skipped: WebDriver उपलब्ध नहीं है")
            return

        user_dir = get_user_dir(chat_id)  
        os.makedirs(user_dir, exist_ok=True)  
          
        screenshot_path = os.path.join(user_dir, f"ERROR_{int(time.time() * 1000)}.png")  
        driver.save_screenshot(screenshot_path)  
          
        error_text = str(exception) if exception else "Unknown interaction error"  
        error_text = mask_sensitive_data(error_text)  
        if len(error_text) > 1000:  
            error_text = "... " + error_text[-1000:]  
              
        caption = (  
            f"❌ {error_message}\n\n"  
            f"💬 Error Details:\n{error_text}\n\n"  
            f"📸 जिस page पर error आया उसका screenshot:"  
        )  
          
        with open(screenshot_path, "rb") as photo:  
            bot.send_photo(chat_id, photo, caption=caption)  
          
        logging.info(f"📸 Error screenshot sent for chat {chat_id}")  
          
        try:  
            if os.path.exists(screenshot_path):  
                os.remove(screenshot_path)  
        except Exception:  
            pass  
              
    except Exception as screenshot_error:  
        logging.error(f"❌ Screenshot भेजने में भी error आया: {screenshot_error}")

# ===================================================================
# Debug Screenshot Helper Function
# ===================================================================

def debug_screenshot(driver, chat_id, name):
    if not chat_id:
        return

    try:  
        user_dir = get_user_dir(chat_id)  
        path = os.path.join(user_dir, f"DEBUG_{name}_{int(time.time())}.png")  
        driver.save_screenshot(path)  
        with open(path, "rb") as f:  
            bot.send_photo(chat_id, f, caption=f"🔎 DEBUG: {name}")  
    except Exception as e:  
        logging.error(f"Debug screenshot error: {e}")

# ===================================================================
# Step-Specific Screenshot Helper Function (For Form Progression)
# ===================================================================

def send_step_screenshot(driver, chat_id, step_name):
    if not chat_id:
        return
    try:
        user_dir = get_user_dir(chat_id)
        os.makedirs(user_dir, exist_ok=True)
        path = os.path.join(user_dir, f"STEP_{step_name}_{int(time.time())}.png")
        driver.save_screenshot(path)
        with open(path, "rb") as f:
            bot.send_photo(chat_id, f, caption=f"📸 Step Screenshot: {step_name}")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    except Exception as e:
        logging.error(f"Step screenshot error for {step_name}: {e}")

ALLOWED_PHOTO_EXTS = ['.jpg', '.jpeg', '.png']
ALLOWED_DOC_EXTS = ['.pdf', '.jpg', '.jpeg', '.png']

def validate_file_content_and_extension(file_path, allowed_extensions):
    if not file_path or not os.path.exists(file_path):
        return False, "फ़ाइल डिस्क पर प्राप्त नहीं हो सकी।"

    actual_ext = os.path.splitext(file_path)[1].lower()      
    if actual_ext not in allowed_extensions:      
        return False, f"अमान्य फ़ाइल एक्सटेंशन ({actual_ext})!"      
      
    try:      
        with open(file_path, 'rb') as f:      
            header = f.read(12)      
              
        if actual_ext == '.pdf':      
            if not header.startswith(b'%PDF'):      
                return False, "अमान्य PDF फ़ाइल संरचना!"      
        elif actual_ext in ['.jpg', '.jpeg']:      
            if not header.startswith(b'\xff\xd8\xff'):      
                return False, "अमान्य JPEG फ़ाइल संरचना!"      
        elif actual_ext == '.png':      
            if not header.startswith(b'\x89\x50\x4e\x47\x0d\x0a\x1a\x0a'):      
                return False, "अमान्य PNG फ़ाइल संरचना!"      
    except Exception:      
        return False, "फ़ाइल सत्यापन त्रुटि।"      
      
    return True, "OK"

def validate_rtps_json(service_type, user_data):
    common_fields = [
        "district", "sub_division", "block", "applicant_name",
        "father_name", "mobile_no", "pin_code", "aadhaar_number"
    ]
    missing = [f for f in common_fields if not user_data.get(f)]

    if service_type == "RESIDENCE" and not user_data.get("residence_type"):      
        missing.append("residence_type")      
    elif service_type == "CASTE":      
        for f in ["profession", "category", "caste"]:      
            if not user_data.get(f):      
                missing.append(f)      
    elif service_type == "INCOME":      
        for f in ["profession", "annual_income"]:      
            if not user_data.get(f):      
                missing.append(f)      
      
    if missing:      
        return False, f"JSON में आवश्यक फ़ील्ड्स गायब हैं: {', '.join(missing)}"      
      
    if not re.match(r'^\d{10}$', str(user_data.get("mobile_no", ""))):      
        return False, "मोबाइल नंबर ठीक 10 अंकों का होना चाहिए।"      
      
    if user_data.get("pin_code") and not re.match(r'^\d{6}$', str(user_data.get("pin_code", ""))):      
        return False, "पिन कोड ठीक 6 अंकों का होना चाहिए।"      
      
    if service_type == "INCOME" and not str(user_data.get("annual_income", "")).isdigit():      
        return False, "वार्षिक आय संख्यात्मक होनी चाहिए।"      
      
    return True, "OK"

def get_chrome_driver(chat_id):
    user_dir = get_user_dir(chat_id)
    download_dir = os.path.join(user_dir, "downloads")
    os.makedirs(download_dir, exist_ok=True)

    chrome_options = Options()      
    chrome_options.add_argument("--headless=new")      
    chrome_options.add_argument("--no-sandbox")      
    chrome_options.add_argument("--disable-dev-shm-usage")      
    chrome_options.add_argument("--disable-gpu")      
    chrome_options.add_argument("--window-size=1920,1080")      
    chrome_options.add_argument("--disable-extensions")      
    chrome_options.add_argument("--lang=hi-IN,hi")  
      
    bin_path = os.getenv("CHROME_BIN", "/usr/bin/chromium")      
    if not os.path.exists(bin_path):      
        bin_path = "/usr/bin/google-chrome"      
    if os.path.exists(bin_path):      
        chrome_options.binary_location = bin_path      
      
    prefs = {      
        "download.default_directory": download_dir,      
        "download.prompt_for_download": False,      
        "download.directory_upgrade": True,      
        "plugins.always_open_pdf_externally": True      
    }      
    chrome_options.add_experimental_option("prefs", prefs)      
      
    driver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")      
    if os.path.exists(driver_path):      
        service = Service(driver_path)      
        return webdriver.Chrome(service=service, options=chrome_options), download_dir      
    else:      
        return webdriver.Chrome(options=chrome_options), download_dir

# ===================================================================
# ROBUST DROPDOWN SELECTOR FOR GENERAL FIELDS
# ===================================================================

def select_location_dropdown(driver, label_words, target_text, timeout=30):
    target_text = " ".join(str(target_text).split()).strip()
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            selects = driver.find_elements(By.XPATH, "//select")
            for select_elem in selects:
                try:
                    if not select_elem.is_displayed() or not select_elem.is_enabled():
                        continue
                    
                    field_text = driver.execute_script("""
                        const sel = arguments[0];
                        let node = sel.parentElement;
                        for (let i = 0; i < 8 && node; i++) {
                            const selects = node.querySelectorAll('select');
                            if (selects.length === 1) {
                                return (node.innerText || '').trim();
                            }
                            node = node.parentElement;
                        }
                        return '';
                    """, select_elem)
                    
                    field_text = " ".join((field_text or "").split()).lower()
                    
                    matched = False
                    for label in label_words:
                        if str(label).lower() in field_text:
                            matched = True
                            break
                    if not matched:
                        continue
                    
                    options = select_elem.find_elements(By.TAG_NAME, "option")
                    if len(options) <= 1:
                        continue
                    
                    matched_option = None
                    for option in options:
                        option_text = " ".join(option.text.split()).strip()
                        if not option_text:
                            continue
                        if option_text.lower() == target_text.lower():
                            matched_option = option
                            break
                    
                    if matched_option is None:
                        for option in options:
                            option_text = " ".join(option.text.split()).strip()
                            if not option_text:
                                continue
                            if target_text.lower() in option_text.lower():
                                matched_option = option
                                break
                    
                    if matched_option is None:
                        continue
                        
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", select_elem)
                    time.sleep(0.4)
                    
                    value = matched_option.get_attribute("value")
                    if value:
                        Select(select_elem).select_by_value(value)
                    else:
                        Select(select_elem).select_by_visible_text(matched_option.text.strip())
                    
                    driver.execute_script("""
                        arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                    """, select_elem)
                    time.sleep(1)
                    
                    selected_text = " ".join(Select(select_elem).first_selected_option.text.split()).strip()
                    if (selected_text.lower() == target_text.lower() or target_text.lower() in selected_text.lower()):
                        logging.info(f"✅ Selected {label_words[0]}: {selected_text}")
                        return True
                except Exception as e:
                    logging.debug(f"Dropdown candidate error: {e}")
        except Exception as e:
            logging.debug(f"Dropdown scan error: {e}")
            
        time.sleep(0.5)
        
    logging.error(f"❌ Could not select {label_words} -> {target_text}")
    return False

# ===================================================================
# Robust Iframe-Safe find_and_interact
# ===================================================================

def find_and_interact(driver, xpaths, action_type="click", text_value=None, timeout=20, chat_id=None):
    last_err = None
    start_time = time.time()

    while time.time() - start_time < timeout:  
        try:  
            driver.switch_to.default_content()  
        except Exception:  
            pass  
          
        for xp in xpaths:      
            try:      
                elems = driver.find_elements(By.XPATH, xp)  
                visible_elems = [e for e in elems if e.is_displayed() and e.is_enabled()]  
                if visible_elems:  
                    elem = visible_elems[0]  
                    try:  
                        return _perform_action(driver, elem, action_type, text_value)  
                    except Exception as e:  
                        last_err = e  
            except Exception as e:      
                last_err = e  

        try:  
            driver.switch_to.default_content()  
            frames = driver.find_elements(By.TAG_NAME, "iframe") + driver.find_elements(By.TAG_NAME, "frame")  
            for frame_index, frame in enumerate(frames):  
                try:  
                    driver.switch_to.default_content()  
                    driver.switch_to.frame(frame)  
                    for xp in xpaths:  
                        try:  
                            elems = driver.find_elements(By.XPATH, xp)  
                            visible_elems = [e for e in elems if e.is_displayed() and e.is_enabled()]  
                            if visible_elems:  
                                elem = visible_elems[0]  
                                return _perform_action(driver, elem, action_type, text_value)  
                        except Exception as e:  
                            last_err = e  
                except Exception as e:  
                    last_err = e  
        except Exception as e:  
            last_err = e  

        time.sleep(0.5)  

    try:  
        driver.switch_to.default_content()  
    except Exception:  
        pass  

    if chat_id:  
        send_error_screenshot(chat_id, driver, f"❌ XPath/Interaction Error ({action_type})", last_err)  

    raise NoSuchElementException(f"Interaction failed. Action: {action_type}, XPaths: {xpaths}, Last error: {repr(last_err)}")

def _perform_action(driver, elem, action_type, text_value):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
    time.sleep(0.3)
    if action_type == "click":
        try:
            elem.click()
        except Exception:
            driver.execute_script("arguments[0].click();", elem)
    elif action_type == "type":
        try:
            elem.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].focus();", elem)
            except Exception:
                pass
        try:
            elem.clear()
        except Exception:
            pass
        elem.send_keys(str(text_value))
    elif action_type == "file":
        elem.send_keys(os.path.abspath(text_value))
    return elem

# ===================================================================
# Helper: Comprehensive Mobile OTP XPaths & Functions
# ===================================================================

def get_mobile_otp_xpaths():
    return [
        "//div[contains(@class, 'modal') or contains(@role, 'dialog')]//input[@type='text' or @type='number']",
        "//div[contains(@class, 'modal-body')]//input",
        "//*[@role='dialog']//input[not(@type='hidden')]",
        "//input[@id='mobile_otp']",
        "//input[@id='txtMobileOtp']",
        "//input[contains(@id, 'mobile_otp')]",
        "//input[contains(@id, 'txtMobileOtp')]",
        "//input[contains(@name, 'mobileOtp')]",
        "//input[contains(@name, 'mobile_otp')]",
        "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]",
        "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]",
        "//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]"
    ]

def wait_after_mobile_otp(driver, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            driver.switch_to.default_content()
            otp_inputs = []
            for xp in get_mobile_otp_xpaths():
                try:
                    otp_inputs.extend(driver.find_elements(By.XPATH, xp))
                except Exception:
                    pass
            visible_otp = any(e.is_displayed() and e.is_enabled() for e in otp_inputs)
            if not visible_otp:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def check_mobile_otp_result(driver, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        try:
            driver.switch_to.default_content()
            error_xpaths = [
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'invalid otp')]",
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'incorrect otp')]"
            ]
            for xp in error_xpaths:
                for elem in driver.find_elements(By.XPATH, xp):
                    if elem.is_displayed():
                        return "INVALID"

            popup_elements = driver.find_elements(By.ID, "digilockerPopUp")
            popup_hidden = not any(e.is_displayed() for e in popup_elements) if popup_elements else True
            if popup_hidden:
                time.sleep(1)
                return "SUCCESS"
        except Exception:
            pass
        time.sleep(0.5)
    return "UNKNOWN"

def detect_mobile_otp_popup(driver, timeout=8):
    start_time = time.time()
    otp_input_xpaths = get_mobile_otp_xpaths()
    while time.time() - start_time < timeout:
        try:
            driver.switch_to.default_content()
            for xp in otp_input_xpaths:
                try:
                    elements = driver.find_elements(By.XPATH, xp)
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.3)
    return False

def detect_current_verification_step(driver, timeout=8):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            driver.switch_to.default_content()
            for xp in get_mobile_otp_xpaths():
                try:
                    elems = driver.find_elements(By.XPATH, xp)
                    for elem in elems:
                        if elem.is_displayed() and elem.is_enabled():
                            return "MOBILE_OTP"
                except Exception:
                    continue

            captcha_xpaths = ["//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]"]
            for xp in captcha_xpaths:
                if any(e.is_displayed() for e in driver.find_elements(By.XPATH, xp)):
                    return "CAPTCHA"

            aadhaar_xpaths = ["//input[@id='aadhaar_otp']", "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'aadhaarotp')]"]
            for xp in aadhaar_xpaths:
                if any(e.is_displayed() for e in driver.find_elements(By.XPATH, xp)):
                    return "AADHAAR_OTP"

            email_xpaths = ["//input[@id='email_otp']", "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'emailotp')]"]
            for xp in email_xpaths:
                if any(e.is_displayed() for e in driver.find_elements(By.XPATH, xp)):
                    return "EMAIL_OTP"
        except Exception:
            pass
        time.sleep(0.5)
    return "UNKNOWN"

def capture_captcha_image(driver, captcha_xpaths, save_path):
    wait = WebDriverWait(driver, 10)
    for xp in captcha_xpaths:
        try:
            driver.switch_to.default_content()
            elem = wait.until(EC.presence_of_element_located((By.XPATH, xp)))
            elem.screenshot(save_path)
            return True
        except Exception:
            continue
    return False

def verify_submission_status(driver, timeout=15):
    start_time = time.time()
    success_indicators = [
        "//span[@id='lblReferenceNumber' or contains(@id, 'RefNo')]",
        "//div[contains(@class, 'alert-success') or contains(@class, 'success-message')]"
    ]
    while time.time() - start_time < timeout:
        try:
            driver.switch_to.default_content()
            for xp in success_indicators:
                if any(e.is_displayed() for e in driver.find_elements(By.XPATH, xp)):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def wait_for_new_pdf(download_dir, initial_files, timeout=30):
    start_time = time.time()
    while time.time() - start_time < timeout:
        current_files = set(os.listdir(download_dir))
        new_files = current_files - initial_files
        for file_name in new_files:
            if file_name.endswith('.crdownload') or file_name.endswith('.tmp'):
                continue
            if file_name.lower().endswith('.pdf'):
                full_path = os.path.join(download_dir, file_name)
                if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                    return full_path
        time.sleep(1)
    return None

def fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=None, user_id=None):
    driver.get("https://serviceonline.bihar.gov.in/")
    time.sleep(5)

    gad_xpaths = ["//a[normalize-space(.)='सामान्य प्रशासन विभाग']", "//span[normalize-space(.)='सामान्य प्रशासन विभाग']"]
    find_and_interact(driver, gad_xpaths, action_type="click", timeout=15, chat_id=chat_id)
    time.sleep(2)

    service_links = {
        "RESIDENCE": ["//a[contains(normalize-space(.), 'आवासीय प्रमाण-पत्र')]"],
        "CASTE": ["//a[contains(normalize-space(.), 'जाति प्रमाण-पत्र')]"],
        "INCOME": ["//a[contains(normalize-space(.), 'आय प्रमाण-पत्र')]"]
    }
    find_and_interact(driver, service_links[service_type], action_type="click", timeout=15, chat_id=chat_id)
    time.sleep(2)

    block_xpaths = ["//a[contains(normalize-space(.), 'अंचल स्तर पर')]"]
    find_and_interact(driver, block_xpaths, action_type="click", timeout=15, chat_id=chat_id)
    time.sleep(5)

    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])

    gender = user_data.get("gender", "MALE").upper()
    if gender == "MALE":
        gender_xpaths = ["//label[contains(normalize-space(.), 'पुरुष')]", "//label[contains(normalize-space(.), 'Male')]"]
    else:
        gender_xpaths = ["//label[contains(normalize-space(.), 'महिला')]", "//label[contains(normalize-space(.), 'Female')]"]
    find_and_interact(driver, gender_xpaths, action_type="click", timeout=20, chat_id=chat_id)
    time.sleep(1)

    find_and_interact(driver, ["//input[@id='applicant_name']"], "type", user_data["applicant_name"], chat_id=chat_id)
    find_and_interact(driver, ["//input[contains(@id,'father')]"], "type", user_data["father_name"], chat_id=chat_id)

    # RTPS ADDRESS - NUMERIC ID DEPENDENT DROPDOWN FLOW
    try:
        def wait_dropdown_options(select_id, timeout=40):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    elem = driver.find_element(By.ID, select_id)
                    options = elem.find_elements(By.TAG_NAME, "option")
                    valid = [opt for opt in options if (opt.get_attribute("value") or "").strip() not in ("0", "", None)]
                    if valid:
                        return elem
                except Exception:
                    pass
                time.sleep(0.7)
            raise TimeoutException(f"Dropdown {select_id} options load नहीं हुए")

        def select_by_exact_value(select_id, value, timeout=20):
            elem = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, select_id)))
            Select(elem).select_by_value(str(value))
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", elem)
            time.sleep(0.8)

        def select_by_text_match(select_id, target, timeout=40):
            target = " ".join(str(target).split()).strip().lower()
            elem = wait_dropdown_options(select_id, timeout=timeout)
            select = Select(elem)
            matched = None
            for opt in select.options:
                text = " ".join(opt.text.split()).strip().lower()
                if text == target or target in text:
                    matched = opt
                    break
            if not matched:
                raise NoSuchElementException(f"{target} option नहीं मिला in {select_id}")
            value = matched.get_attribute("value")
            if value:
                select.select_by_value(value)
            else:
                select.select_by_visible_text(matched.text.strip())
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", elem)
            time.sleep(1)
            return True

        select_by_exact_value("17158", "10", timeout=20)
        wait_dropdown_options("17162", timeout=30)
        select_by_text_match("17162", user_data.get("district"), timeout=40)
        time.sleep(2)

        if user_data.get("sub_division"):
            wait_dropdown_options("17159", timeout=40)
            select_by_text_match("17159", user_data.get("sub_division"), timeout=40)
            time.sleep(3)

        if user_data.get("block"):
            wait_dropdown_options("17163", timeout=50)
            select_by_text_match("17163", user_data.get("block"), timeout=50)
            time.sleep(2)

        try:
            village_radio = driver.find_element(By.XPATH, "//input[@type='radio' and @name='75265' and @value='1']")
            driver.execute_script("arguments[0].click();", village_radio)
        except Exception:
            pass

        if user_data.get("panchayat"):
            try:
                wait_dropdown_options("gpListID", timeout=40)
                select_by_text_match("gpListID", user_data.get("panchayat"), timeout=40)
            except Exception:
                pass

        for field, key in [("village", "village"), ("ward_no", "ward"), ("post_office", "postoffice"), ("police_station", "policestation"), ("pin_code", "pincode")]:
            if user_data.get(field):
                try:
                    find_and_interact(driver, [f"//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{key}')]"], "type", user_data.get(field), chat_id=chat_id)
                except Exception:
                    pass
    except Exception as e:
        if chat_id:
            send_error_screenshot(chat_id, driver, "❌ ADDRESS_FIELDS_ERROR", e)
        raise

    if photo_path and os.path.exists(photo_path):
        try:
            find_and_interact(driver, ["//input[@id='17495']", "//input[@type='file' and contains(@id, 'photo')]"], "file", photo_path, chat_id=chat_id)
        except Exception:
            pass

    if user_data.get("aadhaar_number"):
        try:
            find_and_interact(driver, ["//input[@id='aadhaar_no']"], "type", user_data["aadhaar_number"], chat_id=chat_id)
        except Exception:
            pass

    if user_data.get("email"):
        try:
            find_and_interact(driver, ["//input[contains(@id,'email')]"], "type", user_data["email"], chat_id=chat_id)
        except Exception:
            pass

    try:
        mobile_element = find_and_interact(driver, ["//input[contains(@id,'mobile')]"], "type", user_data["mobile_no"], chat_id=chat_id)
        time.sleep(0.5)
        mobile_element.send_keys(Keys.ENTER)
        time.sleep(2)

        if detect_mobile_otp_popup(driver, timeout=10):
            with session_lock:
                if chat_id in active_user_sessions:
                    active_user_sessions[chat_id].update({"is_processing": False, "waiting_for": "MOBILE_OTP"})
            bot.send_message(chat_id, "📱 मोबाइल नंबर पर OTP भेजा गया है। कृपया OTP दर्ज करें:")
            if user_id:
                bot.set_state(user_id, RTPSState.mobile_otp_input, chat_id)
            return "MOBILE_OTP"
    except Exception:
        pass

def continue_rtps_form_after_mobile_otp(driver, user_data, service_type, photo_path, chat_id=None, user_id=None):
    try:
        driver.switch_to.default_content()
        wait_after_mobile_otp(driver, timeout=20)
        time.sleep(1)
        if chat_id:
            send_step_screenshot(driver, chat_id, "AFTER_MOBILE_OTP")
            bot.send_message(chat_id, "✅ फॉर्म फील्ड्स का चरण समाप्त हुआ। अगले वेरिफिकेशन स्टेप की जांच की जा रही है...")
        return
    except Exception as e:
        if chat_id:
            send_step_screenshot(driver, chat_id, "POST_OTP_FATAL_ERROR")
        raise

# ===================================================================
# ADMIN PANEL & CALLBACK HANDLERS
# ===================================================================

def admin_main_keyboard():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("➕ Add User", callback_data="admin_add_user"),
        telebot.types.InlineKeyboardButton("👥 User List", callback_data="admin_users")
    )
    markup.add(telebot.types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_refresh"))
    return markup

def admin_panel_text():
    with permissions_lock:
        total_users = len(USER_PERMISSIONS)
        enabled_users = sum(1 for p in USER_PERMISSIONS.values() if p.get("enabled", False))
    return (
        "👑 *ADMIN PANEL*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"🟢 Enabled Users: `{enabled_users}`\n\n"
        "नीचे से option चुनें:"
    )

@bot.message_handler(commands=["admin"])
def admin_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    if not is_admin(user_id):
        bot.send_message(chat_id, "❌ आपको Admin Panel access नहीं है।")
        return

    bot.send_message(
        chat_id,
        admin_panel_text(),
        parse_mode="Markdown",
        reply_markup=admin_main_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data.startswith("perm_") or call.data.startswith("user_") or call.data.startswith("remove_"))
def admin_callback_handler(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Admin access required.", show_alert=True)
        return

    data = call.data

    if data in ("admin_refresh", "admin_back"):
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                admin_panel_text(),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=admin_main_keyboard()
            )
        except Exception:
            pass
        return

    if data == "admin_add_user":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            chat_id,
            "➕ *Add User*\n\nअब user का numeric Telegram ID भेजें:\n\nउदाहरण:\n`123456789`",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, admin_add_user_handler)
        return

    if data == "admin_users":
        bot.answer_callback_query(call.id)
        with permissions_lock:
            users = list(USER_PERMISSIONS.keys())

        if not users:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
            bot.edit_message_text(
                "👥 *User List*\n\nअभी कोई user added नहीं है।",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
            return

        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for uid in users:
            with permissions_lock:
                enabled = USER_PERMISSIONS[uid].get("enabled", False)
            status = "🟢" if enabled else "🔴"
            markup.add(telebot.types.InlineKeyboardButton(f"{status} {uid}", callback_data=f"user_{uid}"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))

        bot.edit_message_text(
            "👥 *USER LIST*\n\nकिस user की permission manage करनी है?",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
        return

    if data.startswith("user_"):
        target_id = data.replace("user_", "", 1)
        with permissions_lock:
            perms = USER_PERMISSIONS.get(target_id)

        if not perms:
            bot.answer_callback_query(call.id, "❌ User नहीं मिला।", show_alert=True)
            return

        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        def icon(key):
            return "🟢" if perms.get(key, False) else "🔴"

        markup.add(telebot.types.InlineKeyboardButton(f"{icon('enabled')} Account", callback_data=f"perm_{target_id}_enabled"))
        markup.add(telebot.types.InlineKeyboardButton(f"{icon('use_bot')} Use Bot", callback_data=f"perm_{target_id}_use_bot"))
        markup.add(
            telebot.types.InlineKeyboardButton(f"{icon('RESIDENCE')} Residence", callback_data=f"perm_{target_id}_RESIDENCE"),
            telebot.types.InlineKeyboardButton(f"{icon('CASTE')} Caste", callback_data=f"perm_{target_id}_CASTE")
        )
        markup.add(telebot.types.InlineKeyboardButton(f"{icon('INCOME')} Income", callback_data=f"perm_{target_id}_INCOME"))
        markup.add(telebot.types.InlineKeyboardButton("❌ Remove User", callback_data=f"remove_{target_id}"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Users", callback_data="admin_users"))

        bot.edit_message_text(
            f"👤 *USER PERMISSIONS*\n\nUser ID: `{target_id}`\n\n🟢 = Allowed\n🔴 = Disabled",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
        return

    if data.startswith("perm_"):
        parts = data.split("_", 2)
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Invalid permission.", show_alert=True)
            return
        target_id, permission = parts[1], parts[2]
        new_state = toggle_user_permission(target_id, permission)

        if new_state is None:
            bot.answer_callback_query(call.id, "❌ User नहीं मिला।", show_alert=True)
            return

        bot.answer_callback_query(call.id, "🟢 Enabled" if new_state else "🔴 Disabled")
        with permissions_lock:
            perms = USER_PERMISSIONS[target_id].copy()

        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        def icon2(key):
            return "🟢" if perms.get(key, False) else "🔴"

        markup.add(telebot.types.InlineKeyboardButton(f"{icon2('enabled')} Account", callback_data=f"perm_{target_id}_enabled"))
        markup.add(telebot.types.InlineKeyboardButton(f"{icon2('use_bot')} Use Bot", callback_data=f"perm_{target_id}_use_bot"))
        markup.add(
            telebot.types.InlineKeyboardButton(f"{icon2('RESIDENCE')} Residence", callback_data=f"perm_{target_id}_RESIDENCE"),
            telebot.types.InlineKeyboardButton(f"{icon2('CASTE')} Caste", callback_data=f"perm_{target_id}_CASTE")
        )
        markup.add(telebot.types.InlineKeyboardButton(f"{icon2('INCOME')} Income", callback_data=f"perm_{target_id}_INCOME"))
        markup.add(telebot.types.InlineKeyboardButton("❌ Remove User", callback_data=f"remove_{target_id}"))
        markup.add(telebot.types.InlineKeyboardButton("⬅️ Users", callback_data="admin_users"))

        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
        return

    if data.startswith("remove_"):
        target_id = data.replace("remove_", "", 1)
        if is_admin(int(target_id)):
            bot.answer_callback_query(call.id, "❌ Admin account remove नहीं किया जा सकता।", show_alert=True)
            return
        removed = remove_permission_user(target_id)
        if removed:
            bot.answer_callback_query(call.id, "✅ User removed.")
            bot.edit_message_text(f"✅ User `{target_id}` remove कर दिया गया।", chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
        else:
            bot.answer_callback_query(call.id, "❌ User नहीं मिला।", show_alert=True)
        return

def admin_add_user_handler(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        bot.send_message(message.chat.id, "❌ Invalid Telegram User ID. केवल numeric ID भेजें।")
        return
    target_id = int(raw_id)
    if is_admin(target_id):
        bot.send_message(message.chat.id, "ℹ️ यह ID पहले से Admin है।")
        return
    add_permission_user(target_id)
    bot.send_message(message.chat.id, f"✅ User `{target_id}` add हो गया। अब `/admin` से permissions बदल सकते हैं।", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "rtps_next_step")
def next_step_callback(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    with session_lock:
        session = active_user_sessions.get(chat_id)
        if not session:
            bot.answer_callback_query(call.id, "⚠️ सेशन समाप्त (Expired) हो गया है।")
            return
    bot.answer_callback_query(call.id, "➡️ अगले step पर जा रहे हैं...")
    driver = session["driver"]
    user_dir = get_user_dir(chat_id)
    route_to_next_step(bot, chat_id, user_id, driver, user_dir)

def session_timeout_cleaner():
    while True:
        time.sleep(30)
        now = time.time()
        to_delete = []
        with session_lock:      
            for chat_id, session in list(active_user_sessions.items()):      
                if now - session.get("created_at", now) > MAX_SESSION_LIFETIME_SECONDS or now - session.get("last_activity", now) > SESSION_TIMEOUT_SECONDS:      
                    to_delete.append((chat_id, session))      
            for chat_id, session in to_delete:      
                active_user_sessions.pop(chat_id, None)      
        for chat_id, session in to_delete:      
            try:      
                if session.get("driver"):      
                    session["driver"].quit()      
            except Exception:      
                pass      
            cleanup_user_files(chat_id)

threading.Thread(target=session_timeout_cleaner, daemon=True).start()

def route_to_next_step(bot, chat_id, user_id, driver, user_dir):
    max_retries = 3
    for _ in range(max_retries):
        with session_lock:
            if chat_id not in active_user_sessions:
                return
            active_user_sessions[chat_id]["last_activity"] = time.time()

        step = detect_current_verification_step(driver, timeout=5)      
        if step == "MOBILE_OTP":      
            bot.send_message(chat_id, "📱 मोबाइल पर प्राप्त OTP दर्ज करें:")      
            bot.set_state(user_id, RTPSState.mobile_otp_input, chat_id)      
            return      
        elif step == "CAPTCHA":      
            captcha_img_path = os.path.join(user_dir, "captcha.png")      
            if capture_captcha_image(driver, ["//img[contains(@id, 'captcha')]"], captcha_img_path):      
                with open(captcha_img_path, 'rb') as c_img:      
                    bot.send_photo(chat_id, c_img, caption="🧩 इमेज में दिख रहा CAPTCHA दर्ज करें:")      
                bot.set_state(user_id, RTPSState.captcha_input, chat_id)      
            return      
        elif step == "AADHAAR_OTP":      
            bot.send_message(chat_id, "🔐 आधार OTP दर्ज करें:")      
            bot.set_state(user_id, RTPSState.aadhaar_otp_input, chat_id)      
            return      
        elif step == "EMAIL_OTP":      
            bot.send_message(chat_id, "📧 ईमेल पर प्राप्त OTP दर्ज करें:")      
            bot.set_state(user_id, RTPSState.email_otp_input, chat_id)      
            return      
        time.sleep(2)      

    if verify_submission_status(driver, timeout=3):      
        with session_lock:      
            session = active_user_sessions.get(chat_id)      
        if session:      
            execute_final_submission_internal(chat_id, session, "")

# ===================================================================
# TELEGRAM COMMAND & MESSAGE HANDLERS (WITH PERMISSION CHECKS)
# ===================================================================

@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    ensure_user_permission(user_id)

    if not user_has_permission(user_id, "use_bot"):
        bot.send_message(
            chat_id,
            "❌ आपके account को bot इस्तेमाल करने की permission नहीं है。\n\nAdmin से permission लेने के लिए संपर्क करें।"
        )
        return

    try:  
        bot.delete_state(user_id=user_id, chat_id=chat_id)  
    except Exception:  
        pass  
      
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)      
    markup.add("RESIDENCE", "CASTE", "INCOME")      
    bot.send_message(chat_id, "RTPS बिहार ऑटोमेशन बोट में आपका स्वागत है। कृपया अपनी सेवा चुनें:", reply_markup=markup)      
    bot.set_state(user_id, RTPSState.service_type, chat_id)

@bot.message_handler(state=RTPSState.service_type)
def process_service_type(message):
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    service = message.text.upper()      
    if service not in ["RESIDENCE", "CASTE", "INCOME"]:      
        bot.reply_to(message, "कृपया केवल RESIDENCE, CASTE या INCOME में से ही विकल्प चुनें।")      
        return      

    # SERVICE-SPECIFIC PERMISSION CHECK
    if not user_has_permission(user_id, service):
        bot.reply_to(
            message,
            f"❌ आपके account में *{service}* service की permission नहीं है。\n\nAdmin से permission enable करवाएँ।",
            parse_mode="Markdown"
        )
        return
      
    with bot.retrieve_data(user_id, message.chat.id) as data:      
        data['service_type'] = service      
      
    templates = {      
        "RESIDENCE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_AADHAAR",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "residence_type": "स्थायी"\n}',      
        "CASTE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_AADHAAR",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "छात्र",\n  "category": "अत्यंत पिछड़ा वर्ग (अनुसूची-1)",\n  "caste": "जाति"\n}',      
        "INCOME": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_AADHAAR",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "सरकारी सेवा",\n  "annual_income": "90000"\n}'      
    }      
      
    bot.send_message(message.chat.id, f"सर्विस '{service}' चुनी गई। विवरण नीचे दिए गए JSON प्रारूप में भरें:\n\n`{templates[service]}`", parse_mode="Markdown")      
    bot.set_state(user_id, RTPSState.user_details, message.chat.id)

@bot.message_handler(state=RTPSState.user_details)
def process_user_details(message):
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    try:      
        user_data = json.loads(message.text)      
        with bot.retrieve_data(user_id, message.chat.id) as data:      
            service_type = data['service_type']      
      
        is_valid, err_msg = validate_rtps_json(service_type, user_data)      
        if not is_valid:      
            bot.reply_to(message, f"❌ डेटा त्रुटि: {err_msg}")      
            return      
      
        with bot.retrieve_data(user_id, message.chat.id) as data:      
            data['user_data'] = user_data      
      
        bot.send_message(message.chat.id, "विवरण सत्यापित हुआ। अब अपनी फोटो (JPG/PNG) भेजें:")      
        bot.set_state(user_id, RTPSState.photo_upload, message.chat.id)      
    except json.JSONDecodeError:      
        bot.reply_to(message, "❌ अमान्य JSON प्रारूप। कृपया सही JSON भेजें।")      
    except Exception as e:      
        bot.reply_to(message, "❌ डेटा प्रोसेस करने में त्रुटि हुई।")

@bot.message_handler(content_types=['photo'], state=RTPSState.photo_upload)
def process_photo_upload(message):
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    chat_id = message.chat.id      
    user_dir = get_user_dir(chat_id)      
    file_info = bot.get_file(message.photo[-1].file_id)      
          
    if file_info.file_size and file_info.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:      
        bot.send_message(chat_id, f"❌ फोटो का आकार बहुत बड़ा है।")      
        return      
      
    _, ext = os.path.splitext(file_info.file_path)      
    ext = ext.lower() if ext else ".jpg"      
    if ext not in ALLOWED_PHOTO_EXTS:      
        ext = ".jpg"      
      
    photo_path = os.path.join(user_dir, f"photo{ext}")      
    downloaded = bot.download_file(file_info.file_path)      
    with open(photo_path, 'wb') as f:      
        f.write(downloaded)      
      
    valid, err_msg = validate_file_content_and_extension(photo_path, ALLOWED_PHOTO_EXTS)      
    if not valid:      
        bot.send_message(chat_id, f"❌ फोटो त्रुटि: {err_msg}")      
        return      
      
    with bot.retrieve_data(user_id, chat_id) as data:      
        data['photo_path'] = photo_path      
      
    bot.send_message(chat_id, "फोटो प्राप्त हुई। अब सपोर्टिंग दस्तावेज़ (PDF/JPG/PNG) भेजें:")      
    bot.set_state(user_id, RTPSState.document_upload, chat_id)

@bot.message_handler(content_types=['photo', 'document'], state=RTPSState.document_upload)
def process_document_upload(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    if not user_locks[chat_id].acquire(blocking=False):      
        bot.send_message(chat_id, "⚠️ आपकी एक प्रक्रिया पहले से चल रही है, प्रतीक्षा करें...")      
        return      
      
    driver = None  
    try:      
        user_dir = get_user_dir(chat_id)      
        if message.document:      
            file_info = bot.get_file(message.document.file_id)      
        else:      
            file_info = bot.get_file(message.photo[-1].file_id)      
      
        _, ext = os.path.splitext(file_info.file_path)      
        ext = ext.lower()      
        if ext not in ALLOWED_DOC_EXTS:      
            bot.send_message(chat_id, "❌ अमान्य दस्तावेज़ फॉर्मेट।")      
            return      
      
        doc_path = os.path.join(user_dir, f"doc{ext}")      
        downloaded = bot.download_file(file_info.file_path)      
        with open(doc_path, 'wb') as f:      
            f.write(downloaded)      
      
        bot.send_message(chat_id, "⚙️ RTPS पोर्टल पर ऑटोमेशन शुरू किया जा रहा है...")      
      
        with bot.retrieve_data(user_id, chat_id) as data:      
            user_data = data['user_data']      
            service_type = data['service_type']      
            photo_path = data['photo_path']      
      
        driver, download_dir = get_chrome_driver(chat_id)      
      
        with session_lock:      
            active_user_sessions[chat_id] = {      
                "driver": driver,      
                "user_id": user_id,      
                "doc_path": doc_path,      
                "photo_path": photo_path,      
                "download_dir": download_dir,      
                "created_at": time.time(),      
                "last_activity": time.time(),      
                "is_processing": True,      
                "captcha_attempts": 0,      
                "mobile_otp_attempts": 0,      
                "email_otp_attempts": 0,      
                "aadhaar_otp_attempts": 0      
            }      
      
        res = fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=chat_id, user_id=user_id)      
        if res == "MOBILE_OTP":  
            return  
    except Exception as e:      
        bot.send_message(chat_id, f"❌ ऑटोमेशन में त्रुटि:\n{e}")      
    finally:      
        if user_locks[chat_id].locked():      
            user_locks[chat_id].release()      

    if driver and chat_id in active_user_sessions:  
        session_info = active_user_sessions.get(chat_id)  
        if session_info and session_info.get("waiting_for") == "MOBILE_OTP":  
            return  
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)

@bot.message_handler(state=RTPSState.mobile_otp_input)
def process_mobile_otp_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    otp_code = message.text.strip()      
    if not re.fullmatch(r"\d{4,8}", otp_code):      
        bot.reply_to(message, "❌ OTP केवल 4–8 अंकों का होना चाहिए।")      
        return      
      
    with session_lock:      
        session = active_user_sessions.get(chat_id)      
        if not session:      
            bot.send_message(chat_id, "⚠️ सेशन समाप्त हो गया है।")      
            bot.delete_state(chat_id=chat_id, user_id=user_id)      
            return      
        session["is_processing"] = True      
      
    driver = session["driver"]      
    user_dir = get_user_dir(chat_id)      
      
    try:      
        find_and_interact(driver, get_mobile_otp_xpaths(), action_type="type", text_value=otp_code, timeout=20, chat_id=chat_id)      
        find_and_interact(driver, ["//button[@id='btnValidateOtp']", "//button[contains(normalize-space(.),'Validate')]"], action_type="click", timeout=20, chat_id=chat_id)      
        time.sleep(3)      
          
        session["is_processing"] = False      
        bot.send_message(chat_id, "✅ Mobile OTP सत्यापित हो गया।")      
          
        with bot.retrieve_data(user_id, chat_id) as data:      
            user_data = data["user_data"]      
            service_type = data["service_type"]      
            photo_path = data["photo_path"]      
          
        continue_rtps_form_after_mobile_otp(driver, user_data, service_type, photo_path, chat_id=chat_id, user_id=user_id)      
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)      
    except Exception as e:      
        session["is_processing"] = False      
        bot.send_message(chat_id, f"❌ Mobile OTP के बाद error आया:\n{e}")

@bot.message_handler(state=RTPSState.email_otp_input)
def process_email_otp_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return
    # Standard handler pass
    pass

@bot.message_handler(state=RTPSState.captcha_input)
def process_captcha_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return
    # Standard handler pass
    pass

@bot.message_handler(state=RTPSState.aadhaar_otp_input)
def process_aadhaar_otp_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_has_permission(user_id, "use_bot"):
        return

    otp_code = message.text.strip()      
    with session_lock:      
        session = active_user_sessions.get(chat_id)      
        if not session:      
            bot.delete_state(chat_id=chat_id, user_id=user_id)      
            return      
        session["is_processing"] = True      
      
    execute_final_submission_internal(chat_id, session, otp_code)

def execute_final_submission_internal(chat_id, session, otp_code):
    if not user_locks[chat_id].acquire(blocking=False):
        bot.send_message(chat_id, "⚠️ सबमिशन प्रक्रिया पहले से जारी है...")
        return

    driver = session["driver"]      
    doc_path = session["doc_path"]      
    download_dir = session["download_dir"]      
      
    try:      
        if otp_code:      
            find_and_interact(driver, ["//input[@id='aadhaar_otp']"], "type", otp_code, chat_id=chat_id)      
            find_and_interact(driver, ["//button[contains(text(),'Validate')]"], chat_id=chat_id)      
            time.sleep(3)      

        initial_files = set(os.listdir(download_dir))      
        try:      
            find_and_interact(driver, ["//input[@value='Attach Annexure']"], action_type="click", timeout=15, chat_id=chat_id)
            find_and_interact(driver, ["//div[contains(@id, 'annexure')]//input[@type='file']"], "file", doc_path, chat_id=chat_id)      
            find_and_interact(driver, ["//input[@value='Save Annexure']"], chat_id=chat_id)      
            time.sleep(2)      
        except Exception:      
            pass      
      
        if verify_submission_status(driver, timeout=15):      
            bot.send_message(chat_id, "✅ RTPS फॉर्म सबमिट हो गया है। पावती रसीद डाउनलोड हो रही है...")      
      
        current_handles = driver.window_handles      
        try:      
            find_and_interact(driver, ["//button[contains(text(),'Export to PDF')]"], chat_id=chat_id)      
        except Exception:      
            pass      
      
        time.sleep(1)      
        if len(driver.window_handles) > len(current_handles):      
            driver.switch_to.window(driver.window_handles[-1])      
      
        pdf_path = wait_for_new_pdf(download_dir, initial_files, timeout=30)      
        if pdf_path and os.path.exists(pdf_path):      
            bot.send_message(chat_id, "📄 आपकी RTPS पावती रसीद:")      
            with open(pdf_path, 'rb') as pdf_file:      
                bot.send_document(chat_id, pdf_file)      
        else:      
            bot.send_message(chat_id, "⚠️ फॉर्म सबमिट हो गया, लेकिन PDF डाउनलोड नहीं हो सका।")      
    except Exception as e:      
        bot.send_message(chat_id, f"❌ सबमिशन प्रक्रिया में त्रुटि हुई:\n{e}")      
    finally:      
        with session_lock:      
            active_user_sessions.pop(chat_id, None)      
        try:      
            driver.quit()      
        except Exception:      
            pass      
        cleanup_user_files(chat_id)      
        if user_locks[chat_id].locked():      
            user_locks[chat_id].release()

# ===================================================================
# HEALTH CHECK SERVER (RENDER KEEP-ALIVE) & MAIN RUNNER
# ===================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):  
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"🌐 Health Check Server Running on Port {port}")
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()

    logging.info("🚀 टेलीग्राम बोट आरंभ हो गया है...")      
    bot.infinity_polling(skip_pending=True)
