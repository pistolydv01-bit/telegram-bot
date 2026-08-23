# ===================================================================
# Python Telegram Bot Source Code (Updated with Admin Panel & Runtime Users)
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

try:
    from google import genai
except Exception:
    genai = None

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
ALLOWED_USER_IDS_ENV = os.getenv("ALLOWED_USER_IDS", "6874667015")

ALLOWED_USER_IDS = set()
if ALLOWED_USER_IDS_ENV.strip():
    for uid in ALLOWED_USER_IDS_ENV.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USER_IDS.add(int(uid))

if not TELEGRAM_TOKEN:
    raise ValueError("❌ Error: TELEGRAM_TOKEN .env फ़ाइल में नहीं मिला!")

# ============================================================
# GEMINI DOCUMENT -> RTPS JSON (ADDED)
# ============================================================
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
gemini_client = None
if genai is not None and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("Gemini client initialized: %s", GEMINI_MODEL)
    except Exception as e:
        logging.warning("Gemini client initialization failed: %s", e)

JSON_TEMPLATES = {
    "RESIDENCE": {
        "salutation":"श्री", "gender":"MALE", "applicant_name":"",
        "father_name":"", "mother_name":"", "husband_name":"",
        "aadhaar_number":"", "mobile_no":"", "email":"",
        "district":"", "sub_division":"", "block":"", "ward_no":"",
        "panchayat":"", "village":"", "post_office":"",
        "police_station":"", "pin_code":"", "profession":"",
        "residence_type":""
    },
    "CASTE": {
        "salutation":"श्री", "gender":"MALE", "applicant_name":"",
        "father_name":"", "mother_name":"", "husband_name":"",
        "aadhaar_number":"", "mobile_no":"", "email":"",
        "district":"", "sub_division":"", "block":"", "ward_no":"",
        "panchayat":"", "village":"", "post_office":"",
        "police_station":"", "pin_code":"", "profession":"",
        "category":"", "caste":""
    },
    "INCOME": {
        "salutation":"श्री", "gender":"MALE", "applicant_name":"",
        "father_name":"", "mother_name":"", "husband_name":"",
        "aadhaar_number":"", "mobile_no":"", "email":"",
        "district":"", "sub_division":"", "block":"", "ward_no":"",
        "panchayat":"", "village":"", "post_office":"",
        "police_station":"", "pin_code":"", "profession":"",
        "purpose":"",
        "income_govt_service":"00",
        "income_agriculture":"00",
        "income_business":"00",
        "income_other_sources":"00",
        "annual_income":"",
        "other_records":True
    }
}

def extract_rtps_data_with_gemini(file_path, service_type):
    if gemini_client is None:
        if genai is None:
            raise RuntimeError("Gemini SDK नहीं मिला। पहले 'pip install google-genai' करें।")
        raise RuntimeError("GEMINI_API_KEY configured नहीं है। .env में GEMINI_API_KEY डालें।")
    service_type = service_type.upper().strip()
    if service_type not in JSON_TEMPLATES:
        raise ValueError(f"Unsupported service: {service_type}")
    template = JSON_TEMPLATES[service_type]
    prompt = """इस official document से RTPS form के लिए उपलब्ध जानकारी निकालो।

Selected Service: {service}

IMPORTANT:
1. केवल document में दिखाई देने वाली जानकारी इस्तेमाल करो।
2. कोई value अनुमान से मत बनाओ।
3. जानकारी उपलब्ध न हो तो empty string "" रखो।
4. नीचे दिए JSON के keys बिल्कुल न बदलो।
5. कोई extra key मत जोड़ो।
6. केवल valid JSON return करो।
7. JSON के बाहर कोई explanation मत दो।

JSON TEMPLATE:
{template}
""".format(service=service_type, template=json.dumps(template, ensure_ascii=False, indent=2))
    uploaded_file = gemini_client.files.upload(file=file_path)
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[uploaded_file, prompt]
    )
    result = (response.text or "").strip()
    if result.startswith("```"):
        result = re.sub(r"^```(?:json)?\s*", "", result, flags=re.IGNORECASE)
        result = re.sub(r"\s*```$", "", result)
    raw_data = json.loads(result)
    final_data = dict(template)
    for key in template:
        if key in raw_data and raw_data[key] is not None:
            final_data[key] = str(raw_data[key]).strip()
    return final_data

# ============================================================
# RUNTIME USER ACCESS MANAGEMENT
# ============================================================

RUNTIME_USERS_FILE = "allowed_users.json"
runtime_users_lock = threading.RLock()


def load_runtime_users():
    if not os.path.exists(RUNTIME_USERS_FILE):
        return set()

    try:
        with open(
            RUNTIME_USERS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, list):
            return set()

        return {
            int(uid)
            for uid in data
            if str(uid).isdigit()
        }

    except Exception as e:
        logging.error(
            f"Runtime users load error: {e}"
        )
        return set()


RUNTIME_ALLOWED_USER_IDS = load_runtime_users()


def save_runtime_users():
    temp_file = RUNTIME_USERS_FILE + ".tmp"

    with runtime_users_lock:

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                sorted(RUNTIME_ALLOWED_USER_IDS),
                f,
                indent=2
            )

        os.replace(
            temp_file,
            RUNTIME_USERS_FILE
        )


def user_is_allowed(user_id):
    """
    Existing .env users +
    Admin Panel से runtime में added users.
    """

    try:
        user_id = int(user_id)
    except Exception:
        return False

    # Existing .env access remains unchanged
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True

    # Runtime-added users
    with runtime_users_lock:
        return user_id in RUNTIME_ALLOWED_USER_IDS


def add_allowed_user(user_id):
    user_id = int(user_id)

    with runtime_users_lock:
        RUNTIME_ALLOWED_USER_IDS.add(user_id)
        save_runtime_users()


def remove_allowed_user(user_id):
    user_id = int(user_id)

    with runtime_users_lock:

        if user_id in RUNTIME_ALLOWED_USER_IDS:
            RUNTIME_ALLOWED_USER_IDS.remove(user_id)
            save_runtime_users()
            return True

    return False

# ============================================================
# ADMIN USER IDS CONFIG
# ============================================================

ADMIN_USER_IDS_ENV = os.getenv(
    "ADMIN_USER_IDS",
    "6874667015"
)

ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in ADMIN_USER_IDS_ENV.split(",")
    if uid.strip().isdigit()
}


def is_admin(user_id):
    try:
        return int(user_id) in ADMIN_USER_IDS
    except Exception:
        return False

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
        path = os.path.join(  
            user_dir,  
            f"DEBUG_{name}_{int(time.time())}.png"  
        )  

        driver.save_screenshot(path)  

        with open(path, "rb") as f:  
            bot.send_photo(  
                chat_id,  
                f,  
                caption=f"🔎 DEBUG: {name}"  
            )  

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
            bot.send_photo(
                chat_id,
                f,
                caption=f"📸 Step Screenshot: {step_name}"
            )
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
# RTPS EXACT PROFESSION SELECTOR (ID = 17207)
# ===================================================================
def select_rtps_profession(driver, profession_value, timeout=25):
    """
    RTPS Income form Profession dropdown.

    Official HTML supplied for this form is:
        <select id="17207" ...>
            <option value="1">छात्र / Student</option>
            <option value="2">सरकारी सेवा / Govt. Service</option>
            ...
        </select>

    This helper intentionally uses the exact ID and option value instead
    of label/XPath guessing. It also fires native + jQuery change events
    because the RTPS page uses a custom searchDrop class.
    """
    profession_map = {
        "छात्र": "1",
        "student": "1",
        "छात्र / student": "1",
        "सरकारी सेवा": "2",
        "govt. service": "2",
        "government service": "2",
        "सरकारी सेवा / govt. service": "2",
        "निजी सेवा": "3",
        "private service": "3",
        "निजी सेवा / private service": "3",
        "व्यापार": "4",
        "business": "4",
        "व्यापार / business": "4",
        "किसान": "5",
        "farmer": "5",
        "किसान / farmer": "5",
        "गृहिणी": "6",
        "housewife": "6",
        "गृहिणी / housewife": "6",
        "अन्य": "7",
        "other": "7",
        "अन्य / other": "7",
    }

    raw = " ".join(str(profession_value or "").split()).strip()
    normalized = raw.casefold()
    value = profession_map.get(normalized)

    if value is None:
        # If caller already supplied the official option value 1..7.
        if raw in {"1", "2", "3", "4", "5", "6", "7"}:
            value = raw
        else:
            raise ValueError(
                f"Unsupported Profession: {profession_value!r}. "
                "Expected Student/Govt. Service/Private Service/Business/"
                "Farmer/Housewife/Other or value 1-7."
            )

    end_time = time.time() + timeout
    last_error = None

    while time.time() < end_time:
        try:
            driver.switch_to.default_content()
            elem = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "17207"))
            )

            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                elem
            )
            time.sleep(0.3)

            # Make sure the exact option exists.
            options = elem.find_elements(By.TAG_NAME, "option")
            option_info = []
            for opt in options:
                option_info.append((
                    (opt.get_attribute("value") or "").strip(),
                    " ".join((opt.text or "").split()).strip()
                ))

            if not any(v == value for v, _ in option_info):
                raise NoSuchElementException(
                    f"Profession value {value!r} not present in #17207. "
                    f"Options={option_info!r}"
                )

            # First use Selenium's native select support.
            try:
                Select(elem).select_by_value(value)
            except Exception as e:
                last_error = e

            # Then force the native DOM value and notify RTPS/jQuery.
            driver.execute_script("""
                const el = arguments[0];
                const value = arguments[1];

                const setter = Object.getOwnPropertyDescriptor(
                    HTMLSelectElement.prototype, 'value'
                );
                if (setter && setter.set) {
                    setter.set.call(el, value);
                } else {
                    el.value = value;
                }

                for (const opt of el.options) {
                    opt.selected = (opt.value === value);
                }

                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));

                if (window.jQuery) {
                    window.jQuery(el).val(value);
                    window.jQuery(el).trigger('input');
                    window.jQuery(el).trigger('change');
                }
            """, elem, value)

            time.sleep(0.8)

            actual_value = driver.execute_script(
                "return arguments[0].value;", elem
            )
            actual_text = driver.execute_script("""
                const el = arguments[0];
                const opt = el.options[el.selectedIndex];
                return opt ? opt.text.trim() : '';
            """, elem)

            if actual_value == value:
                logging.info(
                    f"✅ Profession selected: {actual_text} "
                    f"(ID 17207, value={value})"
                )
                return True

            last_error = RuntimeError(
                f"Profession verify failed: expected={value!r}, "
                f"actual={actual_value!r}, text={actual_text!r}"
            )

        except StaleElementReferenceException as e:
            last_error = e
        except Exception as e:
            last_error = e

        time.sleep(0.5)

    raise RuntimeError(
        f"❌ Profession dropdown #17207 select नहीं हुआ. "
        f"Requested={profession_value!r}, value={value!r}, "
        f"LastError={last_error!r}"
    )


# ===================================================================
# Exact RTPS text input helper
# ===================================================================
def fill_rtps_input_by_id(driver, input_id, value, timeout=20, chat_id=None, required=False):
    """Fill an RTPS input using its exact numeric ID and verify the value."""
    text = "" if value is None else str(value).strip()
    if not text and not required:
        return False
    if not text and required:
        raise ValueError(f"Required RTPS field {input_id} is empty")

    elem = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, str(input_id)))
    )
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
        elem
    )
    time.sleep(0.2)

    try:
        elem.click()
    except Exception:
        driver.execute_script("arguments[0].focus();", elem)

    try:
        elem.clear()
    except Exception:
        pass

    elem.send_keys(text)
    time.sleep(0.3)

    actual = (elem.get_attribute("value") or "").strip()
    if actual != text:
        # Some RTPS fields react better to native DOM events.
        driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            el.value = value;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            if (window.jQuery) {
                window.jQuery(el).val(value).trigger('input').trigger('change');
            }
        """, elem, text)
        time.sleep(0.3)
        actual = (elem.get_attribute("value") or "").strip()

    if actual != text:
        raise RuntimeError(
            f"RTPS input #{input_id} verify failed: "
            f"expected={text!r}, actual={actual!r}"
        )

    logging.info(f"✅ RTPS input {input_id} filled successfully")
    return True


# ===================================================================
# Robust Iframe-Safe find_and_interact
# ===================================================================

def find_and_interact(driver, xpaths, action_type="click", text_value=None, timeout=20, chat_id=None):
    # ADDED: preserve the original Email/Mobile block, but defer its actual
    # interaction until the final form stage.
    if chat_id and action_type == "type":
        try:
            session = active_user_sessions.get(chat_id)
            if session and session.get("defer_email_mobile_until_final"):
                joined = " ".join(str(xp).lower() for xp in xpaths)
                if "email" in joined or "mobile" in joined or "mobile no." in joined:
                    raise RuntimeError("DEFER_EMAIL_MOBILE_UNTIL_FINAL_STAGE")
        except RuntimeError:
            raise
        except Exception:
            pass

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
                    logging.info(  
                        f"FOUND {action_type}: {xp} | "  
                        f"tag={elem.tag_name} | "  
                        f"id={elem.get_attribute('id')} | "  
                        f"name={elem.get_attribute('name')}"  
                    )  
                    try:  
                        return _perform_action(driver, elem, action_type, text_value)  
                    except Exception as e:  
                        last_err = e  
                        logging.error(  
                            f"ACTION FAILED: {action_type} | "  
                            f"XPath={xp} | Error={repr(e)}"  
                        )  
            except Exception as e:      
                last_err = e  
                logging.error(  
                    f"SEARCH FAILED: XPath={xp} | Error={repr(e)}"  
                )  

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
                                logging.info(  
                                    f"FOUND IN FRAME {frame_index}: "  
                                    f"{action_type}: {xp}"  
                                )  
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

    logging.error(  
        f"❌ INTERACTION FAILED\n"  
        f"Action: {action_type}\n"  
        f"XPaths: {xpaths}\n"  
        f"Last error: {repr(last_err)}"  
    )  

    if chat_id:  
        send_error_screenshot(chat_id, driver, f"❌ XPath/Interaction Error ({action_type})", last_err)  

    raise NoSuchElementException(  
        f"Interaction failed.\n"  
        f"Action: {action_type}\n"  
        f"XPaths: {xpaths}\n"  
        f"Last error: {repr(last_err)}"  
    )

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
        try:
            entered_value = elem.get_attribute("value")
            if entered_value != str(text_value):
                logging.warning(
                    f"Input value mismatch: "
                    f"expected={mask_sensitive_data(str(text_value))}, "
                    f"actual={mask_sensitive_data(str(entered_value or ''))}"
                )
        except Exception:
            pass
    elif action_type == "file":
        elem.send_keys(os.path.abspath(text_value))
    return elem

# ===================================================================
# Helper: Comprehensive Mobile OTP XPaths
# ===================================================================

def get_mobile_otp_xpaths():
    return [
        "//div[contains(@class, 'modal') or contains(@role, 'dialog')]//input[@type='text' or @type='number']",
        "//div[contains(@class, 'modal-body')]//input",
        "//*[@role='dialog']//input[not(@type='hidden')]",
        "//div[contains(@style, 'display: block')]//input[not(@type='hidden')]",
        "//input[@id='mobile_otp']",
        "//input[@id='txtMobileOtp']",
        "//input[contains(@id, 'mobile_otp')]",
        "//input[contains(@id, 'txtMobileOtp')]",
        "//input[contains(@name, 'mobileOtp')]",
        "//input[contains(@name, 'mobile_otp')]",
        "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]",
        "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]",
        "//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]",
        "//label[contains(normalize-space(.), 'Enter OTP')]/following::input[1]",
        "//label[contains(normalize-space(.), 'OTP')]/following::input[1]"
    ]

# ===================================================================
# Helper: Updated Robust wait_after_mobile_otp Function
# ===================================================================

def wait_after_mobile_otp(driver, timeout=15):
    start = time.time()

    while time.time() - start < timeout:
        try:
            driver.switch_to.default_content()

            otp_inputs = []
            for xp in get_mobile_otp_xpaths():
                try:
                    otp_inputs.extend(
                        driver.find_elements(By.XPATH, xp)
                    )
                except Exception:
                    pass

            visible_otp = any(
                e.is_displayed() and e.is_enabled()
                for e in otp_inputs
            )

            if not visible_otp:
                logging.info("✅ Mobile OTP input अब दिखाई नहीं दे रहा है")
                time.sleep(1)

                captcha = driver.find_elements(
                    By.XPATH,
                    "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha') or contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]"
                )
                if any(e.is_displayed() for e in captcha):
                    logging.info("✅ CAPTCHA stage detected after Mobile OTP")
                    return True

                aadhaar = driver.find_elements(
                    By.XPATH,
                    "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'aadhaar') and contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'otp')]"
                )
                if any(e.is_displayed() for e in aadhaar):
                    logging.info("✅ Aadhaar OTP stage detected")
                    return True

                return True

        except Exception as e:
            logging.debug(
                "wait_after_mobile_otp iteration: %s",
                repr(e)
            )

        time.sleep(0.5)

    logging.warning("⚠️ Mobile OTP popup timeout")
    return False

# ===================================================================
# Helper: Check Mobile OTP Result Explicitly
# ===================================================================

def check_mobile_otp_result(driver, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        try:
            driver.switch_to.default_content()
            
            error_xpaths = [
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'invalid otp')]",
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'incorrect otp')]",
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'wrong otp')]",
                "//*[contains(translate(normalize-space(.), 'अमान्य OTP')]",
                "//*[contains(translate(normalize-space(.), 'गलत OTP')]",
                "//*[@id='otpError' and string-length(normalize-space(.))>0]"
            ]
            for xp in error_xpaths:
                for elem in driver.find_elements(By.XPATH, xp):
                    if elem.is_displayed():
                        return "INVALID"

            popup_elements = driver.find_elements(By.ID, "digilockerPopUp")
            popup_hidden = not any(e.is_displayed() for e in popup_elements) if popup_elements else True
            
            backdrop_elements = driver.find_elements(By.CSS_SELECTOR, ".modal-backdrop.show, .modal-backdrop")
            backdrop_removed = not any(e.is_displayed() for e in backdrop_elements) if backdrop_elements else True

            if popup_hidden and backdrop_removed:
                time.sleep(1)
                return "SUCCESS"

        except Exception as e:
            logging.debug(f"OTP result check iteration error: {e}")
        
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
                        if not element.is_displayed() or not element.is_enabled():
                            continue
                        logging.info(f"📱 Mobile OTP popup detected: {xp}")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"Mobile OTP popup detection retry: {e}")
        time.sleep(0.3)
    return False

def detect_current_verification_step(driver, timeout=8):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            driver.switch_to.default_content()

            mobile_otp_xpaths = get_mobile_otp_xpaths()  
            for xp in mobile_otp_xpaths:  
                try:  
                    elems = driver.find_elements(By.XPATH, xp)  
                    for elem in elems:  
                        if elem.is_displayed() and elem.is_enabled():  
                            return "MOBILE_OTP"  
                except Exception:  
                    continue  

            captcha_xpaths = [  
                "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]",  
                "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]"  
            ]  
            for xp in captcha_xpaths:  
                elems = driver.find_elements(By.XPATH, xp)  
                if any(e.is_displayed() for e in elems): return "CAPTCHA"  

            aadhaar_xpaths = [  
                "//input[@id='aadhaar_otp']",  
                "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'aadhaarotp')]",  
                "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'aadhaarotp')]"  
            ]  
            for xp in aadhaar_xpaths:  
                elems = driver.find_elements(By.XPATH, xp)  
                if any(e.is_displayed() for e in elems): return "AADHAAR_OTP"  

            email_xpaths = [  
                "//input[@id='email_otp']",  
                "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'emailotp')]",  
                "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'emailotp')]"  
            ]  
            for xp in email_xpaths:  
                elems = driver.find_elements(By.XPATH, xp)  
                if any(e.is_displayed() for e in elems): return "EMAIL_OTP"  
        except Exception as e:  
            logging.warning(f"Verification detection error: {e}")  
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
        "//div[contains(@class, 'alert-success') or contains(@class, 'success-message')]",
        "//a[contains(@href, 'Acknowledgement') or contains(@href, 'pdf')]"
    ]
    while time.time() - start_time < timeout:
        try:
            driver.switch_to.default_content()
            for xp in success_indicators:
                elems = driver.find_elements(By.XPATH, xp)
                if elems and any(e.is_displayed() for e in elems):
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
                    initial_size = os.path.getsize(full_path)
                    time.sleep(1)
                    if os.path.exists(full_path) and os.path.getsize(full_path) == initial_size:
                        return full_path
        time.sleep(1)
    return None

def fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=None, user_id=None):
    logging.info(f"Opening Main RTPS Portal for: {service_type}")
    driver.get("https://serviceonline.bihar.gov.in/")
    time.sleep(5)

    if chat_id:  
        try:  
            user_dir = get_user_dir(chat_id)  
            ss_path = os.path.join(user_dir, "rtps_homepage.png")  
            driver.save_screenshot(ss_path)  
            with open(ss_path, 'rb') as ss_file:  
                bot.send_photo(chat_id, ss_file, caption="🌐 RTPS Website open हो गई है।")  
        except Exception as ss_err:  
            logging.warning(f"Screenshot error: {ss_err}")  

    gad_xpaths = [  
        "//a[normalize-space(.)='सामान्य प्रशासन विभाग']",  
        "//span[normalize-space(.)='सामान्य प्रशासन विभाग']",  
        "//li[.//text()[normalize-space(.)='सामान्य प्रशासन विभाग']]",  
        "//*[normalize-space(text())='सामान्य प्रशासन विभाग']"  
    ]  
    find_and_interact(driver, gad_xpaths, action_type="click", timeout=15, chat_id=chat_id)  
    time.sleep(2)  

    service_links = {  
        "RESIDENCE": [  
            "//a[contains(normalize-space(.), 'आवासीय प्रमाण-पत्र')]",  
            "//a[contains(normalize-space(.), 'आवासीय प्रमाण पत्र')]",  
            "//a[contains(@href, 'residence')]"  
        ],  
        "CASTE": [  
            "//a[contains(normalize-space(.), 'जाति प्रमाण-पत्र')]",  
            "//a[contains(normalize-space(.), 'जाति प्रमाण पत्र')]",  
            "//a[contains(@href, 'caste')]"  
        ],  
        "INCOME": [  
            "//a[contains(normalize-space(.), 'आय प्रमाण-पत्र')]",  
            "//a[contains(normalize-space(.), 'आय प्रमाण पत्र')]",  
            "//a[contains(@href, 'income')]"  
        ]  
    }  
    find_and_interact(driver, service_links[service_type], action_type="click", timeout=15, chat_id=chat_id)  
    time.sleep(2)  
      
    block_xpaths = [  
        "//a[contains(normalize-space(.), 'अंचल स्तर पर')]",  
        "//a[contains(normalize-space(.), 'Block Level')]",  
        "//a[contains(normalize-space(.), 'अंचल')]"  
    ]  
    find_and_interact(driver, block_xpaths, action_type="click", timeout=15, chat_id=chat_id)  
    time.sleep(5)  
      
    if len(driver.window_handles) > 1:  
        driver.switch_to.window(driver.window_handles[-1])  

    gender = user_data.get("gender", "MALE").upper()      
    if gender == "MALE":  
        gender_xpaths = [  
            "//label[contains(normalize-space(.), 'पुरुष')]",  
            "//label[contains(normalize-space(.), 'Male')]",  
            "//*[contains(normalize-space(.), 'पुरुष / Male')]",  
            "//input[contains(@id,'Male') or contains(@id,'male')]",  
            "//input[contains(@name,'gender')][1]"  
        ]  
    elif gender == "FEMALE":  
        gender_xpaths = [  
            "//label[contains(normalize-space(.), 'महिला')]",  
            "//label[contains(normalize-space(.), 'Female')]",  
            "//*[contains(normalize-space(.), 'स्त्री / Female')]",  
            "//input[contains(@id,'Female') or contains(@id,'female')]",  
            "//input[contains(@name,'gender')][2]"  
        ]  
    else:  
        gender_xpaths = [  
            "//label[contains(normalize-space(.), 'Third Gender')]",  
            "//label[contains(normalize-space(.), 'तृतीय लिंग')]"  
        ]  
    find_and_interact(driver, gender_xpaths, action_type="click", timeout=20, chat_id=chat_id)      
    time.sleep(1)  
    debug_screenshot(driver, chat_id, "AFTER_GENDER")  

    # Salutation
    if user_data.get("salutation"):
        try:
            select_location_dropdown(
                driver, ["Salutation", "अभिभावक", "श्री"], 
                user_data.get("salutation"), timeout=8
            )
        except Exception as e:
            logging.warning(f"Salutation selection warning: {e}")
      
    find_and_interact(driver, [  
        "//input[@id='applicant_name']",  
        "//input[contains(@id,'applicant')]",  
        "//input[contains(@name,'applicant')]",  
        "//input[contains(@name,'Applicant')]",  
        "//label[contains(normalize-space(.),'Name of Applicant')]/following::input[1]",  
        "//*[contains(normalize-space(.),'Name of Applicant')]/following::input[1]"  
    ], "type", user_data["applicant_name"], chat_id=chat_id)      
    debug_screenshot(driver, chat_id, "AFTER_NAME")  
      
    find_and_interact(driver, [  
        "//input[contains(@id,'father')]",  
        "//input[contains(@name,'father')]",  
        "//label[contains(normalize-space(.),'Name of Father')]/following::input[1]",  
        "//*[contains(normalize-space(.),'Name of Father')]/following::input[1]"  
    ], "type", user_data["father_name"], chat_id=chat_id)      
    debug_screenshot(driver, chat_id, "AFTER_FATHER")  
      
    if user_data.get("mother_name"):      
        try:      
            find_and_interact(driver, [  
                "//input[contains(@id,'mother')]",  
                "//input[contains(@name,'mother')]",  
                "//label[contains(normalize-space(.),'Name of Mother')]/following::input[1]",  
                "//*[contains(normalize-space(.),'Name of Mother')]/following::input[1]"  
            ], "type", user_data["mother_name"], chat_id=chat_id)      
            debug_screenshot(driver, chat_id, "AFTER_MOTHER")      
        except Exception as e:      
            logging.warning(f"Mother name input warning: {e}")      

    if user_data.get("husband_name"):
        try:
            find_and_interact(driver, [
                "//input[contains(@id,'husband')]",
                "//input[contains(@name,'husband')]"
            ], "type", user_data["husband_name"], chat_id=chat_id)
        except Exception as e:
            logging.warning(f"Husband name input warning: {e}")

    # ============================================================
    # RTPS ADDRESS - EXACT NUMERIC ID DEPENDENT DROPDOWN & INPUT FLOW
    # ============================================================
    try:
        logging.info("🏠 Starting RTPS address selection with exact Numeric IDs...")

        def wait_dropdown_options(select_id, timeout=40):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    elem = driver.find_element(By.ID, select_id)
                    options = elem.find_elements(By.TAG_NAME, "option")
                    valid = []
                    for opt in options:
                        value = (opt.get_attribute("value") or "").strip()
                        text = " ".join(opt.text.split()).strip()
                        if value and value not in ("0", ""):
                            if text and "Please Select" not in text:
                                valid.append(opt)
                    if valid:
                        return elem
                except Exception:
                    pass
                time.sleep(0.7)
            raise TimeoutException(f"Dropdown {select_id} options load नहीं हुए")

        def select_by_exact_value(select_id, value, timeout=20):
            elem = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.ID, select_id))
            )
            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            select = Select(elem)
            select.select_by_value(str(value))
            driver.execute_script("""
                arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
            """, elem)
            time.sleep(0.8)
            selected = Select(elem).first_selected_option
            selected_value = (selected.get_attribute("value") or "").strip()
            if selected_value != str(value):
                raise Exception(f"{select_id} selection verify नहीं हुई. Expected={value}, Actual={selected_value}")
            return elem

        def select_by_text_match(select_id, target, timeout=40):
            target = " ".join(str(target).split()).strip().lower()
            elem = wait_dropdown_options(select_id, timeout=timeout)
            select = Select(elem)
            matched = None
            for opt in select.options:
                text = " ".join(opt.text.split()).strip()
                if not text:
                    continue
                lower = text.lower()
                if lower == target:
                    matched = opt
                    break
                if "/" in lower:
                    parts = [x.strip() for x in lower.split("/")]
                    if target in parts:
                        matched = opt
                        break
            if matched is None:
                for opt in select.options:
                    text = " ".join(opt.text.split()).strip()
                    if target in text.lower():
                        matched = opt
                        break
            if matched is None:
                for opt in select.options:
                    text = " ".join(opt.text.split()).strip()
                    if text and text.lower() in target:
                        matched = opt
                        break
            if matched is None:
                raise NoSuchElementException(f"{target} option नहीं मिला in {select_id}")
            value = matched.get_attribute("value")
            if value:
                select.select_by_value(value)
            else:
                select.select_by_visible_text(matched.text.strip())
            driver.execute_script("""
                arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
                arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
                if (window.jQuery) { jQuery(arguments[0]).trigger('change'); }
            """, elem)
            time.sleep(1)
            selected_text = " ".join(Select(elem).first_selected_option.text.split()).strip().lower()
            if (target not in selected_text and selected_text not in target):
                raise Exception(f"{select_id} selection verify failed: {selected_text}")
            logging.info(f"✅ {select_id} selected: {selected_text}")
            return True

        # 1. STATE (ID = 17158, Bihar value = 10)
        logging.info("📍 Selecting STATE = BIHAR (17158)")
        select_by_exact_value("17158", "10", timeout=20)
        wait_dropdown_options("17162", timeout=30)
        logging.info("✅ State Bihar selected successfully")

        # 2. DISTRICT (ID = 17162)
        district = str(user_data.get("district", "")).strip()
        if not district:
            raise ValueError("District user_data में मौजूद नहीं है")
        logging.info(f"📍 Selecting District = {district} (17162)")
        select_by_text_match("17162", district, timeout=40)
        time.sleep(2)

        # 3. SUB-DIVISION (ID = 17159)
        subdivision = str(user_data.get("sub_division", "")).strip()
        if subdivision:
            logging.info(f"📍 Selecting Sub-Division = {subdivision} (17159)")
            wait_dropdown_options("17159", timeout=40)
            select_by_text_match("17159", subdivision, timeout=40)
            time.sleep(3)

        # 4. BLOCK (ID = 17163)
        block = str(user_data.get("block", "")).strip()
        if block:
            logging.info(f"📍 Selecting Block = {block} (17163)")
            wait_dropdown_options("17163", timeout=50)
            select_by_text_match("17163", block, timeout=50)
            time.sleep(2)

        # 5. VILLAGE PANCHAYAT RADIO (id="75265_1" or name="75265" value="1")
        try:
            village_radio = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='radio' and @name='75265' and @value='1']"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", village_radio)
            time.sleep(0.5)
            if not village_radio.is_selected():
                try:
                    village_radio.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", village_radio)
            driver.execute_script("""
                arguments[0].checked = true;
                arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
                arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
            """, village_radio)
            logging.info("✅ Village Panchayat radio selected")
        except Exception as e:
            logging.warning(f"⚠️ Village Panchayat radio selection warning (non-fatal): {e}")

        # ============================================================
        # 6. GRAM PANCHAYAT (ACTUAL RTPS ID = 56895)
        # ============================================================
        panchayat = str(user_data.get("panchayat", "")).strip()
        if panchayat:
            logging.info(f"📍 Waiting for Gram Panchayat = {panchayat} (56895)")
            gp = WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((By.ID, "56895"))
            )
            end_time = time.time() + 45
            selected_ok = False

            while time.time() < end_time:
                try:
                    gp = driver.find_element(By.ID, "56895")
                    options = Select(gp).options
                    target = " ".join(panchayat.split()).strip().casefold()

                    for option in options:
                        text = " ".join((option.text or "").split()).strip()
                        if not text or text.casefold() in ("please select", "select"):
                            continue
                        text_norm = text.casefold()

                        if text_norm == target or target in text_norm or text_norm in target:
                            value = (option.get_attribute("value") or "").strip()
                            logging.info(f"🎯 Panchayat option मिला: {text!r}, value={value!r}")
                            select = Select(gp)
                            if value:
                                select.select_by_value(value)
                            else:
                                select.select_by_visible_text(text)

                            driver.execute_script("""
                                arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                            """, gp)
                            time.sleep(1)

                            selected = Select(gp).first_selected_option
                            selected_value = (selected.get_attribute("value") or "").strip()
                            if selected_value and selected_value != "":
                                selected_ok = True
                            break
                    if selected_ok:
                        break
                except StaleElementReferenceException:
                    pass
                except Exception as e:
                    logging.debug(f"Panchayat wait/select: {repr(e)}")
                time.sleep(0.5)

            if not selected_ok:
                raise TimeoutException(f"Gram Panchayat नहीं मिला: {panchayat}")
            logging.info(f"✅ Gram Panchayat selected successfully: {panchayat}")

        # 7. VILLAGE / MOHALLA (ID = 17160)
        village = str(user_data.get("village", "")).strip()
        if village:
            logging.info(f"📍 Filling Village/Mohalla = {village} (17160)")
            find_and_interact(
                driver,
                ["//input[@id='17160']", "//input[@name='17160']"],
                "type",
                village,
                timeout=30,
                chat_id=chat_id
            )

        # 8. WARD NUMBER (ID = 56894)
        ward = str(user_data.get("ward_no", "")).strip()
        if ward:
            logging.info(f"📍 Filling Ward No. = {ward} (56894)")
            find_and_interact(
                driver,
                ["//input[@id='56894']", "//input[@name='56894']"],
                "type",
                ward,
                timeout=30,
                chat_id=chat_id
            )

        # 9. POST OFFICE (ID = 17164)
        post_office = str(user_data.get("post_office", "")).strip()
        if post_office:
            logging.info(f"📍 Filling Post Office = {post_office} (17164)")
            find_and_interact(
                driver,
                ["//input[@id='17164']", "//input[@name='17164']"],
                "type",
                post_office,
                timeout=30,
                chat_id=chat_id
            )

        # 10. POLICE STATION (ID = 65010)
        police_station = str(user_data.get("police_station", "")).strip()
        if police_station:
            logging.info(f"📍 Selecting Police Station = {police_station} (65010)")
            wait_dropdown_options("65010", timeout=45)
            select_by_text_match("65010", police_station, timeout=45)

        # 11. PIN CODE (ID = 90777)
        pin_code = str(user_data.get("pin_code", "")).strip()
        if pin_code:
            logging.info(f"📍 Filling Pin Code = {pin_code} (90777)")
            find_and_interact(
                driver,
                ["//input[@id='90777']", "//input[@name='90777']"],
                "type",
                pin_code,
                timeout=30,
                chat_id=chat_id
            )

        logging.info("✅ ADDRESS SECTION COMPLETED SUCCESSFULLY")

    except Exception as e:
        logging.exception("❌ ADDRESS_FIELDS_ERROR")
        if chat_id:
            send_error_screenshot(chat_id, driver, "❌ ADDRESS_FIELDS_ERROR", e)
        raise

    # Photo Upload in Main Form
    if photo_path and os.path.exists(photo_path):
        try:
            find_and_interact(driver, [
                "//input[@id='17495']",
                "//input[@type='file' and (contains(@id, 'photo') or contains(@name, 'photo'))]"
            ], "file", photo_path, chat_id=chat_id)
        except Exception as e:
            logging.warning(f"Photo upload in main form warning: {e}")

    # ============================================================
    # EXACT RTPS SERVICE FIELDS
    # Profession = 17207
    # Purpose = 17172
    # Govt Income = 17168
    # Agriculture Income = 17171
    # Business Income = 17169
    # Other Income = 17170
    # Total Income = 17173 (readonly)
    # Other Records = 94045_3
    # ============================================================
    try:
        # --------------------------------------------------------
        # PROFESSION — exact RTPS select #17207
        # --------------------------------------------------------
        profession_val = user_data.get("profession")
        if profession_val:
            logging.info(
                f"📍 Selecting Profession = {profession_val!r} "
                f"(ID: 17207)"
            )
            select_rtps_profession(driver, profession_val, timeout=25)
        else:
            raise ValueError("Profession खाली है; RTPS #17207 select नहीं किया जा सकता")

        # --------------------------------------------------------
        # The following fields are part of the INCOME form shown
        # in the supplied RTPS HTML. Only fill them for INCOME.
        # --------------------------------------------------------
        if service_type == "INCOME":
            purpose_val = user_data.get("purpose", "")
            if purpose_val:
                fill_rtps_input_by_id(
                    driver, "17172", purpose_val, timeout=20, chat_id=chat_id
                )

            fill_rtps_input_by_id(
                driver, "17168", user_data.get("income_govt_service", "00"),
                timeout=20, chat_id=chat_id
            )
            fill_rtps_input_by_id(
                driver, "17171", user_data.get("income_agriculture", "00"),
                timeout=20, chat_id=chat_id
            )
            fill_rtps_input_by_id(
                driver, "17169", user_data.get("income_business", "00"),
                timeout=20, chat_id=chat_id
            )
            fill_rtps_input_by_id(
                driver, "17170", user_data.get("income_other_sources", "00"),
                timeout=20, chat_id=chat_id
            )

            # 17173 is readonly. Never type into it; just verify it.
            total_elem = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "17173"))
            )
            total_income = (total_elem.get_attribute("value") or "").strip()
            logging.info(
                f"💰 Total Annual Income (17173 readonly) = {total_income!r}"
            )

            # Other Records checkbox. The supplied HTML marks it required.
            other_records = user_data.get("other_records", True)
            if isinstance(other_records, str):
                other_records = other_records.strip().casefold() in {
                    "true", "1", "yes", "y", "हाँ", "हां"
                }

            if other_records:
                chk = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.ID, "94045_3"))
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", chk
                )
                if not chk.is_selected():
                    try:
                        chk.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", chk)
                if not chk.is_selected():
                    raise RuntimeError(
                        "अन्यान्य अभिलेख checkbox #94045_3 select नहीं हुआ"
                    )
                logging.info(
                    "✅ अन्यान्य अभिलेख selected (ID: 94045_3)"
                )

        # --------------------------------------------------------
        # CASTE form extras
        # --------------------------------------------------------
        if service_type == "CASTE":
            if user_data.get("category"):
                ok = select_location_dropdown(
                    driver, ["Category", "कोटि"],
                    user_data.get("category"), timeout=20
                )
                if not ok:
                    raise RuntimeError("Category select नहीं हुआ")

            if user_data.get("caste"):
                ok = select_location_dropdown(
                    driver, ["Caste", "जाति"],
                    user_data.get("caste"), timeout=20
                )
                if not ok:
                    raise RuntimeError("Caste select नहीं हुआ")

        # --------------------------------------------------------
        # RESIDENCE form extra
        # --------------------------------------------------------
        if service_type == "RESIDENCE" and user_data.get("residence_type"):
            ok = select_location_dropdown(
                driver, ["Residence Type", "निवास का प्रकार"],
                user_data.get("residence_type"), timeout=20
            )
            if not ok:
                raise RuntimeError("Residence Type select नहीं हुआ")

        logging.info("✅ RTPS service-specific fields completed")

    except Exception as e:
        logging.exception("❌ RTPS service-specific fields failed")
        if chat_id:
            send_error_screenshot(
                chat_id, driver,
                "❌ RTPS Service Fields Error", e
            )
        raise

    # ============================================================
    # [REDACTED] NUMBER FILLING (EXACT ID 57339_txt)
    # ============================================================
    aadhaar_number = str(user_data.get("aadhaar_number", "")).strip()
    if aadhaar_number:
        try:
            find_and_interact(
                driver,
                ["//input[@id='57339_txt']", "//input[@name='57339_txt']"],
                "type",
                aadhaar_number,
                timeout=30,
                chat_id=chat_id
            )
            logging.info("✅ [Redacted] number entered via 57339_txt")
        except Exception as e:
            logging.warning(f"[Redacted] number filling warning: {e}")

    # ============================================================
    # EMAIL - SAFE / OPTIONAL
    # ============================================================
    # Some RTPS forms do not contain an Email field at all.
    # Do not make the whole automation fail if it is absent.
    email_value = str(user_data.get("email", "")).strip()
    if email_value:
        try:
            email_xpaths = [
                "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]",
                "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]",
                "//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]",
                "//input[@type='email']",
                "//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]/following::input[1]"
            ]

            email_element = None
            for xp in email_xpaths:
                try:
                    for el in driver.find_elements(By.XPATH, xp):
                        if el.is_displayed() and el.is_enabled():
                            email_element = el
                            break
                    if email_element is not None:
                        break
                except Exception:
                    continue

            if email_element is None:
                logging.info(
                    "ℹ️ Email field इस RTPS form पर उपलब्ध नहीं है; skipped."
                )
            else:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});",
                    email_element
                )
                time.sleep(0.3)
                try:
                    email_element.click()
                except Exception:
                    driver.execute_script(
                        "arguments[0].focus();", email_element
                    )
                email_element.send_keys(Keys.CONTROL, "a")
                email_element.send_keys(email_value)
                logging.info("✅ Email address filled successfully.")

        except Exception as e:
            # Email is optional on this form; continue to Mobile.
            logging.warning(f"⚠️ Email filling skipped: {e}")
    else:
        logging.info("ℹ️ Email JSON में खाली है; skipped.")

    # ============================================================
    # MOBILE NUMBER - LAST FIELD & ENTER TRIGGER
    # ============================================================
    try:
        mobile_xpaths = [
            "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mobile')]",
            "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mobile')]",
            "//label[contains(normalize-space(.),'Mobile No. of Applicant')]/following::input[1]"
        ]

        mobile_element = find_and_interact(
            driver,
            mobile_xpaths,
            action_type="type",
            text_value=user_data["mobile_no"],
            timeout=15,
            chat_id=chat_id
        )
        logging.info("✅ Mobile number filled successfully.")
        time.sleep(0.5)

        try:
            mobile_element.send_keys(Keys.ENTER)
        except Exception:
            driver.execute_script("""
                arguments[0].dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                }));
            """, mobile_element)

        time.sleep(2)

        if detect_mobile_otp_popup(driver, timeout=10):
            logging.info("📱 Mobile OTP popup detected after ENTER.")
            with session_lock:
                if chat_id in active_user_sessions:
                    active_user_sessions[chat_id].update({
                        "is_processing": False,
                        "waiting_for": "MOBILE_OTP"
                    })

            bot.send_message(chat_id, "📱 मोबाइल नंबर पर OTP भेजा गया है। कृपया OTP दर्ज करें:")
            if user_id:
                bot.set_state(user_id, RTPSState.mobile_otp_input, chat_id)
            return "MOBILE_OTP"

    except Exception as e:
        logging.warning(f"⚠️ Mobile number / ENTER trigger warning: {e}")

    # ADDED: final Email + Mobile stage; original block above remains intact.
    try:
        final_result = fill_final_email_mobile_and_wait_for_otp(
            driver, user_data, chat_id=chat_id, user_id=user_id
        )
        if final_result == "MOBILE_OTP":
            return "MOBILE_OTP"
    except Exception as e:
        logging.error("Final Email/Mobile stage failed: %s", e)
        if chat_id:
            send_error_screenshot(chat_id, driver, "Final Email/Mobile Error", e)
        raise

# ============================================================
# FINAL EMAIL + MOBILE STAGE (ADDED)
# ============================================================
def fill_final_email_mobile_and_wait_for_otp(driver, user_data, chat_id=None, user_id=None):
    if chat_id:
        with session_lock:
            if chat_id in active_user_sessions:
                active_user_sessions[chat_id]["defer_email_mobile_until_final"] = False

    if user_data.get("email"):
        email_value = str(user_data["email"]).strip()
        email_xpaths = [
            "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]",
            "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'email')]",
            "//input[@type='email']"
        ]
        find_and_interact(driver, email_xpaths, action_type="type",
                          text_value=email_value, timeout=20, chat_id=chat_id)
        logging.info("Email filled at final stage")

    mobile_xpaths = [
        "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mobile')]",
        "//input[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mobile')]",
        "//label[contains(normalize-space(.),'Mobile No. of Applicant')]/following::input[1]"
    ]
    mobile_element = find_and_interact(
        driver, mobile_xpaths, action_type="type",
        text_value=user_data["mobile_no"], timeout=20, chat_id=chat_id
    )
    logging.info("Mobile number filled at FINAL stage")
    time.sleep(1)

    try:
        mobile_element.send_keys(Keys.ENTER)
    except Exception:
        driver.execute_script("""
            arguments[0].dispatchEvent(new KeyboardEvent('keydown', {
                key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true
            }));
        """, mobile_element)
    time.sleep(2)

    agree_xpaths = [
        "//button[normalize-space()='Agree']",
        "//button[contains(normalize-space(.),'Agree')]",
        "//input[@type='button' and contains(@value,'Agree')]",
        "//input[@type='submit' and contains(@value,'Agree')]",
        "//*[self::button or self::a][contains(normalize-space(.),'Agree')]"
    ]
    try:
        find_and_interact(driver, agree_xpaths, action_type="click", timeout=8, chat_id=chat_id)
        logging.info("Mobile verification Agree clicked")
        time.sleep(2)
    except Exception as e:
        logging.info("Agree popup not found/already handled: %s", e)

    if detect_mobile_otp_popup(driver, timeout=10):
        logging.info("Mobile OTP popup detected after final mobile field")
        with session_lock:
            if chat_id in active_user_sessions:
                active_user_sessions[chat_id].update({
                    "is_processing": False,
                    "waiting_for": "MOBILE_OTP"
                })
        bot.send_message(chat_id, "📱 मोबाइल नंबर पर OTP भेजा गया है। कृपया OTP दर्ज करें:")
        if user_id:
            bot.set_state(user_id, RTPSState.mobile_otp_input, chat_id)
        return "MOBILE_OTP"
    return None


def continue_rtps_form_after_mobile_otp(
    driver, user_data, service_type,
    photo_path, chat_id=None, user_id=None
):
    try:
        driver.switch_to.default_content()
        wait_after_mobile_otp(driver, timeout=20)
        time.sleep(1)

        if chat_id:
            send_step_screenshot(driver, chat_id, "AFTER_MOBILE_OTP")
            bot.send_message(chat_id, "🔄 फॉर्म डिटेल्स सत्यापित हो रही हैं...")

        bot.send_message(chat_id, "✅ फॉर्म फील्ड्स का चरण समाप्त हुआ। अगले वेरिफिकेशन स्टेप की जांच की जा रही है...")
        logging.info("✅ Mobile OTP के बाद form fields भरने का चरण पूरा हुआ")
        return

    except Exception as e:
        logging.exception("Post-mobile-OTP form processing failed")
        if chat_id:
            send_step_screenshot(driver, chat_id, "POST_OTP_FATAL_ERROR")
        raise

# ===================================================================
# Telegram Callback Handler for Next Step
# ===================================================================

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
    
    bot.send_message(chat_id, "➡️ Next Step शुरू किया जा रहा है...")
    route_to_next_step(bot, chat_id, user_id, driver, user_dir)

def session_timeout_cleaner():
    while True:
        time.sleep(30)
        now = time.time()
        to_delete = []

        with session_lock:      
            for chat_id, session in list(active_user_sessions.items()):      
                created_at = session.get("created_at", now)      
                last_activity = session.get("last_activity", now)      
      
                if now - created_at > MAX_SESSION_LIFETIME_SECONDS:      
                    to_delete.append((chat_id, session))      
                    continue      
      
                if session.get("is_processing", False):      
                    if now - last_activity > 600:      
                        to_delete.append((chat_id, session))      
                    continue      
      
                if now - last_activity > SESSION_TIMEOUT_SECONDS:      
                    to_delete.append((chat_id, session))      
      
            for chat_id, session in to_delete:      
                active_user_sessions.pop(chat_id, None)      
      
        for chat_id, session in to_delete:      
            user_id = session.get("user_id", chat_id)      
            try:      
                driver = session.get("driver")      
                if driver:      
                    driver.quit()      
            except Exception:      
                pass      
            cleanup_user_files(chat_id)      
            try:      
                bot.delete_state(chat_id=chat_id, user_id=user_id)      
            except Exception:      
                pass      
            try:      
                bot.send_message(chat_id, "⚠️ सेशन समय समाप्त हो गया! पुनः प्रयास के लिए /start दबाएं।")      
            except Exception:      
                pass

threading.Thread(target=session_timeout_cleaner, daemon=True).start()

def captcha_inline_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "🔄 CAPTCHA Refresh", callback_data="captcha_refresh"
    ))
    return markup


def get_captcha_image_xpaths():
    return [
        "//img[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]",
        "//img[contains(translate(@src,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha')]",
        "//img[contains(@id,'imgCaptcha')]"
    ]


def refresh_captcha_image(driver, timeout=10):
    refresh_xpaths = [
        "//button[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcharefresh')]",
        "//button[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'refreshcaptcha')]",
        "//a[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcharefresh')]",
        "//input[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcharefresh')]",
        "//*[self::button or self::a or self::span or self::img][contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha') and contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'refresh')]",
        "//*[self::button or self::a][contains(translate(@title,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'refresh') and (contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha') or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha'))]",
        "//*[self::button or self::a][contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'refresh') and (contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha') or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha'))]"
    ]
    before_src = None
    try:
        driver.switch_to.default_content()
        for xp in get_captcha_image_xpaths():
            for e in driver.find_elements(By.XPATH, xp):
                if e.is_displayed():
                    before_src = e.get_attribute("src")
                    break
            if before_src is not None:
                break
    except Exception:
        pass

    last_error = None
    for xp in refresh_xpaths:
        try:
            driver.switch_to.default_content()
            visible = [e for e in driver.find_elements(By.XPATH, xp)
                       if e.is_displayed() and e.is_enabled()]
            if not visible:
                continue
            try:
                visible[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", visible[0])
            time.sleep(1.5)

            end = time.time() + timeout
            while time.time() < end:
                for img_xp in get_captcha_image_xpaths():
                    try:
                        for img in driver.find_elements(By.XPATH, img_xp):
                            if img.is_displayed():
                                new_src = img.get_attribute("src")
                                if before_src is None or new_src != before_src:
                                    return True
                    except Exception:
                        pass
                time.sleep(0.3)
            return True
        except Exception as e:
            last_error = e
    logging.warning("CAPTCHA refresh control नहीं मिला: %r", last_error)
    return False


def send_captcha_prompt(bot, chat_id, user_id, driver, user_dir, caption="🧩 CAPTCHA दर्ज करें:"):
    captcha_img_path = os.path.join(user_dir, f"captcha_{int(time.time()*1000)}.png")
    if capture_captcha_image(driver, get_captcha_image_xpaths(), captcha_img_path):
        try:
            with open(captcha_img_path, 'rb') as c_img:
                bot.send_photo(
                    chat_id, c_img, caption=caption,
                    reply_markup=captcha_inline_keyboard()
                )
        finally:
            try:
                if os.path.exists(captcha_img_path):
                    os.remove(captcha_img_path)
            except Exception:
                pass
        bot.set_state(user_id, RTPSState.captcha_input, chat_id)
        return True
    bot.send_message(chat_id, "⚠️ CAPTCHA इमेज लोड नहीं हो सकी।")
    return False


@bot.callback_query_handler(func=lambda call: call.data == "captcha_refresh")
def captcha_refresh_callback(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    if not user_is_allowed(user_id):
        bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।")
        return
    with session_lock:
        session = active_user_sessions.get(chat_id)
    if not session:
        bot.answer_callback_query(call.id, "⚠️ सेशन समाप्त हो गया है।")
        return
    driver = session.get("driver")
    user_dir = get_user_dir(chat_id)
    try:
        bot.answer_callback_query(call.id, "🔄 CAPTCHA refresh हो रहा है...")
        session["last_activity"] = time.time()
        if not refresh_captcha_image(driver, timeout=8):
            bot.send_message(chat_id, "⚠️ CAPTCHA refresh button नहीं मिला।")
            return
        send_captcha_prompt(
            bot, chat_id, user_id, driver, user_dir,
            caption="🔄 नया CAPTCHA आ गया है। इसे दर्ज करें:"
        )
    except Exception as e:
        logging.exception("CAPTCHA refresh failed")
        bot.send_message(chat_id, f"❌ CAPTCHA refresh में समस्या: {e}")
        send_error_screenshot(chat_id, driver, "CAPTCHA Refresh Error", e)


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
            # ADDED: Telegram CAPTCHA screenshot with Refresh button.
            if send_captcha_prompt(
                bot, chat_id, user_id, driver, user_dir,
                caption="🧩 CAPTCHA दर्ज करें (गलत हो तो नीचे Refresh दबाएँ):"
            ):
                return
            # Original CAPTCHA handling remains below as fallback.
            captcha_img_path = os.path.join(user_dir, "captcha.png")      
            captcha_xpaths = ["//img[contains(@id, 'captcha') or contains(@src, 'captcha') or contains(@id, 'imgCaptcha')]"]      
            if capture_captcha_image(driver, captcha_xpaths, captcha_img_path):      
                with open(captcha_img_path, 'rb') as c_img:      
                    bot.send_photo(chat_id, c_img, caption="🧩 इमेज में दिख रहा CAPTCHA दर्ज करें:")      
                bot.set_state(user_id, RTPSState.captcha_input, chat_id)      
            else:      
                bot.send_message(chat_id, "⚠️ CAPTCHA इमेज लोड नहीं हो सकी।")      
            return      
      
        elif step == "AADHAAR_OTP":      
            bot.send_message(chat_id, "🔐 [Redacted] OTP दर्ज करें:")      
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
    else:      
        bot.send_message(chat_id, "⚠️ वर्तमान चरण की स्पष्ट पहचान नहीं हो सकी। कृपया पुनः प्रयास करें।")

# ============================================================
# ADMIN USER MANAGEMENT PANEL & HANDLERS
# ============================================================

def admin_keyboard():
    markup = telebot.types.InlineKeyboardMarkup(
        row_width=2
    )

    markup.add(
        telebot.types.InlineKeyboardButton(
            "➕ Add User",
            callback_data="adm_add"
        ),
        telebot.types.InlineKeyboardButton(
            "➖ Remove User",
            callback_data="adm_remove"
        )
    )

    markup.add(
        telebot.types.InlineKeyboardButton(
            "👥 User List",
            callback_data="adm_list"
        )
    )

    return markup


@bot.message_handler(commands=["admin"])
def admin_cmd(message):

    if not is_admin(message.from_user.id):
        bot.reply_to(
            message,
            "❌ केवल Admin इस panel का उपयोग कर सकता है।"
        )
        return

    bot.send_message(
        message.chat.id,
        "👑 *ADMIN PANEL*\n\n"
        "यहाँ से bot users को Add/Remove कर सकते हैं।",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("adm_")
)
def admin_callback(call):

    if not is_admin(call.from_user.id):

        bot.answer_callback_query(
            call.id,
            "❌ Admin access required.",
            show_alert=True
        )

        return

    if call.data == "adm_add":

        bot.answer_callback_query(call.id)

        msg = bot.send_message(
            call.message.chat.id,
            "➕ *Add User*\n\n"
            "User का Telegram numeric ID भेजें:\n\n"
            "Example:\n"
            "`123456789`",
            parse_mode="Markdown"
        )

        bot.register_next_step_handler(
            msg,
            admin_add_user
        )

        return

    if call.data == "adm_remove":

        bot.answer_callback_query(call.id)

        msg = bot.send_message(
            call.message.chat.id,
            "➖ *Remove User*\n\n"
            "जिस User ID को remove करना है वह भेजें:",
            parse_mode="Markdown"
        )

        bot.register_next_step_handler(
            msg,
            admin_remove_user
        )

        return

    if call.data == "adm_list":

        bot.answer_callback_query(call.id)

        env_users = sorted(
            ALLOWED_USER_IDS
        )

        runtime_users = sorted(
            RUNTIME_ALLOWED_USER_IDS
        )

        text = "👥 *BOT USERS*\n\n"

        text += "📌 `.env` Users:\n"

        if env_users:
            for uid in env_users:
                text += f"• `{uid}`\n"
        else:
            text += "None\n"

        text += "\n📌 Admin Panel Users:\n"

        if runtime_users:
            for uid in runtime_users:
                text += f"• `{uid}`\n"
        else:
            text += "None\n"

        bot.send_message(
            call.message.chat.id,
            text,
            parse_mode="Markdown"
        )

        return


def admin_add_user(message):

    if not is_admin(message.from_user.id):
        return

    raw_id = (
        message.text or ""
    ).strip()

    if not raw_id.isdigit():

        bot.reply_to(
            message,
            "❌ Invalid User ID.\n"
            "केवल numeric Telegram ID भेजें।"
        )

        return

    user_id = int(raw_id)

    if user_id in ALLOWED_USER_IDS:

        bot.reply_to(
            message,
            f"ℹ️ User `{user_id}` पहले से `.env` में allowed है।",
            parse_mode="Markdown"
        )

        return

    add_allowed_user(user_id)

    bot.reply_to(
        message,
        f"✅ User `{user_id}` successfully added.\n\n"
        "अब यह user bot चला सकता है।",
        parse_mode="Markdown"
    )


def admin_remove_user(message):

    if not is_admin(message.from_user.id):
        return

    raw_id = (
        message.text or ""
    ).strip()

    if not raw_id.isdigit():

        bot.reply_to(
            message,
            "❌ Invalid User ID.\n"
            "केवल numeric Telegram ID भेजें।"
        )

        return

    user_id = int(raw_id)

    if user_id in ALLOWED_USER_IDS:

        bot.reply_to(
            message,
            f"⚠️ `{user_id}` `.env` में मौजूद है。\n\n"
            "इसे Admin Panel से permanently remove नहीं किया जाएगा।\n"
            "इसके लिए `.env` से ID हटानी होगी।",
            parse_mode="Markdown"
        )

        return

    removed = remove_allowed_user(user_id)

    if removed:

        bot.reply_to(
            message,
            f"✅ User `{user_id}` remove कर दिया गया।",
            parse_mode="Markdown"
        )

    else:

        bot.reply_to(
            message,
            f"❌ User `{user_id}` runtime list में नहीं मिला।",
            parse_mode="Markdown"
        )


@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    try:  
        bot.delete_state(user_id=user_id, chat_id=chat_id)  
    except Exception:  
        pass  

    if not user_is_allowed(user_id):      
        bot.send_message(
            chat_id, 
            f"❌ आप इस bot का उपयोग करने के लिए authorized नहीं हैं。\n"
            f"👤 अपना Telegram User ID: `{user_id}`\n"
            f"📞 कृपया Admin से permission लें।",
            parse_mode="Markdown"
        )      
        return      
      
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)      
    markup.add("RESIDENCE", "CASTE", "INCOME")      
    bot.send_message(chat_id, "RTPS बिहार ऑटोमेशन बोट में आपका स्वागत है। कृपया अपनी सेवा चुनें:", reply_markup=markup)      
    bot.set_state(user_id, RTPSState.service_type, chat_id)

@bot.message_handler(state=RTPSState.service_type)
def process_service_type(message):
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
        return

    service = message.text.upper()      
    if service not in ["RESIDENCE", "CASTE", "INCOME"]:      
        bot.reply_to(message, "कृपया केवल RESIDENCE, CASTE या INCOME में से ही विकल्प चुनें।")      
        return      
      
    with bot.retrieve_data(user_id, message.chat.id) as data:      
        data['service_type'] = service      
      
    templates = {      
        "RESIDENCE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_ID",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "residence_type": "स्थायी"\n}',      
        "CASTE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_ID",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "छात्र / Student",\n  "purpose": "स्कूल",\n  "category": "अत्यंत पिछड़ा वर्ग (अनुसूची-1)",\n  "caste": "जाति"\n}',      
        "INCOME": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "husband_name": "",\n  "aadhaar_number": "YOUR_ID",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "छात्र / Student",\n  "purpose": "स्कूल",\n  "income_govt_service": "00",\n  "income_agriculture": "60000",\n  "income_business": "00",\n  "income_other_sources": "24000",\n  "annual_income": "84000",\n  "other_records": true\n}'      
    }      
      
    bot.send_message(message.chat.id, f"सर्विस '{service}' चुनी गई। विवरण नीचे दिए गए JSON प्रारूप में भरें ([Redacted] number सहित):\n\n`{templates[service]}`", parse_mode="Markdown")      
    bot.set_state(user_id, RTPSState.user_details, message.chat.id)

@bot.message_handler(state=RTPSState.user_details)
def process_user_details(message):
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
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
            gemini_pending = bool(data.get("gemini_review_pending"))
            saved_photo = data.get("photo_path")
            saved_doc = data.get("doc_path")

        if gemini_pending and saved_photo and saved_doc:
            with bot.retrieve_data(user_id, message.chat.id) as data:
                data["gemini_review_pending"] = False
            bot.send_message(message.chat.id, "✅ Gemini JSON सत्यापित हुआ। अब RTPS automation शुरू किया जा रहा है...")
            start_saved_rtps_automation(message.chat.id, user_id)
            return
      
        bot.send_message(message.chat.id, "विवरण सत्यापित हुआ। अब अपनी फोटो (JPG/PNG) भेजें:")      
        bot.set_state(user_id, RTPSState.photo_upload, message.chat.id)      
    except json.JSONDecodeError:      
        bot.reply_to(message, "❌ अमान्य JSON प्रारूप। कृपया सही JSON भेजें।")      
    except Exception as e:      
        logging.error(f"Error in process_user_details: {e}")      
        bot.reply_to(message, "❌ डेटा प्रोसेस करने में त्रुटि हुई।")

@bot.message_handler(content_types=['photo'], state=RTPSState.photo_upload)
def process_photo_upload(message):
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
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

def start_saved_rtps_automation(chat_id, user_id):
    """ADDED: start existing automation after Gemini JSON review."""
    driver = None
    user_dir = get_user_dir(chat_id)
    try:
        with bot.retrieve_data(user_id, chat_id) as data:
            user_data = data['user_data']
            service_type = data['service_type']
            photo_path = data['photo_path']
            doc_path = data.get('doc_path', '')

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
                "aadhaar_otp_attempts": 0,
                "defer_email_mobile_until_final": True
            }

        res = fill_initial_rtps_form(
            driver, user_data, service_type, photo_path,
            chat_id=chat_id, user_id=user_id
        )
        if res == "MOBILE_OTP":
            return

        with session_lock:
            if chat_id in active_user_sessions:
                active_user_sessions[chat_id]["is_processing"] = False

        if driver and chat_id in active_user_sessions:
            session_info = active_user_sessions.get(chat_id)
            if not session_info or session_info.get("waiting_for") != "MOBILE_OTP":
                route_to_next_step(bot, chat_id, user_id, driver, user_dir)

    except Exception as e:
        logging.exception("Saved RTPS automation failed")
        bot.send_message(chat_id, f"❌ Automation में समस्या आई: {e}")
        if driver:
            send_error_screenshot(chat_id, driver, "Automation Critical Error", e)
        with session_lock:
            session = active_user_sessions.get(chat_id)
        if session:
            finish_and_cleanup_session(chat_id, session)


@bot.message_handler(content_types=['photo', 'document'], state=RTPSState.document_upload)
def process_document_upload(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
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
      
        if file_info.file_size and file_info.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:      
            bot.send_message(chat_id, "❌ दस्तावेज़ का आकार बहुत बड़ा है।")      
            return      
      
        _, ext = os.path.splitext(file_info.file_path)      
        ext = ext.lower()      
        if ext not in ALLOWED_DOC_EXTS:      
            bot.send_message(chat_id, "❌ अमान्य दस्तावेज़ फॉर्मेट।")      
            return      
      
        doc_path = os.path.join(user_dir, f"doc{ext}")      
        downloaded = bot.download_file(file_info.file_path)      
        with open(doc_path, 'wb') as f:      
            f.write(downloaded)      
      
        valid, err_msg = validate_file_content_and_extension(doc_path, ALLOWED_DOC_EXTS)      
        if not valid:      
            bot.send_message(chat_id, f"❌ दस्तावेज़ त्रुटि: {err_msg}")      
            return      
      
        # ========================================================
        # ADDED: GEMINI DOCUMENT -> JSON -> USER REVIEW
        # Set GEMINI_AUTO_EXTRACT=false to keep only the old flow.
        # ========================================================
        if os.getenv("GEMINI_AUTO_EXTRACT", "true").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                with bot.retrieve_data(user_id, chat_id) as data:
                    gemini_service_type = data.get("service_type")
                    data["doc_path"] = doc_path

                gemini_data = extract_rtps_data_with_gemini(
                    doc_path, gemini_service_type
                )

                with bot.retrieve_data(user_id, chat_id) as data:
                    data["gemini_data"] = gemini_data
                    data["gemini_review_pending"] = True

                bot.send_message(
                    chat_id,
                    "🤖 Document से JSON तैयार हो गया है।\n\n"
                    "👇 इसे पूरा copy करके इसी bot में paste करें:"
                )
                bot.send_message(
                    chat_id,
                    json.dumps(gemini_data, ensure_ascii=False, indent=2)
                )
                bot.set_state(user_id, RTPSState.user_details, chat_id)
                bot.send_message(chat_id, "✍️ ऊपर वाला JSON paste करें।")
                return

            except Exception as e:
                logging.exception("Gemini document extraction failed")
                bot.send_message(
                    chat_id,
                    f"⚠️ Gemini extraction नहीं हो सकी: {e}\n\n"
                    "➡️ Existing RTPS automation सामान्य तरीके से जारी रहेगी।"
                )

        bot.send_message(chat_id, "⚙️ RTPS पोर्टल पर ऑटोमेशन शुरू किया जा रहा है...")      
      
        with bot.retrieve_data(user_id, chat_id) as data:      
            user_data = data['user_data']      
            service_type = data['service_type']      
            photo_path = data['photo_path']      
      
        try:      
            driver, download_dir = get_chrome_driver(chat_id)      
        except Exception as e:      
            logging.error(f"Chrome driver initialization error in chat {chat_id}: {e}")      
            bot.send_message(chat_id, f"❌ ब्राउज़र इनिशियलाइज़ेशन विफल हुआ: {e}")      
            return      
      
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
                "aadhaar_otp_attempts": 0,
                "defer_email_mobile_until_final": True
            }      
      
        try:  
            res = fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=chat_id, user_id=user_id)      
            if res == "MOBILE_OTP":  
                return  
        except Exception as e:  
            logging.error(f"Automation critical error in chat {chat_id}: {e}")  
            bot.send_message(  
                chat_id,   
                "❌ Automation में समस्या आई। ऊपर भेजे गए screenshot में देखें कि कहाँ अटके हैं।"  
            )  
            session = active_user_sessions.get(chat_id)  
            if session:  
                finish_and_cleanup_session(chat_id, session)  
            return  
              
        with session_lock:      
            if chat_id in active_user_sessions:      
                active_user_sessions[chat_id]["is_processing"] = False      
      
    except Exception as e:      
        logging.error(f"Automation error in chat {chat_id}: {e}")      
        bot.send_message(chat_id, f"❌ ऑटोमेशन में त्रुटि:\n{e}")      
          
        active_driver = None  
        with session_lock:  
            s_obj = active_user_sessions.get(chat_id)  
            if s_obj:  
                active_driver = s_obj.get("driver")  
        send_error_screenshot(chat_id, active_driver, "❌ Automation Critical Error", e)  
          
        with session_lock:      
            session = active_user_sessions.get(chat_id)      
        if session:      
            finish_and_cleanup_session(chat_id, session)      
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
    if not user_is_allowed(user_id):
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
        session["last_activity"] = time.time()      
      
    driver = session["driver"]      
    user_dir = get_user_dir(chat_id)      
      
    try:      
        try:      
            driver.switch_to.default_content()      
        except Exception:      
            pass      
          
        otp_xpaths = get_mobile_otp_xpaths()      
        time.sleep(1.5)      

        find_and_interact(      
            driver,      
            otp_xpaths,      
            action_type="type",      
            text_value=otp_code,      
            timeout=20,      
            chat_id=chat_id      
        )      
        logging.info("✅ Mobile OTP successfully entered")      
          
        validate_btn_xpaths = [      
            "//button[@id='btnValidateOtp']",
            "//input[@id='btnValidateOtp']",
            "//button[contains(normalize-space(.),'Validate')]",      
            "//button[contains(normalize-space(.),'Verify')]",      
            "//button[contains(normalize-space(.),'सत्यापित')]",      
            "//input[@type='button' and contains(@value,'Validate')]",      
            "//input[@type='submit' and contains(@value,'Validate')]"      
        ]      
        
        time.sleep(1)
        find_and_interact(      
            driver,      
            validate_btn_xpaths,      
            action_type="click",      
            timeout=20,      
            chat_id=chat_id      
        )      
        logging.info("✅ Mobile OTP Validate button clicked")      
          
        otp_verified_successfully = False
        try:
            WebDriverWait(driver, 8).until(EC.alert_is_present())
            alert = driver.switch_to.alert
            alert_text = alert.text.strip()
            if "verified" in alert_text.lower():
                otp_verified_successfully = True
            alert.accept()
        except Exception:
            pass

        wait_after_mobile_otp(driver, timeout=15)
        time.sleep(3) 

        dom_result = check_mobile_otp_result(driver, timeout=5)
        
        if not otp_verified_successfully and dom_result == "INVALID":
            otp_result = "INVALID"
        elif otp_verified_successfully or dom_result == "SUCCESS":
            otp_result = "SUCCESS"
        else:
            otp_result = dom_result

        if otp_result == "INVALID":  
            session["mobile_otp_attempts"] += 1  
            session["is_processing"] = False  
            session["last_activity"] = time.time()  
            attempts = session["mobile_otp_attempts"]  
            if attempts >= MAX_OTP_ATTEMPTS:  
                bot.send_message(chat_id, f"❌ अधिकतम मोबाइल OTP प्रयास ({MAX_OTP_ATTEMPTS}) समाप्त हुए।")  
                finish_and_cleanup_session(chat_id, session)  
                return  
            bot.send_message(chat_id, f"❌ OTP गलत है। पुनः OTP दर्ज करें (प्रयास {attempts}/{MAX_OTP_ATTEMPTS}):")  
            return  

        session["is_processing"] = False  
        session["last_activity"] = time.time()  
        bot.send_message(chat_id, "✅ Mobile OTP सत्यापित हो गया।")  
          
        with bot.retrieve_data(user_id, chat_id) as data:      
            user_data = data["user_data"]      
            service_type = data["service_type"]      
            photo_path = data["photo_path"]      
          
        continue_rtps_form_after_mobile_otp(      
            driver, user_data, service_type, photo_path, chat_id=chat_id, user_id=user_id      
        )      
          
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)      
    except Exception as e:      
        session["is_processing"] = False      
        session["last_activity"] = time.time()      
        logging.exception("❌ MOBILE OTP PROCESSING FAILED: %r", e)
        bot.send_message(chat_id, f"❌ Mobile OTP के बाद error आया:\n{e}")
        send_error_screenshot(chat_id, driver, "❌ Mobile OTP Error", e)

@bot.message_handler(state=RTPSState.email_otp_input)
def process_email_otp_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
        return

    otp_code = message.text.strip()      
    if not re.fullmatch(r"\d{4,8}", otp_code):      
        bot.reply_to(message, "❌ अमान्य OTP फॉर्मेट।")      
        return      
      
    with session_lock:      
        session = active_user_sessions.get(chat_id)      
        if not session:      
            bot.delete_state(chat_id=chat_id, user_id=user_id)      
            return      
        session["is_processing"] = True      
        session["last_activity"] = time.time()      
      
    driver = session["driver"]      
    user_dir = get_user_dir(chat_id)      
      
    try:      
        find_and_interact(driver, ["//input[@id='email_otp' or contains(@id, 'txtEmailOtp')]"], "type", otp_code, chat_id=chat_id)      
        find_and_interact(driver, ["//button[contains(text(), 'Verify') or contains(text(), 'सत्यापित')]", "//input[@value='Validate']"], chat_id=chat_id)      
        time.sleep(2)      
          
        step = detect_current_verification_step(driver, timeout=5)      
        if step == "EMAIL_OTP":      
            session["email_otp_attempts"] += 1      
            session["is_processing"] = False      
            if session["email_otp_attempts"] >= MAX_OTP_ATTEMPTS:      
                bot.send_message(chat_id, f"❌ अधिकतम ईमेल OTP प्रयास समाप्त हुए।")      
                finish_and_cleanup_session(chat_id, session)      
                return      
            bot.send_message(chat_id, f"❌ अमान्य ईमेल OTP! पुनः दर्ज करें:")      
            return      
              
        session["is_processing"] = False      
        bot.send_message(chat_id, "✅ ईमेल OTP सत्यापित हुआ।")      
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)      
    except Exception as e:      
        session["is_processing"] = False      
        logging.error(f"Email OTP processing error: {e}")      
        bot.send_message(chat_id, f"❌ ईमेल OTP सत्यापन प्रक्रिया में त्रुटि:\n{e}")      
        send_error_screenshot(chat_id, driver, "❌ Email OTP Error", e)

@bot.message_handler(state=RTPSState.captcha_input)
def process_captcha_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
        return

    captcha_text = message.text.strip()      
    with session_lock:      
        session = active_user_sessions.get(chat_id)      
        if not session:      
            bot.delete_state(chat_id=chat_id, user_id=user_id)      
            return      
        session["is_processing"] = True      
        session["last_activity"] = time.time()      
      
    driver = session["driver"]      
    user_dir = get_user_dir(chat_id)      
      
    try:      
        find_and_interact(driver, ["//input[@id='captcha' or contains(@id, 'txtCaptcha') or contains(@name, 'captcha')]"], "type", captcha_text, chat_id=chat_id)      
        
        process_btn_xpaths = [
            "//button[contains(normalize-space(.),'Process') or contains(normalize-space(.),'प्रक्रिया') or contains(text(),'Proceed') or contains(text(),'आगे बढ़ें')]",
            "//input[@value='Process' or @value='Proceed' or contains(@id, 'btnProcess')]"
        ]
        find_and_interact(driver, process_btn_xpaths, action_type="click", timeout=15, chat_id=chat_id)      
      
        time.sleep(2)      
        step = detect_current_verification_step(driver, timeout=5)      
      
        if step == "CAPTCHA":      
            session["captcha_attempts"] += 1      
            session["is_processing"] = False      
            if session["captcha_attempts"] >= MAX_CAPTCHA_ATTEMPTS:      
                bot.send_message(chat_id, f"❌ अधिकतम CAPTCHA प्रयास समाप्त हुए।")      
                finish_and_cleanup_session(chat_id, session)      
                return      
      
            captcha_img_path = os.path.join(user_dir, "captcha_retry.png")      
            if capture_captcha_image(driver, ["//img[contains(@id, 'captcha') or contains(@src, 'captcha')]"], captcha_img_path):      
                with open(captcha_img_path, 'rb') as c_img:      
                    bot.send_photo(chat_id, c_img, caption="❌ गलत CAPTCHA! पुनः दर्ज करें:", reply_markup=captcha_inline_keyboard())      
            return      
      
        session["is_processing"] = False      
        bot.send_message(chat_id, "✅ CAPTCHA सत्यापित और Process हुआ।")      
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)      
    except Exception as e:      
        session["is_processing"] = False      
        logging.error(f"Captcha processing error: {e}")      
        bot.send_message(chat_id, f"❌ CAPTCHA प्रक्रिया में त्रुटि:\n{e}")      
        send_error_screenshot(chat_id, driver, "❌ Captcha Error", e)

@bot.message_handler(state=RTPSState.aadhaar_otp_input)
def process_aadhaar_otp_input(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not user_is_allowed(user_id):
        return

    otp_code = message.text.strip()      
    if not re.fullmatch(r"\d{4,8}", otp_code):      
        bot.reply_to(message, "❌ अमान्य OTP फॉर्मेट।")      
        return      
      
    with session_lock:      
        session = active_user_sessions.get(chat_id)      
        if not session:      
            bot.delete_state(chat_id=chat_id, user_id=user_id)      
            return      
        session["is_processing"] = True      
        session["last_activity"] = time.time()      
      
    execute_final_submission_internal(chat_id, session, otp_code)

def execute_final_submission_internal(chat_id, session, otp_code):
    if not user_locks[chat_id].acquire(blocking=False):
        bot.send_message(chat_id, "⚠️ सबमिशन प्रक्रिया पहले से जारी है...")
        return

    driver = session["driver"]      
    doc_path = session["doc_path"]      
    download_dir = session["download_dir"]      
    should_cleanup = True      
      
    try:      
        wait = WebDriverWait(driver, 20)      
        if otp_code:      
            find_and_interact(driver, ["//input[@id='aadhaar_otp' or contains(@id, 'txtAadhaarOtp')]"], "type", otp_code, chat_id=chat_id)      
            find_and_interact(driver, ["//button[contains(text(),'Validate') or contains(text(),'सत्यापित')]", "//input[@value='Validate']"], chat_id=chat_id)      
            time.sleep(3)      
              
            step = detect_current_verification_step(driver, timeout=3)      
            if step == "AADHAAR_OTP":      
                session["aadhaar_otp_attempts"] += 1      
                session["is_processing"] = False      
                if session["aadhaar_otp_attempts"] >= MAX_OTP_ATTEMPTS:      
                    bot.send_message(chat_id, f"❌ अधिकतम OTP प्रयास समाप्त हुए।")      
                else:      
                    bot.send_message(chat_id, f"❌ अमान्य OTP! पुनः दर्ज करें:")      
                    should_cleanup = False      
                return      
      
        try:      
            find_and_interact(driver, ["//input[@value='Submit' or contains(@value, 'Final Submit') or contains(@id, 'btnSubmit')]"], chat_id=chat_id)      
            time.sleep(3)      
        except Exception as e:      
            logging.warning(f"Final Submit button warning: {e}")      

        initial_files = set(os.listdir(download_dir))      
        try:      
            attach_xpaths = [
                "//input[@value='Attach Annexure']",
                "//input[contains(@id, 'btnAttach')]",
                "//button[contains(normalize-space(.), 'Attach Annexure')]"
            ]
            find_and_interact(driver, attach_xpaths, action_type="click", timeout=15, chat_id=chat_id)
              
            find_and_interact(driver, [      
                "//div[contains(@id, 'annexure')]//input[@type='file']",      
                "//table//input[@type='file']",      
                "//form//input[@type='file']"      
            ], "file", doc_path, chat_id=chat_id)      
              
            find_and_interact(driver, ["//input[@value='Save Annexure' or contains(@id, 'btnSave')]"], chat_id=chat_id)      
            time.sleep(2)      
        except Exception as e:      
            logging.warning(f"Annexure attachment step warning: {e}")      
      
        if verify_submission_status(driver, timeout=15):      
            bot.send_message(chat_id, "✅ RTPS फॉर्म सबमिट हो गया है। पावती रसीद डाउनलोड हो रही है...")      
        else:      
            bot.send_message(chat_id, "⚠️ सबमिशन की पुष्टि की जा रही है...")      
      
        current_handles = driver.window_handles      
        try:      
            find_and_interact(driver, [      
                "//button[contains(text(),'Export to PDF') or contains(text(),'पहुंच रसीद')]",      
                "//a[contains(text(),'Export to PDF') or contains(@href, 'pdf')]"      
            ], chat_id=chat_id)      
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
        logging.error(f"Submission execution error in chat {chat_id}: {e}")      
        bot.send_message(chat_id, f"❌ सबमिशन प्रक्रिया में त्रुटि हुई:\n{e}")      
        send_error_screenshot(chat_id, driver, "❌ Final Submission Error", e)  
    finally:      
        if should_cleanup:      
            finish_and_cleanup_session(chat_id, session)      
        if user_locks[chat_id].locked():      
            user_locks[chat_id].release()

def finish_and_cleanup_session(chat_id, session):
    user_id = session.get("user_id", chat_id)
    with session_lock:
        active_user_sessions.pop(chat_id, None)

    try:      
        session["driver"].quit()      
    except Exception:      
        pass      
      
    cleanup_user_files(chat_id)      
    try:      
        bot.delete_state(chat_id=chat_id, user_id=user_id)      
    except Exception:      
        pass

def cleanup_all_sessions():
    with session_lock:
        for chat_id, session in list(active_user_sessions.items()):
            try:
                session["driver"].quit()
            except Exception:
                pass
            cleanup_user_files(chat_id)
        active_user_sessions.clear()

atexit.register(cleanup_all_sessions)

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
