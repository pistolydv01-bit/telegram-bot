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
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException    

# -------------------------------------------------------------------    
# Safe Logging & Sensitive Data Masking Helper    
# -------------------------------------------------------------------    
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
ALLOWED_USER_IDS_ENV = os.getenv("ALLOWED_USER_IDS", "")    
    
# Updated Chat ID / User ID
ALLOWED_USER_IDS = {6874667015}    
if ALLOWED_USER_IDS_ENV.strip():    
    try:    
        for uid in ALLOWED_USER_IDS_ENV.split(","):
            if uid.strip().isdigit():
                ALLOWED_USER_IDS.add(int(uid.strip()))
    except Exception:    
        pass    
    
if not TELEGRAM_TOKEN:    
    raise ValueError("❌ Error: TELEGRAM_TOKEN .env फ़ाइल में नहीं मिला!")    
    
state_storage = StateMemoryStorage()    
bot = telebot.TeleBot(TELEGRAM_TOKEN, state_storage=state_storage)    
bot.add_custom_filter(custom_filters.StateFilter(bot))

SESSION_TIMEOUT_SECONDS = 900       
MAX_SESSION_LIFETIME_SECONDS = 2700 
MAX_CAPTCHA_ATTEMPTS = 3    
MAX_OTP_ATTEMPTS = 3    
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
        "father_name", "mobile_no", "pin_code"    
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
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    chrome_options.add_argument("--remote-debugging-port=9222")    
    
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

def wait_for_dropdown_options(driver, element_by, locator_value, minimum=2, timeout=20):    
    def _check_options(d):    
        try:    
            elem = d.find_element(element_by, locator_value)    
            select = Select(elem)    
            return len(select.options) >= minimum    
        except Exception:    
            return False    
    WebDriverWait(driver, timeout).until(_check_options)    
    
def select_dropdown_robustly(driver, element_by, locator_value, target_text, timeout=20, min_options=1):    
    if not target_text:    
        return    
        
    try:    
        wait_for_dropdown_options(driver, element_by, locator_value, minimum=min_options, timeout=timeout)    
    except Exception:    
        logging.warning(f"Dropdown options load timeout for {locator_value}")    
        
    wait = WebDriverWait(driver, timeout)    
    target_clean = str(target_text).strip().lower()    
    
    end_time = time.time() + timeout    
    while time.time() < end_time:    
        try:    
            element = wait.until(EC.element_to_be_clickable((element_by, locator_value)))    
            select = Select(element)    
            if len(select.options) >= 1:    
                for opt in select.options:    
                    text = opt.text.strip().lower()    
                    value = (opt.get_attribute("value") or "").strip().lower()    
                    if text == target_clean or value == target_clean:    
                        select.select_by_visible_text(opt.text)    
                        time.sleep(0.5)    
                        return    
                for opt in select.options:    
                    text = opt.text.strip().lower()    
                    value = (opt.get_attribute("value") or "").strip().lower()    
                    if target_clean in text or target_clean in value:    
                        select.select_by_visible_text(opt.text)    
                        time.sleep(0.5)    
                        return    
        except (StaleElementReferenceException, NoSuchElementException):    
            time.sleep(0.5)    
            continue    
        time.sleep(0.5)    
    logging.warning(f"ड्रॉपडाउन में विकल्प '{target_text}' चुनने में विफलता।")

def check_and_switch_iframe(driver, xpaths):
    try:
        driver.switch_to.default_content()
        iframes = driver.find_elements(By.TAG_NAME, "iframe") + driver.find_elements(By.TAG_NAME, "frame")
        
        for index, frame in enumerate(iframes):
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                
                for xp in xpaths:
                    elems = driver.find_elements(By.XPATH, xp)
                    if elems and len(elems) > 0:
                        logging.info(f"✅ Target element found inside iFrame index [{index}]")
                        return True
            except Exception:
                continue
    except Exception as e:
        logging.warning(f"iFrame search error: {e}")
        
    driver.switch_to.default_content()
    return False

def find_and_interact(driver, xpaths, action_type="click", text_value=None, timeout=20):    
    last_err = None    
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        for xp in xpaths:    
            try:    
                elems = driver.find_elements(By.XPATH, xp)
                if elems and any(e.is_displayed() for e in elems):
                    elem = [e for e in elems if e.is_displayed()][0]
                    
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                    time.sleep(0.3)

                    if action_type == "click":    
                        try:
                            elem.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", elem)
                        return elem    
                    elif action_type == "type":    
                        try:
                            elem.clear()
                        except Exception:
                            pass
                        elem.send_keys(str(text_value))    
                        elem.send_keys(Keys.TAB)    
                        return elem    
                    elif action_type == "file":    
                        elem.send_keys(os.path.abspath(text_value))    
                        return elem    
            except Exception as e:    
                last_err = e    
                continue    
        time.sleep(1)

    logging.info("🔎 Searching inside iFrames...")
    if check_and_switch_iframe(driver, xpaths):
        for xp in xpaths:
            elems = driver.find_elements(By.XPATH, xp)
            if elems and any(e.is_displayed() for e in elems):
                elem = [e for e in elems if e.is_displayed()][0]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                
                if action_type == "click":
                    driver.execute_script("arguments[0].click();", elem)
                    return elem
                elif action_type == "type":
                    try:
                        elem.clear()
                    except Exception:
                        pass
                    elem.send_keys(str(text_value))
                    elem.send_keys(Keys.TAB)
                    return elem
                elif action_type == "file":
                    elem.send_keys(os.path.abspath(text_value))
                    return elem

    try:
        debug_pic = os.path.join(os.getcwd(), "error_screenshot.png")
        driver.save_screenshot(debug_pic)
        logging.error(f"Saved debug screenshot to {debug_pic}")
    except Exception:
        pass

    raise NoSuchElementException(
        f"Selector त्रुटि: {timeout} सेकंड में कोई भी XPath नहीं मिला। "
        f"Last error: {last_err}"
    )

def detect_current_verification_step(driver, timeout=6):    
    start_time = time.time()    
    while time.time() - start_time < timeout:    
        try:    
            m_otp = driver.find_elements(By.XPATH, "//input[@id='mobile_otp' or contains(@id, 'txtMobileOtp') or contains(@name, 'mobileOtp')]")    
            if m_otp and any(e.is_displayed() for e in m_otp):    
                return "MOBILE_OTP"    
    
            c_img = driver.find_elements(By.XPATH, "//img[contains(@id, 'captcha') or contains(@src, 'captcha') or contains(@id, 'imgCaptcha')]")    
            c_inp = driver.find_elements(By.XPATH, "//input[@id='captcha' or contains(@id, 'txtCaptcha') or contains(@name, 'captcha')]")    
            if (c_img and any(e.is_displayed() for e in c_img)) or (c_inp and any(e.is_displayed() for e in c_inp)):    
                return "CAPTCHA"    
    
            a_otp = driver.find_elements(By.XPATH, "//input[@id='aadhaar_otp' or contains(@id, 'txtAadhaarOtp') or contains(@name, 'aadhaarOtp')]")    
            if a_otp and any(e.is_displayed() for e in a_otp):    
                return "AADHAAR_OTP"    
    
            e_otp = driver.find_elements(By.XPATH, "//input[@id='email_otp' or contains(@id, 'txtEmailOtp') or contains(@name, 'emailOtp')]")    
            if e_otp and any(e.is_displayed() for e in e_otp):    
                return "EMAIL_OTP"    
        except Exception:    
            pass    
        time.sleep(0.5)    
    
    return "UNKNOWN"    

def capture_captcha_image(driver, captcha_xpaths, save_path):    
    wait = WebDriverWait(driver, 10)    
    for xp in captcha_xpaths:    
        try:    
            elem = wait.until(EC.presence_of_element_located((By.XPATH, xp)))    
            elem.screenshot(save_path)    
            return True    
        except Exception:    
            continue    
    return False    

def verify_submission_status(driver, timeout=15):    
    start_time = time.time()    
    success_indicators = [    
        "//div[contains(@class, 'alert-success') or contains(@class, 'success')]",    
        "//*[contains(text(), 'Application Reference Number') or contains(text(), 'आवेदन संदर्भ संख्या') or contains(text(), 'रिफरेंस नंबर')]",    
        "//button[contains(text(),'Export to PDF') or contains(text(),'Print') or contains(text(),'पहुंच रसीद')]",    
        "//a[contains(text(),'Export to PDF') or contains(@href, 'pdf') or contains(@href, 'Acknowledgement')]"    
    ]    
    while time.time() - start_time < timeout:    
        try:    
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

# -------------------------------------------------------------------    
# RTPS Form Filling Logic (With Screenshot & Dynamic Navigation)
# -------------------------------------------------------------------    
def fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=None):    
    logging.info(f"Opening Main RTPS Portal for: {service_type}")
    driver.get("https://serviceonline.bihar.gov.in/")    
    time.sleep(5)

    if chat_id:
        try:
            user_dir = get_user_dir(chat_id)
            ss_path = os.path.join(user_dir, "rtps_homepage.png")
            driver.save_screenshot(ss_path)
            with open(ss_path, 'rb') as ss_file:
                bot.send_photo(chat_id, ss_file, caption="🌐 RTPS Website open ho gayi hai.")
            logging.info("Screenshot successfully sent to Telegram.")
        except Exception as ss_err:
            logging.warning(f"Screenshot error: {ss_err}")

    try:
        logging.info("Clicking General Administration Department...")
        find_and_interact(driver, [
            "//a[contains(text(), 'सामान्य प्रशासन विभाग')]",
            "//a[contains(text(), 'General Administration')]",
            "//li[contains(@id, 'GAD')]//a"
        ])
        time.sleep(2)
        
        service_links = {
            "RESIDENCE": "//a[contains(text(), 'आवासीय प्रमाण-पत्र का निर्गमन') or contains(text(), 'आवासीय प्रमाण पत्र')]",
            "CASTE": "//a[contains(text(), 'जाति प्रमाण-पत्र का निर्गमन') or contains(text(), 'जाति प्रमाण पत्र')]",
            "INCOME": "//a[contains(text(), 'आय प्रमाण-पत्र का निर्गमन') or contains(text(), 'आय प्रमाण पत्र')]"
        }
        logging.info(f"Clicking service link for {service_type}...")
        find_and_interact(driver, [service_links[service_type]])
        time.sleep(2)
        
        logging.info("Clicking Block Level option...")
        find_and_interact(driver, [
            "//a[contains(text(), 'अंचल स्तर पर')]",
            "//a[contains(text(), 'Block Level')]"
        ])
        time.sleep(5)
        
        if len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
            
    except Exception as e:
        logging.error(f"Menu navigation failed: {e}")
        raise NoSuchElementException("Homepage menu se service link tak nahi pahunch sake.")

    check_and_switch_iframe(driver, ["//input[contains(@id, 'applicant') or contains(@name, 'applicant')]"])

    gender = user_data.get("gender", "MALE").upper()    
    gender_xpaths = [
        "//input[@id='gender1']", 
        "//input[@value='M']", 
        "//input[@name='gender' and @value='1']",
        "//label[contains(text(), 'पुरुष')]/preceding-sibling::input"
    ] if gender == "MALE" else [
        "//input[@id='gender2']", 
        "//input[@value='F']", 
        "//input[@name='gender' and @value='2']",
        "//label[contains(text(), 'महिला')]/preceding-sibling::input"
    ]    
    find_and_interact(driver, gender_xpaths)    
    
    if user_data.get("salutation"):    
        try:
            select_dropdown_robustly(driver, By.XPATH, "//select[@id='salutation' or contains(@name, 'salutation')]", user_data["salutation"], min_options=1)    
        except Exception:
            pass

    find_and_interact(driver, ["//input[@id='applicant_name' or contains(@name, 'applicantName') or contains(@id, 'applicant_name_en') or contains(@name, 'name')]"], "type", user_data["applicant_name"])    
    find_and_interact(driver, ["//input[@id='father_name' or contains(@name, 'fatherName') or contains(@id, 'father_name_en')]"], "type", user_data["father_name"])    
    
    if user_data.get("mother_name"):    
        try:    
            find_and_interact(driver, ["//input[@id='mother_name' or contains(@name, 'motherName') or contains(@id, 'mother_name_en')]"], "type", user_data["mother_name"])    
        except Exception:    
            pass    
            
    find_and_interact(driver, ["//input[@id='mobile_no' or contains(@name, 'mobile') or contains(@id, 'mobile')]"], "type", user_data["mobile_no"])    
    
    if user_data.get("email"):    
        try:    
            find_and_interact(driver, ["//input[@id='email' or contains(@name, 'email')]"], "type", user_data["email"])    
        except Exception:    
            pass    
    
    select_dropdown_robustly(driver, By.XPATH, "//select[@id='state' or contains(@name, 'state')]", "बिहार")    
    select_dropdown_robustly(driver, By.XPATH, "//select[@id='district' or contains(@name, 'district')]", user_data.get("district"))    
    select_dropdown_robustly(driver, By.XPATH, "//select[@id='sub_division' or contains(@name, 'subDivision')]", user_data.get("sub_division"))    
    select_dropdown_robustly(driver, By.XPATH, "//select[@id='block' or contains(@name, 'block')]", user_data.get("block"))    
    
    local_body = user_data.get("local_body_type", "GRAM_PANCHAYAT").upper()    
    local_body_xpaths = {    
        "GRAM_PANCHAYAT": "//input[@name='local_body_type' and (@value='1' or contains(@value, 'PANCHAYAT'))]",    
        "NAGAR_NIGAM": "//input[@name='local_body_type' and (@value='2' or contains(@value, 'NIGAM'))]",    
        "NAGAR_PARISHAD": "//input[@name='local_body_type' and (@value='3' or contains(@value, 'PARISHAD'))]",    
        "NAGAR_PANCHAYAT": "//input[@name='local_body_type' and (@value='4' or contains(@value, 'NAGAR_PANCHAYAT'))]"    
    }    
    if local_body in local_body_xpaths:    
        try:    
            find_and_interact(driver, [local_body_xpaths[local_body]])    
            time.sleep(1)    
        except Exception:    
            pass    
    
    if local_body == "GRAM_PANCHAYAT" and user_data.get("panchayat"):    
        select_dropdown_robustly(driver, By.XPATH, "//select[@id='panchayat' or contains(@name, 'panchayat')]", user_data.get("panchayat"))    
    
    if user_data.get("ward_no"):    
        try:    
            find_and_interact(driver, ["//input[@id='ward_no' or contains(@name, 'ward')]"], "type", str(user_data["ward_no"]))    
        except Exception:    
            pass    
    
    if user_data.get("village"):    
        try:    
            find_and_interact(driver, ["//input[@id='village' or contains(@name, 'village')]"], "type", user_data.get("village", ""))    
        except Exception:    
            pass    
    
    if user_data.get("post_office"):    
        try:    
            find_and_interact(driver, ["//input[@id='post_office' or contains(@name, 'postOffice')]"], "type", user_data.get("post_office", ""))    
        except Exception:    
            pass    
    
    if user_data.get("police_station"):    
        try:    
            select_dropdown_robustly(driver, By.XPATH, "//select[@id='police_station' or contains(@name, 'policeStation')]", user_data.get("police_station"))    
        except Exception:    
            pass    
    
    find_and_interact(driver, ["//input[@id='pin_code' or contains(@name, 'pinCode')]"], "type", user_data.get("pin_code", ""))    
    find_and_interact(driver, ["//input[@type='file' and (contains(@id, 'photo') or contains(@name, 'photo'))]"], "file", photo_path)    
    
    if service_type == "RESIDENCE":    
        res_val = user_data.get("residence_type", "स्थायी")    
        try:    
            select_dropdown_robustly(driver, By.XPATH, "//select[@id='residence_type' or contains(@name, 'residenceType')]", res_val, min_options=1)    
        except Exception:    
            pass    
    elif service_type == "CASTE":    
        select_dropdown_robustly(driver, By.XPATH, "//select[@id='profession' or contains(@name, 'profession')]", user_data.get("profession"))    
        select_dropdown_robustly(driver, By.XPATH, "//select[@id='category' or contains(@name, 'category')]", user_data.get("category"))    
        select_dropdown_robustly(driver, By.XPATH, "//select[@id='caste' or contains(@name, 'caste')]", user_data.get("caste"))    
    elif service_type == "INCOME":    
        select_dropdown_robustly(driver, By.XPATH, "//select[@id='profession' or contains(@name, 'profession')]", user_data.get("profession"))    
        find_and_interact(driver, ["//input[@id='annual_income' or contains(@name, 'income')]"], "type", str(user_data["annual_income"]))    
    
    try:    
        find_and_interact(driver, [    
            "//button[contains(text(), 'Get Mobile OTP') or contains(text(), 'ओटीपी भेजें')]",    
            "//input[@id='btn_mobile_otp' or contains(@value, 'OTP')]"    
        ], timeout=5)    
    except Exception as e:    
        logging.warning(f"⚠️ Mobile OTP button not interactable or skipped: {e}")    

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
            captcha_xpaths = ["//img[contains(@id, 'captcha') or contains(@src, 'captcha') or contains(@id, 'imgCaptcha')]"]    
            if capture_captcha_image(driver, captcha_xpaths, captcha_img_path):    
                with open(captcha_img_path, 'rb') as c_img:    
                    bot.send_photo(chat_id, c_img, caption="🧩 इमेज में दिख रहा CAPTCHA दर्ज करें:")    
                bot.set_state(user_id, RTPSState.captcha_input, chat_id)    
            else:    
                bot.send_message(chat_id, "⚠️ CAPTCHA इमेज लोड नहीं हो सकी।")    
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
    else:    
        bot.send_message(chat_id, "⚠️ वर्तमान चरण की स्पष्ट पहचान नहीं हो सकी। कृपया पुनः प्रयास करें।")    

@bot.message_handler(commands=['start'])    
def start_cmd(message):    
    user_id = message.from_user.id    
    chat_id = message.chat.id
    
    try:
        bot.delete_state(user_id=user_id, chat_id=chat_id)
    except Exception:
        pass

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
        bot.send_message(chat_id, f"❌ क्षमा करें, आप इस बॉट का उपयोग करने के लिए अधिकृत नहीं हैं। (ID: {user_id})")    
        return    
    
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)    
    markup.add("RESIDENCE", "CASTE", "INCOME")    
    bot.send_message(chat_id, "RTPS बिहार ऑटोमेशन बोट में आपका स्वागत है। कृपया अपनी सेवा चुनें:", reply_markup=markup)    
    bot.set_state(user_id, RTPSState.service_type, chat_id)    

@bot.message_handler(state=RTPSState.service_type)    
def process_service_type(message):    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
        return    
    
    service = message.text.upper()    
    if service not in ["RESIDENCE", "CASTE", "INCOME"]:    
        bot.reply_to(message, "कृपया केवल RESIDENCE, CASTE या INCOME में से ही विकल्प चुनें।")    
        return    
    
    with bot.retrieve_data(user_id, message.chat.id) as data:    
        data['service_type'] = service    
    
    templates = {    
        "RESIDENCE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "local_body_type": "GRAM_PANCHAYAT",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "residence_type": "स्थायी"\n}',    
        "CASTE": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "local_body_type": "GRAM_PANCHAYAT",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "छात्र",\n  "category": "अत्यंत पिछड़ा वर्ग (अनुसूची-1)",\n  "caste": "जाति"\n}',    
        "INCOME": '{\n  "salutation": "श्री",\n  "gender": "MALE",\n  "applicant_name": "YOUR_NAME",\n  "father_name": "FATHERS_NAME",\n  "mother_name": "MOTHERS_NAME",\n  "mobile_no": "10_DIGIT_MOBILE",\n  "email": "your_email@gmail.com",\n  "district": "जिला",\n  "sub_division": "अनुमंडल",\n  "block": "प्रखंड",\n  "local_body_type": "GRAM_PANCHAYAT",\n  "ward_no": "12",\n  "panchayat": "पंचायत",\n  "village": "गाँव",\n  "post_office": "डाकघर",\n  "police_station": "थाना",\n  "pin_code": "PINCODE",\n  "profession": "सरकारी सेवा",\n  "annual_income": "90000"\n}'    
    }    
    
    bot.send_message(message.chat.id, f"सर्विस '{service}' चुनी गई। विवरण नीचे दिए गए JSON प्रारूप में भरें:\n\n`{templates[service]}`", parse_mode="Markdown")    
    bot.set_state(user_id, RTPSState.user_details, message.chat.id)    

@bot.message_handler(state=RTPSState.user_details)    
def process_user_details(message):    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
        logging.error(f"Error in process_user_details: {e}")    
        bot.reply_to(message, "❌ डेटा प्रोसेस करने में त्रुटि हुई।")    

@bot.message_handler(content_types=['photo'], state=RTPSState.photo_upload)    
def process_photo_upload(message):    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
        return    
    
    if not user_locks[chat_id].acquire(blocking=False):    
        bot.send_message(chat_id, "⚠️ आपकी एक प्रक्रिया पहले से चल रही है, प्रतीक्षा करें...")    
        return    
    
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
                "aadhaar_otp_attempts": 0    
            }    
    
        fill_initial_rtps_form(driver, user_data, service_type, photo_path, chat_id=chat_id)    
            
        with session_lock:    
            if chat_id in active_user_sessions:    
                active_user_sessions[chat_id]["is_processing"] = False    
    
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)    
    
    except Exception as e:    
        logging.error(f"Automation error in chat {chat_id}: {e}")    
        bot.send_message(chat_id, f"❌ ऑटोमेशन में त्रुटि: {e}")    
        with session_lock:    
            session = active_user_sessions.get(chat_id)    
        if session:    
            finish_and_cleanup_session(chat_id, session)    
    finally:    
        if user_locks[chat_id].locked():    
            user_locks[chat_id].release()    

@bot.message_handler(state=RTPSState.mobile_otp_input)    
def process_mobile_otp_input(message):    
    chat_id = message.chat.id    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
        find_and_interact(driver, ["//input[@id='mobile_otp' or contains(@id, 'txtMobileOtp') or contains(@name, 'mobileOtp')]"], "type", otp_code)    
        find_and_interact(driver, ["//button[contains(text(), 'Verify') or contains(text(), 'सत्यापित')]", "//input[@value='Validate']"])    
    
        time.sleep(2)    
        step = detect_current_verification_step(driver, timeout=5)    
    
        if step == "MOBILE_OTP":    
            session["mobile_otp_attempts"] += 1    
            session["is_processing"] = False    
            if session["mobile_otp_attempts"] >= MAX_OTP_ATTEMPTS:    
                bot.send_message(chat_id, f"❌ अधिकतम मोबाइल OTP प्रयास ({MAX_OTP_ATTEMPTS}) समाप्त हुए।")    
                finish_and_cleanup_session(chat_id, session)    
                return    
            bot.send_message(chat_id, f"❌ अमान्य OTP! पुनः दर्ज करें (प्रयास {session['mobile_otp_attempts']}/{MAX_OTP_ATTEMPTS}):")    
            return    
    
        session["is_processing"] = False    
        bot.send_message(chat_id, "✅ Mobile OTP सत्यापित हुआ।")    
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)    
    except Exception as e:    
        session["is_processing"] = False    
        logging.error(f"Mobile OTP processing error: {e}")    
        bot.send_message(chat_id, "❌ OTP प्रक्रिया में त्रुटि हुई।")    

@bot.message_handler(state=RTPSState.email_otp_input)    
def process_email_otp_input(message):    
    chat_id = message.chat.id    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
        find_and_interact(driver, ["//input[@id='email_otp' or contains(@id, 'txtEmailOtp')]"], "type", otp_code)    
        find_and_interact(driver, ["//button[contains(text(), 'Verify') or contains(text(), 'सत्यापित')]", "//input[@value='Validate']"])    
        time.sleep(2)    
        
        step = detect_current_verification_step(driver, timeout=5)    
        if step == "EMAIL_OTP":    
            session["email_otp_attempts"] += 1    
            session["is_processing"] = False    
            if session["email_otp_attempts"] >= MAX_OTP_ATTEMPTS:    
                bot.send_message(chat_id, f"❌ अधिकतम ईमेल OTP प्रयास ({MAX_OTP_ATTEMPTS}) समाप्त हुए।")    
                finish_and_cleanup_session(chat_id, session)    
                return    
            bot.send_message(chat_id, f"❌ अमान्य ईमेल OTP! पुनः दर्ज करें (प्रयास {session['email_otp_attempts']}/{MAX_OTP_ATTEMPTS}):")    
            return    
            
        session["is_processing"] = False    
        bot.send_message(chat_id, "✅ ईमेल OTP सत्यापित हुआ।")    
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)    
    except Exception as e:    
        session["is_processing"] = False    
        logging.error(f"Email OTP processing error: {e}")    
        bot.send_message(chat_id, "❌ ईमेल OTP सत्यापन प्रक्रिया में त्रुटि।")    

@bot.message_handler(state=RTPSState.captcha_input)    
def process_captcha_input(message):    
    chat_id = message.chat.id    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
        find_and_interact(driver, ["//input[@id='captcha' or contains(@id, 'txtCaptcha') or contains(@name, 'captcha')]"], "type", captcha_text)    
        find_and_interact(driver, ["//button[contains(text(),'Proceed') or contains(text(),'आगे बढ़ें')]", "//input[@value='Proceed']"])    
    
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
                    bot.send_photo(chat_id, c_img, caption="❌ गलत CAPTCHA! पुनः दर्ज करें:")    
            return    
    
        session["is_processing"] = False    
        bot.send_message(chat_id, "✅ CAPTCHA सत्यापित हुआ।")    
        route_to_next_step(bot, chat_id, user_id, driver, user_dir)    
    except Exception as e:    
        session["is_processing"] = False    
        logging.error(f"Captcha processing error: {e}")    
        bot.send_message(chat_id, "❌ CAPTCHA प्रक्रिया में त्रुटि।")    

@bot.message_handler(state=RTPSState.aadhaar_otp_input)    
def process_aadhaar_otp_input(message):    
    chat_id = message.chat.id    
    user_id = message.from_user.id    
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:    
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
            find_and_interact(driver, ["//input[@id='aadhaar_otp' or contains(@id, 'txtAadhaarOtp')]"], "type", otp_code)    
            find_and_interact(driver, ["//button[contains(text(),'Validate') or contains(text(),'सत्यापित')]", "//input[@value='Validate']"])    
            time.sleep(3)    
            
            step = detect_current_verification_step(driver, timeout=3)    
            if step == "AADHAAR_OTP":    
                session["aadhaar_otp_attempts"] += 1    
                session["is_processing"] = False    
                if session["aadhaar_otp_attempts"] >= MAX_OTP_ATTEMPTS:    
                    bot.send_message(chat_id, f"❌ अधिकतम आधार OTP प्रयास ({MAX_OTP_ATTEMPTS}) समाप्त हुए।")    
                else:    
                    bot.send_message(chat_id, f"❌ अमान्य आधार OTP! पुनः दर्ज करें (प्रयास {session['aadhaar_otp_attempts']}/{MAX_OTP_ATTEMPTS}):")    
                    should_cleanup = False    
                return    
    
        initial_files = set(os.listdir(download_dir))    
        try:    
            attach_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@value='Attach Annexure' or contains(@id, 'btnAttach')]")))    
            attach_btn.click()    
            
            find_and_interact(driver, [    
                "//div[contains(@id, 'annexure')]//input[@type='file']",    
                "//table//input[@type='file']",    
                "//form//input[@type='file']"    
            ], "file", doc_path)    
            
            find_and_interact(driver, ["//input[@value='Save Annexure' or contains(@id, 'btnSave')]"])    
            time.sleep(2)    
        except Exception as e:    
            logging.warning(f"Annexure attachment step warning: {e}")    
    
        try:    
            find_and_interact(driver, ["//input[@value='Submit' or contains(@value, 'Final Submit') or contains(@id, 'btnSubmit')]"])    
            time.sleep(3)    
        except Exception as e:    
            logging.warning(f"Final Submit button warning: {e}")    
    
        if verify_submission_status(driver, timeout=15):    
            bot.send_message(chat_id, "✅ RTPS फॉर्म सबमिट हो गया है। पावती रसीद डाउनलोड हो रही है...")    
        else:    
            bot.send_message(chat_id, "⚠️ सबमिशन की पुष्टि की जा रही है...")    
    
        current_handles = driver.window_handles    
        try:    
            find_and_interact(driver, [    
                "//button[contains(text(),'Export to PDF') or contains(text(),'पहुंच रसीद')]",    
                "//a[contains(text(),'Export to PDF') or contains(@href, 'pdf')]"    
            ])    
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
        bot.send_message(chat_id, f"❌ सबमिशन प्रक्रिया में त्रुटि हुई: {e}")    
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
    
    logging.info("🚀 RTPS टेलीग्राम बोट आरंभ हो गया है...")    
    bot.infinity_polling(skip_pending=True)
