import os
import base64
import re
import json
import time
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, redirect, session
from openai import OpenAI
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env file.")
client = OpenAI(api_key=OPENAI_API_KEY)

# Model used for cheap/frequent calls (screening, classification)
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-4.1-mini")
# Model used for reply composition (a bit more capable)
OPENAI_REPLY_MODEL = os.getenv("OPENAI_REPLY_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1"))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Storage files
DASHBOARD_CATALOG_FILE = BASE_DIR / "dashboard_catalog.json"      # orders + emails, keyed by thread/order key
PROCESSED_ORDERS_FILE = BASE_DIR / "processed_orders.json"        # order_key -> {status, sent_message_id, ...}
AUTOMATION_SETTINGS_FILE = BASE_DIR / "automation_settings.json"
BRIEFING_FILE = BASE_DIR / "daily_briefing.md"
SCAN_DEBUG_FILE = BASE_DIR / "last_scan_debug.json"
GMAIL_TOKEN_FILE = BASE_DIR / "token.json"
GMAIL_CREDENTIALS_FILE = BASE_DIR / "credentials.json"

PERSONAL_LABEL = "Personal"
WORK_LABEL = "Work"
RENEWAL_LABEL = "Renewals"

DASHBOARD_CACHE_TTL_SECONDS = 30
DEFAULT_CONNECTED_EMAIL = os.getenv("CONNECTED_EMAIL", "success@pharmacyprep.com")

try:
    SCAN_START_DT = datetime.strptime(os.getenv("SCAN_START_DATE", "2026-06-01"), "%Y-%m-%d")
except Exception:
    SCAN_START_DT = datetime(2026, 6, 1)
SCAN_START_DISPLAY = f"{SCAN_START_DT.strftime('%B')} {SCAN_START_DT.day}, {SCAN_START_DT.year}"
SCAN_START_GMAIL_AFTER = (SCAN_START_DT - timedelta(days=1)).strftime("%Y/%m/%d")

REFRESH_SCAN_DAYS = int(os.getenv("REFRESH_SCAN_DAYS", "21"))
MAX_ORDER_THREADS_PER_SCAN = int(os.getenv("MAX_ORDER_THREADS_PER_SCAN", "200"))
MAX_EMAIL_THREADS_PER_SCAN = int(os.getenv("MAX_EMAIL_THREADS_PER_SCAN", "300"))
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "120"))

GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "16"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "4"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "12"))
GMAIL_CONTEXT_CHARS_PER_THREAD = int(os.getenv("GMAIL_CONTEXT_CHARS_PER_THREAD", "4000"))

# Order welcome auto-send toggle (separate from the general email Auto Reply setting)
AUTO_SEND_ORDER_WELCOME = os.getenv("AUTO_SEND_ORDER_WELCOME", "true").strip().lower() not in ("0", "false", "no", "off")

APP_LOGIN_USERNAME = os.getenv("APP_LOGIN_USERNAME", "success@pharmacyprep.com")
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "Pharmacy1966")

EMAIL_SCREENING_VERSION = "2026-09-recent-first-v2"

_dashboard_cache = {"built_at": 0.0, "payload": None}
# In-process guard against double-send within the same scan/request burst.
_ORDER_SEND_LOCK_KEYS: set = set()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "pharmacy-prep-gmail-assistant-session-key-change-me")
app.permanent_session_lifetime = timedelta(days=int(os.getenv("LOGIN_SESSION_DAYS", "30")))
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)


# ---------------------------------------------------------------------
# JSON STORAGE
# ---------------------------------------------------------------------
def load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_file(path: Path, data):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def get_automation_settings() -> Dict:
    data = load_json_file(AUTOMATION_SETTINGS_FILE, {})
    return {
        "auto_reply_enabled": bool(data.get("auto_reply_enabled", False)),
        "auto_scan_enabled": bool(data.get("auto_scan_enabled", True)),
        "auto_scan_minutes": max(1, int(data.get("auto_scan_minutes", 5))),
    }


def save_automation_settings(updates: Dict):
    current = get_automation_settings()
    current.update(updates)
    save_json_file(AUTOMATION_SETTINGS_FILE, current)


def invalidate_dashboard_cache():
    _dashboard_cache["built_at"] = 0.0
    _dashboard_cache["payload"] = None


def upgrade_legacy_access_fallback(item: Dict, connected_email: str) -> Dict:
    if not isinstance(item, dict) or item.get("category") not in ("work", "personal"):
        return item
    reply = item.get("reply")
    original = item.get("original") or {}
    old = "Please confirm the email address used for your registration so we can check the account and send the correct login or access details."
    if not isinstance(reply, dict) or old not in reply.get("body", ""):
        return item
    latest_text = norm_text(original.get("subject", ""), original.get("body", ""))
    if not any(term in latest_text for term in ("login", "access", "password")):
        return item
    updated = fallback_reply_for_thread({"emails": [original]}, connected_email, item["category"])
    # Keep the thread identifier, recipient override, subject, and surrounding draft text.
    main = updated["body"].split("\n\n", 2)[1]
    repaired = {**reply, "body": reply["body"].replace(old, main)}
    repaired.pop("body_html", None)
    return {**item, "reply": repaired}


def get_dashboard_catalog() -> Dict:
    data = load_json_file(DASHBOARD_CATALOG_FILE, {})
    emails = data.get("emails", {})
    if isinstance(emails, dict):
        connected = (data.get("meta") or {}).get("connected_email") or DEFAULT_CONNECTED_EMAIL
        data["emails"] = {key: upgrade_legacy_access_fallback(item, connected) for key, item in emails.items()}
    return {
        "meta": data.get("meta", {}) if isinstance(data.get("meta", {}), dict) else {},
        "orders": data.get("orders", {}) if isinstance(data.get("orders", {}), dict) else {},
        "emails": data.get("emails", {}) if isinstance(data.get("emails", {}), dict) else {},
    }


def save_dashboard_catalog(catalog: Dict):
    save_json_file(DASHBOARD_CATALOG_FILE, {
        "meta": catalog.get("meta", {}),
        "orders": catalog.get("orders", {}),
        "emails": catalog.get("emails", {}),
    })


def upsert_catalog_item(kind: str, key: str, payload: Dict):
    catalog = get_dashboard_catalog()
    bucket = catalog.setdefault(kind, {})
    current = bucket.get(key, {})
    bucket[key] = {
        **current,
        **payload,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "first_seen_at": current.get("first_seen_at") or payload.get("first_seen_at") or datetime.now().isoformat(timespec="seconds"),
    }
    save_dashboard_catalog(catalog)


def get_catalog_item(kind: str, key: str) -> Dict:
    return get_dashboard_catalog().get(kind, {}).get(key, {})


def get_processed_orders() -> Dict:
    return load_json_file(PROCESSED_ORDERS_FILE, {})


def upsert_processed_order(order_key: str, payload: Dict):
    data = get_processed_orders()
    current = data.get(order_key, {})
    data[order_key] = {**current, **payload, "updated_at": datetime.now().isoformat(timespec="seconds")}
    save_json_file(PROCESSED_ORDERS_FILE, data)


def get_processed_order(order_key: str) -> Dict:
    return get_processed_orders().get(order_key, {})


# ---------------------------------------------------------------------
# GMAIL AUTH
# ---------------------------------------------------------------------
class GmailAuthRequired(Exception):
    """Raised when Gmail needs the user to sign in again."""
    pass


def _public_base_url() -> str:
    configured = (os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    try:
        return request.host_url.rstrip("/")
    except Exception:
        return "http://127.0.0.1:5050"


def _gmail_redirect_uri() -> str:
    return f"{_public_base_url()}/oauth2callback"


def _make_gmail_flow() -> Flow:
    if not GMAIL_CREDENTIALS_FILE.exists():
        raise FileNotFoundError("Missing credentials.json. Put credentials.json next to backend.py.")
    return Flow.from_client_secrets_file(str(GMAIL_CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=_gmail_redirect_uri())


def _safe_next_url(value: str) -> str:
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/login"):
        return "/"
    return value


def _gmail_auth_url(return_to: str = "/") -> str:
    return f"/auth/gmail?next={_safe_next_url(return_to)}"


def get_gmail_service():
    """Never blocks on run_local_server. Raises GmailAuthRequired if re-auth is needed."""
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), SCOPES)
        except Exception:
            GMAIL_TOKEN_FILE.unlink(missing_ok=True)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError:
            GMAIL_TOKEN_FILE.unlink(missing_ok=True)
            raise GmailAuthRequired("Please sign in again.")

    if not creds or not creds.valid:
        raise GmailAuthRequired("Please sign in again.")

    return build("gmail", "v1", credentials=creds)


def get_connected_email(service) -> str:
    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress", DEFAULT_CONNECTED_EMAIL)
    except Exception:
        return DEFAULT_CONNECTED_EMAIL


# ---------------------------------------------------------------------
# GMAIL / TEXT HELPERS
# ---------------------------------------------------------------------
def decode_base64url(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode((data + padding).encode("utf-8"))
    return decoded.decode("utf-8", errors="ignore")


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_body_from_payload(payload: Dict) -> str:
    plain_texts, html_texts = [], []

    def walk(part):
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if body_data:
            decoded = decode_base64url(body_data)
            if mime_type == "text/plain":
                plain_texts.append(decoded)
            elif mime_type == "text/html":
                html_texts.append(clean_html(decoded))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    if plain_texts:
        return "\n\n".join(p for p in plain_texts if p).strip()
    if html_texts:
        return "\n\n".join(p for p in html_texts if p).strip()
    return ""


def get_header(headers: List[Dict], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def search_threads(service, query: str, max_results: int = 100) -> List[str]:
    thread_ids: List[str] = []
    seen = set()
    page_token = None
    while len(thread_ids) < max_results:
        page_size = min(100, max_results - len(thread_ids))
        try:
            result = service.users().messages().list(
                userId="me", q=query, maxResults=page_size, pageToken=page_token,
            ).execute()
        except Exception as error:
            print(f"[gmail] search failed | query={query} | error={error}", flush=True)
            raise
        for message in result.get("messages", []):
            thread_id = message.get("threadId")
            if thread_id and thread_id not in seen:
                seen.add(thread_id)
                thread_ids.append(thread_id)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return thread_ids


def gmail_search_any(service, query: str, max_results: int = 5) -> bool:
    try:
        result = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return bool(result.get("messages"))
    except Exception:
        return False


def read_thread(service, thread_id: str) -> Dict:
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    emails = []
    message_ids = []
    for message in thread.get("messages", []):
        message_id = message.get("id")
        message_ids.append(message_id)
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        emails.append({
            "gmail_message_id": message_id,
            "thread_id": thread_id,
            "subject": get_header(headers, "Subject"),
            "from": get_header(headers, "From"),
            "to": get_header(headers, "To"),
            "date": get_header(headers, "Date"),
            "message_id_header": get_header(headers, "Message-ID"),
            "references": get_header(headers, "References"),
            "body": extract_body_from_payload(payload)[:25000],
        })
    return {"thread_id": thread_id, "message_ids": message_ids, "emails": emails}


def format_thread_for_ai(thread: Dict) -> str:
    sections = []
    for i, email in enumerate(thread.get("emails", []), start=1):
        sections.append(
            f"EMAIL {i}\nFrom: {email.get('from','')}\nTo: {email.get('to','')}\n"
            f"Date: {email.get('date','')}\nSubject: {email.get('subject','')}\nBody:\n{email.get('body','')}"
        )
    return "\n\n---\n\n".join(sections)


def combined_thread_text(thread: Dict) -> str:
    pieces = []
    for email in thread.get("emails", []):
        pieces.extend([
            f"Subject: {email.get('subject','')}", f"From: {email.get('from','')}",
            f"To: {email.get('to','')}", f"Date: {email.get('date','')}", "",
            email.get("body", ""), "",
        ])
    return "\n".join(pieces)


def clean_preview_text(text: str, limit: int = 2800) -> str:
    text = str(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"=\n", "", text)
    text = re.sub(r"\n\s*>?\s*On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[\s\S]*?wrote:\s*[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n\s*>?\s*On\s+.{0,260}?wrote:\s*[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n\s*-{2,}\s*Original Message\s*-{2,}\s*[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = "\n".join(line.replace(">", "", 1).rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def email_date_to_datetime(value: str) -> datetime:
    if not value:
        return datetime.min
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is None:
            return datetime.min
        if getattr(parsed, "tzinfo", None) is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value[:19], fmt)
            except Exception:
                continue
    return datetime.min


def email_date_to_sort_key(value: str) -> str:
    parsed = email_date_to_datetime(value)
    return "" if parsed == datetime.min else parsed.isoformat(timespec="seconds")


def norm_text(*parts) -> str:
    raw = "\n".join(str(p or "") for p in parts)
    raw = raw.replace("&zwnj;", " ").replace("\u200c", " ").replace("\u200b", " ").replace("\xa0", " ")
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip().lower()


def email_addr(value: str) -> str:
    try:
        return parseaddr(value or "")[1].lower().strip()
    except Exception:
        return ""


def sender_display_name(from_value: str, fallback_email: str = "") -> str:
    name, email = parseaddr(from_value or "")
    clean_name = re.sub(r"[\"<>]", "", name or "").strip()
    if clean_name and "@" not in clean_name:
        return clean_name if len(clean_name.split()) > 1 else clean_name.title()
    local = (email or fallback_email or "").split("@")[0]
    cleaned = re.sub(r"[._+-]+", " ", local).strip()
    return " ".join(p.title() for p in cleaned.split()[:3]) if cleaned else "The sender"


def latest_inbound_email(thread: Dict, connected_email: str) -> Dict:
    connected = (connected_email or "").lower().strip()
    inbound = [e for e in thread.get("emails", []) if email_addr(e.get("from", "")) != connected]
    if inbound:
        return inbound[-1]
    return thread.get("emails", [])[-1] if thread.get("emails") else {}


def latest_inbound_sort_key(thread: Dict, connected_email: str) -> str:
    return email_date_to_sort_key(latest_inbound_email(thread, connected_email).get("date", ""))


def latest_inbound_message_id(thread: Dict, connected_email: str) -> str:
    connected = (connected_email or "").lower().strip()
    latest_id = ""
    for email in thread.get("emails", []):
        if email_addr(email.get("from", "")) != connected:
            latest_id = email.get("gmail_message_id", "") or latest_id
    return latest_id or (thread.get("message_ids", [""]) or [""])[-1]


def thread_has_reply_from_connected_account(thread: Dict, connected_email: str) -> bool:
    """True if ANY message in the thread was sent from the connected account.
    This is the primary 'already handled by a human or by us' signal."""
    connected = (connected_email or "").lower().strip()
    if not connected:
        return False
    return any(email_addr(e.get("from", "")) == connected for e in thread.get("emails", []))


def latest_email_is_from_connected_account(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    return email_addr(thread["emails"][-1].get("from", "")) == (connected_email or "").lower().strip()


def thread_action_key(thread: Dict, connected_email: str = "") -> str:
    return f"{thread.get('thread_id','')}:{latest_inbound_message_id(thread, connected_email)}"


# ---------------------------------------------------------------------
# LABELS
# ---------------------------------------------------------------------
def get_or_create_label(service, label_name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label.get("name", "").lower() == label_name.lower():
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def apply_label_to_thread_messages(service, thread: Dict, label_id: str):
    for message_id in thread.get("message_ids", []):
        if not message_id:
            continue
        try:
            service.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label_id], "removeLabelIds": []},
            ).execute()
        except Exception as error:
            print(f"[labels] failed to apply label | message={message_id} | error={error}", flush=True)


# ---------------------------------------------------------------------
# CLASSIFICATION: spam/promo detection, work/personal/renewal split
# ---------------------------------------------------------------------
NEW_QUESTION_NOTICE_PHRASES = [
    "new question submitted", "new question submited",
    "new question has been submitted", "new question has been submited",
    "new support question has been submitted at eprepstation.com",
    "new support question",
]

PROMO_BRAND_TERMS = [
    "xbox", "game pass", "microsoft rewards", "microsoft store", "playstation", "nintendo",
    "steam", "epic games", "vimeo", "mail.vimeo", "email.vimeo",
    "netflix", "spotify", "youtube", "prime video", "disney+", "twitch", "duolingo", "udemy",
    "coursera", "skillshare", "canva", "grammarly", "mailchimp", "hubspot", "constant contact",
    "jp morgan", "jpmorgan", "j.p. morgan", "chase", "morgan stanley",
    "market update", "market insights", "investment outlook", "investor newsletter",
    "openai", "chatgpt", "getsmarter", "mit sloan",
]

PROMO_MARKETING_TERMS = [
    "unsubscribe", "manage your preferences", "manage preferences", "view this email in your browser",
    "you are receiving this email", "you're receiving this email", "newsletter", "digest",
    "promotion", "promotional", "limited time", "special offer", "exclusive offer", "sale",
    "discount", "coupon", "deal", "deals", "save ", "% off", "free trial", "watch now",
    "stream now", "new video", "featured video", "trailer", "webinar", "sponsored",
    "advertisement", "recommended for you", "see what's new", "register today", "join us for",
    "creator update", "weekly update", "monthly update", "usage alert", "billing alert",
    "plan renewal", "trial ends", "subscription",
]

NO_REPLY_SENDER_FRAGMENTS = [
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster",
    "notifications@", "notification@", "marketing@", "mailer@", "news@", "newsletter@",
    "updates@", "promo@", "promos@", "offers@", "deals@", "events@",
]

# Misspelled/spoofed lookalikes of the business domain that should never be treated as real business mail
BLOCKED_SENDER_EXACT = {"support@pharrmacyprep.com"}

DIRECT_REQUEST_TERMS = [
    "?", "can you", "could you", "would you", "please send", "please provide", "please confirm",
    "please advise", "i need", "need help", "i would like", "how do i", "when will", "where is",
    "not received", "still waiting", "checking in", "follow up", "follow-up", "unable to",
    "cannot access", "can't access", "order number", "invoice", "receipt", "refund", "login",
    "access", "appointment", "meeting",
]

HARD_AUTOMATED_TERMS = [
    "mailer-daemon", "postmaster", "delivery status notification", "undeliverable",
    "verification code", "password reset", "security alert",
    "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
    "delivered", "tracking number", "shipment", "shipping confirmation",
    "payment received", "e-transfer received", "etransfer received", "interac e-transfer",
    "receipt for your payment", "charge receipt", "invoice paid", "successful payment",
    "please moderate", "comment awaiting moderation", "this is an automated message",
    "do not reply to this email", "mail delivery",
]

PHARMACY_PREP_DIRECT_TERMS = [
    "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "www.pharmacyprep.com",
    "416-223-prep", "416-223-7737", "647-221-0457",
    "eprepstation", "eprep station", "online exam prep station",
    "pebc", "pebc ee", "ee course", "ee prep", "evaluating exam", "evaluating examination",
    "qualifying exam", "qualifying examination", "pharmacist evaluating", "pharmacist qualifying",
    "osce", "ospe", "fpgee", "opra", "naplex", "qbank", "question bank", "mock exam", "mock exams",
    "prep course", "pharmacy prep course", "renewed prep course", "course renewal",
    "account renewal request", "course extension", "online account access", "online account",
    "course access", "course login", "class notes", "recorded video", "recorded videos",
    "live online", "interactive lectures", "pharmacy technician", "home study plus online",
]

WORK_COURSE_WORDS = ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "book", "books", "materials", "module", "online account"]
WORK_STUDENT_WORDS = ["student", "students", "candidate", "enrolled", "enrollment", "enrolment", "registered", "registration", "customer"]
WORK_EXAM_WORDS = ["exam", "pebc", "evaluating", "qualifying", "mock", "qbank", "mcq", "osce", "ospe", "pharmacist", "technician"]
WORK_ORDER_WORDS = ["order number", "order #", "invoice", "receipt", "payment", "refund", "paid", "e-transfer", "etransfer"]

PERSONAL_OVERRIDE_TERMS = [
    "pest control", "exterminator", "lease agreement", "rental agreement", "tenancy",
    "landlord", "tenant", "condo", "building management", "property management",
    "management office", "unit key", "master key", "contractor", "real estate",
    "mortgage", "bank statement", "insurance", "policy document", "legal document",
    "lawyer", "attorney", "docusign", "adobe sign", "signed document", "signature request",
    "doctor", "clinic", "dental",
]

RENEWAL_SUBJECT_MARKERS = ["account renewal request"]


def is_blocked_sender(sender: str) -> bool:
    sender = (sender or "").lower().strip()
    if sender in BLOCKED_SENDER_EXACT:
        return True
    return "pharrmacyprep" in sender  # misspelled spoof of pharmacyprep


def is_new_question_notice(text: str) -> bool:
    text = (text or "").lower()
    return any(p in text for p in NEW_QUESTION_NOTICE_PHRASES)


def has_direct_request_signal(text: str) -> bool:
    text = (text or "").lower()
    return any(t in text for t in DIRECT_REQUEST_TERMS)


def is_promotional_text(text: str, sender: str = "") -> bool:
    text = (text or "").lower()
    sender = (sender or "").lower()
    if is_blocked_sender(sender):
        return True
    if is_new_question_notice(text):
        return True
    has_request = has_direct_request_signal(text)
    has_brand = any(t in text or t in sender for t in PROMO_BRAND_TERMS)
    has_marketing = any(t in text for t in PROMO_MARKETING_TERMS)
    no_reply_sender = any(t in sender for t in NO_REPLY_SENDER_FRAGMENTS)
    if has_marketing and not has_request:
        return True
    if has_brand and not has_request:
        return True
    if no_reply_sender and not has_request:
        return True
    return False


def is_infrastructure_notice(text: str, sender: str = "") -> bool:
    """Recognize machine hosting notices, not a student's mention of an SSL error."""
    text = norm_text(text)
    local = email_addr(sender).split("@", 1)[0]
    system_sender = local in {"cpanel", "whm", "root", "autossl", "plesk"}
    platform = any(term in text for term in ("cpanel", "autossl", "webhost manager", "plesk"))
    notice = any(term in text for term in (
        "certificate expires", "certificate will expire", "certificate expiry",
        "certificate expiration", "certificate has expired", "certificate is expiring",
        "certificate has not been renewed", "ssl certificate", "ssl cert",
        "autossl", "disk usage warning", "disk quota", "service failure",
        "backup failed", "backup failure", "certificate renewal failed",
    ))
    machine_footer = any(term in text for term in (
        "this is an automated", "automatically generated", "do not reply",
        "system generated this notice", "system generated this notification",
        "disable the notification", "notification preferences",
    ))
    return notice and (system_sender or (platform and machine_footer))


def is_hard_automated_text(text: str) -> bool:
    text = (text or "").lower()
    return any(t in text for t in HARD_AUTOMATED_TERMS)


def is_renewal_text(text: str) -> bool:
    text = (text or "").lower()
    if "account renewal request" in text:
        return True
    return "renewal" in text and "eprepstation" in text and ("e-mail address" in text or "email address" in text or "course" in text)


def has_personal_override(text: str) -> bool:
    text = (text or "").lower()
    return any(t in text for t in PERSONAL_OVERRIDE_TERMS)


def has_pharmacy_prep_work_signal(text: str, sender: str = "") -> bool:
    """Strict Work detection: only Pharmacy Prep / PEBC / EprepStation / student-course-exam-order context."""
    text = (text or "").lower()
    sender = (sender or "").lower()
    if not text and not sender:
        return False
    if is_new_question_notice(text) or is_promotional_text(text, sender) or is_blocked_sender(sender):
        return False
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    if any(t in text for t in PHARMACY_PREP_DIRECT_TERMS):
        return True
    if has_personal_override(text):
        return False
    has_course = any(t in text for t in WORK_COURSE_WORDS)
    has_student = any(t in text for t in WORK_STUDENT_WORDS)
    has_exam = any(t in text for t in WORK_EXAM_WORDS)
    has_order = any(t in text for t in WORK_ORDER_WORDS)
    if re.search(r"\bee\b", text) and (has_course or has_exam or has_student or "prep" in text):
        return True
    if has_exam and (has_course or has_student or has_order):
        return True
    if has_student and (has_course or has_order) and ("prep" in text or "pharmacy" in text or has_exam):
        return True
    return False


def latest_inbound_context_text(thread: Dict, connected_email: str) -> Tuple[str, str]:
    """Text/sender of just the latest inbound message (used for category decisions)."""
    latest = latest_inbound_email(thread, connected_email)
    sender = email_addr(latest.get("from", ""))
    text = norm_text(latest.get("from", ""), latest.get("subject", ""), latest.get("body", ""))
    return text, sender


def full_inbound_context_text(thread: Dict, connected_email: str) -> str:
    """All inbound (non-connected-account) messages in the thread, for fallback context."""
    connected = (connected_email or "").lower().strip()
    parts = []
    for email in thread.get("emails", []) or []:
        if email_addr(email.get("from", "")) == connected:
            continue
        parts.extend([email.get("from", ""), email.get("subject", ""), clean_preview_text(email.get("body", ""), 2500)])
    return norm_text(*parts)


def classify_thread(thread: Dict, connected_email: str) -> str:
    """Returns one of: 'spam', 'renewal', 'work', 'personal'."""
    latest_text, sender = latest_inbound_context_text(thread, connected_email)
    if is_infrastructure_notice(latest_text, sender) or is_hard_automated_text(latest_text):
        return "spam"
    if is_new_question_notice(latest_text) or is_promotional_text(latest_text, sender) or is_blocked_sender(sender):
        return "spam"
    if is_renewal_text(latest_text) or is_renewal_text(full_inbound_context_text(thread, connected_email)):
        return "renewal"
    if has_pharmacy_prep_work_signal(latest_text, sender):
        return "work"
    if has_personal_override(latest_text):
        return "personal"
    full_text = full_inbound_context_text(thread, connected_email)
    if has_pharmacy_prep_work_signal(full_text, sender):
        return "work"
    return "personal"


def classify_catalog_item(item: Dict) -> str:
    """Same as classify_thread but operating on a stored catalog item's 'original' fields."""
    if not isinstance(item, dict):
        return "spam"
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = email_addr(original.get("from", ""))
    text = norm_text(item.get("title", ""), item.get("important_reason", ""),
                      original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if is_new_question_notice(text) or is_promotional_text(text, sender) or is_blocked_sender(sender):
        return "spam"
    if is_infrastructure_notice(norm_text(original.get("subject", ""), original.get("body", "")), sender):
        return "spam"
    if item.get("is_renewal_request") or is_renewal_text(text):
        return "renewal"
    if has_pharmacy_prep_work_signal(text, sender):
        return "work"
    return "personal"


# ---------------------------------------------------------------------
# ORDER DETECTION
# ---------------------------------------------------------------------
ORDER_SUCCESS_MARKERS = [
    "you've received the following order", "you\u2019ve received the following order",
    "received the following order from", "new order:", "billing address",
    "payment method:", "customer details", "order details", "total:",
]

ORDER_FAILURE_TERMS = [
    "order status changed from pending payment to failed", "order status changed to failed",
    "status changed to failed", "payment failed", "failed payment", "payment was unsuccessful",
    "payment unsuccessful", "transaction failed", "card was declined", "declined payment",
    "order failed", "failed order", "status: failed", "order status: failed",
    "unsuccessful order", "cancelled order", "canceled order", "order cancelled", "order canceled",
]

ENROLLMENT_CONFIRMATION_PHRASES = [
    "welcome to pharmacy prep", "thank you for your order", "we received your order",
    "received your order", "your order has been received", "we will enroll you",
    "we will enrol you", "enroll you in the prep course", "enrol you in the prep course",
    "enrolled you in the prep course", "enrolment is completed", "enrollment is completed",
    "course login details", "login details by email", "course access details",
    "online account access", "your online account", "access to your course",
    "prep course shortly", "welcome. renewed prep course", "renewed prep course",
]


def looks_like_order_email(text: str) -> bool:
    lowered = (text or "").lower()
    score = sum(1 for m in ORDER_SUCCESS_MARKERS if m in lowered)
    return score >= 3


def order_text_looks_failed(text: str) -> bool:
    text = (text or "").lower()
    if not text:
        return False
    if any(p in text for p in ORDER_FAILURE_TERMS):
        return True
    if "failed" in text and any(w in text for w in ["payment", "transaction", "order status", "order #"]):
        success_terms = ["payment successful", "successful payment", "paid successfully"]
        return not any(s in text for s in success_terms)
    return False


def order_text_looks_successful(text: str) -> bool:
    if not text or order_text_looks_failed(text):
        return False
    lowered = text.lower()
    return looks_like_order_email(text) and sum(1 for m in ORDER_SUCCESS_MARKERS if m in lowered) >= 2


def get_best_order_email_text(thread: Dict) -> Optional[str]:
    for email in thread.get("emails", []):
        combined = f"Subject: {email.get('subject','')}\n\n{email.get('body','')}"
        if looks_like_order_email(combined):
            return combined
    return None


def extract_order_number(text: str) -> Optional[str]:
    for pattern in [r"New\s+Order:\s*#?\s*(\d+)", r"\[Order\s*#\s*(\d+)\]", r"Order\s*#\s*(\d+)"]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def infer_customer_name_from_email(email: str) -> Optional[str]:
    local = (email or "").split("@")[0].strip().lower()
    if not local:
        return None
    cleaned = re.sub(r"\d+", " ", local)
    cleaned = re.sub(r"[._+-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return " ".join(p.title() for p in cleaned.split()) if cleaned else None


def extract_customer_name(text: str) -> Optional[str]:
    m = re.search(r"received\s+the\s+following\s+order\s+from\s+(.+?):", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"Billing address\s+([A-Za-z][^\n\r]+)", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_customer_email(text: str, connected_email: str = "") -> Optional[str]:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if not emails:
        return None
    blocked = ["noreply", "no-reply", "wordpress", "woocommerce", "pharmacyprep.com", "eprepstation.com"]
    connected = (connected_email or "").lower().strip()
    for e in emails:
        lowered = e.lower().strip()
        if connected and lowered == connected:
            continue
        if any(b in lowered for b in blocked):
            continue
        return e
    return emails[-1]


def best_customer_name(text: str, email: str = "") -> str:
    name = extract_customer_name(text or "")
    if name and name.lower() not in ("unknown", "student", "customer"):
        return name
    inferred = infer_customer_name_from_email(email or extract_customer_email(text or "") or "")
    return inferred or "Customer"


def extract_product_lines(text: str) -> List[str]:
    products = []
    for line in (l.strip() for l in text.splitlines() if l.strip()):
        lowered = line.lower()
        if "$" in line and any(t in lowered for t in ("prep", "book", "exam", "course", "digital", "pebc")):
            products.append(line)
    return products[:5]


def extract_total(text: str) -> Optional[str]:
    m = re.search(r"Total:\s*\$?([0-9,]+\.\d{2})", text, flags=re.IGNORECASE)
    return f"${m.group(1)}" if m else None


def build_order_welcome_email(customer_name: str, order_number: str) -> Tuple[str, str]:
    subject = "Welcome to Pharmacy Prep"
    body = f"""Dear {customer_name},

Thank you for your order #{order_number}.

We received your order and will enroll you in the prep course shortly. We will send your course login details by email as soon as the enrollment is completed.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    return subject, body


def order_key_for(order_number: str, customer_email: str, thread_id: str = "") -> str:
    """Stable dedup key. Prefer order_number+email; fall back to thread_id so
    orders lacking a parsed number still get a durable identity."""
    order_number = (order_number or "").strip().lower()
    customer_email = (customer_email or "").strip().lower()
    if order_number and order_number != "unknown" and customer_email and customer_email != "unknown":
        return f"order:{order_number}:{customer_email}"
    if order_number and order_number != "unknown":
        return f"order-only:{order_number}"
    return f"thread:{thread_id}"


# ---------------------------------------------------------------------
# ORDER DEDUP: "was this already handled?" — the core anti-duplicate logic
# ---------------------------------------------------------------------
def _sent_from_connected_account(email: Dict, connected_email: str) -> bool:
    sender = email_addr(email.get("from", ""))
    connected = (connected_email or DEFAULT_CONNECTED_EMAIL or "").lower().strip()
    if connected and sender == connected:
        return True
    return sender.endswith("@pharmacyprep.com")


def _sent_message_targets_customer(email: Dict, customer_email: str) -> bool:
    target = (customer_email or "").lower().strip()
    if not target:
        return False
    haystack = f"{email.get('to','')}\n{email.get('body','')[:1200]}".lower()
    return target in haystack


def _sent_message_is_enrollment_confirmation(email: Dict, order_number: str = "") -> bool:
    text = norm_text(email.get("subject", ""), email.get("body", ""))
    if not text:
        return False
    negative_terms = ["failed", "declined", "unsuccessful", "cancelled", "canceled",
                       "delivery status notification", "undeliverable"]
    if any(t in text for t in negative_terms):
        return False
    if any(p in text for p in ENROLLMENT_CONFIRMATION_PHRASES):
        return True
    order_number = (order_number or "").strip().lower()
    if order_number and order_number != "unknown" and order_number in text:
        order_ctx = any(t in text for t in ["order", "welcome", "received", "thank you"])
        enroll_ctx = any(t in text for t in ["enroll", "enrol", "course", "prep", "login", "access", "account"])
        if order_ctx and enroll_ctx:
            return True
    return False


def order_already_handled(service, thread: Dict, connected_email: str,
                           customer_email: str, order_number: str) -> Tuple[bool, str, str]:
    """Determines whether an order should be treated as already handled, so we
    never send a second welcome email. Checks, in order of cheapness:

      1. Local processed_orders.json record for this order_key
      2. Any reply already present in the order's own Gmail thread, from the
         connected account (covers a human replying manually, in-thread)
      3. A Gmail 'Sent' search for an enrollment/welcome confirmation to this
         customer (covers replies sent from a separate thread, e.g. because
         the order notification and the reply ended up in different threads)

    Returns (handled: bool, method: str, note: str).
    """
    key = order_key_for(order_number, customer_email, thread.get("thread_id", ""))

    # 1. Local record
    local = get_processed_order(key)
    if local.get("status") in ("sent", "sent_automatically", "sent_from_dashboard", "already_replied", "manually_handled"):
        return True, "local_record", f"Marked as {local.get('status')} in local records."

    # 2. In-thread reply from connected account
    if thread_has_reply_from_connected_account(thread, connected_email):
        return True, "thread_reply", "A reply from the connected account already exists in this order's email thread."

    # 3. Gmail Sent-folder search (covers replies sent outside this thread)
    if customer_email and customer_email.lower() != "unknown":
        queries = [f'in:sent newer_than:365d to:{customer_email}']
        if order_number and order_number.lower() != "unknown":
            queries.append(f'in:sent newer_than:365d to:{customer_email} "{order_number}"')
        seen_threads = set()
        for query in queries:
            try:
                thread_ids = search_threads(service, query=query, max_results=25)
            except Exception as error:
                print(f"[orders] sent search failed | query={query} | error={error}", flush=True)
                continue
            for sent_thread_id in thread_ids:
                if sent_thread_id in seen_threads or sent_thread_id == thread.get("thread_id", ""):
                    continue
                seen_threads.add(sent_thread_id)
                try:
                    sent_thread = read_thread(service, sent_thread_id)
                except Exception as error:
                    print(f"[orders] sent thread read failed | thread={sent_thread_id} | error={error}", flush=True)
                    continue
                for email in sent_thread.get("emails", []):
                    if not _sent_from_connected_account(email, connected_email):
                        continue
                    if not _sent_message_targets_customer(email, customer_email):
                        continue
                    if _sent_message_is_enrollment_confirmation(email, order_number):
                        print(f"[orders] found prior welcome in Sent | to={customer_email} | order={order_number} | thread={sent_thread_id}", flush=True)
                        return True, "gmail_sent_search", "A welcome/enrollment email to this customer was found in Gmail Sent."

    return False, "not_found", "No existing reply or welcome email found."


# ---------------------------------------------------------------------
# ORDER ITEM BUILDING + SAFE SEND
# ---------------------------------------------------------------------
def build_order_item(service, thread: Dict, connected_email: str) -> Optional[Dict]:
    order_text = get_best_order_email_text(thread)
    if not order_text:
        return None
    if not _thread_on_or_after_scan_start(thread, connected_email):
        return None

    latest = latest_inbound_email(thread, connected_email)
    order_number = extract_order_number(order_text) or "Unknown"
    customer_email = extract_customer_email(order_text, connected_email) or ""
    customer_name = best_customer_name(order_text, customer_email)
    total = extract_total(order_text) or ""
    products = extract_product_lines(order_text)
    thread_id = thread.get("thread_id", "")
    key = order_key_for(order_number, customer_email, thread_id)
    sort_ts = latest_inbound_sort_key(thread, connected_email) or email_date_to_sort_key(latest.get("date", ""))

    base = {
        "order_key": key,
        "thread_id": thread_id,
        "order_number": order_number,
        "customer_name": customer_name,
        "customer_email": customer_email or "Unknown",
        "total": total,
        "products": products,
        "processed_at": latest.get("date", "") or datetime.now().isoformat(timespec="seconds"),
        "sort_ts": sort_ts,
        "original": {
            "from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""),
            "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800),
        },
    }

    if order_text_looks_failed(order_text):
        return {**base, "status": "Failed Order", "reply": None, "order_failed": True,
                "filtered_out": True, "check_note": "Failed/cancelled order notification. No welcome email sent."}

    if not order_text_looks_successful(order_text):
        return {**base, "status": "Needs Review", "reply": None,
                "check_note": "Order email was not clearly a successful/paid order."}

    if not customer_email or not order_number or order_number.lower() == "unknown":
        return {**base, "status": "Needs Review", "reply": None,
                "check_note": "Missing customer email or order number; cannot safely auto-send."}

    handled, method, note = order_already_handled(service, thread, connected_email, customer_email, order_number)
    if handled:
        return {**base, "status": "Already Replied", "reply": None,
                "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
                "check_method": method, "check_note": note}

    subject, body = build_order_welcome_email(customer_name, order_number)
    return {
        **base, "status": "Waiting to Send",
        "reply": {"mode": "new_email", "to": customer_email, "subject": subject, "body": body},
        "check_method": method, "check_note": note,
    }


def send_new_email(service, to_email: str, subject: str, body: str) -> Dict:
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def auto_send_order_if_safe(service, thread: Dict, connected_email: str, order_item: Dict) -> Tuple[Dict, bool]:
    """Sends the order welcome email exactly once, with layered protection:
      - skip if the order isn't a clean successful order
      - skip if order_already_handled() says it's handled (local/thread/Sent)
      - skip if a concurrent scan already claimed this key this run (in-process lock)
      - mark as sent in local storage BEFORE the network call returns control,
        so a retry/overlap can't slip through the same window
    """
    key = order_item.get("order_key", "")
    order_number = str(order_item.get("order_number") or "").strip()
    customer_email = str(order_item.get("customer_email") or "").strip()
    if customer_email.lower() == "unknown":
        customer_email = ""
    customer_name = order_item.get("customer_name") or "Customer"

    if order_item.get("order_failed") or order_item.get("status") in ("Failed Order", "Needs Review", "Already Replied"):
        return order_item, False

    if not customer_email or not order_number or order_number.lower() == "unknown":
        return order_item, False

    if not get_automation_settings().get("auto_reply_enabled", False) and not AUTO_SEND_ORDER_WELCOME:
        return order_item, False

    # In-process lock: prevents two overlapping scans/requests from both passing
    # the "not yet handled" check and double-sending in the same run.
    if key in _ORDER_SEND_LOCK_KEYS:
        return {**order_item, "status": "Already Replied", "reply": None,
                "check_note": "Already sent earlier in this scan run."}, False
    _ORDER_SEND_LOCK_KEYS.add(key)

    # Re-check right before sending (covers state that changed since build_order_item ran).
    handled, method, note = order_already_handled(service, thread, connected_email, customer_email, order_number)
    if handled:
        upsert_processed_order(key, {"status": "manually_handled", "customer_email": customer_email,
                                      "customer_name": customer_name, "check_method": method})
        return {**order_item, "status": "Already Replied", "reply": None, "check_method": method, "check_note": note}, False

    reply = order_item.get("reply") or {}
    subject = reply.get("subject") or "Welcome to Pharmacy Prep"
    body = reply.get("body") or build_order_welcome_email(customer_name, order_number)[1]

    # Mark BEFORE sending, so any concurrent process reading local storage right
    # after this point sees it as claimed, even if the send itself is still in flight.
    upsert_processed_order(key, {
        "status": "sending", "customer_email": customer_email, "customer_name": customer_name,
        "order_number": order_number, "total": order_item.get("total", ""), "products": order_item.get("products", []),
    })

    try:
        sent = send_new_email(service, customer_email, subject, body)
        sent_id = sent.get("id", "") if isinstance(sent, dict) else ""
        sent_at = datetime.now().isoformat(timespec="seconds")
        upsert_processed_order(key, {"status": "sent_automatically", "sent_message_id": sent_id, "sent_at": sent_at})
        print(f"[orders] SENT | key={key} | to={customer_email} | order={order_number} | id={sent_id}", flush=True)
        return {**order_item, "status": "Sent Automatically", "reply": None,
                "reply_sent_at": sent_at, "sent_message_id": sent_id}, True
    except Exception as error:
        # Roll the local mark back to "waiting" so a future scan retries the send.
        upsert_processed_order(key, {"status": "send_failed", "error": str(error)})
        print(f"[orders] SEND FAILED | key={key} | to={customer_email} | order={order_number} | error={error}", flush=True)
        return {**order_item, "status": "Waiting to Send", "auto_send_error": str(error)}, False


# ---------------------------------------------------------------------
# SCAN WINDOW HELPERS
# ---------------------------------------------------------------------
def _date_on_or_after_scan_start(value: str) -> bool:
    parsed = email_date_to_datetime(value or "")
    if parsed == datetime.min:
        return True
    return parsed >= SCAN_START_DT


def _thread_on_or_after_scan_start(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email(thread, connected_email)
    return _date_on_or_after_scan_start(latest.get("date", ""))


def _item_date_for_window(item: Dict) -> str:
    return (item.get("sort_ts") or item.get("processed_at") or item.get("reply_sent_at")
            or item.get("updated_at") or item.get("first_seen_at") or item.get("original", {}).get("date", "") or "")


def _item_on_or_after_scan_start(item: Dict) -> bool:
    return _date_on_or_after_scan_start(_item_date_for_window(item))


# ---------------------------------------------------------------------
# AI: JSON helper, screening, reply composition
# ---------------------------------------------------------------------
def extract_json_like_text(text: str) -> str:
    text = (text or "").strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def parse_ai_json(ai_text: str) -> Optional[Dict]:
    try:
        return json.loads(extract_json_like_text(ai_text))
    except Exception:
        return None


def openai_json(prompt: str, model: str) -> Optional[Dict]:
    try:
        response = client.responses.create(model=model, input=prompt)
        parsed = parse_ai_json((getattr(response, "output_text", "") or "").strip())
        if isinstance(parsed, dict):
            return parsed
        print(f"[ai] {model} returned non-JSON", flush=True)
    except Exception as error:
        print(f"[ai] {model} call failed: {error}", flush=True)
    return None


def compact_ai_context(text: str, limit: int = 9000) -> str:
    text = clean_preview_text(text or "", limit)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def copied_sequence_found(candidate: str, source: str, sequence_len: int = 12) -> bool:
    def tokens(t):
        return [w for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower())
                if w not in {"the", "and", "for", "you", "your", "that", "this", "with", "from",
                             "have", "will", "email", "message", "thank", "thanks", "regards",
                             "pharmacy", "prep"}]
    c, s = tokens(candidate), tokens(source)
    if len(c) < sequence_len or len(s) < sequence_len:
        return False
    chunks = {tuple(s[i:i + sequence_len]) for i in range(len(s) - sequence_len + 1)}
    return any(tuple(c[i:i + sequence_len]) in chunks for i in range(len(c) - sequence_len + 1))


def is_bad_generic_reply(body: str) -> bool:
    text = (body or "").lower().strip()
    if not text:
        return True
    meaningful = re.sub(r"(?is)regards\s*\n\s*pharmacy prep.*$", "", body or "")
    meaningful = re.sub(r"(?im)^\s*(hello|hi|dear)\s+[^,\n]+,?\s*$", "", meaningful).strip()
    if len(meaningful.split()) < 12:
        return True
    bad_phrases = [
        "we received your message and will get back to you", "we received your email and will get back to you",
        "we will review your request and get back to you", "we will get back to you shortly",
        "i will take a look and get back to you", "we will check and get back", "i will check and get back",
        "we will look into", "we will follow up shortly", "please wait while",
    ]
    if any(p in text for p in bad_phrases):
        return True
    if len([l for l in body.splitlines() if re.match(r"^\s*([-*\u2022\u2013\u2014]|\d+[.)])\s+", l)]) > 0:
        return True
    return False


def professionalize_reply(body: str, category: str) -> str:
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?im)^\s*subject\s*:.*\n?", "", text)
    lines = []
    for raw_line in text.split("\n"):
        line = re.sub(r"^\s*[-*\u2022\u2013\u2014]\s+", "", raw_line.strip())
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace("\u2014", ",").replace("\u2013", ",")
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    for pattern in [
        r"(?i)\bwe (received|have received) your (message|email)[^.\n]*(\.|\n)",
        r"(?i)\bwe (will|would) (review|check|look into|verify)[^.\n]*(\.|\n)",
        r"(?i)\b(i|we) (will|would) get back to you (shortly|soon)?\.??",
        r"(?i)\bthank you for (reaching out|your email|contacting us)\.\s*",
    ]:
        text = re.sub(pattern, "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    main = re.split(r"(?i)\n\s*regards\b", text, maxsplit=1)[0].strip()
    paragraphs = [p.strip() for p in main.split("\n\n") if p.strip()][:3]
    main = "\n\n".join(paragraphs).strip()
    if len(main.split()) < 10:
        return ""
    if category == "work":
        signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
        return f"{main}\n\n{signature}"
    return f"{main}\n\nRegards"


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str) -> bool:
    body = professionalize_reply(reply_body or "", category)
    if not body or is_bad_generic_reply(body):
        return True
    latest = clean_preview_text(latest_body or "", 6000).strip()
    if latest and (body.lower().startswith(latest.lower()[:80]) or copied_sequence_found(body, latest, 14)):
        return True
    return False


def heuristic_context_queries(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    text = f"{subject}\n{body}"
    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()

    phrases = []
    for pattern in [r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})",
                     r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})"]:
        for m in re.findall(pattern, text, flags=re.IGNORECASE):
            v = re.sub(r"\s+", " ", m).strip()
            if v and v not in phrases:
                phrases.append(v)

    name_bits = []
    for source in [sender_name or "", infer_customer_name_from_email(sender_email) or ""]:
        for part in re.split(r"[^A-Za-z]+", source):
            if len(part) >= 3 and part.lower() not in {"pharmacy", "prep", "student", "customer", "support"}:
                if part not in name_bits:
                    name_bits.append(part)

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}', f'in:anywhere to:{sender_email}', f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email}) ("Order #" OR invoice OR receipt OR payment OR login OR access OR PEBC OR course OR renewal)',
        ])
    if clean_subject and len(clean_subject) >= 6:
        queries.append(f'in:anywhere "{clean_subject[:80]}"')
    for part in name_bits[:4]:
        queries.append(f'in:anywhere "{part}"')
    for phrase in phrases[:6]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')

    deduped = []
    for q in queries:
        if q and q not in deduped:
            deduped.append(q)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_gmail_context(service, queries: List[str], current_thread_id: str = "") -> str:
    blocks, seen = [], set()
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
        try:
            thread_ids = search_threads(service, query=query, max_results=GMAIL_CONTEXT_THREADS_PER_QUERY)
        except Exception as error:
            blocks.append(f"Search failed for '{query}': {error}")
            continue
        for tid in thread_ids:
            if tid == current_thread_id or tid in seen:
                continue
            seen.add(tid)
            try:
                thread = read_thread(service, tid)
            except Exception:
                continue
            latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
            blocks.append(
                f"Search query: {query}\nThread ID: {tid}\nLatest subject: {latest.get('subject','')}\n"
                f"Latest from: {latest.get('from','')}\nThread excerpt:\n"
                f"{format_thread_for_ai(thread)[:GMAIL_CONTEXT_CHARS_PER_THREAD]}"
            )
            if len(blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                return "\n\n====\n\n".join(blocks)
    return "\n\n====\n\n".join(blocks)


def ai_screen_thread(thread: Dict, connected_email: str, forced_category: str) -> Optional[Dict]:
    latest = latest_inbound_email(thread, connected_email)
    if not latest:
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 5500)
    subject = latest.get("subject", "") or ""
    prompt = f"""Classify this Gmail thread for a reply dashboard.

Include only actionable human messages: a question, request, problem, missing detail,
appointment/decision, student/customer support issue, or personal/business task.

Exclude all promotions, newsletters, marketing, brand updates, and system notifications.

Category MUST be exactly: {forced_category}

Return JSON only:
{{"include": true, "title": "4-9 word dashboard title",
  "summary": "one specific sentence mentioning {display_name} and what they need",
  "reason": "why", "confidence": 0.0}}

Sender: {latest.get('from','')}
Subject: {subject}
Body:
{latest_body}
"""
    parsed = openai_json(prompt, OPENAI_FAST_MODEL)
    if not isinstance(parsed, dict):
        text = norm_text(subject, latest.get("body", ""))
        direct = has_direct_request_signal(text)
        if not direct:
            return {"include": False, "title": "Not actionable", "summary": "No direct request detected.",
                    "reason": "fallback", "confidence": 0.25}
        clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject or "Email", flags=re.IGNORECASE).strip()
        return {"include": True, "title": clean_subject[:70] or "Actionable email",
                "summary": f"{display_name} needs a response about {clean_subject or 'this message'}.",
                "reason": "fallback direct request", "confidence": 0.35}

    include = parsed.get("include", False)
    if isinstance(include, str):
        include = include.strip().lower() in ("true", "yes", "1")
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    return {"include": bool(include), "title": str(parsed.get("title", "") or "").strip(),
            "summary": str(parsed.get("summary", "") or "").strip(),
            "reason": str(parsed.get("reason", "") or "").strip(), "confidence": confidence}


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 11000)

    prompt = f"""Write a concise, professional email reply using the current Gmail thread and related Gmail API context.

Rules:
- No bullet points, numbered lists, dashes, headings, or markdown.
- Do not use filler like "we received your email" or "we will review/get back to you".
- Answer the actual request directly using Gmail context when it has order numbers,
  payment/receipt details, login/access details, course/PEBC details, or prior replies.
- If exact information is missing, say what is missing and ask one specific follow-up question.
- Keep it 60-130 words before the signature.
- For Work emails, end with the exact Pharmacy Prep signature. For Personal emails, end with "Regards" only.

Return JSON only:
{{"title": "4-9 word title", "summary": "one specific sentence", "subject": "{clean_subject}", "body": "full reply only"}}

Work signature:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com

Category: {category}
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject','')}
Latest body:
{latest_body}

Current thread:
{thread_text}

Related Gmail API context:
{extra_context or 'None found'}
"""
    parsed = openai_json(prompt, OPENAI_REPLY_MODEL)
    if not isinstance(parsed, dict):
        return None
    body = professionalize_reply(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        retry_prompt = prompt + "\n\nRewrite once: the previous draft was too generic or empty. Give one concrete answer or a specific follow-up question."
        retry = openai_json(retry_prompt, OPENAI_REPLY_MODEL)
        if isinstance(retry, dict):
            retry_body = professionalize_reply(str(retry.get("body", "") or "").strip(), category)
            if retry_body and not reply_needs_regeneration(retry_body, latest_body, category):
                parsed, body = retry, retry_body
    if not body or reply_needs_regeneration(body, latest_body, category):
        return None
    return {"title": str(parsed.get("title", "") or "").strip(), "summary": str(parsed.get("summary", "") or "").strip(),
            "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject, "body": body}


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Dict:
    """Used only when AI composition genuinely fails twice - keeps the row useful
    rather than empty, while still asking a real next-step question."""
    latest = latest_inbound_email(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    text = norm_text(latest.get("subject", ""), latest.get("body", ""))
    if any(t in text for t in ["login", "access", "password"]):
        account_email = to_email.strip()
        # Prefer an explicitly identified registration address in the student's message.
        supplied = re.search(
            r"(?:registered\s+(?:email|e-mail)(?:\s+address)?|registration\s+(?:email|e-mail)(?:\s+address)?|account\s+(?:email|e-mail)(?:\s+address)?)"
            r"\s*(?:is|was|:|=|-)?\s*([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
            latest.get("body", ""), re.IGNORECASE,
        )
        if supplied:
            account_email = supplied.group(1)
        if account_email:
            main = f"Thank you for letting us know about the access issue. We'll use {account_email} as the starting point for checking your account."
            if "pharmacist" in text and "technician" in text and "mcq" in text:
                main += " We understand that your pharmacist MCQ access is missing after you signed up for the pharmacy technician MCQ course. We'll review the access for both courses."
            else:
                main += " We'll review the access issue you described and follow up with the next steps."
            if not supplied:
                main += " If you registered using a different email address, please send that address instead."
        else:
            main = "Thank you for letting us know about the access issue. Please send the email address used for registration so we can locate your account."
    elif any(t in text for t in ["order number", "order #"]):
        main = "Please confirm the name or email address used for the order so we can locate the correct order number for you."
    elif any(t in text for t in ["invoice", "receipt", "payment"]):
        main = "Please send the payment name, email address, or transaction reference so we can match the record and confirm the details."
    elif any(t in text for t in ["pebc", "exam", "evaluating", "qualifying"]):
        main = "Please send the exact detail you would like confirmed about the exam or course, and we will follow up with accurate information."
    else:
        main = "Please send the specific detail you would like confirmed, and we will respond with the correct information."
    if category == "work":
        body = f"Hello {greeting},\n\n{main}\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    else:
        body = f"Hello {greeting},\n\n{main}\n\nRegards"
    return {"mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


# ---------------------------------------------------------------------
# RENEWAL REQUEST PARSING (EprepStation account renewal forms)
# ---------------------------------------------------------------------
def _renewal_extract_field(text: str, labels: List[str]) -> str:
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").replace("\r", "\n").split("\n")]
    labels_norm = [l.lower() for l in labels]
    for i, line in enumerate(lines):
        low = line.lower().strip(" :\t")
        for label in labels_norm:
            if low == label:
                for nxt in lines[i + 1:i + 6]:
                    nl = nxt.lower().strip(" :\t")
                    if not nxt or nl in labels_norm:
                        continue
                    return nxt.strip(" -:\t")
            if low.startswith(label + ":") or low.startswith(label + " -"):
                return re.sub(rf"^{re.escape(label)}\s*[:\-]?\s*", "", line, flags=re.IGNORECASE).strip(" -:\t")
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^\n]+)", text or "", flags=re.IGNORECASE)
        if m:
            v = re.sub(r"\s+", " ", m.group(1)).strip(" -:\t")
            if v:
                return v
    return ""


def _renewal_extract_email(text: str) -> str:
    explicit = _renewal_extract_field(text, ["Your E-mail Address", "Your Email Address", "Email Address", "Email"])
    candidates = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", explicit or "")
    candidates += re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text or "")
    for e in candidates:
        lowered = e.lower()
        if "pharmacyprep.com" not in lowered and "eprepstation.com" not in lowered:
            return e.strip()
    return ""


def extract_renewal_details(thread: Dict) -> Optional[Dict]:
    for email in thread.get("emails", []):
        subject = email.get("subject", "") or ""
        body = email.get("body", "") or ""
        text = f"Subject: {subject}\n\n{body}"
        if "account renewal request" not in text.lower():
            continue
        student_email = _renewal_extract_email(text)
        course = _renewal_extract_field(text, ["Exam you are taking", "Course", "Course Name", "Exam"])
        name_raw = _renewal_extract_field(text, ["Your Name", "Name"])
        name = name_raw.strip() if name_raw and "@" not in name_raw else (infer_customer_name_from_email(student_email) or "Customer")
        if not student_email or not course:
            continue
        return {
            "student_name": name, "student_email": student_email,
            "course": re.sub(r"\s+", " ", course).strip(" -:\t"),
            "source_subject": subject, "source_from": email.get("from", ""),
            "source_to": email.get("to", ""), "source_date": email.get("date", ""),
        }
    return None


def renewal_key(student_email: str, course: str) -> str:
    return "renewal:" + hashlib.sha1(f"{student_email.lower().strip()}|{course.lower().strip()}".encode()).hexdigest()[:16]


def build_renewal_reply_body(name: str, course: str) -> str:
    first_name = (name or "there").strip().split()[0] if (name or "").strip() else "there"
    return f"""Hello {first_name},

Thank you for submitting your account renewal request for {course}. We have received the request and will review the account details connected to your course access.

We will follow up shortly with the renewal status and any next steps needed to restore or extend your access.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""


def build_renewal_item(details: Dict, existing: Optional[Dict] = None) -> Dict:
    existing = existing or {}
    name = details.get("student_name") or "Customer"
    student_email = details.get("student_email", "")
    course = details.get("course", "")
    key = renewal_key(student_email, course)
    already_replied = existing.get("status") == "Already Replied" or bool(existing.get("reply_sent_at"))
    reply = None if already_replied else {
        "mode": "new_email", "to": student_email,
        "subject": f"Account renewal request - {course}", "body": build_renewal_reply_body(name, course),
    }
    return {
        **existing, "renewal_key": key, "category": "renewal", "is_renewal_request": True,
        "title": f"Account renewal request from {name}",
        "important_reason": f"{name} submitted an account renewal request for {course}.",
        "status": "Already Replied" if already_replied else "Needs Reply",
        "sort_ts": email_date_to_sort_key(details.get("source_date", "")) or existing.get("sort_ts", ""),
        "filtered_out": False,
        "original": {
            "from": details.get("source_from", ""), "to": details.get("source_to", ""),
            "date": details.get("source_date", ""),
            "subject": details.get("source_subject", "Account Renewal Request"),
            "body": f"Student email: {student_email}\nCourse: {course}",
        },
        "reply": reply,
    }


# ---------------------------------------------------------------------
# GENERAL EMAIL ITEM (work/personal)
# ---------------------------------------------------------------------
def build_general_email_item(service, thread: Dict, connected_email: str,
                              work_label_id: str, personal_label_id: str) -> Optional[Dict]:
    latest = latest_inbound_email(thread, connected_email)
    if not latest:
        return None
    if is_infrastructure_notice(norm_text(latest.get("subject", ""), latest.get("body", "")), latest.get("from", "")):
        return None
    thread_id = thread.get("thread_id", "")

    # Already handled by a human/us -> just record as replied, don't re-screen.
    if thread_has_reply_from_connected_account(thread, connected_email):
        stored = get_catalog_item("emails", thread_id)
        if not stored:
            return None
        return {**stored, "thread_id": thread_id, "status": "Already Replied", "reply": None,
                "latest_inbound_id": latest_inbound_message_id(thread, connected_email)}

    category = classify_thread(thread, connected_email)
    if category == "spam":
        return None  # dropped entirely, never stored
    if category == "renewal":
        return None  # handled separately by the renewal pipeline

    text, sender = latest_inbound_context_text(thread, connected_email)
    if is_hard_automated_text(text):
        return None

    screening = ai_screen_thread(thread, connected_email, category)
    if not screening or not screening.get("include"):
        return {
            "thread_id": thread_id, "category": category,
            "title": latest.get("subject", "Email") or "Email",
            "important_reason": (screening or {}).get("reason", "Not actionable."),
            "status": "Filtered Out", "filtered_out": True,
            "latest_inbound_id": latest_inbound_message_id(thread, connected_email),
            "sort_ts": latest_inbound_sort_key(thread, connected_email),
            "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""),
                          "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)},
            "reply": None,
        }

    label_id = work_label_id if category == "work" else personal_label_id
    try:
        apply_label_to_thread_messages(service, thread, label_id)
    except Exception:
        pass

    title = screening.get("title") or latest.get("subject", "Important email") or "Important email"
    important_reason = screening.get("summary") or f"{sender_display_name(latest.get('from',''))} needs a response."

    stored = get_catalog_item("emails", thread_id)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_body = clean_preview_text(latest.get("body", ""), 6000)
    cached_reply = stored.get("reply") if stored.get("latest_inbound_id") == latest_inbound_id else None

    reply = None
    if cached_reply and not reply_needs_regeneration(cached_reply.get("body", ""), latest_body, category):
        reply = cached_reply
    else:
        queries = heuristic_context_queries(thread, connected_email)
        extra_context = gather_gmail_context(service, queries, current_thread_id=thread_id) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        if composed:
            reply = {"mode": "thread_reply", "to": parseaddr(latest.get("from", ""))[1].strip(),
                      "subject": composed.get("subject", ""), "body": composed.get("body", "")}
            if composed.get("summary"):
                important_reason = composed.get("summary")
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                title = composed.get("title")
        else:
            reply = fallback_reply_for_thread(thread, connected_email, category)

    return {
        "thread_id": thread_id, "category": category, "title": title,
        "important_reason": important_reason, "status": "Needs Reply",
        "latest_inbound_id": latest_inbound_id,
        "sort_ts": latest_inbound_sort_key(thread, connected_email),
        "filtered_out": False,
        "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""),
                      "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)},
        "reply": reply,
    }


# ---------------------------------------------------------------------
# VISIBILITY FILTERS (what actually shows up on the dashboard)
# ---------------------------------------------------------------------
def is_catalog_order_visible(item: Dict) -> bool:
    if not isinstance(item, dict) or not _item_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("order_failed"):
        return False
    return True


def is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict) or not _item_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    original = item.get("original") or {}
    if is_infrastructure_notice(norm_text(original.get("subject", ""), original.get("body", "")), original.get("from", "")):
        return False
    category = item.get("category")
    if category == "renewal":
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    if category not in ("work", "personal"):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if reply and reply_needs_regeneration(reply.get("body", ""), item.get("original", {}).get("body", ""), category):
        return False
    return bool(reply) or item.get("status") in ("Already Replied", "Suggestion Removed")


# ---------------------------------------------------------------------
# SCAN ORCHESTRATION
# ---------------------------------------------------------------------
def _order_scan_queries(date_clause: str) -> List[str]:
    base = f'{date_clause} -in:spam -in:trash'
    return [
        f'{base} "New Order:"', f'{base} "[Order #"',
        f'{base} "you have received the following order"',
        f'{base} "Billing address" "Payment method:" "Total:"',
        f'{base} from:(wordpress OR woocommerce) "Order #"',
    ]


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    # Discover broadly. Keyword/category exclusions can hide real student requests.
    base = f'in:anywhere {date_clause} -in:spam -in:trash'
    if connected_email:
        base += f' -from:{connected_email}'
    return [base]


def _recent_email_queries(scan_start: str, connected_email: str) -> List[str]:
    """Non-overlapping calendar-month windows, newest first, including the start date."""
    start = datetime.strptime(scan_start, "%Y-%m-%d")
    upper = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    queries = []
    while upper > start:
        month_start = (upper - timedelta(days=1)).replace(day=1)
        lower = max(start, month_start)
        # Gmail date windows are bounded at calendar-day midnight.
        clause = f"after:{lower.strftime('%Y/%m/%d')} before:{upper.strftime('%Y/%m/%d')}"
        queries.extend(_email_scan_queries(clause, connected_email))
        upper = lower
    return queries


def _collect_thread_ids(service, queries: List[str], per_query_limit: int, total_limit: int) -> List[str]:
    output, seen = [], set()
    for query in queries:
        if len(output) >= total_limit:
            break
        for tid in search_threads(service, query=query, max_results=min(per_query_limit, total_limit - len(output))):
            if tid not in seen:
                seen.add(tid)
                output.append(tid)
                if len(output) >= total_limit:
                    break
    return output


def _scan_date_clause(catalog: Dict, force_full: bool) -> Tuple[str, str]:
    if force_full:
        start_dt = SCAN_START_DT
    else:
        start_dt = datetime.now() - timedelta(days=REFRESH_SCAN_DAYS)
        last = catalog.get("meta", {}).get("last_successful_scan_at", "")
        try:
            if last:
                parsed = datetime.fromisoformat(last[:19]) - timedelta(days=3)
                if parsed < start_dt:
                    start_dt = parsed
        except Exception:
            pass
        if start_dt < SCAN_START_DT:
            start_dt = SCAN_START_DT
    return f"after:{(start_dt - timedelta(days=1)).strftime('%Y/%m/%d')}", start_dt.strftime("%Y-%m-%d")


def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    work_label_id = get_or_create_label(service, WORK_LABEL)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    get_or_create_label(service, RENEWAL_LABEL)

    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog.setdefault("orders", {})
    catalog.setdefault("emails", {})
    date_clause, scan_start_used = _scan_date_clause(catalog, force_full)

    debug = {
        "started_at": datetime.now().isoformat(timespec="seconds"), "force_full": force_full,
        "scan_start": scan_start_used, "orders_checked": 0, "emails_checked": 0,
        "orders_sent": 0, "orders_skipped_handled": 0, "emails_accepted": 0, "emails_dropped_spam": 0,
        "renewals_found": 0, "errors": [],
    }
    print(f"[scan] starting | force_full={force_full} | {date_clause}", flush=True)

    order_thread_ids = _collect_thread_ids(service, _order_scan_queries(date_clause),
                                            per_query_limit=max(20, MAX_ORDER_THREADS_PER_SCAN // 5),
                                            total_limit=MAX_ORDER_THREADS_PER_SCAN)
    email_thread_ids = _collect_thread_ids(service, _recent_email_queries(scan_start_used, connected_email),
                                            per_query_limit=MAX_EMAIL_THREADS_PER_SCAN,
                                            total_limit=MAX_EMAIL_THREADS_PER_SCAN)
    debug["candidate_limit_reached"] = len(email_thread_ids) >= MAX_EMAIL_THREADS_PER_SCAN
    debug["emails_deferred"] = 0
    debug["emails_unchanged"] = 0
    debug["orders_checked"] = len(order_thread_ids)
    debug["emails_checked"] = len(email_thread_ids)

    processed_order_threads = set()
    for thread_id in order_thread_ids:
        try:
            thread = read_thread(service, thread_id)
            order_item = build_order_item(service, thread, connected_email)
            if not order_item:
                continue
            processed_order_threads.add(thread_id)
            order_item, sent = auto_send_order_if_safe(service, thread, connected_email, order_item)
            if sent:
                debug["orders_sent"] += 1
            elif order_item.get("status") == "Already Replied":
                debug["orders_skipped_handled"] += 1
            upsert_catalog_item("orders", order_item.get("order_key", thread_id), order_item)
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["errors"].append(f"order {thread_id}: {error}")

    candidates = []
    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads:
            continue
        try:
            candidates.append(read_thread(service, thread_id))
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["errors"].append(f"read {thread_id}: {error}")
            # Stop immediately on throttling rather than issuing the rest of the batch.
            if getattr(getattr(error, "resp", None), "status", None) in (403, 429):
                raise
    candidates.sort(key=lambda thread: latest_inbound_sort_key(thread, connected_email), reverse=True)
    ai_screenings_used = 0
    for thread in candidates:
        thread_id = thread.get("thread_id", "")
        try:
            if not _thread_on_or_after_scan_start(thread, connected_email):
                continue
            if get_best_order_email_text(thread):
                order_item = build_order_item(service, thread, connected_email)
                if order_item:
                    order_item, sent = auto_send_order_if_safe(service, thread, connected_email, order_item)
                    if sent:
                        debug["orders_sent"] += 1
                    upsert_catalog_item("orders", order_item.get("order_key", thread_id), order_item)
                continue

            renewal_details = extract_renewal_details(thread)
            if renewal_details:
                key = renewal_key(renewal_details["student_email"], renewal_details["course"])
                existing = get_catalog_item("emails", key)
                item = build_renewal_item(renewal_details, existing=existing)
                upsert_catalog_item("emails", key, item)
                debug["renewals_found"] += 1
                continue

            category = classify_thread(thread, connected_email)
            if category == "spam":
                debug["emails_dropped_spam"] += 1
                continue

            stored = get_catalog_item("emails", thread_id)
            latest_id = latest_inbound_message_id(thread, connected_email)
            # Unchanged, previously screened threads must not exhaust every future scan.
            if (latest_id and stored.get("latest_inbound_id") == latest_id
                    and stored.get("screening_version") == EMAIL_SCREENING_VERSION
                    and not thread_has_reply_from_connected_account(thread, connected_email)):
                debug["emails_unchanged"] += 1
                continue
            if ai_screenings_used >= MAX_AI_SCREENINGS_PER_SCAN:
                debug["emails_deferred"] += 1
                continue

            ai_screenings_used += 1
            item = build_general_email_item(service, thread, connected_email, work_label_id, personal_label_id)
            if item:
                item["screening_version"] = EMAIL_SCREENING_VERSION
                upsert_catalog_item("emails", thread_id, item)
                if not item.get("filtered_out"):
                    debug["emails_accepted"] += 1
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["errors"].append(f"email {thread_id}: {error}")

    debug["partial"] = bool(debug["errors"] or debug["emails_deferred"] or debug["candidate_limit_reached"])
    catalog = get_dashboard_catalog()
    catalog["meta"] = {
        **catalog.get("meta", {}), "connected_email": connected_email,
        "last_scan_attempt_at": datetime.now().isoformat(timespec="seconds"),
        "last_successful_scan_at": (catalog.get("meta", {}).get("last_successful_scan_at", "") if debug["partial"]
                                    else datetime.now().isoformat(timespec="seconds")),
        "scan_window": f"{SCAN_START_DISPLAY} onward", "screening_version": EMAIL_SCREENING_VERSION,
    }
    save_dashboard_catalog(catalog)
    save_json_file(SCAN_DEBUG_FILE, {**debug, "finished_at": datetime.now().isoformat(timespec="seconds")})
    invalidate_dashboard_cache()

    payload = build_dashboard_payload(force_refresh=True)
    payload["scan_summary"] = debug
    print(f"[scan] complete | orders_sent={debug['orders_sent']} | orders_skipped={debug['orders_skipped_handled']} | "
          f"emails_accepted={debug['emails_accepted']} | spam_dropped={debug['emails_dropped_spam']} | "
          f"renewals={debug['renewals_found']}", flush=True)
    return payload


# ---------------------------------------------------------------------
# DASHBOARD PAYLOAD + BRIEFING
# ---------------------------------------------------------------------
def _catalog_sort_key(item: Dict) -> str:
    return (item.get("sort_ts") or item.get("reply_sent_at") or item.get("processed_at")
            or item.get("updated_at") or item.get("first_seen_at") or item.get("original", {}).get("date", "") or "")


def build_daily_briefing(connected_email: str, orders: List[Dict], work_emails: List[Dict],
                          personal_emails: List[Dict], renewals: List[Dict]) -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    waiting_orders = [o for o in orders if o.get("reply")]
    handled_orders = [o for o in orders if not o.get("reply") and o.get("status") != "Needs Review"]

    lines = [
        "# AI Summary", "", f"Generated: {now}", f"Connected Gmail: {connected_email}",
        f"Scan window: {SCAN_START_DISPLAY} onward", "", "Executive Overview",
        f"- {len(orders)} orders stored; {len(waiting_orders)} waiting to send, {len(handled_orders)} already handled.",
        f"- {len(work_emails)} work email(s), {len(personal_emails)} personal email(s), {len(renewals)} renewal request(s) need review.",
        "- Spam, promotions, newsletters, and automated notifications are filtered out entirely.",
        "", "Orders",
    ]
    for order in orders[:15]:
        status = "Waiting to send" if order.get("reply") else order.get("status", "Handled")
        lines.append(f"- Order #{order.get('order_number','Unknown')} | {order.get('customer_name','Customer')} | "
                      f"{order.get('customer_email','Unknown')} | {status}")
    if not orders:
        lines.append(f"- No orders from {SCAN_START_DISPLAY} onward yet.")

    lines.extend(["", "Work Emails"])
    for e in work_emails[:15]:
        lines.append(f"- {e.get('title','Email')} | {e.get('status','Needs Reply')} | {e.get('important_reason','')}")
    if not work_emails:
        lines.append("- None currently need review.")

    lines.extend(["", "Personal Emails"])
    for e in personal_emails[:15]:
        lines.append(f"- {e.get('title','Email')} | {e.get('status','Needs Reply')} | {e.get('important_reason','')}")
    if not personal_emails:
        lines.append("- None currently need review.")

    lines.extend(["", "Renewal Requests"])
    for r in renewals[:15]:
        lines.append(f"- {r.get('title','Renewal')} | {r.get('status','Needs Reply')}")
    if not renewals:
        lines.append("- None currently pending.")

    briefing = "\n".join(lines)
    BRIEFING_FILE.write_text(briefing, encoding="utf-8")
    return briefing


def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    if not force_refresh and _dashboard_cache["payload"] and (time.time() - _dashboard_cache["built_at"] < DASHBOARD_CACHE_TTL_SECONDS):
        return deepcopy(_dashboard_cache["payload"])

    catalog = get_dashboard_catalog()
    meta = catalog.get("meta", {})
    connected_email = meta.get("connected_email") or DEFAULT_CONNECTED_EMAIL

    orders = [item for item in catalog.get("orders", {}).values() if is_catalog_order_visible(item)]
    orders.sort(key=_catalog_sort_key, reverse=True)

    all_emails = [item for item in catalog.get("emails", {}).values() if is_catalog_email_visible(item)]
    all_emails.sort(key=_catalog_sort_key, reverse=True)
    work_emails = [e for e in all_emails if e.get("category") == "work"]
    personal_emails = [e for e in all_emails if e.get("category") == "personal"]
    renewals = [e for e in all_emails if e.get("category") == "renewal"]

    briefing = build_daily_briefing(connected_email, orders, work_emails, personal_emails, renewals)
    pending_reply_ids = [e.get("thread_id") or e.get("renewal_key") for e in all_emails if e.get("reply")] + \
                        [o.get("order_key") for o in orders if o.get("reply")]

    payload = {
        "ok": True, "connected_email": connected_email,
        "orders": orders, "emails": all_emails, "work_emails": work_emails,
        "personal_emails": personal_emails, "renewals": renewals,
        "pending_replies": [p for p in pending_reply_ids if p],
        "briefing": briefing,
        "automation_settings": {**get_automation_settings(), "last_auto_scan_at": meta.get("last_successful_scan_at", "")},
        "stats": {
            "orders_waiting": len([o for o in orders if o.get("reply")]),
            "orders_handled": len([o for o in orders if not o.get("reply")]),
            "pending_replies": len([p for p in pending_reply_ids if p]),
            "work_emails": len(work_emails), "personal_emails": len(personal_emails),
            "renewal_emails": len(renewals),
        },
    }
    _dashboard_cache["built_at"] = time.time()
    _dashboard_cache["payload"] = deepcopy(payload)
    return payload


def read_daily_briefing() -> str:
    if not BRIEFING_FILE.exists():
        return "No briefing yet. Click Scan Gmail to create one."
    return BRIEFING_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# MANUAL SEND (dashboard "Send" button) - same dedup protection as auto-send
# ---------------------------------------------------------------------
def send_frontend_thread_reply(service, thread: Dict, connected_email: str, to_email: str, subject: str, body: str) -> Dict:
    if not thread.get("emails"):
        raise ValueError("Thread has no emails.")
    if thread_has_reply_from_connected_account(thread, connected_email):
        raise ValueError("This conversation already has a reply from the connected account.")
    latest_email = latest_inbound_email(thread, connected_email)
    resolved_to = (to_email or "").strip() or parseaddr(latest_email.get("from", ""))[1].strip()
    if not resolved_to:
        raise ValueError("Could not determine a recipient email address.")
    clean_subject = (subject or latest_email.get("subject", "Your email")).strip()
    if not clean_subject.lower().startswith("re:"):
        clean_subject = "Re: " + clean_subject
    clean_body = (body or "").strip()
    if not clean_body:
        raise ValueError("Reply body is empty.")
    message = MIMEText(clean_body, "plain", "utf-8")
    message["To"] = resolved_to
    message["Subject"] = clean_subject
    if latest_email.get("message_id_header"):
        message["In-Reply-To"] = latest_email["message_id_header"]
        references = latest_email.get("references", "")
        message["References"] = (references + " " + latest_email["message_id_header"]).strip() if references else latest_email["message_id_header"]
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"threadId": thread.get("thread_id", ""), "raw": raw}).execute()


def send_order_reply_manually(service, connected_email: str, order_item: Dict, to_override: str,
                               subject: str, body: str) -> Dict:
    key = order_item.get("order_key", "")
    order_number = str(order_item.get("order_number") or "").strip()
    customer_email = (to_override or order_item.get("customer_email") or "").strip()
    if not customer_email or customer_email.lower() == "unknown":
        raise ValueError("Please provide a valid recipient email address.")

    try:
        thread = read_thread(service, order_item.get("thread_id", ""))
    except Exception:
        thread = {"emails": [], "thread_id": order_item.get("thread_id", "")}

    handled, method, note = order_already_handled(service, thread, connected_email, customer_email, order_number)
    if handled:
        upsert_catalog_item("orders", key, {**order_item, "status": "Already Replied", "reply": None, "check_note": note})
        raise ValueError(f"This order already appears handled ({note})")

    customer_name = order_item.get("customer_name") or "Customer"
    final_subject = (subject or "").strip() or f"Welcome to Pharmacy Prep"
    final_body = (body or "").strip() or build_order_welcome_email(customer_name, order_number)[1]

    upsert_processed_order(key, {"status": "sending", "customer_email": customer_email, "customer_name": customer_name})
    sent = send_new_email(service, customer_email, final_subject, final_body)
    sent_id = sent.get("id", "") if isinstance(sent, dict) else ""
    sent_at = datetime.now().isoformat(timespec="seconds")
    upsert_processed_order(key, {"status": "sent_from_dashboard", "sent_message_id": sent_id, "sent_at": sent_at})
    upsert_catalog_item("orders", key, {**order_item, "status": "Already Replied", "reply": None,
                                          "reply_sent_at": sent_at, "sent_message_id": sent_id})
    return sent


# ---------------------------------------------------------------------
# APP LOGIN / GMAIL OAUTH ROUTES
# ---------------------------------------------------------------------
def _is_app_logged_in() -> bool:
    return bool(session.get("app_logged_in"))


@app.before_request
def _require_app_login():
    path = request.path or "/"
    allowed = (path in ("/login", "/login.html", "/auth/gmail", "/oauth2callback", "/favicon.ico")
               or path.startswith("/static/"))
    if allowed or _is_app_logged_in():
        return None
    if path.startswith("/api/"):
        return jsonify({"ok": False, "auth_required": True, "auth_type": "app_login",
                         "error": "Please sign in to continue.",
                         "login_url": f"/login?next={_safe_next_url(request.full_path)}"}), 401
    return redirect(f"/login?next={_safe_next_url(request.full_path)}")


@app.route("/login", methods=["GET", "POST"])
@app.route("/login.html", methods=["GET"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        next_url = _safe_next_url(request.form.get("next") or request.args.get("next") or "/")
        if username == APP_LOGIN_USERNAME and password == APP_LOGIN_PASSWORD:
            session.clear()
            session.permanent = True
            session["app_logged_in"] = True
            session["app_user"] = username
            return redirect(next_url)
        return redirect(f"/login?error=1&next={next_url}")
    login_path = BASE_DIR / "login.html"
    if login_path.exists():
        return send_from_directory(BASE_DIR, "login.html")
    return "Put login.html next to backend.py.", 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/auth/gmail")
def auth_gmail():
    next_url = _safe_next_url(request.args.get("next") or request.referrer or "/")
    session["gmail_auth_return_to"] = next_url
    flow = _make_gmail_flow()
    authorization_url, state_value = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    session["gmail_oauth_state"] = state_value
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    if request.host.startswith("127.0.0.1") or request.host.startswith("localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_gmail_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    invalidate_dashboard_cache()
    return redirect(_safe_next_url(session.pop("gmail_auth_return_to", "/")))


def _json_gmail_auth_required():
    return jsonify({"ok": False, "auth_required": True, "auth_type": "gmail",
                     "error": "Please sign in again.", "auth_url": _gmail_auth_url(request.full_path or "/")}), 401


@app.errorhandler(GmailAuthRequired)
def _handle_gmail_auth_required(error):
    return _json_gmail_auth_required()


# ---------------------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------------------
@app.route("/")
def home():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return send_from_directory(BASE_DIR, "index.html")
    return "Put index.html next to backend.py, then open http://127.0.0.1:5050/"


@app.route("/api/dashboard")
def api_dashboard():
    try:
        return jsonify(build_dashboard_payload(force_refresh=False))
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        body = request.get_json(silent=True) or {}
        payload = perform_gmail_scan(force_full=bool(body.get("force_full", False)))
        summary = payload.get("scan_summary", {})
        return jsonify({
            "ok": True,
            "message": (
                f"Scan incomplete: {summary.get('emails_checked', 0)} candidate threads, "
                f"{summary.get('emails_accepted', 0)} accepted, {summary.get('emails_deferred', 0)} deferred, "
                f"{len(summary.get('errors', []))} errors. "
                + ("Candidate limit reached; older mail may remain unchecked. " if summary.get("candidate_limit_reached") else "")
                + "See last_scan_debug.json for details."
                if summary.get("partial") else
                f"Scan complete: {summary.get('emails_checked', 0)} candidate threads checked; "
                f"{summary.get('emails_accepted', 0)} emails accepted."
            ),
            "partial": summary.get("partial", False),
            "emails_deferred": summary.get("emails_deferred", 0),
            "errors_count": len(summary.get("errors", [])),
            "orders_sent": summary.get("orders_sent", 0),
            "orders_skipped_handled": summary.get("orders_skipped_handled", 0),
            "emails_accepted": summary.get("emails_accepted", 0),
            "emails_dropped_spam": summary.get("emails_dropped_spam", 0),
            "renewals_found": summary.get("renewals_found", 0),
            "orders_checked": summary.get("orders_checked", 0),
            "emails_checked": summary.get("emails_checked", 0),
            "scan_start": summary.get("scan_start", ""),
            "scan_window": f"{SCAN_START_DISPLAY} onward",
        })
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/automation-settings", methods=["GET", "POST"])
def api_automation_settings():
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "settings": get_automation_settings()})
        payload = request.get_json(silent=True) or {}
        updates = {}
        if "auto_reply_enabled" in payload:
            updates["auto_reply_enabled"] = bool(payload.get("auto_reply_enabled"))
        if "auto_scan_enabled" in payload:
            updates["auto_scan_enabled"] = bool(payload.get("auto_scan_enabled"))
        if "auto_scan_minutes" in payload:
            updates["auto_scan_minutes"] = max(1, int(payload.get("auto_scan_minutes")))
        save_automation_settings(updates)
        invalidate_dashboard_cache()
        return jsonify({"ok": True, "settings": get_automation_settings()})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/replies/<path:item_key>/send", methods=["POST"])
def api_send_reply(item_key: str):
    """item_key is either an order_key, a renewal_key, or a Gmail thread_id."""
    try:
        if not get_automation_settings().get("auto_reply_enabled", False) and not AUTO_SEND_ORDER_WELCOME:
            # Manual sends are still allowed even when auto-send is off; this only
            # blocks nothing here, kept for clarity that auto vs manual are independent.
            pass
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        body = request.get_json(silent=True) or {}
        to_override = (body.get("to") or "").strip()
        subject = (body.get("subject") or "").strip()
        reply_body = (body.get("body") or "").strip()

        # 1. Order?
        order_item = get_catalog_item("orders", item_key)
        if order_item:
            sent = send_order_reply_manually(service, connected_email, order_item, to_override, subject, reply_body)
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})

        # 2. Renewal?
        renewal_item = get_catalog_item("emails", item_key)
        if renewal_item and renewal_item.get("is_renewal_request"):
            reply = renewal_item.get("reply") or {}
            to_email = to_override or reply.get("to", "")
            if not to_email:
                raise ValueError("Please provide a valid recipient email address.")
            final_subject = subject or reply.get("subject") or "Account renewal request"
            final_body = professionalize_reply(reply_body or reply.get("body", ""), "work")
            sent = send_new_email(service, to_email, final_subject, final_body)
            upsert_catalog_item("emails", item_key, {**renewal_item, "status": "Already Replied", "reply": None,
                                                       "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
                                                       "sent_message_id": sent.get("id", "")})
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})

        # 3. Regular work/personal thread reply.
        thread = read_thread(service, item_key)
        sent = send_frontend_thread_reply(service, thread, connected_email, to_override, subject, reply_body)
        upsert_catalog_item("emails", item_key, {"status": "Already Replied", "reply": None,
                                                   "reply_sent_at": datetime.now().isoformat(timespec="seconds")})
        invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/replies/<path:item_key>", methods=["DELETE"])
def api_remove_reply(item_key: str):
    try:
        for kind in ("orders", "emails"):
            item = get_catalog_item(kind, item_key)
            if item:
                upsert_catalog_item(kind, item_key, {"status": "Suggestion Removed", "reply": None})
                invalidate_dashboard_cache()
                return jsonify({"ok": True, "message": "Suggested reply removed."})
        return jsonify({"ok": False, "error": "Item not found."}), 404
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/briefing")
def api_briefing():
    try:
        return jsonify({"ok": True, "briefing": read_daily_briefing()})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


if __name__ == "__main__":
    print("\nPharmacy Prep Gmail Assistant is starting...")
    port = int(os.getenv("PORT", "5050"))
    print(f"Open this link in your browser: http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
