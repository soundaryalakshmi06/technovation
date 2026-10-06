import os
import queue
import threading
import time
from urllib.parse import quote

from injury_notes import safe_display_player_name


# ============================================
# CONFIG
# ============================================

ENABLED = True

# Receiving number: country code + number, no "+" or spaces. e.g. "919876543210"
TARGET_PHONE = os.environ.get("WHATSAPP_PHONE", "91XXXXXXXXXX")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Dedicated Chrome profile so the WhatsApp Web login (QR scan) is remembered
PROFILE_DIR = os.path.join(PROJECT_ROOT, "output", "whatsapp_profile")

LOGIN_TIMEOUT = 120      # seconds to wait for QR scan / WhatsApp Web load
CHAT_LOAD_TIMEOUT = 45   # seconds to wait for the chat to open


# ============================================
# INTERNAL STATE
# All Selenium work happens on ONE background
# thread, so the detection loop never blocks.
# ============================================

_queue = queue.Queue()
_worker = None
_lock = threading.Lock()


def _create_driver():

    from selenium import webdriver

    os.makedirs(PROFILE_DIR, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument("--user-data-dir=" + PROFILE_DIR)
    options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    return webdriver.Chrome(options=options)


def _wait_for_login(driver):

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get("https://web.whatsapp.com")

    print("[WHATSAPP] Opening WhatsApp Web... scan the QR code in Chrome if asked.")

    def logged_in(d):
        return (
            len(d.find_elements(By.ID, "pane-side")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "div[aria-label='Chat list']")) > 0
        )

    WebDriverWait(driver, LOGIN_TIMEOUT).until(logged_in)

    print("[WHATSAPP] Logged in and ready.")


def _send(driver, text):

    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    url = "https://web.whatsapp.com/send?phone=" + TARGET_PHONE + "&text=" + quote(text)

    driver.get(url)

    box = WebDriverWait(driver, CHAT_LOAD_TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "footer div[contenteditable='true']"))
    )

    time.sleep(1.5)

    box.send_keys(Keys.ENTER)

    # give WhatsApp time to actually send before the next navigation
    time.sleep(4)


def _worker_loop():

    driver = None

    try:
        driver = _create_driver()
        _wait_for_login(driver)

    except Exception as e:
        print("[WHATSAPP] Could not start WhatsApp Web — messages will be skipped:", e)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        driver = None

    while True:

        text = _queue.get()

        if text is None:
            break

        if driver is None:
            print("[WHATSAPP] Skipped (not logged in):", text.splitlines()[0])
            continue

        for attempt in range(1, 3):

            try:
                _send(driver, text)
                print("[WHATSAPP] Message sent to", TARGET_PHONE)
                break

            except Exception as e:
                print("[WHATSAPP] Send attempt", attempt, "failed:", e)

    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass


# ============================================
# PUBLIC API
# ============================================

def start():
    """Open WhatsApp Web early so the QR scan can happen before detection starts."""

    global _worker

    if not ENABLED:
        return

    with _lock:

        if _worker is None or not _worker.is_alive():

            _worker = threading.Thread(target=_worker_loop, daemon=True)
            _worker.start()


def send_message(text):
    """Queue a message; returns immediately."""

    if not ENABLED:
        return

    start()

    _queue.put(text)


def shutdown(timeout=120):
    """Wait for queued messages to go out, then close Chrome."""

    if _worker is None:
        return

    print("[WHATSAPP] Sending any remaining messages before exit...")

    _queue.put(None)

    _worker.join(timeout=timeout)


def build_alert_message(player_name, jersey_number, event_type, region, risk,
                        injury_note, match_name, when_text, player_id=None,
                        identified_by=None, id_confidence=None):

    player_line = safe_display_player_name(
        player_name, player_id, identified_by, id_confidence,
    )

    lines = [
        "🚨 AthleteGuard Alert",
        "Event: " + str(event_type),
        "Player: " + player_line,
        "Body area: " + str(region or "Unknown"),
        "Risk: " + str(risk or "-"),
    ]

    if injury_note:
        lines.append("Note: " + injury_note)

    if match_name:
        lines.append("Match: " + match_name)

    lines.append("Time: " + when_text)

    return "\n".join(lines)