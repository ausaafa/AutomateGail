import os
import base64
import re
import json
import time
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
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from googleapiclient.discovery import build
load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env file.")
client = OpenAI(api_key=OPENAI_API_KEY)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
BRIEFING_FILE = BASE_DIR / "daily_briefing.md"
PROCESSED_ORDERS_FILE = BASE_DIR / "processed_orders.json"
PROCESSED_ACTIONS_FILE = BASE_DIR / "processed_actions.json"
AUTOMATION_SETTINGS_FILE = BASE_DIR / "automation_settings.json"
DASHBOARD_CATALOG_FILE = BASE_DIR / "dashboard_catalog.json"
PERSONAL_LABEL = "Personal"
WORK_LABEL = "Work"
DASHBOARD_CACHE_TTL_SECONDS = 30
DEFAULT_CONNECTED_EMAIL = os.getenv("CONNECTED_EMAIL", "success@pharmacyprep.com")
try:
    SCAN_START_DT = datetime.strptime(os.getenv("SCAN_START_DATE", "2026-06-01"), "%Y-%m-%d")
except Exception:
    SCAN_START_DT = datetime(2026, 6, 1)
SCAN_START_DISPLAY = f"{SCAN_START_DT.strftime('%B')} {SCAN_START_DT.day}, {SCAN_START_DT.year}"
# Gmail date search is date-only. Use the previous day so June 1 itself is included.
SCAN_START_GMAIL_AFTER = (SCAN_START_DT - timedelta(days=1)).strftime("%Y/%m/%d")
INCREMENTAL_SCAN_DAYS = int(os.getenv("INCREMENTAL_SCAN_DAYS", "7"))
MAX_ORDER_THREADS_PER_SCAN = int(os.getenv("MAX_ORDER_THREADS_PER_SCAN", "140"))
MAX_EMAIL_THREADS_PER_SCAN = int(os.getenv("MAX_EMAIL_THREADS_PER_SCAN", "160"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "40"))
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "45"))
EMAIL_SCREENING_VERSION = "2026-06-context-v4"
_dashboard_cache = {
    "built_at": 0.0,
    "payload": None,
}
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
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
def get_automation_settings() -> Dict:
    data = load_json_file(AUTOMATION_SETTINGS_FILE, {})
    return {
        "auto_reply_enabled": bool(data.get("auto_reply_enabled", True)),
        "auto_scan_enabled": bool(data.get("auto_scan_enabled", True)),
        "auto_scan_minutes": max(1, int(data.get("auto_scan_minutes", 1))),
    }
def save_automation_settings(updates: Dict):
    current = get_automation_settings()
    current.update(updates)
    save_json_file(AUTOMATION_SETTINGS_FILE, current)
def invalidate_dashboard_cache():
    _dashboard_cache["built_at"] = 0.0
    _dashboard_cache["payload"] = None
# ---------------------------------------------------------------------
# GMAIL AUTH
# ---------------------------------------------------------------------
def get_gmail_service():
    token_path = BASE_DIR / "token.json"
    credentials_path = BASE_DIR / "credentials.json"
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            token_path.unlink(missing_ok=True)
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                token_path.unlink(missing_ok=True)
                creds = None
        if not creds or not creds.valid:
            if not credentials_path.exists():
                raise FileNotFoundError("Missing credentials.json. Put credentials.json in the same folder as backend.py.")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)
def get_connected_email(service) -> str:
    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress", "Unknown Gmail account")
    except Exception:
        return "Unknown Gmail account"
# ---------------------------------------------------------------------
# GMAIL HELPERS
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
    plain_texts = []
    html_texts = []
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
        return "\n\n".join(part for part in plain_texts if part).strip()
    if html_texts:
        return "\n\n".join(part for part in html_texts if part).strip()
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
        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=page_size,
            pageToken=page_token,
        ).execute()
        for message in result.get("messages", []):
            thread_id = message.get("threadId")
            if thread_id and thread_id not in seen:
                seen.add(thread_id)
                thread_ids.append(thread_id)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return thread_ids
def search_recent_threads(service, query: str, max_results: int = 100) -> List[str]:
    return search_threads(service, query, max_results=max_results)
def gmail_search_any(service, query: str, max_results: int = 5) -> bool:
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return bool(result.get("messages"))
def read_thread(service, thread_id: str) -> Dict:
    thread = service.users().threads().get(
        userId="me",
        id=thread_id,
        format="full",
    ).execute()
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
    return {
        "thread_id": thread_id,
        "message_ids": message_ids,
        "emails": emails,
    }
def format_thread_for_ai(thread: Dict) -> str:
    sections = []
    for index, email in enumerate(thread.get("emails", []), start=1):
        sections.append(
            f"""
EMAIL {index}
From: {email.get("from", "")}
To: {email.get("to", "")}
Date: {email.get("date", "")}
Subject: {email.get("subject", "")}
Body:
{email.get("body", "")}
""".strip()
        )
    return "\n\n---\n\n".join(sections)
def combined_thread_text(thread: Dict) -> str:
    pieces = []
    for email in thread.get("emails", []):
        pieces.extend([
            f"Subject: {email.get('subject', '')}",
            f"From: {email.get('from', '')}",
            f"To: {email.get('to', '')}",
            f"Date: {email.get('date', '')}",
            "",
            email.get("body", ""),
            "",
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
    if parsed == datetime.min:
        return ""
    return parsed.isoformat(timespec="seconds")

def _date_is_on_or_after_scan_start(value: str) -> bool:
    parsed = email_date_to_datetime(value or "")
    if parsed == datetime.min:
        # Do not throw away saved records solely because Gmail gave no parseable date.
        return True
    return parsed >= SCAN_START_DT

def _thread_is_on_or_after_scan_start(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _date_is_on_or_after_scan_start(latest.get("date", ""))

def _item_date_for_window(item: Dict) -> str:
    return (
        item.get("sort_ts")
        or item.get("processed_at")
        or item.get("reply_sent_at")
        or item.get("updated_at")
        or item.get("first_seen_at")
        or item.get("original", {}).get("date", "")
        or ""
    )

def _item_is_on_or_after_scan_start(item: Dict) -> bool:
    return _date_is_on_or_after_scan_start(_item_date_for_window(item))

def latest_inbound_sort_key(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return email_date_to_sort_key(latest.get("date", ""))
def latest_inbound_email_for_dashboard(thread: Dict, connected_email: str) -> Dict:
    connected = (connected_email or "").lower().strip()
    inbound = []
    for email in thread.get("emails", []):
        sender = parseaddr(email.get("from", ""))[1].lower().strip()
        if connected and sender == connected:
            continue
        inbound.append(email)
    if inbound:
        return inbound[-1]
    return thread.get("emails", [])[-1] if thread.get("emails") else {}
def latest_inbound_message_id(thread: Dict, connected_email: str = "") -> str:
    connected = (connected_email or "").lower().strip()
    latest_id = ""
    for email in thread.get("emails", []):
        sender_email = parseaddr(email.get("from", ""))[1].lower().strip()
        if connected and sender_email == connected:
            continue
        latest_id = email.get("gmail_message_id", "") or latest_id
    return latest_id or (thread.get("message_ids", [""]) or [""])[-1]
def thread_action_key(thread: Dict, connected_email: str = "") -> str:
    return f"{thread.get('thread_id', '')}:{latest_inbound_message_id(thread, connected_email)}"
def latest_email_is_from_connected_account(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    latest_email = thread["emails"][-1]
    latest_sender = parseaddr(latest_email.get("from", ""))[1].lower().strip()
    connected = (connected_email or "").lower().strip()
    return bool(connected and latest_sender == connected)
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
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]
def apply_label_to_thread_messages(service, thread: Dict, label_id: str):
    for message_id in thread.get("message_ids", []):
        if not message_id:
            continue
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id], "removeLabelIds": []},
        ).execute()
# ---------------------------------------------------------------------
# TRACKING
# ---------------------------------------------------------------------
def get_processed_actions() -> Dict:
    return load_json_file(PROCESSED_ACTIONS_FILE, {})
def get_thread_action(thread_key: str) -> Dict:
    return get_processed_actions().get(thread_key, {})
def is_thread_action_processed(thread_key: str) -> bool:
    return bool(get_thread_action(thread_key))
def mark_thread_action_processed(thread_key: str, action_type: str, external_id: str = "", **extra):
    data = get_processed_actions()
    data[thread_key] = {
        "action_type": action_type,
        "external_id": external_id,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    save_json_file(PROCESSED_ACTIONS_FILE, data)
def get_processed_orders() -> Dict:
    return load_json_file(PROCESSED_ORDERS_FILE, {})
def upsert_processed_order(order_number: str, payload: Dict):
    data = get_processed_orders()
    current = data.get(order_number, {})
    data[order_number] = {
        **current,
        **payload,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_json_file(PROCESSED_ORDERS_FILE, data)
def get_dashboard_catalog() -> Dict:
    data = load_json_file(DASHBOARD_CATALOG_FILE, {})
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
def update_catalog_meta(**updates):
    catalog = get_dashboard_catalog()
    meta = catalog.setdefault("meta", {})
    meta.update({key: value for key, value in updates.items() if value is not None})
    save_dashboard_catalog(catalog)
def upsert_catalog_item(kind: str, thread_id: str, payload: Dict):
    catalog = get_dashboard_catalog()
    bucket = catalog.setdefault(kind, {})
    current = bucket.get(thread_id, {})
    bucket[thread_id] = {
        **current,
        **payload,
        "thread_id": thread_id,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "first_seen_at": current.get("first_seen_at") or payload.get("first_seen_at") or datetime.now().isoformat(timespec="seconds"),
    }
    save_dashboard_catalog(catalog)
def get_catalog_item(kind: str, thread_id: str) -> Dict:
    return get_dashboard_catalog().get(kind, {}).get(thread_id, {})
# ---------------------------------------------------------------------
# ORDER DETECTION
# ---------------------------------------------------------------------
def looks_like_order_email(text: str) -> bool:
    lowered = text.lower()
    indicators = [
        "new order:",
        "you’ve received the following order",
        "you've received the following order",
        "[order #",
        "billing address",
        "payment method:",
        "total:",
    ]
    score = sum(1 for item in indicators if item in lowered)
    return score >= 3
def get_best_order_email_text(thread: Dict) -> Optional[str]:
    for email in thread.get("emails", []):
        subject = email.get("subject", "")
        body = email.get("body", "")
        combined = f"Subject: {subject}\n\n{body}"
        if looks_like_order_email(combined):
            return combined
    return None
def extract_order_number(text: str) -> Optional[str]:
    patterns = [
        r"New\s+Order:\s*#?\s*(\d+)",
        r"\[Order\s*#\s*(\d+)\]",
        r"Order\s*#\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None
def extract_customer_name(text: str) -> Optional[str]:
    from_match = re.search(r"received\s+the\s+following\s+order\s+from\s+(.+?):", text, flags=re.IGNORECASE)
    if from_match:
        return from_match.group(1).strip()
    billing_match = re.search(r"Billing address\s+([A-Za-z][^\n\r]+)", text, flags=re.IGNORECASE)
    if billing_match:
        return billing_match.group(1).strip()
    return None
def infer_customer_name_from_email(email: str) -> Optional[str]:
    local = (email or "").split("@")[0].strip().lower()
    if not local:
        return None
    cleaned = re.sub(r"\d+", " ", local)
    cleaned = re.sub(r"[._+-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    common_first_names = [
        "rajani", "rajanish", "naveen", "kiratpal", "parbdeep", "karan", "jolly",
        "olufolahan", "debbie", "mehak", "aman", "harpreet", "mandeep", "sukh",
        "preet", "simran", "jaspreet", "gurpreet", "ravneet", "navneet", "prabh",
        "parm", "manpreet", "komal", "neha", "ravi", "rahul", "sandeep",
    ]
    compact = cleaned.replace(" ", "")
    for first in sorted(common_first_names, key=len, reverse=True):
        if compact.startswith(first) and len(compact) > len(first) + 2:
            rest = compact[len(first):]
            return f"{first.title()} {rest.title()}"
    return " ".join(part.title() for part in cleaned.split() if part)
def extract_customer_email(text: str, connected_email: str = "") -> Optional[str]:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if not emails:
        return None
    blocked_fragments = [
        "noreply",
        "no-reply",
        "wordpress",
        "woocommerce",
        "pharmacyprep.com",
        "eprepstation.com",
    ]
    connected_email = connected_email.lower().strip()
    for email in emails:
        lowered = email.lower().strip()
        if connected_email and lowered == connected_email:
            continue
        if any(blocked in lowered for blocked in blocked_fragments):
            continue
        return email
    return emails[-1]
def best_customer_name(text: str, email: str = "") -> str:
    name = extract_customer_name(text or "")
    if name and name.lower() not in ("unknown", "student", "customer"):
        return name
    inferred = infer_customer_name_from_email(email or extract_customer_email(text or "") or "")
    if inferred:
        return inferred
    return "Customer"
def extract_product_lines(text: str) -> List[str]:
    products = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if "$" in line and any(token in lowered for token in ("prep", "book", "exam", "course", "digital", "pebc")):
            products.append(line)
    return products[:5]
def extract_total(text: str) -> Optional[str]:
    total_match = re.search(r"Total:\s*\$?([0-9,]+\.\d{2})", text, flags=re.IGNORECASE)
    if total_match:
        return "$" + total_match.group(1)
    return None
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
def was_order_message_already_sent(service, customer_email: str, order_number: str) -> bool:
    """Return True if this customer/order already appears to have been answered manually or by this app."""
    if not customer_email:
        return False
    order_number = str(order_number or "").strip()
    queries = [
        f'in:sent newer_than:365d to:{customer_email}',
        f'in:sent newer_than:365d "{customer_email}"',
    ]
    if order_number and order_number.lower() != "unknown":
        queries = [
            f'in:sent newer_than:365d to:{customer_email} "order #{order_number}"',
            f'in:sent newer_than:365d to:{customer_email} "{order_number}"',
            f'in:sent newer_than:365d "{customer_email}" "order #{order_number}"',
            f'in:sent newer_than:365d "{customer_email}" "{order_number}"',
            f'in:sent newer_than:365d to:{customer_email} (welcome OR enrolled OR enrollment OR login OR course)',
        ]
    for query in queries:
        try:
            if gmail_search_any(service, query, max_results=5):
                return True
        except Exception:
            continue
    return False

def was_thread_manually_replied(service, thread: Dict, connected_email: str) -> bool:
    """A thread is already handled when the newest actual message is from the connected Gmail account."""
    return latest_email_is_from_connected_account(thread, connected_email)
# ---------------------------------------------------------------------
# GENERAL EMAIL HEURISTICS
# ---------------------------------------------------------------------
def thread_has_reply_worthy_signals(thread: Dict) -> bool:
    latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 8000).lower()
    text = f"{subject}\n{body}"
    question_words = [
        "?", "can you", "could you", "would you", "please", "i need", "i need help",
        "i want", "i would like", "i am interested", "can i", "could i", "how do i",
        "when will", "where is", "what is", "what's", "wondering", "let me know",
        "advise", "help", "question", "follow up", "follow-up", "confirm",
        "send me", "share", "provide", "update", "clarify", "explain",
    ]
    domain_words = [
        "order number", "order", "invoice", "receipt", "payment", "refund", "mouse",
        "course", "login", "access", "pebc", "exam", "class", "schedule", "notes",
        "recording", "extension", "renewal", "enroll", "enrol", "registration",
        "meeting", "call", "client", "lawyer", "availability", "available",
    ]
    has_question = any(token in text for token in question_words)
    has_domain = any(token in text for token in domain_words)
    return has_question and (has_domain or len(body.split()) >= 5)

def is_obvious_automated_email(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    subject = (latest.get("subject", "") or "").lower()
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = combined_thread_text(thread).lower()
    block_subjects = [
        "naplex", "please moderate", "new question submitted", "question submitted",
        "comment awaiting moderation", "awaiting moderation", "moderate:", "[moderate]",
        "wordpress", "woocommerce status", "newsletter", "promotion", "promotional",
        "sale", "limited time", "deal", "discount", "subscribe", "subscription",
        "security alert", "login alert", "verification code", "password reset",
        "auto-reply", "automatic reply", "delivery status notification", "undeliverable",
        "mail delivery", "digest", "notification", "order has shipped", "has shipped",
        "on the way", "out for delivery", "delivered", "payment received", "e-transfer received",
        "etransfer received", "receipt for your payment", "invoice paid", "charge receipt",
    ]
    block_senders = [
        "noreply", "no-reply", "donotreply", "wordpress", "mailer-daemon",
        "postmaster", "notifications@", "marketing@", "auto@", "billing@",
    ]
    block_body = [
        "unsubscribe", "you received this email because", "manage your preferences",
        "view this email in your browser", "click here to unsubscribe", "marketing email",
        "this notification was sent", "new user registration", "track your package",
        "your package is on the way", "payment has been received", "e-transfer received",
        "this is an automated message", "do not reply to this email",
    ]
    if any(term in subject for term in block_subjects):
        return True
    if any(term in sender for term in block_senders):
        return True
    if any(term in text for term in block_body) and not thread_has_reply_worthy_signals(thread):
        return True
    return False

def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    text = f"{latest.get('subject', '')}\n{latest.get('body', '')}".lower()
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    personal_words = [
        "family", "mom", "dad", "sister", "brother", "birthday",
        "dinner", "appointment", "personal", "vacation", "weekend", "catch up",
    ]
    work_words = [
        "pharmacy", "prep", "course", "pebc", "exam", "mock", "student",
        "class", "order", "payment", "invoice", "login", "access", "extension",
        "renewal", "enroll", "enrol", "registration", "book", "notes",
        "recording", "support", "refund", "schedule", "meeting", "call",
        "client", "lawyer", "business", "mouse",
    ]
    if any(word in text for word in personal_words):
        return "personal"
    if any(word in text for word in work_words):
        return "work"
    if sender and not sender.endswith("@pharmacyprep.com"):
        return "work"
    return "work"

def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    """Fast non-AI pre-screen. This should be inclusive enough to avoid missing
    human/student emails, while hard-blocking obvious automation before OpenAI is used.
    OpenAI then makes the final important/work/personal decision.
    """
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if is_obvious_automated_email(thread, connected_email):
        return False

    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 7000).lower()
    text = f"{subject}\n{body}"

    hard_excludes = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery", "delivered",
        "e-transfer received", "etransfer received", "interac e-transfer", "payment received",
        "receipt", "thanks for your payment", "invoice paid", "charge receipt", "successful payment",
        "your order is confirmed", "order confirmation", "tracking number", "shipment", "shipping confirmation",
        "newsletter", "unsubscribe", "promotion", "webinar", "download your certificate",
        "please moderate", "comment awaiting moderation", "new question submitted", "security alert",
        "verification code", "password reset", "delivery status notification", "undeliverable",
    ]
    if any(term in text for term in hard_excludes):
        return False

    # Human request cues. Do not require Pharmacy Prep keywords here; otherwise real student/customer
    # questions like "can you send it?" or "what is my order number?" can be missed.
    request_cues = [
        "?", "can you", "could you", "would you", "please", "let me know", "wondering",
        "i need", "need help", "i would like", "how do i", "when will", "where is",
        "what is", "what's", "confirm", "clarify", "advise", "help", "question",
        "follow up", "follow-up", "send me", "share", "provide", "update me", "details",
        "available", "availability", "looking for", "interested in", "request", "can i",
        "do you", "should i", "am i", "is there", "are there", "i have not received",
        "i didn", "i did not", "not received", "still waiting", "checking in",
    ]
    topic_cues = [
        "order number", "mouse", "order", "invoice", "payment", "refund", "course",
        "login", "access", "pebc", "exam", "class", "schedule", "notes", "recording",
        "extension", "renewal", "enroll", "enrol", "registration", "meeting", "call",
        "client", "lawyer", "student", "support", "announcement", "qualifying", "evaluating",
    ]

    asks_for_action = any(term in text for term in request_cues)
    on_topic = any(term in text for term in topic_cues)
    human_sender = bool(sender) and not any(blocked in sender for blocked in [
        "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@", "marketing@", "wordpress", "woocommerce"
    ])

    if asks_for_action:
        return True
    if human_sender and on_topic and len(body.split()) >= 4:
        return True
    if human_sender and thread.get("emails") and len(thread.get("emails", [])) >= 2 and len(body.split()) >= 4:
        # A short follow-up in an existing thread may be important even without a clear keyword.
        return True
    return False



# ---------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------
def extract_json_like_text(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text.replace("```json", "", 1).strip()
    if text.startswith("```"):
        text = text.replace("```", "", 1).strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text
def parse_ai_json(ai_text: str) -> Optional[Dict]:
    try:
        return json.loads(extract_json_like_text(ai_text))
    except Exception:
        return None
def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_email = parseaddr(latest.get("from", ""))[1].strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    raw_text = f"{subject}\n{body}".lower()

    # Pull strong searchable entities from the student's message. These are used to
    # find order confirmations, prior replies, payments, access emails, and product details in Gmail.
    quoted_phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})",
        r"#\s*(\d{3,})",
        r"(?:for|about|regarding)\s+([a-zA-Z0-9][a-zA-Z0-9 ._-]{2,40})",
    ]:
        for match in re.findall(pattern, raw_text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in quoted_phrases:
                quoted_phrases.append(value)

    stopwords = {
        "the", "and", "for", "that", "with", "this", "from", "have", "your", "please",
        "could", "would", "about", "there", "their", "them", "they", "what", "when",
        "where", "which", "need", "help", "reply", "email", "thanks", "thank", "hello",
        "regards", "pharmacy", "prep", "course", "order", "number", "student", "message",
        "information", "details", "update", "know", "send", "sent", "asking", "regarding",
    }
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", raw_text)
    priority = []
    for token in tokens:
        token = token.lower().strip()
        if token in stopwords or token.isdigit():
            continue
        if token not in priority:
            priority.append(token)
        if len(priority) >= 10:
            break

    queries = []
    if sender_email:
        # Most important: find this student's/customer's previous order/support history both directions.
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:anywhere ({sender_email})',
            f'in:anywhere from:{sender_email} ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access)',
            f'in:anywhere to:{sender_email} ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access)',
            f'in:sent to:{sender_email}',
        ])
    for phrase in quoted_phrases[:4]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in priority[:6]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:12]

def plan_context_queries_with_ai(thread: Dict, connected_email: str, category: str) -> List[str]:
    # Keep dashboard loading reliable: use deterministic Gmail queries first.
    return heuristic_context_queries_for_thread(thread, connected_email)

def search_processed_orders_context(sender_email: str, latest_text: str) -> str:
    sender_email = (sender_email or "").lower().strip()
    latest_text = (latest_text or "").lower()
    keywords = [token for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", latest_text) if len(token) >= 4][:12]
    blocks = []
    for order_number, item in get_processed_orders().items():
        customer_email = (item.get("customer_email", "") or "").lower().strip()
        products = " | ".join(item.get("products", []) or [])
        haystack = f"{customer_email}\n{products}\n{item.get('customer_name', '')}\n{item.get('status', '')}".lower()
        email_match = sender_email and customer_email == sender_email
        keyword_match = any(keyword in haystack for keyword in keywords)
        if email_match or keyword_match:
            blocks.append(
                f"Stored order #{order_number} | customer={item.get('customer_name', '')} | email={item.get('customer_email', '')} | total={item.get('total', '')} | products={products} | status={item.get('status', '')}"
            )
        if len(blocks) >= 6:
            break
    return "\n".join(blocks)
def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = 3) -> str:
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL CONTEXT ================\n\n"
    for query in (queries or [])[:6]:
        try:
            thread_ids = search_threads(service, query=query, max_results=max_threads_per_query)
            for thread_id in thread_ids:
                if thread_id == current_thread_id or thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                thread = read_thread(service, thread_id)
                latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
                context_blocks.append(
                    f"Search query: {query}\n"
                    f"Thread ID: {thread_id}\n"
                    f"Latest subject: {latest.get('subject', '')}\n"
                    f"Latest from: {latest.get('from', '')}\n"
                    f"Thread content:\n{format_thread_for_ai(thread)[:6000]}"
                )
                if len(context_blocks) >= 8:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)
def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    """Use OpenAI only after hard Gmail rules have found a possible human request.

    This is the AI screening layer: it decides whether the latest inbound message is
    actually important enough for the dashboard and whether it belongs in Work or
    Personal. It deliberately rejects notifications, receipts, newsletters, status
    updates, and FYI-only messages even when they contain words like course/order.
    """
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None

    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 5000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 9000)

    prompt = f"""
You are screening Gmail for a Pharmacy Prep dashboard. Decide if the latest inbound email should be shown to the user.

Show messages that are genuinely actionable: a student/customer/vendor/personal contact is asking a question, requesting help, asking for a decision, asking for details, reporting a problem, asking for a missing order/login/payment/course detail, or continuing a conversation that needs a human reply.

Exclude these even if they mention orders, payment, course, exam, or account:
- shipping, tracking, delivered, order status, payment received, e-transfer received, receipts, invoices paid, confirmations
- newsletters, promotions, announcements sent as one-way broadcast messages
- WordPress/WooCommerce/system notifications, moderation notices, app alerts, security alerts
- no-reply/automated messages, delivery failures, calendar reminders
- FYI-only messages, thank-you messages, or messages where no response is expected

Category rules:
- work = Pharmacy Prep, PEBC, students, courses, orders, payments, invoices, support, vendors, business/client messages.
- personal = friends/family/personal appointments that are not Pharmacy Prep/business.
- If it is actionable and could affect Pharmacy Prep or a student/customer, choose work.

Return JSON only:
{{
  "include": true,
  "category": "work",
  "title": "4-9 word dashboard title",
  "summary": "specific one-sentence summary mentioning {display_name} and the concrete thing they need",
  "reason": "why this is important enough to show",
  "confidence": 0.0
}}

Sender display name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get("subject", "")}
Latest body:
{latest_body}

Current thread:
{thread_text}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        include = parsed.get("include", False)
        if isinstance(include, str):
            include = include.strip().lower() in ("true", "yes", "1", "include")
        category = str(parsed.get("category", "work")).lower().strip()
        if category not in ("work", "personal"):
            category = "work"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        return {
            "include": bool(include),
            "category": category,
            "title": str(parsed.get("title", "")).strip(),
            "summary": str(parsed.get("summary", "")).strip(),
            "reason": str(parsed.get("reason", "")).strip(),
            "confidence": confidence,
        }
    except Exception as error:
        return None

def sender_display_name(from_value: str, fallback_email: str = "") -> str:
    name, email = parseaddr(from_value or "")
    clean_name = re.sub(r"[\"<>]", "", name or "").strip()
    if clean_name and "@" not in clean_name:
        return clean_name.split()[0].title() if len(clean_name.split()) <= 3 else clean_name.title()
    inferred = infer_customer_name_from_email(email or fallback_email or "")
    return inferred or "The sender"


def compact_ai_context(text: str, limit: int = 12000) -> str:
    text = clean_preview_text(text or "", limit)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def normalize_for_overlap(text: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if token not in {
        "the", "and", "for", "you", "your", "that", "this", "with", "from", "have", "will",
        "email", "message", "thank", "thanks", "regards", "pharmacy", "prep"
    }]


def copied_sequence_found(candidate: str, source: str, sequence_len: int = 12) -> bool:
    candidate_tokens = normalize_for_overlap(candidate)
    source_tokens = normalize_for_overlap(source)
    if len(candidate_tokens) < sequence_len or len(source_tokens) < sequence_len:
        return False
    source_chunks = {tuple(source_tokens[index:index + sequence_len]) for index in range(0, len(source_tokens) - sequence_len + 1)}
    for index in range(0, len(candidate_tokens) - sequence_len + 1):
        if tuple(candidate_tokens[index:index + sequence_len]) in source_chunks:
            return True
    return False


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = (reply_body or "").strip()
    latest = clean_preview_text(latest_body or "", 5000).strip()
    body_lower = body.lower()
    if len(body.split()) < 28:
        return True
    bad_fillers = [
        "we received your message and will get back to you",
        "we received your message",
        "we will review your request and get back to you shortly",
        "thank you for your email. we will review your request",
        "wanted to reply right away",
        "based on the information currently available, i may need to verify",
    ]
    if any(phrase in body_lower for phrase in bad_fillers):
        return True
    if latest and body_lower.startswith(latest.lower()[:80]):
        return True
    if copied_sequence_found(body, latest, sequence_len=12):
        return True
    body_tokens = set(normalize_for_overlap(body))
    latest_tokens = set(normalize_for_overlap(latest))
    if latest_tokens and len(body_tokens) >= 10:
        overlap_ratio = len(body_tokens & latest_tokens) / max(1, min(len(body_tokens), len(latest_tokens)))
        if overlap_ratio > 0.72 and len(body.split()) > 40:
            return True
    if category != "personal" and "pharmacy prep" not in body_lower:
        return True
    return False


def summary_is_generic(summary: str) -> bool:
    lowered = (summary or "").strip().lower()
    generic = [
        "student is asking for update",
        "student is asking a course-related question",
        "latest inbound email contains",
        "sender is asking",
        "conversation contains",
        "important email",
        "suggested frontend reply ready",
    ]
    return not lowered or any(phrase in lowered for phrase in generic) or len(lowered.split()) < 8


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 9000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 15000)
    prompt = f"""
You are Pharmacy Prep's senior email assistant. Write an outbound reply that sounds like a helpful human at Pharmacy Prep wrote it after reading the current Gmail thread and checking the related Gmail context.

Critical direction:
- DO NOT repeat, paraphrase, or mirror the sender's email back to them.
- DO NOT write from the sender's point of view.
- DO NOT copy the inbound message into the reply.
- DO NOT use vague filler such as "we received your message and will get back to you".
- The reply must answer the actual request as specifically as the available Gmail context allows.
- Use the Gmail context aggressively: if the sender asks for an order number, product, receipt, payment, course/login/access detail, prior sent message, or announcement detail, search the provided Gmail context and include the found detail directly in the reply.
- If a relevant order number appears anywhere in Stored order context or Related Gmail context, include that order number in the answer.
- If the exact answer is not available after checking the context, say exactly what was checked, explain what information is still missing, give the best next step, and ask only one specific follow-up question if needed.
- The reply should be detailed enough to be useful, normally 2-4 short paragraphs plus the signature for work emails.
- Keep it professional, warm, informative, and complete.
- For work emails, sign exactly with the Pharmacy Prep signature below.
- For personal emails, do not use the Pharmacy Prep signature.

Also create a dashboard summary that is more specific than "student is asking for update". Mention the sender's name when available and the concrete topic, for example: "{display_name} is asking for details regarding the latest PEBC exam announcement and how it affects upcoming preparation."

Return JSON only with this schema:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence dashboard summary mentioning who is asking, the concrete topic, and what Gmail/context was checked or needs checking",
  "subject": "{clean_subject}",
  "body": "full outbound reply only; do not include the inbound email text"
}}

Work signature to use when category is work:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com

Category: {category}
Sender display name: {display_name}
Sender email: {sender_email}
Latest inbound subject: {latest.get("subject", "")}
Latest inbound body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail context found through Gmail API searches:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if parsed and str(parsed.get("body", "")).strip():
            body = str(parsed.get("body", "")).strip()
            if reply_needs_regeneration(body, latest_body, category):
                return None
            return {
                "title": str(parsed.get("title", "")).strip(),
                "summary": str(parsed.get("summary", "")).strip(),
                "subject": str(parsed.get("subject", clean_subject)).strip() or clean_subject,
                "body": body,
            }
    except Exception:
        pass
    return None

def send_new_email(service, to_email: str, subject: str, body: str) -> Dict:
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = subject
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return service.users().messages().send(
        userId="me",
        body={"raw": raw_message},
    ).execute()
def send_frontend_thread_reply(service, thread: Dict, connected_email: str, subject: str, body: str) -> Dict:
    if not thread.get("emails"):
        raise ValueError("Thread has no emails.")
    if latest_email_is_from_connected_account(thread, connected_email):
        raise ValueError("Latest message is already from your Gmail account.")
    latest_email = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest_email.get("from", ""))
    if not to_email:
        raise ValueError("Could not find recipient email address.")
    clean_subject = (subject or latest_email.get("subject", "Your email")).strip()
    if not clean_subject.lower().startswith("re:"):
        clean_subject = "Re: " + clean_subject
    clean_body = (body or "").strip()
    if not clean_body:
        raise ValueError("Reply body is empty.")
    message = MIMEText(clean_body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = clean_subject
    if latest_email.get("message_id_header"):
        message["In-Reply-To"] = latest_email["message_id_header"]
        references = latest_email.get("references", "")
        if references:
            message["References"] = references + " " + latest_email["message_id_header"]
        else:
            message["References"] = latest_email["message_id_header"]
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return service.users().messages().send(
        userId="me",
        body={
            "threadId": thread.get("thread_id", ""),
            "raw": raw_message,
        },
    ).execute()
# ---------------------------------------------------------------------
# DASHBOARD BUILDERS
# ---------------------------------------------------------------------
def build_order_item(service, thread: Dict, connected_email: str) -> Optional[Dict]:
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return None
    order_text = get_best_order_email_text(thread)
    if not order_text:
        return None
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    order_number = extract_order_number(order_text)
    customer_email = extract_customer_email(order_text, connected_email)
    customer_name = best_customer_name(order_text, customer_email)
    total = extract_total(order_text)
    products = extract_product_lines(order_text)
    thread_key = thread_action_key(thread, connected_email)
    action = get_thread_action(thread_key)
    stored_order = get_processed_orders().get(order_number or "", {})
    status = "Waiting to Send"
    reply = None
    if action.get("action_type") == "dismissed":
        status = "Suggestion Removed"
    elif action.get("action_type") in ("frontend_reply_sent", "order_auto_sent", "manual_or_prior_reply_found"):
        status = "Already Replied"
    elif stored_order.get("status") in ("Already Replied", "Sent", "Sent from Dashboard", "Sent Automatically", "Suggestion Removed"):
        status = stored_order.get("status")
    elif was_thread_manually_replied(service, thread, connected_email):
        status = "Already Replied"
    elif customer_email and order_number and was_order_message_already_sent(service, customer_email, order_number):
        status = "Already Replied"
    if status == "Waiting to Send" and customer_email and order_number:
        subject, body = build_order_welcome_email(customer_name, order_number)
        reply = {
            "thread_id": thread.get("thread_id", ""),
            "mode": "new_email",
            "to": customer_email,
            "subject": subject,
            "body": body,
        }
    sort_ts = latest_inbound_sort_key(thread, connected_email) or email_date_to_sort_key(stored_order.get("updated_at", ""))
    return {
        "thread_id": thread.get("thread_id", ""),
        "order_number": order_number or "Unknown",
        "customer_name": customer_name,
        "customer_email": customer_email or "Unknown",
        "total": total or "",
        "products": products,
        "processed_at": stored_order.get("updated_at") or latest.get("date", "") or datetime.now().isoformat(timespec="seconds"),
        "sort_ts": sort_ts,
        "status": status,
        "reply": reply,
        "original": {
            "from": latest.get("from", ""),
            "to": latest.get("to", ""),
            "date": latest.get("date", ""),
            "subject": latest.get("subject", ""),
            "body": clean_preview_text(latest.get("body", ""), 1800),
        },
    }

def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting_name = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    latest_body = clean_preview_text(latest.get("body", ""), 1200).replace("\n", " ").strip()
    lowered = f"{latest.get('subject', '')}\n{latest_body}".lower()

    if "pebc" in lowered and ("announcement" in lowered or "update" in lowered or "latest" in lowered):
        core = "Thank you for reaching out about the latest PEBC exam announcement. We understand you are looking for clarity on what the update means for your preparation. We will review the announcement against the course schedule/materials and reply with the specific details that apply to you."
        follow = "If there is a specific part of the announcement you want us to confirm, please send that portion as well so we can address it directly."
    elif "pebc" in lowered or "exam" in lowered:
        core = "Thank you for your email about the PEBC exam. We understand you are looking for clear guidance, so we will review the exact exam/course details and respond with the information most relevant to your situation."
        follow = "Please include the exam stream or course you are referring to if it was not already mentioned."
    elif "order number" in lowered:
        core = "Thank you for your email. We will check the order history connected to your email address and confirm the correct order number for you."
        follow = "If the order may have been placed under a different email address or name, please send that detail so we can locate it faster."
    elif "invoice" in lowered or "receipt" in lowered:
        core = "Thank you for your email. We will check the billing/order record connected to your email address and provide the correct invoice or receipt details."
        follow = "If the payment was made under a different name or email address, please send that information so we can match it correctly."
    elif "login" in lowered or "access" in lowered:
        core = "Thank you for your email. We will check your enrollment and access details, then send the correct login or course-access instructions."
        follow = "Please confirm the email address you used for registration if it is different from this email."
    elif "payment" in lowered or "paid" in lowered:
        core = "Thank you for your email. We will check the payment record and confirm the status clearly for you."
        follow = "If you have a transaction reference or payment screenshot, please send it so we can match the record accurately."
    elif "course" in lowered or "class" in lowered or "recording" in lowered or "notes" in lowered:
        core = "Thank you for your email. We will review the relevant course details and send you the correct information about the class, materials, recordings, or next steps."
        follow = "Please let us know the exact course or module you are asking about if it was not already included."
    else:
        core = "Thank you for your email. We reviewed your request and will respond with the specific information needed for this conversation rather than a generic update."
        follow = "If there is one exact detail you need confirmed first, please send it and we will address that directly."

    if category == "personal":
        body = f"""Hello {greeting_name},

{core}

{follow}

Regards"""
    else:
        body = f"""Hello {greeting_name},

{core}

{follow}

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    return {
        "thread_id": thread.get("thread_id", ""),
        "mode": "thread_reply",
        "to": to_email,
        "subject": subject,
        "body": body,
    }

def build_important_reason(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    display_name = sender_display_name(latest.get("from", ""), parseaddr(latest.get("from", ""))[1])
    text = f"{latest.get('subject', '')}\n{latest.get('body', '')}".lower()
    clean_name = display_name if display_name and display_name != "The sender" else "The sender"

    if "pebc" in text and ("announcement" in text or "announced" in text or "update" in text or "latest" in text):
        return f"{clean_name} is asking for details regarding the latest PEBC exam announcement and needs guidance on what it means for their preparation."
    if "pebc" in text or "exam" in text or "qualifying" in text or "evaluating" in text:
        return f"{clean_name} is asking a PEBC exam-related question that needs a clear answer about the relevant exam, course, or preparation details."
    if "order number" in text:
        return f"{clean_name} is asking for an order number, so Gmail/order history should be checked before replying."
    if "invoice" in text or "receipt" in text:
        return f"{clean_name} is asking for invoice or receipt information that may need to be verified from Gmail/order records."
    if "refund" in text:
        return f"{clean_name} is asking about a refund or payment issue and needs a careful account-specific response."
    if "payment" in text or "paid" in text:
        return f"{clean_name} is asking about payment details and needs a clear confirmation based on available records."
    if "login" in text or "access" in text or "password" in text:
        return f"{clean_name} needs help with login or course access and is waiting for account-support instructions."
    if "course" in text or "class" in text or "recording" in text or "notes" in text:
        return f"{clean_name} is asking about course details, materials, classes, or recordings and needs a direct Pharmacy Prep response."
    if "extension" in text or "renewal" in text:
        return f"{clean_name} is asking about an extension or renewal and needs confirmation of the available options."
    if "meeting" in text or "call" in text or "available" in text or "availability" in text or "schedule" in text:
        return f"{clean_name} is trying to coordinate timing or availability and needs a scheduling response."
    if "details" in text or "information" in text or "info" in text:
        return f"{clean_name} is asking for more details and needs a specific response based on the current conversation and Gmail context."
    return f"{clean_name} sent a reply-worthy question or request that needs a specific response rather than a generic acknowledgement."

def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    thread_id = thread.get("thread_id", "")
    thread_key = thread_action_key(thread, connected_email)
    action = get_thread_action(thread_key)
    stored_item = get_catalog_item("emails", thread_id)
    category = stored_item.get("category") or dashboard_category_for_thread(thread, connected_email)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)
    title = stored_item.get("title") or (latest.get("subject", "") or "Important email").strip()
    strict_candidate = should_consider_thread_for_dashboard(thread, connected_email)

    if was_thread_manually_replied(service, thread, connected_email):
        if not stored_item:
            return None
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": stored_item.get("important_reason") or "Latest message is already from the connected account, so this conversation appears handled.",
            "status": "Already Replied",
            "reply_sent_at": stored_item.get("reply_sent_at") or datetime.now().isoformat(timespec="seconds"),
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
                "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
                "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
                "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
            },
            "reply": None,
        }

    has_history = bool(stored_item) and (
        stored_item.get("reply_sent_at")
        or stored_item.get("status") in ("Already Replied", "Suggestion Removed", "Needs Reply")
        or stored_item.get("reply")
    )
    if not strict_candidate and not has_history:
        return None

    screening = None
    if strict_candidate:
        screening = analyze_dashboard_thread_with_ai(thread, connected_email)
        # If OpenAI successfully screened the email and says it is not actionable, hide it.
        # This is the main guard against random notifications or FYI emails.
        if screening and not screening.get("include"):
            return {
                "thread_id": thread_id,
                "category": category,
                "title": title,
                "important_reason": "",
                "status": "Filtered Out",
                "filtered_out": True,
                "ai_screened": True,
                "screen_confidence": screening.get("confidence", 0),
                "screening_version": EMAIL_SCREENING_VERSION,
                "latest_inbound_id": latest_inbound_id,
                "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
                "original": {
                    "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
                    "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
                    "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
                    "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
                    "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
                },
                "reply": None,
            }
        if screening and screening.get("include"):
            category = screening.get("category") or category
            if screening.get("title") and 3 <= len(screening.get("title", "").split()) <= 12:
                title = screening.get("title", "").strip()
        elif screening is None:
            # If AI screening fails, only allow very clear human questions through.
            latest_text_for_fallback = f"{latest.get('subject', '')}\n{clean_preview_text(latest.get('body', ''), 2000)}".lower()
            very_clear = ("?" in latest_text_for_fallback or "order number" in latest_text_for_fallback or "please" in latest_text_for_fallback) and len(latest_text_for_fallback.split()) >= 6
            if not very_clear:
                return None

    if category == "personal":
        apply_label_to_thread_messages(service, thread, personal_label_id)
    else:
        apply_label_to_thread_messages(service, thread, work_label_id)

    stored_reason = stored_item.get("important_reason", "")
    screen_summary = (screening or {}).get("summary", "")
    if screen_summary and not summary_is_generic(screen_summary):
        important_reason = screen_summary.strip()
    else:
        important_reason = stored_reason if stored_reason and not summary_is_generic(stored_reason) else build_important_reason(thread, connected_email)
    reply = None
    status = stored_item.get("status") or ("Needs Reply" if strict_candidate else "No Reply Needed")
    reply_sent_at = stored_item.get("reply_sent_at", "")

    if action.get("action_type") == "dismissed":
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": important_reason,
            "status": "Suggestion Removed",
            "reply_sent_at": reply_sent_at,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
                "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
                "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
                "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
            },
            "reply": None,
        }

    if action.get("action_type") == "frontend_reply_sent":
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": important_reason,
            "status": "Already Replied",
            "reply_sent_at": action.get("processed_at", "") or reply_sent_at,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
                "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
                "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
                "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
            },
            "reply": None,
        }

    if strict_candidate:
        status = "Needs Reply"
        latest_clean_body = clean_preview_text(latest.get("body", ""), 5000)
        cached_reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
        cached_is_usable = bool(cached_reply) and not reply_needs_regeneration(cached_reply.get("body", ""), latest_clean_body, category)

        if cached_is_usable and not summary_is_generic(important_reason):
            reply = cached_reply
        else:
            queries = heuristic_context_queries_for_thread(thread, connected_email)
            extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=3) if queries else ""
            composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
            if composed:
                if composed.get("summary") and not summary_is_generic(composed.get("summary", "")):
                    important_reason = composed.get("summary", "").strip()
                if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                    title = composed.get("title", "").strip()
                reply = {
                    "thread_id": thread_id,
                    "mode": "thread_reply",
                    "to": parseaddr(latest.get("from", ""))[1].strip(),
                    "subject": composed.get("subject", ""),
                    "body": composed.get("body", ""),
                }
            if not reply or not str(reply.get("body", "")).strip() or reply_needs_regeneration(reply.get("body", ""), latest_clean_body, category):
                reply = fallback_reply_for_thread(thread, connected_email, category)
                if summary_is_generic(important_reason):
                    important_reason = build_important_reason(thread, connected_email)
    else:
        status = stored_item.get("status") or "No Reply Needed"
        reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None

    return {
        "thread_id": thread_id,
        "category": category,
        "title": title,
        "important_reason": important_reason,
        "status": status,
        "reply_sent_at": reply_sent_at,
        "latest_inbound_id": latest_inbound_id,
        "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
        "ai_screened": bool(screening) or stored_item.get("ai_screened", False),
        "screen_confidence": (screening or {}).get("confidence", stored_item.get("screen_confidence", 0)),
        "screening_version": EMAIL_SCREENING_VERSION,
        "original": {
            "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
            "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
            "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
            "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
            "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
        },
        "reply": reply,
    }

def build_daily_briefing(connected_email: str, orders: List[Dict], emails: List[Dict]) -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    orders = [order for order in orders if _is_catalog_order_visible(order)]
    emails = [email for email in emails if _is_catalog_email_visible(email)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)

    # De-duplicate duplicate order notifications in the summary by order number/customer email.
    seen_orders = set()
    deduped_orders = []
    for order in orders:
        key = (str(order.get("order_number", "")).lower(), str(order.get("customer_email", "")).lower())
        if key in seen_orders:
            continue
        seen_orders.add(key)
        deduped_orders.append(order)

    waiting_orders = [order for order in deduped_orders if order.get("reply")]
    handled_orders = [order for order in deduped_orders if not order.get("reply")]
    work_emails = [email for email in emails if email.get("category") == "work"]
    personal_emails = [email for email in emails if email.get("category") == "personal"]

    lines = [
        "# AI Summary",
        "",
        f"Generated: {now}",
        f"Connected Gmail: {connected_email}",
        f"Scan window: {SCAN_START_DISPLAY} onward",
        "",
        "Executive Overview",
        f"- {len(deduped_orders)} June-forward orders are stored on the dashboard; {len(waiting_orders)} still need approval/sending and {len(handled_orders)} are already handled.",
        f"- {len(work_emails)} work email(s) and {len(personal_emails)} personal email(s) passed the important-message screen and need review.",
        "- Shipping updates, e-transfer/payment receipts, newsletters, WordPress/system notifications, confirmations, and no-reply alerts are intentionally hidden.",
        "",
        "Orders",
    ]

    if deduped_orders:
        for order in deduped_orders[:15]:
            status = "Waiting to send" if order.get("reply") else order.get("status", "Handled")
            product_text = "; ".join(order.get("products", [])[:2])
            total_text = f" | Total: {order.get('total')}" if order.get("total") else ""
            product_part = f" | Products: {product_text}" if product_text else ""
            lines.append(
                f"- Order #{order.get('order_number', 'Unknown')} | {order.get('customer_name', 'Customer')} | "
                f"{order.get('customer_email', 'Unknown email')} | {status}{total_text}{product_part}"
            )
    else:
        lines.append(f"- No orders from {SCAN_START_DISPLAY} onward are stored yet.")

    lines.extend(["", "Actionable Emails"] )
    if emails:
        for email in emails[:25]:
            reason = email.get("important_reason", "").strip() or "Needs a specific response."
            original = email.get("original", {})
            subject = original.get("subject", "") or email.get("title", "Email")
            sender = original.get("from", "Unknown sender")
            status = email.get("status", "Needs Reply")
            reply_state = "suggested reply ready" if email.get("reply") else "handled/no draft"
            lines.append(
                f"- [{email.get('category', 'work').title()}] {email.get('title', subject)} | From: {sender} | "
                f"Subject: {subject} | Status: {status} ({reply_state}) | Summary: {reason}"
            )
    else:
        lines.append(f"- No work/personal messages from {SCAN_START_DISPLAY} onward passed the important-message screen yet. Run Scan Gmail once after this update to re-screen June-forward messages with the broader Gmail search and stricter AI filter.")

    lines.extend([
        "",
        "How to read this",
        "- Orders remain visible after they are handled so you can confirm what was processed.",
        "- Work/personal email rows only appear when the latest inbound message looks like a real human question/request and passes AI screening.",
        "- Suggested replies use the current thread plus related Gmail search context, including matching order/payment/login/access history when available.",
    ])

    briefing = "\n".join(lines)
    BRIEFING_FILE.write_text(briefing, encoding="utf-8")
    return briefing

def read_daily_briefing() -> str:
    if not BRIEFING_FILE.exists():
        return "No briefing yet. Click Scan Gmail to create one."
    return BRIEFING_FILE.read_text(encoding="utf-8")
def _safe_iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _catalog_sort_key(item: Dict) -> str:
    return item.get("sort_ts") or item.get("reply_sent_at") or item.get("processed_at") or item.get("updated_at") or item.get("first_seen_at") or item.get("original", {}).get("date", "") or ""

def _catalog_text(item: Dict) -> str:
    return "\n".join([
        str(item.get("title", "")),
        str(item.get("important_reason", "")),
        str(item.get("status", "")),
        str(item.get("original", {}).get("from", "")),
        str(item.get("original", {}).get("subject", "")),
        str(item.get("original", {}).get("body", "")),
    ]).lower()

def _catalog_item_looks_unimportant(item: Dict) -> bool:
    text = _catalog_text(item)
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "e-transfer received", "etransfer received", "interac e-transfer",
        "payment received", "receipt for your payment", "charge receipt", "invoice paid",
        "tracking number", "shipment", "unsubscribe", "promotion", "newsletter",
        "please moderate", "comment awaiting moderation", "new question submitted",
        "security alert", "verification code", "password reset", "mail delivery", "undeliverable",
        "do not reply", "no-reply", "noreply",
    ]
    return any(term in text for term in bad_terms)

def _is_catalog_order_visible(item: Dict) -> bool:
    return _item_is_on_or_after_scan_start(item)

def _is_catalog_email_visible(item: Dict) -> bool:
    """Show only June-2026-and-newer actionable dashboard emails. Old or filtered items stay stored but hidden."""
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if item.get("screening_version") != EMAIL_SCREENING_VERSION:
        return False

    reason = item.get("important_reason", "")
    has_specific_reason = bool(reason) and not summary_is_generic(reason)
    has_reply = bool(item.get("reply"))
    handled = item.get("status") in ("Already Replied", "Suggestion Removed") and has_specific_reason

    # After this fix, suggested-reply emails must have passed AI screening before showing.
    # Existing handled items can remain visible when they have a specific reason, but old cached
    # suggestions from earlier buggy filters are hidden until a June-forward scan re-screens them.
    if has_reply and not item.get("ai_screened"):
        return False
    if item.get("ai_screened") and not has_reply and not handled and not has_specific_reason:
        return False

    return has_reply or handled or (has_specific_reason and item.get("ai_screened"))

def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    """Return the saved dashboard fast. Gmail/OpenAI work happens in /api/scan, which the UI runs automatically."""
    if not force_refresh and _dashboard_cache["payload"] and (time.time() - _dashboard_cache["built_at"] < DASHBOARD_CACHE_TTL_SECONDS):
        return deepcopy(_dashboard_cache["payload"])

    catalog = get_dashboard_catalog()
    meta = catalog.get("meta", {})
    connected_email = meta.get("connected_email") or DEFAULT_CONNECTED_EMAIL

    orders = [item for item in catalog.get("orders", {}).values() if _is_catalog_order_visible(item)]
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]

    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)

    # Keep the AI Summary tab working even before/after a scan refreshes daily_briefing.md.
    briefing = build_daily_briefing(connected_email, orders, emails)
    pending_reply_ids = [item["thread_id"] for item in emails if item.get("reply")] + [item["thread_id"] for item in orders if item.get("reply")]
    orders_replied = len([order for order in orders if not order.get("reply")])

    payload = {
        "ok": True,
        "connected_email": connected_email,
        "orders": orders,
        "emails": emails,
        "pending_replies": pending_reply_ids,
        "briefing": briefing,
        "automation_settings": {
            **get_automation_settings(),
            "last_auto_scan_at": meta.get("last_successful_scan_at", ""),
        },
        "stats": {
            "orders_replied": orders_replied,
            "pending_replies": len(pending_reply_ids),
            "work_emails": len([email for email in emails if email.get("category") == "work"]),
            "personal_emails": len([email for email in emails if email.get("category") == "personal"]),
        },
    }
    _dashboard_cache["built_at"] = time.time()
    _dashboard_cache["payload"] = deepcopy(payload)
    return payload

def _scan_after_clause_for_catalog(catalog: Dict, force_full: bool = False) -> Tuple[str, str]:
    """Return a Gmail date clause that never reaches before June 2026.

    Full scans start at June 1, 2026. Incremental scans start from the previous
    successful scan minus a small overlap, but never earlier than June 1, 2026.
    """
    start_dt = SCAN_START_DT
    meta = catalog.get("meta", {})
    # If this is the first run after the June-2026 window/screening change, do a full June-forward scan once.
    if meta.get("scan_start_date") != SCAN_START_DT.strftime("%Y-%m-%d") or meta.get("email_screening_version") != EMAIL_SCREENING_VERSION:
        force_full = True
    if not force_full:
        last_scan = meta.get("last_successful_scan_at", "")
        try:
            if last_scan:
                parsed = datetime.fromisoformat(last_scan[:19]) - timedelta(days=2)
                if parsed > start_dt:
                    start_dt = parsed
        except Exception:
            start_dt = SCAN_START_DT
    # Gmail date operators are date-only; use previous day to include the desired start date.
    after_value = (start_dt - timedelta(days=1)).strftime("%Y/%m/%d")
    return f"after:{after_value}", start_dt.strftime("%Y-%m-%d")

def _collect_thread_ids(service, queries: List[str], per_query_limit: int, total_limit: int) -> List[str]:
    output = []
    seen = set()
    for query in queries:
        if len(output) >= total_limit:
            break
        try:
            for thread_id in search_threads(service, query=query, max_results=per_query_limit):
                if thread_id not in seen:
                    seen.add(thread_id)
                    output.append(thread_id)
                    if len(output) >= total_limit:
                        break
        except Exception:
            continue
    return output

def _order_scan_queries(date_clause: str) -> List[str]:
    base = f'{date_clause} -in:spam -in:trash'
    return [
        f'{base} "New Order:"',
        f'{base} "[Order #"',
        f'{base} "you have received the following order"',
        f'{base} "you\'ve received the following order"',
        f'{base} "Billing address" "Payment method:" "Total:"',
        f'{base} from:(wordpress OR woocommerce) "Order #"',
    ]

def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'{date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    # Broad latest inbox/all-mail searches are first so we do not miss messages that do not use
    # exact phrases like "please" or "question". Hard filters + AI screening decide what is shown.
    return [
        f'{base} in:inbox',
        f'{base}',
        f'{base} ("order number" OR invoice OR receipt OR payment OR refund OR login OR access)',
        f'{base} (PEBC OR exam OR course OR class OR schedule OR notes OR recording OR extension OR renewal)',
        f'{base} ("can you" OR "could you" OR "would you" OR "please" OR "I need" OR "I would like")',
        f'{base} ("let me know" OR "how do I" OR "when will" OR "what is" OR question OR help)',
        f'{base} ("not received" OR "still waiting" OR "follow up" OR "checking in" OR "send me")',
    ]

def _upsert_order_in_catalog(catalog: Dict, thread_id: str, order_item: Dict):
    existing = catalog.setdefault("orders", {}).get(thread_id, {})
    catalog["orders"][thread_id] = {
        **existing,
        **order_item,
        "thread_id": thread_id,
        "first_seen_at": existing.get("first_seen_at") or _safe_iso_now(),
        "updated_at": _safe_iso_now(),
    }

def _upsert_email_in_catalog(catalog: Dict, thread_id: str, email_item: Dict):
    existing = catalog.setdefault("emails", {}).get(thread_id, {})
    catalog["emails"][thread_id] = {
        **existing,
        **email_item,
        "thread_id": thread_id,
        "first_seen_at": existing.get("first_seen_at") or _safe_iso_now(),
        "updated_at": _safe_iso_now(),
    }

def _auto_send_order_if_safe(service, thread: Dict, connected_email: str, order_item: Dict) -> Tuple[Dict, bool]:
    """Automatically send the standard welcome only for new orders that have not already been answered."""
    reply = order_item.get("reply") or {}
    if not reply:
        return order_item, False
    if not get_automation_settings().get("auto_reply_enabled", True):
        return order_item, False
    customer_email = reply.get("to") or order_item.get("customer_email")
    order_number = order_item.get("order_number")
    if not customer_email or not order_number or str(order_number).lower() == "unknown":
        return order_item, False
    if was_thread_manually_replied(service, thread, connected_email) or was_order_message_already_sent(service, customer_email, order_number):
        order_item = {
            **order_item,
            "status": "Already Replied",
            "reply": None,
            "reply_sent_at": order_item.get("reply_sent_at") or _safe_iso_now(),
        }
        mark_thread_action_processed(thread_action_key(thread, connected_email), "manual_or_prior_reply_found", "", order_number=order_number)
        upsert_processed_order(order_number, {
            "customer_email": customer_email,
            "customer_name": order_item.get("customer_name", "Customer"),
            "status": "Already Replied",
            "total": order_item.get("total", ""),
            "products": order_item.get("products", []),
        })
        return order_item, False
    sent = send_new_email(service, customer_email, reply.get("subject", "Welcome to Pharmacy Prep"), reply.get("body", ""))
    sent_at = _safe_iso_now()
    order_item = {
        **order_item,
        "status": "Sent Automatically",
        "reply": None,
        "reply_sent_at": sent_at,
        "sent_message_id": sent.get("id", ""),
    }
    mark_thread_action_processed(thread_action_key(thread, connected_email), "order_auto_sent", sent.get("id", ""), order_number=order_number)
    upsert_processed_order(order_number, {
        "customer_email": customer_email,
        "customer_name": order_item.get("customer_name", "Customer"),
        "status": "Sent Automatically",
        "sent_message_id": sent.get("id", ""),
        "sent_at": sent_at,
        "total": order_item.get("total", ""),
        "products": order_item.get("products", []),
    })
    return order_item, True

def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    work_label_id = get_or_create_label(service, WORK_LABEL)

    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog.setdefault("orders", {})
    catalog.setdefault("emails", {})
    date_clause, scan_start_used = _scan_after_clause_for_catalog(catalog, force_full=force_full)

    order_thread_ids = _collect_thread_ids(
        service,
        _order_scan_queries(date_clause),
        per_query_limit=max(10, MAX_ORDER_THREADS_PER_SCAN // 4),
        total_limit=MAX_ORDER_THREADS_PER_SCAN,
    )
    email_queries = _email_scan_queries(date_clause, connected_email)
    email_thread_ids = _collect_thread_ids(
        service,
        email_queries,
        per_query_limit=max(25, MAX_EMAIL_THREADS_PER_SCAN // max(1, len(email_queries))),
        total_limit=MAX_EMAIL_THREADS_PER_SCAN,
    )

    auto_orders_sent = 0
    order_replies_waiting = 0
    suggested_replies = 0
    skipped_failed_orders = 0
    processed_order_threads = set()
    ai_screenings_used = 0

    for thread_id in order_thread_ids:
        try:
            thread = read_thread(service, thread_id)
            order_item = build_order_item(service, thread, connected_email)
            if not order_item:
                continue
            processed_order_threads.add(thread_id)
            if order_item.get("order_number") == "Unknown":
                skipped_failed_orders += 1
            order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
            if did_send:
                auto_orders_sent += 1
            if order_item.get("reply"):
                order_replies_waiting += 1
            _upsert_order_in_catalog(catalog, thread_id, order_item)
        except Exception:
            continue

    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads:
            continue
        try:
            thread = read_thread(service, thread_id)
            if get_best_order_email_text(thread):
                # A targeted email query can still hit an order notification. Process it as an order instead.
                order_item = build_order_item(service, thread, connected_email)
                if order_item:
                    order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
                    if did_send:
                        auto_orders_sent += 1
                    if order_item.get("reply"):
                        order_replies_waiting += 1
                    _upsert_order_in_catalog(catalog, thread_id, order_item)
                continue
            # Cap expensive OpenAI screening per scan. Deterministic hard filters already removed obvious noise.
            if ai_screenings_used >= MAX_AI_SCREENINGS_PER_SCAN and not get_catalog_item("emails", thread_id):
                continue
            before = time.time()
            email_item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id)
            if email_item and email_item.get("ai_screened"):
                ai_screenings_used += 1
            if email_item:
                if email_item.get("reply"):
                    suggested_replies += 1
                _upsert_email_in_catalog(catalog, thread_id, email_item)
            # Do not delete old catalog entries here. Old important items stay visible until answered or removed.
        except Exception:
            continue

    catalog["meta"] = {
        **catalog.get("meta", {}),
        "connected_email": connected_email,
        "last_successful_scan_at": _safe_iso_now(),
        "last_scan_start": scan_start_used,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "scan_start_date": SCAN_START_DT.strftime("%Y-%m-%d"),
        "email_screening_version": EMAIL_SCREENING_VERSION,
    }
    save_dashboard_catalog(catalog)

    orders = [item for item in catalog.get("orders", {}).values() if _is_catalog_order_visible(item)]
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)
    build_daily_briefing(connected_email, orders, emails)
    invalidate_dashboard_cache()
    payload = build_dashboard_payload(force_refresh=True)
    payload["scan_summary"] = {
        "scan_start": scan_start_used,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "orders_checked": len(order_thread_ids),
        "emails_checked": len(email_thread_ids),
        "auto_orders_sent": auto_orders_sent,
        "order_replies_waiting": order_replies_waiting,
        "failed_orders_skipped": skipped_failed_orders,
        "suggested_replies": suggested_replies,
        "ai_screenings_used": ai_screenings_used,
    }
    return payload

# ---------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------
@app.route("/")
def home():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return send_from_directory(BASE_DIR, "index.html")
    return "Put index.html in the same folder as backend.py, then open http://127.0.0.1:5050/"
@app.route("/api/dashboard")
def api_dashboard():
    try:
        return jsonify(build_dashboard_payload(force_refresh=False))
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        request_body = request.get_json(silent=True) or {}
        payload = perform_gmail_scan(force_full=bool(request_body.get("force_full", False)))
        summary = payload.get("scan_summary", {})
        return jsonify({
            "ok": True,
            "message": "Scan complete.",
            "order_replies_waiting": summary.get("order_replies_waiting", len([order for order in payload.get("orders", []) if order.get("reply")])) ,
            "failed_orders_skipped": summary.get("failed_orders_skipped", len([order for order in payload.get("orders", []) if order.get("order_number") == "Unknown"])),
            "suggested_replies": summary.get("suggested_replies", len([email for email in payload.get("emails", []) if email.get("reply")])),
            "auto_orders_sent": summary.get("auto_orders_sent", 0),
            "emails_checked": summary.get("emails_checked", 0),
            "email_threads_read": summary.get("email_threads_read", 0),
            "emails_accepted": summary.get("emails_accepted", 0),
            "emails_rejected": summary.get("emails_rejected", 0),
            "ai_screenings_used": summary.get("ai_screenings_used", 0),
            "debug_file": summary.get("debug_file", ""),
            "scan_start": summary.get("scan_start", ""),
            "scan_window": summary.get("scan_window", f"{SCAN_START_DISPLAY} onward"),
        })
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
@app.route("/api/replies/<thread_id>/send", methods=["POST"])
def api_send_reply(thread_id: str):
    try:
        if not get_automation_settings().get("auto_reply_enabled", True):
            return jsonify({"ok": False, "error": "Auto Reply is off. Turn it on before sending replies."}), 403
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        body = request.get_json(silent=True) or {}
        subject = (body.get("subject") or "").strip()
        reply_body = (body.get("body") or "").strip()
        thread = read_thread(service, thread_id)
        thread_key = thread_action_key(thread, connected_email)
        if get_best_order_email_text(thread):
            order_text = get_best_order_email_text(thread) or ""
            order_number = extract_order_number(order_text) or "Unknown"
            customer_email = extract_customer_email(order_text, connected_email)
            if not customer_email:
                raise ValueError("Could not find the customer email address for this order.")
            if was_thread_manually_replied(service, thread, connected_email) or was_order_message_already_sent(service, customer_email, order_number):
                sent = {"id": "already-replied"}
                upsert_processed_order(order_number, {
                    "customer_email": customer_email,
                    "customer_name": best_customer_name(order_text, customer_email),
                    "status": "Already Replied",
                })
                upsert_catalog_item("orders", thread_id, {
                    "status": "Already Replied",
                    "reply": None,
                    "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
                })
            else:
                if not subject or not reply_body:
                    customer_name = best_customer_name(order_text, customer_email)
                    subject, reply_body = build_order_welcome_email(customer_name, order_number)
                sent = send_new_email(service, customer_email, subject, reply_body)
                upsert_processed_order(order_number, {
                    "customer_email": customer_email,
                    "customer_name": best_customer_name(order_text, customer_email),
                    "status": "Sent from Dashboard",
                    "sent_message_id": sent.get("id", ""),
                })
                upsert_catalog_item("orders", thread_id, {
                    "status": "Already Replied",
                    "reply": None,
                    "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
                })
        else:
            sent = send_frontend_thread_reply(service, thread, connected_email, subject, reply_body)
            upsert_catalog_item("emails", thread_id, {
                "status": "Already Replied",
                "reply": None,
                "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
            })
        mark_thread_action_processed(thread_key, "frontend_reply_sent", sent.get("id", ""))
        invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
@app.route("/api/replies/<thread_id>", methods=["DELETE"])
def api_remove_reply(thread_id: str):
    try:
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        thread = read_thread(service, thread_id)
        thread_key = thread_action_key(thread, connected_email)
        action_type = "dismissed"
        extra = {}
        order_text = get_best_order_email_text(thread)
        if order_text:
            order_number = extract_order_number(order_text)
            if order_number:
                upsert_processed_order(order_number, {
                    "customer_email": extract_customer_email(order_text, connected_email) or "",
                    "customer_name": best_customer_name(order_text, extract_customer_email(order_text, connected_email) or ""),
                    "status": "Suggestion Removed",
                })
                upsert_catalog_item("orders", thread_id, {
                    "status": "Suggestion Removed",
                    "reply": None,
                })
                extra["order_number"] = order_number
        else:
            upsert_catalog_item("emails", thread_id, {
                "status": "Suggestion Removed",
                "reply": None,
            })
        mark_thread_action_processed(thread_key, action_type, **extra)
        invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Suggested reply removed."})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


# ---------------------------------------------------------------------
# EMERGENCY PATCH: diagnostics + less aggressive important-email screening
# ---------------------------------------------------------------------
# Manual Scan should prove what happened instead of silently returning zero.
EMAIL_SCREENING_VERSION = "2026-06-diagnostic-v7"
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "120"))
SCAN_DEBUG_FILE = BASE_DIR / "last_scan_debug.json"


def _debug_counts_template() -> Dict:
    return {
        "started_at": _safe_iso_now(),
        "scan_start": "",
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "order_threads_found": 0,
        "email_threads_found": 0,
        "email_threads_read": 0,
        "email_skipped_order_notification": 0,
        "email_skipped_old_date": 0,
        "email_skipped_latest_from_us": 0,
        "email_skipped_automation": 0,
        "email_skipped_prescreen": 0,
        "email_ai_rejected": 0,
        "email_ai_accepted": 0,
        "email_deterministic_accepted": 0,
        "email_suggested_replies": 0,
        "email_errors": 0,
        "first_accepted_examples": [],
        "first_rejected_examples": [],
        "errors": [],
    }


def _save_scan_debug(debug: Dict):
    try:
        debug["finished_at"] = _safe_iso_now()
        save_json_file(SCAN_DEBUG_FILE, debug)
    except Exception:
        pass


def _scan_debug_example(thread: Dict, connected_email: str, reason: str) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return {
        "reason": reason,
        "thread_id": thread.get("thread_id", ""),
        "from": latest.get("from", ""),
        "date": latest.get("date", ""),
        "subject": latest.get("subject", ""),
        "preview": clean_preview_text(latest.get("body", ""), 240),
    }


def _append_scan_example(debug: Dict, key: str, thread: Dict, connected_email: str, reason: str):
    try:
        if len(debug.get(key, [])) < 12:
            debug.setdefault(key, []).append(_scan_debug_example(thread, connected_email, reason))
    except Exception:
        pass


def _strong_request_score(thread: Dict, connected_email: str) -> int:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 8000).lower()
    text = f"{subject}\n{body}"
    score = 0
    direct = [
        "?", "can you", "could you", "would you", "please", "i need", "need help",
        "i would like", "i want", "how do i", "when will", "where is", "what is", "what's",
        "let me know", "advise", "clarify", "confirm", "question", "help", "send me",
        "share", "provide", "update me", "follow up", "follow-up", "checking in",
        "not received", "still waiting", "missing", "issue", "problem", "unable to", "can't", "cannot",
    ]
    topics = [
        "pebc", "exam", "qualifying", "evaluating", "announcement", "course", "class",
        "schedule", "notes", "recording", "login", "access", "account", "order", "order number",
        "invoice", "payment", "receipt", "refund", "extension", "renewal", "enroll", "enrol",
        "registration", "student", "book", "mouse", "call", "meeting", "availability", "available",
    ]
    if any(x in text for x in direct):
        score += 3
    if any(x in text for x in topics):
        score += 2
    if "?" in text:
        score += 2
    if len(body.split()) >= 8:
        score += 1
    if len(thread.get("emails", [])) >= 2:
        score += 1
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    if sender and not any(x in sender for x in ["noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@", "marketing@", "wordpress", "woocommerce"]):
        score += 1
    return score


def _automation_or_noise_reason(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 5000).lower()
    text = f"{subject}\n{body}"
    score = _strong_request_score(thread, connected_email)

    automated_senders = [
        "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "wordpress",
        "woocommerce", "notifications@", "marketing@", "security@", "billing@",
    ]
    if any(x in sender for x in automated_senders):
        return "automated/no-reply sender"

    hard_status_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "tracking number", "shipment", "shipping confirmation", "payment received",
        "e-transfer received", "etransfer received", "interac e-transfer", "receipt for your payment",
        "charge receipt", "invoice paid", "successful payment", "order confirmation",
        "your order is confirmed", "subscription confirmed",
    ]
    if any(x in text for x in hard_status_terms) and score < 4:
        return "automated status/payment/shipping/receipt message"

    marketing_terms = [
        "unsubscribe", "manage your preferences", "view this email in your browser", "newsletter",
        "promotion", "limited time", "sale ends", "special offer", "webinar", "digest",
    ]
    if any(x in text for x in marketing_terms) and score < 5:
        return "marketing/newsletter"

    system_terms = [
        "please moderate", "comment awaiting moderation", "new question submitted", "security alert",
        "verification code", "password reset", "delivery status notification", "undeliverable",
        "mail delivery", "new user registration", "this is an automated message", "do not reply to this email",
    ]
    if any(x in text for x in system_terms) and score < 5:
        return "system/app notification"

    return ""


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    """Inclusive human-message pre-screen. Hard-block only obvious automation.

    The previous version was letting /api/scan finish but then hide every normal email.
    This version keeps the hard junk filters but allows real human emails into AI screening.
    """
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _automation_or_noise_reason(thread, connected_email):
        return False

    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    body = clean_preview_text(latest.get("body", ""), 7000)
    human_sender = bool(sender) and not any(blocked in sender for blocked in [
        "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@", "marketing@", "wordpress", "woocommerce"
    ])
    score = _strong_request_score(thread, connected_email)
    if score >= 4:
        return True
    if human_sender and len(body.split()) >= 10 and len(thread.get("emails", [])) >= 2:
        return True
    return False


def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 5000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 9000)
    prompt = f"""
You are screening Gmail for Pharmacy Prep. Decide if the latest inbound message should appear on an assistant dashboard.

Include when a real person is asking a question, asking for help, asking for details, asking for an order/payment/login/course/access/PEBC/exam detail, reporting a problem, or continuing a conversation that likely needs a reply.

Exclude only when it is clearly not reply-worthy: shipping/tracking updates, payment/e-transfer received notices, receipts, newsletters, promotions, WordPress/WooCommerce/system alerts, confirmations, FYI-only messages, thank-yous, no-reply messages, or automated notices.

Important: if the sender is a human and the message contains a request/question, include it even if the wording is short or imperfect.

Category:
- work = Pharmacy Prep, PEBC, courses, students, orders, payments, invoices, support, vendors, business/client messages.
- personal = friends/family/personal appointments not related to Pharmacy Prep/business.

Return JSON only:
{{
  "include": true,
  "category": "work",
  "title": "4-9 word dashboard title",
  "summary": "specific one-sentence summary mentioning {display_name} and exactly what they need",
  "reason": "why this needs review or why it should be hidden",
  "confidence": 0.0
}}

Sender display name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get("subject", "")}
Latest body:
{latest_body}

Current thread:
{thread_text}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        include = parsed.get("include", False)
        if isinstance(include, str):
            include = include.strip().lower() in ("true", "yes", "1", "include")
        category = str(parsed.get("category", "work")).lower().strip()
        if category not in ("work", "personal"):
            category = "work"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        return {
            "include": bool(include),
            "category": category,
            "title": str(parsed.get("title", "")).strip(),
            "summary": str(parsed.get("summary", "")).strip(),
            "reason": str(parsed.get("reason", "")).strip(),
            "confidence": confidence,
        }
    except Exception:
        return None


def _context_search_terms_from_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    text = f"{latest.get('subject','')}\n{latest.get('body','')}".lower()
    terms = []
    for pat in [r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})"]:
        for m in re.findall(pat, text, flags=re.IGNORECASE):
            if m and m not in terms:
                terms.append(m)
    important = [
        "mouse", "pebc", "exam", "qualifying", "evaluating", "announcement", "course",
        "login", "access", "invoice", "receipt", "payment", "refund", "extension",
        "recording", "notes", "schedule", "registration", "enroll", "enrol"
    ]
    for word in important:
        if word in text and word not in terms:
            terms.append(word)
    # Add distinctive tokens from the latest message.
    stop = set("the and for that with this from have your please could would about there their them they what when where which need help reply email thanks thank hello regards pharmacy prep course order number student message information details update know send sent asking regarding".split())
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", text):
        token = token.lower()
        if token not in stop and token not in terms:
            terms.append(token)
        if len(terms) >= 12:
            break
    return terms[:12]


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_email = parseaddr(latest.get("from", ""))[1].strip()
    terms = _context_search_terms_from_thread(thread, connected_email)
    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email}) ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access OR PEBC OR course)',
        ])
    for term in terms:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{term}"')
        queries.append(f'in:anywhere "{term}"')
    deduped = []
    for q in queries:
        if q and q not in deduped:
            deduped.append(q)
    return deduped[:16]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = 4) -> str:
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL CONTEXT ================\n\n"
    for query in (queries or [])[:10]:
        try:
            thread_ids = search_threads(service, query=query, max_results=max_threads_per_query)
            for thread_id in thread_ids:
                if thread_id == current_thread_id or thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                thread = read_thread(service, thread_id)
                latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
                context_blocks.append(
                    f"Search query: {query}\n"
                    f"Thread ID: {thread_id}\n"
                    f"Latest subject: {latest.get('subject', '')}\n"
                    f"Latest from: {latest.get('from', '')}\n"
                    f"Thread content:\n{format_thread_for_ai(thread)[:7000]}"
                )
                if len(context_blocks) >= 10:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 9000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 15000)
    prompt = f"""
You are Pharmacy Prep's senior email assistant. Write a real outbound reply that is detailed, useful, and contextual.

You MUST use the Current Gmail thread, Stored order context, and Related Gmail context. The reply should not sound generic.

Rules:
1. Do not repeat/copy the sender's message.
2. Do not write vague filler like "we received your message".
3. Directly answer the request using Gmail context when possible.
4. If they ask for an order number, receipt, payment, product, course access/login, PEBC/exam announcement, schedule, notes, recording, or prior reply, search the provided context and include the exact detail found.
5. If you find an order number anywhere in Stored order context or Related Gmail context, include it clearly.
6. If exact information is missing, explain what was checked, what is missing, and ask one specific follow-up question only if required.
7. Work replies should usually be 2-5 concise paragraphs and include the Pharmacy Prep signature exactly.
8. Personal replies should be warm and natural without the business signature.

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence dashboard summary mentioning who is asking and the concrete topic/context",
  "subject": "{clean_subject}",
  "body": "full outbound reply only"
}}

Work signature:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com

Category: {category}
Sender display name: {display_name}
Sender email: {sender_email}
Latest inbound subject: {latest.get("subject", "")}
Latest inbound body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail context found through Gmail API searches:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if parsed and str(parsed.get("body", "")).strip():
            body = str(parsed.get("body", "")).strip()
            if reply_needs_regeneration(body, latest_body, category):
                return None
            return {
                "title": str(parsed.get("title", "")).strip(),
                "summary": str(parsed.get("summary", "")).strip(),
                "subject": str(parsed.get("subject", clean_subject)).strip() or clean_subject,
                "body": body,
            }
    except Exception:
        pass
    return None


def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    thread_id = thread.get("thread_id", "")
    thread_key = thread_action_key(thread, connected_email)
    action = get_thread_action(thread_key)
    stored_item = get_catalog_item("emails", thread_id)
    category = stored_item.get("category") or dashboard_category_for_thread(thread, connected_email)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)
    title = stored_item.get("title") or (latest.get("subject", "") or "Important email").strip()
    candidate = should_consider_thread_for_dashboard(thread, connected_email)
    request_score = _strong_request_score(thread, connected_email)

    if was_thread_manually_replied(service, thread, connected_email):
        if not stored_item:
            return None
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": stored_item.get("important_reason") or "This conversation appears handled because a later message was sent from the connected Gmail account.",
            "status": "Already Replied",
            "reply_sent_at": stored_item.get("reply_sent_at") or datetime.now().isoformat(timespec="seconds"),
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "ai_screened": stored_item.get("ai_screened", True),
            "screening_version": EMAIL_SCREENING_VERSION,
            "original": {
                "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
                "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
                "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
                "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
            },
            "reply": None,
        }

    has_history = bool(stored_item) and (stored_item.get("reply_sent_at") or stored_item.get("status") in ("Already Replied", "Suggestion Removed", "Needs Reply") or stored_item.get("reply"))
    if not candidate and not has_history:
        return None

    screening = analyze_dashboard_thread_with_ai(thread, connected_email) if candidate else None
    # Do not let a single uncertain AI rejection hide a clearly actionable human request.
    ai_says_no_confidently = bool(screening and not screening.get("include") and float(screening.get("confidence", 0) or 0) >= 0.88)
    if ai_says_no_confidently and request_score < 5 and not has_history:
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": "",
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": True,
            "screen_confidence": screening.get("confidence", 0),
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", ""),
                "to": latest.get("to", ""),
                "date": latest.get("date", ""),
                "subject": latest.get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800),
            },
            "reply": None,
        }

    if screening and screening.get("include"):
        category = screening.get("category") or category
        if screening.get("title") and 3 <= len(screening.get("title", "").split()) <= 12:
            title = screening.get("title", "").strip()

    if category == "personal":
        apply_label_to_thread_messages(service, thread, personal_label_id)
    else:
        apply_label_to_thread_messages(service, thread, work_label_id)

    stored_reason = stored_item.get("important_reason", "")
    screen_summary = (screening or {}).get("summary", "")
    if screen_summary and not summary_is_generic(screen_summary):
        important_reason = screen_summary.strip()
    elif stored_reason and not summary_is_generic(stored_reason):
        important_reason = stored_reason
    else:
        important_reason = build_important_reason(thread, connected_email)

    reply = None
    status = stored_item.get("status") or "Needs Reply"
    reply_sent_at = stored_item.get("reply_sent_at", "")

    if action.get("action_type") == "dismissed":
        status = "Suggestion Removed"
    elif action.get("action_type") == "frontend_reply_sent":
        status = "Already Replied"
        reply_sent_at = action.get("processed_at", "") or reply_sent_at
    else:
        status = "Needs Reply"
        latest_clean_body = clean_preview_text(latest.get("body", ""), 5000)
        cached_reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
        cached_is_usable = bool(cached_reply) and not reply_needs_regeneration(cached_reply.get("body", ""), latest_clean_body, category)
        if cached_is_usable and not summary_is_generic(important_reason):
            reply = cached_reply
        else:
            queries = heuristic_context_queries_for_thread(thread, connected_email)
            extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=4) if queries else ""
            composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
            if composed:
                if composed.get("summary") and not summary_is_generic(composed.get("summary", "")):
                    important_reason = composed.get("summary", "").strip()
                if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                    title = composed.get("title", "").strip()
                reply = {
                    "thread_id": thread_id,
                    "mode": "thread_reply",
                    "to": parseaddr(latest.get("from", ""))[1].strip(),
                    "subject": composed.get("subject", ""),
                    "body": composed.get("body", ""),
                }
            if not reply or not str(reply.get("body", "")).strip() or reply_needs_regeneration(reply.get("body", ""), latest_clean_body, category):
                reply = fallback_reply_for_thread(thread, connected_email, category)
                if summary_is_generic(important_reason):
                    important_reason = build_important_reason(thread, connected_email)

    return {
        "thread_id": thread_id,
        "category": category,
        "title": title,
        "important_reason": important_reason,
        "status": status,
        "reply_sent_at": reply_sent_at,
        "latest_inbound_id": latest_inbound_id,
        "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
        "ai_screened": bool(screening) or candidate or stored_item.get("ai_screened", False),
        "screen_confidence": (screening or {}).get("confidence", stored_item.get("screen_confidence", 0)),
        "screening_version": EMAIL_SCREENING_VERSION,
        "original": {
            "from": latest.get("from", "") or stored_item.get("original", {}).get("from", ""),
            "to": latest.get("to", "") or stored_item.get("original", {}).get("to", ""),
            "date": latest.get("date", "") or stored_item.get("original", {}).get("date", ""),
            "subject": latest.get("subject", "") or stored_item.get("original", {}).get("subject", ""),
            "body": clean_preview_text(latest.get("body", ""), 1800) or stored_item.get("original", {}).get("body", ""),
        },
        "reply": reply,
    }


def _is_catalog_email_visible(item: Dict) -> bool:
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if item.get("screening_version") != EMAIL_SCREENING_VERSION:
        return False
    reason = item.get("important_reason", "")
    has_specific_reason = bool(reason) and not summary_is_generic(reason)
    has_reply = bool(item.get("reply"))
    handled = item.get("status") in ("Already Replied", "Suggestion Removed") and has_specific_reason
    return has_reply or handled or (has_specific_reason and item.get("ai_screened"))


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'{date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    # Put likely human requests before broad inbox scanning so the AI cap is spent on useful messages.
    return [
        f'{base} in:inbox ("?" OR "please" OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know")',
        f'{base} ("order number" OR "not received" OR "still waiting" OR "follow up" OR "checking in" OR "send me")',
        f'{base} (PEBC OR exam OR announcement OR qualifying OR evaluating OR course OR class OR schedule OR notes OR recording)',
        f'{base} (login OR access OR account OR invoice OR payment OR refund OR receipt OR extension OR renewal OR registration)',
        f'{base} in:inbox -from:(noreply OR no-reply OR donotreply OR wordpress OR woocommerce OR notifications)',
        f'{base} -from:(noreply OR no-reply OR donotreply OR wordpress OR woocommerce OR notifications)',
    ]


def build_daily_briefing(connected_email: str, orders: List[Dict], emails: List[Dict]) -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    orders = [order for order in orders if _is_catalog_order_visible(order)]
    emails = [email for email in emails if _is_catalog_email_visible(email)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)
    debug = load_json_file(SCAN_DEBUG_FILE, {})

    seen_orders = set()
    deduped_orders = []
    for order in orders:
        key = (str(order.get("order_number", "")).lower(), str(order.get("customer_email", "")).lower())
        if key in seen_orders:
            continue
        seen_orders.add(key)
        deduped_orders.append(order)

    waiting_orders = [order for order in deduped_orders if order.get("reply")]
    handled_orders = [order for order in deduped_orders if not order.get("reply")]
    work_emails = [email for email in emails if email.get("category") == "work"]
    personal_emails = [email for email in emails if email.get("category") == "personal"]

    def sender_name(email: Dict) -> str:
        original = email.get("original", {}) or {}
        raw_sender = original.get("from", "") or ""
        name, address = parseaddr(raw_sender)
        name = re.sub(r"[\"']", "", name or "").strip()
        if name and name.lower() not in ("unknown", "unknown sender"):
            return name
        local = (address or raw_sender).split("@")[0]
        local = re.sub(r"[._+-]+", " ", local).strip()
        return " ".join(part.capitalize() for part in local.split()[:3]) or "A sender"

    def compact_topic(email: Dict) -> str:
        original = email.get("original", {}) or {}
        subject = original.get("subject", "") or email.get("title", "Email")
        reason = (email.get("important_reason", "") or "").strip()
        body = original.get("body", "") or ""
        text = f"{subject}\n{reason}\n{body}".lower()

        topic_checks = [
            (("login", "access", "account", "password", "enroll", "enrol"), "course login or access details"),
            (("book", "books", "manual", "textbook", "materials", "notes"), "books or course materials"),
            (("pebc", "exam", "announcement", "qualifying", "evaluating", "mcq", "osce"), "PEBC exam updates or preparation details"),
            (("order number", "order #", "order details", "order status"), "order number or order details"),
            (("payment", "invoice", "receipt", "refund", "etransfer", "e-transfer"), "payment, invoice, or refund details"),
            (("schedule", "class", "session", "date", "time", "availability", "available"), "class schedule or availability"),
            (("recording", "video", "zoom", "link"), "recordings or online session links"),
            (("extension", "renewal", "expire", "expired"), "course extension or renewal"),
            (("call", "meeting", "appointment"), "a call or meeting request"),
        ]
        for keywords, label in topic_checks:
            if any(keyword in text for keyword in keywords):
                return label

        if reason and not summary_is_generic(reason):
            cleaned = re.sub(r"\s+", " ", reason).strip().rstrip(".")
            if len(cleaned) > 130:
                cleaned = cleaned[:127].rsplit(" ", 1)[0] + "..."
            return cleaned[0].lower() + cleaned[1:] if cleaned else "a message that needs a response"

        cleaned_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()
        return f"the message about {cleaned_subject}" if cleaned_subject else "a message that needs a response"

    def briefing_sentence(email: Dict) -> str:
        name = sender_name(email)
        topic = compact_topic(email)
        status = email.get("status", "Needs Reply")
        reply_note = "suggested reply ready" if email.get("reply") else "already handled/no reply draft"
        if email.get("category") == "work":
            return f"- {name} needs a response about {topic}; {reply_note}."
        return f"- {name} has a personal/actionable message about {topic}; {reply_note}."

    def grouped_topic_lines(email_list: List[Dict], category_label: str) -> List[str]:
        topic_counts: Dict[str, int] = {}
        for email in email_list:
            topic = compact_topic(email)
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

        grouped = []
        for topic, count in sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:6]:
            if count >= 2:
                noun = "students/customers" if category_label == "work" else "personal senders"
                grouped.append(f"- {count} {noun} need replies about {topic}.")
        return grouped

    lines = [
        "# AI Summary",
        "",
        f"Generated: {now}",
        f"Connected Gmail: {connected_email}",
        f"Scan window: {SCAN_START_DISPLAY} onward",
        "",
        "General Briefing",
    ]

    if emails:
        grouped_lines = grouped_topic_lines(work_emails, "work") + grouped_topic_lines(personal_emails, "personal")
        if grouped_lines:
            lines.extend(grouped_lines)
        for email in emails[:12]:
            lines.append(briefing_sentence(email))
    else:
        lines.append("- No work or personal messages currently need review. The dashboard is only showing stored orders and handled items right now.")

    if waiting_orders:
        for order in waiting_orders[:5]:
            lines.append(
                f"- Order #{order.get('order_number', 'Unknown')} for {order.get('customer_name', 'Customer')} is waiting for approval before sending."
            )

    if handled_orders and not waiting_orders:
        newest = handled_orders[0]
        lines.append(
            f"- {len(handled_orders)} order(s) are already handled; the latest visible order is #{newest.get('order_number', 'Unknown')} for {newest.get('customer_name', 'Customer')}."
        )

    lines.extend(["", "Email Details"])
    if emails:
        for email in emails[:25]:
            original = email.get("original", {}) or {}
            reason = email.get("important_reason", "").strip() or compact_topic(email)
            reply_state = "suggested reply ready" if email.get("reply") else "handled/no draft"
            lines.append(
                f"- [{email.get('category', 'work').title()}] {sender_name(email)} | "
                f"Subject: {original.get('subject', '') or email.get('title', 'Email')} | "
                f"Status: {email.get('status', 'Needs Reply')} ({reply_state}) | {reason}"
            )
    else:
        lines.append("- No actionable work/personal emails are currently visible.")

    lines.extend(["", "Orders"])
    if deduped_orders:
        for order in deduped_orders[:20]:
            status = "Waiting to send" if order.get("reply") else order.get("status", "Handled")
            product_text = "; ".join(order.get("products", [])[:2])
            total_text = f" | Total: {order.get('total')}" if order.get("total") else ""
            product_part = f" | Products: {product_text}" if product_text else ""
            lines.append(
                f"- Order #{order.get('order_number', 'Unknown')} | {order.get('customer_name', 'Customer')} | "
                f"{order.get('customer_email', 'Unknown email')} | {status}{total_text}{product_part}"
            )
    else:
        lines.append(f"- No orders from {SCAN_START_DISPLAY} onward are stored yet.")

    lines.extend([
        "",
        "Executive Overview",
        f"- Stored June-forward orders: {len(deduped_orders)} total; {len(waiting_orders)} waiting; {len(handled_orders)} handled.",
        f"- Actionable emails currently visible: {len(work_emails)} work and {len(personal_emails)} personal.",
        f"- Last scan checked {debug.get('order_threads_found', 0)} order thread(s) and {debug.get('email_threads_found', 0)} possible email thread(s); accepted {debug.get('email_ai_accepted', 0) + debug.get('email_deterministic_accepted', 0)} email(s), rejected {debug.get('email_ai_rejected', 0)} by AI, skipped {debug.get('email_skipped_automation', 0)} automation/noise, skipped {debug.get('email_skipped_prescreen', 0)} weak/non-actionable candidate(s).",
        "- Shipping updates, e-transfer/payment receipts, newsletters, WordPress/system notifications, confirmations, and no-reply alerts are intentionally hidden.",
    ])

    briefing = "\n".join(lines)
    BRIEFING_FILE.write_text(briefing, encoding="utf-8")
    return briefing

def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    work_label_id = get_or_create_label(service, WORK_LABEL)

    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog.setdefault("orders", {})
    catalog.setdefault("emails", {})
    date_clause, scan_start_used = _scan_after_clause_for_catalog(catalog, force_full=force_full)
    debug = _debug_counts_template()
    debug["scan_start"] = scan_start_used
    debug["force_full"] = force_full
    print(f"[scan] starting Gmail scan | force_full={force_full} | date_clause={date_clause}", flush=True)

    order_thread_ids = _collect_thread_ids(service, _order_scan_queries(date_clause), per_query_limit=max(10, MAX_ORDER_THREADS_PER_SCAN // 4), total_limit=MAX_ORDER_THREADS_PER_SCAN)
    email_queries = _email_scan_queries(date_clause, connected_email)
    email_thread_ids = _collect_thread_ids(service, email_queries, per_query_limit=max(30, MAX_EMAIL_THREADS_PER_SCAN // max(1, len(email_queries))), total_limit=MAX_EMAIL_THREADS_PER_SCAN)
    debug["order_threads_found"] = len(order_thread_ids)
    debug["email_threads_found"] = len(email_thread_ids)
    print(f"[scan] Gmail returned {len(order_thread_ids)} order thread(s), {len(email_thread_ids)} possible email thread(s)", flush=True)

    auto_orders_sent = 0
    order_replies_waiting = 0
    suggested_replies = 0
    skipped_failed_orders = 0
    processed_order_threads = set()
    ai_screenings_used = 0

    for thread_id in order_thread_ids:
        try:
            thread = read_thread(service, thread_id)
            order_item = build_order_item(service, thread, connected_email)
            if not order_item:
                continue
            processed_order_threads.add(thread_id)
            if order_item.get("order_number") == "Unknown":
                skipped_failed_orders += 1
            order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
            if did_send:
                auto_orders_sent += 1
            if order_item.get("reply"):
                order_replies_waiting += 1
            _upsert_order_in_catalog(catalog, thread_id, order_item)
        except Exception as error:
            debug["email_errors"] += 1
            debug.setdefault("errors", []).append(f"order {thread_id}: {error}")
            continue

    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads:
            continue
        try:
            thread = read_thread(service, thread_id)
            debug["email_threads_read"] += 1
            if not _thread_is_on_or_after_scan_start(thread, connected_email):
                debug["email_skipped_old_date"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "before scan start")
                continue
            if latest_email_is_from_connected_account(thread, connected_email):
                debug["email_skipped_latest_from_us"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "latest message from connected account")
                continue
            if get_best_order_email_text(thread):
                debug["email_skipped_order_notification"] += 1
                order_item = build_order_item(service, thread, connected_email)
                if order_item:
                    order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
                    if did_send:
                        auto_orders_sent += 1
                    if order_item.get("reply"):
                        order_replies_waiting += 1
                    _upsert_order_in_catalog(catalog, thread_id, order_item)
                continue
            noise_reason = _automation_or_noise_reason(thread, connected_email)
            if noise_reason:
                debug["email_skipped_automation"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, noise_reason)
                continue
            if not should_consider_thread_for_dashboard(thread, connected_email):
                debug["email_skipped_prescreen"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "no clear human request/action needed")
                continue
            if ai_screenings_used >= MAX_AI_SCREENINGS_PER_SCAN and not get_catalog_item("emails", thread_id):
                # Accept very strong deterministic requests rather than silently dropping everything after the AI cap.
                if _strong_request_score(thread, connected_email) < 6:
                    debug["email_skipped_prescreen"] += 1
                    _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "AI cap reached and request not strong enough")
                    continue
            email_item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id)
            if email_item and email_item.get("filtered_out"):
                debug["email_ai_rejected"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "AI rejected as not actionable")
                _upsert_email_in_catalog(catalog, thread_id, email_item)
                continue
            if email_item:
                if email_item.get("ai_screened"):
                    ai_screenings_used += 1
                    debug["email_ai_accepted"] += 1
                else:
                    debug["email_deterministic_accepted"] += 1
                if email_item.get("reply"):
                    suggested_replies += 1
                    debug["email_suggested_replies"] += 1
                _append_scan_example(debug, "first_accepted_examples", thread, connected_email, "accepted for dashboard")
                _upsert_email_in_catalog(catalog, thread_id, email_item)
            else:
                debug["email_skipped_prescreen"] += 1
                _append_scan_example(debug, "first_rejected_examples", thread, connected_email, "build returned no dashboard item")
        except Exception as error:
            debug["email_errors"] += 1
            if len(debug.get("errors", [])) < 15:
                debug.setdefault("errors", []).append(f"email {thread_id}: {error}")
            continue

    catalog["meta"] = {
        **catalog.get("meta", {}),
        "connected_email": connected_email,
        "last_successful_scan_at": _safe_iso_now(),
        "last_scan_start": scan_start_used,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "scan_start_date": SCAN_START_DT.strftime("%Y-%m-%d"),
        "email_screening_version": EMAIL_SCREENING_VERSION,
    }
    save_dashboard_catalog(catalog)
    _save_scan_debug(debug)

    orders = [item for item in catalog.get("orders", {}).values() if _is_catalog_order_visible(item)]
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)
    build_daily_briefing(connected_email, orders, emails)
    invalidate_dashboard_cache()
    payload = build_dashboard_payload(force_refresh=True)
    payload["scan_summary"] = {
        "scan_start": scan_start_used,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "orders_checked": len(order_thread_ids),
        "emails_checked": len(email_thread_ids),
        "email_threads_read": debug.get("email_threads_read", 0),
        "emails_accepted": debug.get("email_ai_accepted", 0) + debug.get("email_deterministic_accepted", 0),
        "emails_rejected": debug.get("email_ai_rejected", 0) + debug.get("email_skipped_automation", 0) + debug.get("email_skipped_prescreen", 0),
        "auto_orders_sent": auto_orders_sent,
        "order_replies_waiting": order_replies_waiting,
        "failed_orders_skipped": skipped_failed_orders,
        "suggested_replies": suggested_replies,
        "ai_screenings_used": ai_screenings_used,
        "debug_file": str(SCAN_DEBUG_FILE),
    }
    print(f"[scan] complete | read={debug.get('email_threads_read',0)} | accepted={payload['scan_summary']['emails_accepted']} | suggested={suggested_replies} | rejected={payload['scan_summary']['emails_rejected']} | debug={SCAN_DEBUG_FILE}", flush=True)
    return payload



# ---------------------------------------------------------------------
# FINAL PATCH: realistic contextual replies + stricter reply-worthy screening
# ---------------------------------------------------------------------
# This version fixes the issue where FYI notices/document confirmations were shown
# and then given generic Pharmacy Prep/course-access replies.
EMAIL_SCREENING_VERSION = "2026-06-short-workpersonal-v9"
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "140"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "80"))


def _latest_subject_body(thread: Dict, connected_email: str, limit: int = 9000) -> Tuple[Dict, str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    subject = latest.get("subject", "") or ""
    body = clean_preview_text(latest.get("body", ""), limit)
    return latest, subject, body


def _text_has_any(text: str, phrases: List[str]) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in phrases)


def _is_fyi_notice_or_confirmation(thread: Dict, connected_email: str) -> str:
    """Return a reason when an email is not reply-worthy even if it contains words
    like 'please', 'notify', or 'thank you'. This blocks the exact type of bogus
    replies shown in the screenshots.
    """
    latest, subject, body = _latest_subject_body(thread, connected_email, 8000)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = f"{subject}\n{body}".lower()

    # Building/property notices often say things like "just to let you know" and
    # "notify management", but they are not requests to Pharmacy Prep.
    property_notice_terms = [
        "hi everyone", "dear residents", "dear tenant", "dear tenants", "unit door",
        "unit key", "master key", "contractor", "management office", "building management",
        "property management", "security staff", "concierge", "service work", "your unit",
        "properly secured", "working on-site", "maintenance notice", "there is no need to provide",
    ]
    if sum(1 for term in property_notice_terms if term in text) >= 2:
        return "FYI building/property notice; no direct reply needed"

    # Document signing acknowledgements are usually confirmations, not emails asking for a reply.
    document_confirmation_terms = [
        "thank you for signing the document", "merci d’avoir signé", "merci d'avoir signé",
        "merci d’avoir signature", "thank you for signing", "signed the document",
        "document has been signed", "completed document", "adobe sign", "docusign",
        "signature completed", "signed successfully",
    ]
    if any(term in text for term in document_confirmation_terms):
        return "document-signing confirmation; no reply needed"

    fyi_openers = [
        "just to let you know", "for your information", "fyi", "please be advised",
        "this is to inform you", "we would like to inform you", "please note that",
    ]
    has_actual_question = "?" in text or _text_has_any(text, [
        "can you", "could you", "would you", "i need", "need help", "how do i", "what is", "what's",
        "when will", "where is", "please send", "please provide", "please confirm", "can i", "do you"
    ])
    if any(term in text for term in fyi_openers) and not has_actual_question:
        return "FYI/informational notice; no direct reply needed"

    # Generic confirmations/thanks should not get suggested replies unless there is a new question.
    confirmation_only = [
        "thank you for your order", "order confirmed", "payment received", "e-transfer received",
        "etransfer received", "receipt", "invoice paid", "has shipped", "delivered", "tracking",
        "confirmation", "confirmed successfully", "successfully completed", "subscription confirmed",
    ]
    if any(term in text for term in confirmation_only) and not has_actual_question:
        return "confirmation/status email; no reply needed"

    no_reply_senders = [
        "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@",
        "notification@", "wordpress", "woocommerce", "docusign", "adobesign", "adobe-sign",
    ]
    if any(term in sender for term in no_reply_senders) and not has_actual_question:
        return "automated/no-reply sender without a direct request"

    return ""


def _strong_request_score(thread: Dict, connected_email: str) -> int:
    latest, subject, body = _latest_subject_body(thread, connected_email, 8000)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = f"{subject}\n{body}".lower()
    score = 0

    # Direct asks get the most weight.
    direct_asks = [
        "?", "can you", "could you", "would you", "can i", "could i", "do you", "should i",
        "please send", "please provide", "please confirm", "please advise", "please let me know",
        "i need", "need help", "i would like", "i want", "how do i", "how can i", "when will",
        "where is", "what is", "what's", "i have not received", "not received", "still waiting",
        "unable to", "can't access", "cannot access", "issue", "problem", "refund", "order number",
        "invoice", "receipt", "login", "access", "password", "extension", "renewal", "enroll", "enrol",
    ]
    for phrase in direct_asks:
        if phrase in text:
            score += 3
            break
    if "?" in text:
        score += 3

    pharmacy_topics = [
        "pharmacy prep", "pebc", "exam", "qualifying", "evaluating", "course", "class", "student",
        "mock", "notes", "recording", "schedule", "login", "access", "account", "order", "invoice",
        "payment", "receipt", "refund", "book", "extension", "renewal", "registration", "enrollment",
        "enrolment", "announcement", "prep", "mouse",
    ]
    if any(term in text for term in pharmacy_topics):
        score += 2

    if len(body.split()) >= 10:
        score += 1
    if len(thread.get("emails", [])) >= 2:
        score += 1
    if sender and not any(x in sender for x in ["noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@", "marketing@", "wordpress", "woocommerce"]):
        score += 1

    if _is_fyi_notice_or_confirmation(thread, connected_email):
        score -= 5
    return score


def _automation_or_noise_reason(thread: Dict, connected_email: str) -> str:
    latest, subject, body = _latest_subject_body(thread, connected_email, 7000)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = f"{subject}\n{body}".lower()

    fyi_reason = _is_fyi_notice_or_confirmation(thread, connected_email)
    if fyi_reason:
        return fyi_reason

    score = _strong_request_score(thread, connected_email)
    automated_senders = [
        "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "wordpress", "woocommerce",
        "notifications@", "notification@", "marketing@", "security@", "billing@", "docusign", "adobesign",
    ]
    if any(x in sender for x in automated_senders) and score < 5:
        return "automated/no-reply sender"

    hard_status_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "tracking number", "shipment", "shipping confirmation", "payment received",
        "e-transfer received", "etransfer received", "interac e-transfer", "receipt for your payment",
        "charge receipt", "invoice paid", "successful payment", "order confirmation", "your order is confirmed",
    ]
    if any(x in text for x in hard_status_terms) and score < 5:
        return "automated status/payment/shipping/receipt message"

    marketing_terms = [
        "unsubscribe", "manage your preferences", "view this email in your browser", "newsletter",
        "promotion", "limited time", "sale ends", "special offer", "digest",
    ]
    if any(x in text for x in marketing_terms) and score < 5:
        return "marketing/newsletter"

    system_terms = [
        "please moderate", "comment awaiting moderation", "new question submitted", "security alert",
        "verification code", "password reset", "delivery status notification", "undeliverable",
        "mail delivery", "new user registration", "this is an automated message", "do not reply to this email",
    ]
    if any(x in text for x in system_terms) and score < 5:
        return "system/app notification"

    return ""


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _automation_or_noise_reason(thread, connected_email):
        return False
    return _strong_request_score(thread, connected_email) >= 4



def _pharmacy_prep_related_text(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    parts = [
        latest.get("subject", ""),
        latest.get("body", ""),
        combined_thread_text(thread),
    ]
    return "\n".join(str(part or "") for part in parts).lower()


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    text = _pharmacy_prep_related_text(thread, connected_email)
    sender = parseaddr(latest_inbound_email_for_dashboard(thread, connected_email).get("from", ""))[1].lower().strip()
    # Keep this intentionally strict: work means Pharmacy Prep-related only.
    # Generic words such as order, payment, invoice, login, course, or student are
    # not enough by themselves because they can be personal/non-Pharmacy Prep emails.
    pharmacy_terms = [
        "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com",
        "eprepstation", "pebc", "evaluating exam", "qualifying exam",
        "pebc exam", "pebc exams", "osce", "mcq", "naplex",
        "pharmacy exam", "pharmacist exam", "prep course",
        "pharmacy prep course", "pharmacy prep login", "pharmacy prep access",
        "pharmacy prep order", "pharmacy prep invoice", "pharmacy prep payment",
        "pharmacy prep registration", "pharmacy prep enrollment", "pharmacy prep enrolment",
    ]
    pharmacy_domains = ["pharmacyprep.com", "eprepstation.com"]
    return any(term in text for term in pharmacy_terms) or any(domain in sender for domain in pharmacy_domains)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"

def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 6000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 10000)
    fyi_reason = _is_fyi_notice_or_confirmation(thread, connected_email)

    prompt = f"""
You are screening Gmail for a dashboard used by Pharmacy Prep.

Your job is to decide if the latest inbound message needs a human-visible suggested reply.

STRICT INCLUDE RULES:
Include only if the latest inbound message is a real request/question/problem that needs a reply, such as:
- Pharmacy Prep student/customer asking about PEBC exams, course details, login/access, registration, schedules, notes, recordings, extensions, renewals, orders, invoices, receipts, payment, refund, or support.
- A Pharmacy Prep student/customer/vendor asking about Pharmacy Prep, PEBC, courses, orders, login/access, payments, invoices, schedules, recordings, notes, registration, support, or related business.
- A non-Pharmacy-Prep personal contact asking a direct question or asking the user to do something.
- A follow-up where the sender is waiting for a concrete answer.

STRICT EXCLUDE RULES:
Exclude if it is an FYI notice, announcement to a group, building/property/security notice, document-signing confirmation, receipt, shipment/payment/status update, automated notification, newsletter, marketing email, thank-you-only message, or anything where a reply would be awkward/unnecessary.

CATEGORY RULES:
- work = only emails directly related to Pharmacy Prep, PEBC, courses, students/customers, orders, login/access, payments, invoices, refunds, support, schedules, notes, recordings, registration, renewals, or Pharmacy Prep vendors/business.
- personal = any actionable email outside Pharmacy Prep, even if it is business-like or from an external organization.

VERY IMPORTANT EXAMPLES:
- "Hi Everyone, security staff have a master key..." = exclude. It is a building/property notice, not a Pharmacy Prep request.
- "Thank you for signing the document / Merci..." = exclude. It is a document confirmation, not a request.
- If the email does not mention Pharmacy Prep/course/order/support and does not ask a direct personal/business question, exclude it.

Return JSON only:
{{
  "include": true,
  "category": "work",
  "title": "4-9 word dashboard title",
  "summary": "specific one-sentence summary mentioning who is asking and exactly what they need",
  "reason": "why it needs a reply or why it was excluded",
  "confidence": 0.0
}}

Pre-screen reason, if any: {fyi_reason or 'None'}
Sender display name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get("subject", "")}
Latest body:
{latest_body}

Current thread:
{thread_text}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        include = parsed.get("include", False)
        if isinstance(include, str):
            include = include.strip().lower() in ("true", "yes", "1", "include")
        category = str(parsed.get("category", "work")).lower().strip()
        if category not in ("work", "personal"):
            category = "work"
        # Final category rule from user: work is only Pharmacy Prep-related; everything actionable outside that is personal.
        category = category_for_thread_strict(thread, connected_email)
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        # Hard safety: never allow known FYI/confirmation items through even if AI says include.
        if fyi_reason:
            include = False
            confidence = max(confidence, 0.95)
        return {
            "include": bool(include),
            "category": category,
            "title": str(parsed.get("title", "")).strip(),
            "summary": str(parsed.get("summary", "")).strip(),
            "reason": str(parsed.get("reason", "")).strip() or fyi_reason,
            "confidence": confidence,
        }
    except Exception:
        return None


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = (reply_body or "").strip()
    latest = clean_preview_text(latest_body or "", 5000).strip()
    body_lower = body.lower()
    if len(body.split()) < 28:
        return True
    bad_fillers = [
        "we received your message and will get back to you",
        "we received your message",
        "we will review your request and get back to you shortly",
        "thank you for your email. we will review your request",
        "we will check your enrollment and access details",
        "then send the correct login or course-access instructions",
        "please confirm the email address you used for registration",
        "wanted to reply right away",
        "based on the information currently available, i may need to verify",
    ]
    if any(phrase in body_lower for phrase in bad_fillers):
        return True
    # If the response talks about Pharmacy Prep/course access when the incoming email had nothing to do with it, reject it.
    latest_lower = latest.lower()
    pharmacy_terms = ["pharmacy", "prep", "pebc", "course", "class", "exam", "student", "login", "access", "order", "invoice", "payment", "registration", "enroll", "enrol"]
    if category != "personal" and ("pharmacy prep" in body_lower or "course" in body_lower or "login" in body_lower or "access" in body_lower):
        if not any(term in latest_lower for term in pharmacy_terms):
            return True
    if latest and body_lower.startswith(latest.lower()[:80]):
        return True
    if copied_sequence_found(body, latest, sequence_len=12):
        return True
    body_tokens = set(normalize_for_overlap(body))
    latest_tokens = set(normalize_for_overlap(latest))
    if latest_tokens and len(body_tokens) >= 10:
        overlap_ratio = len(body_tokens & latest_tokens) / max(1, min(len(body_tokens), len(latest_tokens)))
        if overlap_ratio > 0.70 and len(body.split()) > 40:
            return True
    if category != "personal" and "pharmacy prep" not in body_lower:
        return True
    return False


def summary_is_generic(summary: str) -> bool:
    lowered = (summary or "").strip().lower()
    generic = [
        "student is asking for update", "student is asking a course-related question",
        "latest inbound email contains", "sender is asking", "conversation contains",
        "important email", "suggested frontend reply ready", "needs a response", "needs review",
    ]
    return not lowered or any(phrase in lowered for phrase in generic) or len(lowered.split()) < 10


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 10000)
    fyi_reason = _is_fyi_notice_or_confirmation(thread, connected_email)
    if fyi_reason:
        return None

    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 16000)
    prompt = f"""
You are writing a realistic outbound Gmail reply for Pharmacy Prep.

Before writing, decide whether a reply should be drafted at all. If the latest email is an FYI notice, document confirmation, building/security notice, receipt, shipment/payment status, thank-you-only message, or automated notice, return should_reply=false.

When should_reply=true, write a polished, human reply that is specific to the actual email. Avoid template language. Do not begin every reply the same way. Do not say "we received your email". Do not say you will check something if the provided Gmail context already contains the answer.

Context rules:
1. Use Related Gmail context and Stored order context as evidence.
2. If the sender asks for an order number, search the context for order numbers, customer email, product names, payment/order confirmations, and prior sent replies. Include the exact order number if found.
3. If the sender asks about login/access/course details, use the thread and related context to answer what is known. If the exact login/access detail is not present, say what you checked and give the precise next step.
4. If the sender asks about PEBC exams/announcements, answer the specific question as far as the Gmail/context allows and explain what detail they should confirm next if needed.
5. Ask at most ONE follow-up question, only when needed.
6. Do not invent facts. If context does not contain the answer, be transparent but still helpful.
7. Keep the reply shorter: usually 2-3 short paragraphs, about 80-150 words before the signature. Use more only when the question truly requires exact details from Gmail context.
8. Still be specific and useful: include the concrete answer, order number, date, course/detail, or next step when Gmail context provides it.
9. For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not use that signature.
10. The reply must not copy or summarize the inbound message back to the sender.

Return JSON only:
{{
  "should_reply": true,
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence dashboard summary mentioning who is asking and the concrete topic/context",
  "subject": "{clean_subject}",
  "body": "full outbound reply only"
}}

Work signature:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com

Category: {category}
Sender display name: {display_name}
Sender email: {sender_email}
Latest inbound subject: {latest.get("subject", "")}
Latest inbound body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail context found through Gmail API searches:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        should_reply = parsed.get("should_reply", True)
        if isinstance(should_reply, str):
            should_reply = should_reply.strip().lower() in ("true", "yes", "1")
        if not should_reply:
            return None
        body = str(parsed.get("body", "")).strip()
        if not body or reply_needs_regeneration(body, latest_body, category):
            return None
        return {
            "title": str(parsed.get("title", "")).strip(),
            "summary": str(parsed.get("summary", "")).strip(),
            "subject": str(parsed.get("subject", clean_subject)).strip() or clean_subject,
            "body": body,
        }
    except Exception:
        return None


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Optional[Dict]:
    # No generic fallback anymore. A bad fallback is worse than no suggestion.
    return None


def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    thread_id = thread.get("thread_id", "")
    thread_key = thread_action_key(thread, connected_email)
    action = get_thread_action(thread_key)
    stored_item = get_catalog_item("emails", thread_id)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)
    title = (latest.get("subject", "") or stored_item.get("title") or "Important email").strip()

    if was_thread_manually_replied(service, thread, connected_email):
        if not stored_item:
            return None
        return {
            **stored_item,
            "thread_id": thread_id,
            "status": "Already Replied",
            "reply": None,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "screening_version": EMAIL_SCREENING_VERSION,
        }

    if action.get("action_type") == "dismissed":
        return {
            **stored_item,
            "thread_id": thread_id,
            "status": "Suggestion Removed",
            "reply": None,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "screening_version": EMAIL_SCREENING_VERSION,
        }

    noise_reason = _automation_or_noise_reason(thread, connected_email)
    if noise_reason:
        return {
            "thread_id": thread_id,
            "category": stored_item.get("category", "work"),
            "title": title,
            "important_reason": noise_reason,
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": False,
            "screen_confidence": 1.0,
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", ""),
                "to": latest.get("to", ""),
                "date": latest.get("date", ""),
                "subject": latest.get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800),
            },
            "reply": None,
        }

    candidate = should_consider_thread_for_dashboard(thread, connected_email)
    if not candidate and not stored_item:
        return None

    screening = analyze_dashboard_thread_with_ai(thread, connected_email) if candidate else None
    if not screening or not screening.get("include"):
        if not stored_item:
            return {
                "thread_id": thread_id,
                "category": "work",
                "title": title,
                "important_reason": (screening or {}).get("reason", "Not actionable"),
                "status": "Filtered Out",
                "filtered_out": True,
                "ai_screened": True,
                "screen_confidence": (screening or {}).get("confidence", 0.0),
                "screening_version": EMAIL_SCREENING_VERSION,
                "latest_inbound_id": latest_inbound_id,
                "sort_ts": latest_sort_ts or "",
                "original": {
                    "from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""),
                    "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800),
                },
                "reply": None,
            }
        return {**stored_item, "filtered_out": True, "status": "Filtered Out", "reply": None, "screening_version": EMAIL_SCREENING_VERSION}

    # Final category rule from user: work is only Pharmacy Prep-related; anything actionable outside that is personal.
    category = category_for_thread_strict(thread, connected_email)
    if screening.get("title") and 3 <= len(screening.get("title", "").split()) <= 12:
        title = screening.get("title", "").strip()

    if category == "personal":
        apply_label_to_thread_messages(service, thread, personal_label_id)
    else:
        apply_label_to_thread_messages(service, thread, work_label_id)

    important_reason = screening.get("summary", "").strip()
    if summary_is_generic(important_reason):
        important_reason = build_important_reason(thread, connected_email)

    latest_clean_body = clean_preview_text(latest.get("body", ""), 6000)
    cached_reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
    cached_is_usable = bool(cached_reply) and not reply_needs_regeneration(cached_reply.get("body", ""), latest_clean_body, category)

    reply = None
    if cached_is_usable and stored_item.get("screening_version") == EMAIL_SCREENING_VERSION:
        reply = cached_reply
    else:
        queries = heuristic_context_queries_for_thread(thread, connected_email)
        extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=5) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        if composed:
            if composed.get("summary") and not summary_is_generic(composed.get("summary", "")):
                important_reason = composed.get("summary", "").strip()
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                title = composed.get("title", "").strip()
            reply = {
                "thread_id": thread_id,
                "mode": "thread_reply",
                "to": parseaddr(latest.get("from", ""))[1].strip(),
                "subject": composed.get("subject", ""),
                "body": composed.get("body", ""),
            }

    # If we cannot make a good contextual reply, do not show a bogus suggestion.
    if not reply:
        return {
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "important_reason": important_reason or "AI screening accepted this message, but a safe contextual reply could not be generated.",
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": True,
            "screen_confidence": screening.get("confidence", 0),
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
            "original": {
                "from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""),
                "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800),
            },
            "reply": None,
        }

    return {
        "thread_id": thread_id,
        "category": category,
        "title": title,
        "important_reason": important_reason,
        "status": "Needs Reply",
        "reply_sent_at": stored_item.get("reply_sent_at", ""),
        "latest_inbound_id": latest_inbound_id,
        "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
        "ai_screened": True,
        "screen_confidence": screening.get("confidence", 0),
        "screening_version": EMAIL_SCREENING_VERSION,
        "original": {
            "from": latest.get("from", ""),
            "to": latest.get("to", ""),
            "date": latest.get("date", ""),
            "subject": latest.get("subject", ""),
            "body": clean_preview_text(latest.get("body", ""), 1800),
        },
        "reply": reply,
    }


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    text = _catalog_text(item)
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "e-transfer received", "etransfer received", "interac e-transfer",
        "payment received", "receipt for your payment", "charge receipt", "invoice paid",
        "tracking number", "shipment", "unsubscribe", "promotion", "newsletter",
        "please moderate", "comment awaiting moderation", "new question submitted", "security alert",
        "verification code", "password reset", "mail delivery", "undeliverable", "do not reply",
        "no-reply", "noreply", "thank you for signing the document", "merci d’avoir signé",
        "merci d'avoir signé", "master key", "unit door", "security staff", "management office",
        "contractor requires access", "building management", "property management",
    ]
    return any(term in text for term in bad_terms)


def _is_catalog_email_visible(item: Dict) -> bool:
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if item.get("screening_version") != EMAIL_SCREENING_VERSION:
        return False
    reason = item.get("important_reason", "")
    has_specific_reason = bool(reason) and not summary_is_generic(reason)
    has_reply = bool(item.get("reply")) and not reply_needs_regeneration(item.get("reply", {}).get("body", ""), item.get("original", {}).get("body", ""), item.get("category", "work"))
    handled = item.get("status") in ("Already Replied", "Suggestion Removed") and has_specific_reason
    return has_reply or handled


# Final override: keep Work limited to Pharmacy Prep-related items only.
def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)



# ---------------------------------------------------------------------
# FINAL PATCH: app login, browser Gmail re-auth, and true incremental refresh
# ---------------------------------------------------------------------
APP_LOGIN_USERNAME = os.getenv("APP_LOGIN_USERNAME", "")
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "")
GMAIL_TOKEN_FILE = BASE_DIR / "token.json"
GMAIL_CREDENTIALS_FILE = BASE_DIR / "credentials.json"

class GmailAuthRequired(Exception):
    """Raised when Gmail needs the user to sign in again."""
    pass


def _safe_next_url(value: str) -> str:
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/login"):
        return "/"
    return value


def _is_app_logged_in() -> bool:
    return bool(session.get("app_logged_in"))


@app.before_request
def _require_app_login():
    path = request.path or "/"
    allowed = (
        path in ("/login", "/login.html", "/auth/gmail", "/oauth2callback", "/favicon.ico")
        or path.startswith("/static/")
    )
    if allowed:
        return None
    if _is_app_logged_in():
        return None
    if path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "auth_required": True,
            "auth_type": "app_login",
            "error": "Please sign in to continue.",
            "login_url": f"/login?next={_safe_next_url(request.full_path)}",
        }), 401
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
    return "Put login.html in the same folder as app.py.", 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


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
        raise FileNotFoundError("Missing credentials.json. Put credentials.json in the same folder as app.py.")
    return Flow.from_client_secrets_file(
        str(GMAIL_CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=_gmail_redirect_uri(),
    )


def _gmail_auth_url(return_to: str = "/") -> str:
    return f"/auth/gmail?next={_safe_next_url(return_to)}"


# Override previous get_gmail_service: never blocks AWS with run_local_server.
def get_gmail_service():
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


@app.route("/auth/gmail")
def auth_gmail():
    next_url = _safe_next_url(request.args.get("next") or request.referrer or "/")
    session["gmail_auth_return_to"] = next_url
    flow = _make_gmail_flow()
    authorization_url, state_value = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["gmail_oauth_state"] = state_value
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    # Local testing over http needs this. For AWS, set PUBLIC_BASE_URL to your real https URL when available.
    if request.host.startswith("127.0.0.1") or request.host.startswith("localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_gmail_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    invalidate_dashboard_cache()
    return redirect(_safe_next_url(session.pop("gmail_auth_return_to", "/")))


@app.errorhandler(GmailAuthRequired)
def _handle_gmail_auth_required(error):
    return jsonify({
        "ok": False,
        "auth_required": True,
        "auth_type": "gmail",
        "error": "Please sign in again.",
        "auth_url": _gmail_auth_url(request.full_path or "/"),
    }), 401


def _latest_catalog_datetime_for_incremental(catalog: Dict) -> Optional[datetime]:
    latest_dt = None
    for bucket_name in ("orders", "emails"):
        bucket = catalog.get(bucket_name, {}) or {}
        if not isinstance(bucket, dict):
            continue
        for item in bucket.values():
            try:
                raw = _item_date_for_window(item)
                parsed = email_date_to_datetime(raw)
                if parsed and parsed != datetime.min and parsed >= SCAN_START_DT:
                    if latest_dt is None or parsed > latest_dt:
                        latest_dt = parsed
            except Exception:
                continue
    return latest_dt


# Override previous scan window logic: refresh only scans newer than the newest stored item.
def _scan_after_clause_for_catalog(catalog: Dict, force_full: bool = False) -> Tuple[str, str]:
    if force_full:
        start_dt = SCAN_START_DT
    else:
        newest_seen = _latest_catalog_datetime_for_incremental(catalog)
        if newest_seen and newest_seen > SCAN_START_DT:
            # Gmail search is date-only, so query that day and locally skip anything not newer.
            start_dt = newest_seen
        else:
            start_dt = SCAN_START_DT
    after_value = (start_dt - timedelta(days=1)).strftime("%Y/%m/%d")
    return f"after:{after_value}", start_dt.isoformat(timespec="seconds")


def _looks_like_fyi_only_notice(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    text = f"{latest.get('subject', '')}\n{latest.get('body', '')}".lower()
    fyi_terms = [
        "for your information", "fyi", "no action required", "no need to reply",
        "please be advised", "notice to residents", "building notice", "management office",
        "master key", "unit door", "contractor requires access", "thank you for signing",
        "merci d’avoir signé", "merci d'avoir signé", "document has been signed",
        "completed document", "signed document", "receipt", "confirmation",
    ]
    request_terms = ["?", "please send", "can you", "could you", "would you", "i need", "i would like", "help me"]
    if any(term in text for term in request_terms):
        return False
    return any(term in text for term in fyi_terms)


# Final override: incremental refresh scans only threads with a latest inbound message newer than stored catalog.
def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    work_label_id = get_or_create_label(service, WORK_LABEL)

    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog.setdefault("orders", {})
    catalog.setdefault("emails", {})

    newest_existing_dt = None if force_full else _latest_catalog_datetime_for_incremental(catalog)
    date_clause, scan_start_used = _scan_after_clause_for_catalog(catalog, force_full=force_full)
    debug = _debug_counts_template() if "_debug_counts_template" in globals() else {"errors": []}
    debug["scan_start"] = scan_start_used
    debug["force_full"] = force_full
    debug["incremental_newer_than"] = newest_existing_dt.isoformat(timespec="seconds") if newest_existing_dt else ""
    print(f"[scan] starting Gmail scan | force_full={force_full} | date_clause={date_clause} | newer_than={debug['incremental_newer_than']}", flush=True)

    order_thread_ids = _collect_thread_ids(service, _order_scan_queries(date_clause), per_query_limit=max(10, MAX_ORDER_THREADS_PER_SCAN // 4), total_limit=MAX_ORDER_THREADS_PER_SCAN)
    email_queries = _email_scan_queries(date_clause, connected_email)
    email_thread_ids = _collect_thread_ids(service, email_queries, per_query_limit=max(30, MAX_EMAIL_THREADS_PER_SCAN // max(1, len(email_queries))), total_limit=MAX_EMAIL_THREADS_PER_SCAN)
    debug["order_threads_found"] = len(order_thread_ids)
    debug["email_threads_found"] = len(email_thread_ids)
    print(f"[scan] Gmail returned {len(order_thread_ids)} order thread(s), {len(email_thread_ids)} possible email thread(s)", flush=True)

    auto_orders_sent = 0
    order_replies_waiting = 0
    suggested_replies = 0
    skipped_failed_orders = 0
    processed_order_threads = set()
    ai_screenings_used = 0

    def _thread_newer_than_catalog(thread: Dict) -> bool:
        if force_full or not newest_existing_dt:
            return True
        latest = latest_inbound_email_for_dashboard(thread, connected_email)
        latest_dt = email_date_to_datetime(latest.get("date", ""))
        return bool(latest_dt and latest_dt != datetime.min and latest_dt > newest_existing_dt)

    for thread_id in order_thread_ids:
        try:
            thread = read_thread(service, thread_id)
            if not _thread_newer_than_catalog(thread):
                continue
            order_item = build_order_item(service, thread, connected_email)
            if not order_item:
                continue
            processed_order_threads.add(thread_id)
            if order_item.get("order_number") == "Unknown":
                skipped_failed_orders += 1
            order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
            if did_send:
                auto_orders_sent += 1
            if order_item.get("reply"):
                order_replies_waiting += 1
            _upsert_order_in_catalog(catalog, thread_id, order_item)
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["email_errors"] = debug.get("email_errors", 0) + 1
            debug.setdefault("errors", []).append(f"order {thread_id}: {error}")
            continue

    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads:
            continue
        try:
            thread = read_thread(service, thread_id)
            debug["email_threads_read"] = debug.get("email_threads_read", 0) + 1
            latest = latest_inbound_email_for_dashboard(thread, connected_email)
            subject = latest.get("subject", "")
            sender = latest.get("from", "")

            if not _thread_newer_than_catalog(thread):
                debug["email_skipped_old_date"] = debug.get("email_skipped_old_date", 0) + 1
                continue
            if not _thread_is_on_or_after_scan_start(thread, connected_email):
                debug["email_skipped_old_date"] = debug.get("email_skipped_old_date", 0) + 1
                continue
            if get_best_order_email_text(thread):
                debug["email_skipped_order_notification"] = debug.get("email_skipped_order_notification", 0) + 1
                continue
            if latest_email_is_from_connected_account(thread, connected_email):
                debug["email_skipped_latest_from_us"] = debug.get("email_skipped_latest_from_us", 0) + 1
                continue
            if is_obvious_automated_email(thread, connected_email) or _looks_like_fyi_only_notice(thread, connected_email):
                debug["email_skipped_automation"] = debug.get("email_skipped_automation", 0) + 1
                debug.setdefault("first_rejected_examples", [])[:8]
                if len(debug.setdefault("first_rejected_examples", [])) < 8:
                    debug["first_rejected_examples"].append({"subject": subject, "from": sender, "reason": "automation/fyi"})
                continue
            if not should_consider_thread_for_dashboard(thread, connected_email):
                debug["email_skipped_prescreen"] = debug.get("email_skipped_prescreen", 0) + 1
                if len(debug.setdefault("first_rejected_examples", [])) < 8:
                    debug["first_rejected_examples"].append({"subject": subject, "from": sender, "reason": "prescreen"})
                continue

            item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id)
            ai_screenings_used += 1

            if item:
                _upsert_email_in_catalog(catalog, thread_id, item)

            if item and item.get("reply") and not item.get("filtered_out"):
                suggested_replies += 1
                debug["email_ai_accepted"] = debug.get("email_ai_accepted", 0) + 1
                if len(debug.setdefault("first_accepted_examples", [])) < 8:
                    debug["first_accepted_examples"].append({"subject": subject, "from": sender, "category": item.get("category"), "reason": item.get("important_reason", "")})
            else:
                debug["email_ai_rejected"] = debug.get("email_ai_rejected", 0) + 1
                if len(debug.setdefault("first_rejected_examples", [])) < 8:
                    debug["first_rejected_examples"].append({"subject": subject, "from": sender, "reason": "ai rejected or no contextual reply"})
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["email_errors"] = debug.get("email_errors", 0) + 1
            debug.setdefault("errors", []).append(f"email {thread_id}: {error}")
            continue

    now = datetime.now().isoformat(timespec="seconds")
    catalog["meta"] = {
        **catalog.get("meta", {}),
        "connected_email": connected_email,
        "last_successful_scan_at": now,
        "last_incremental_newer_than": debug.get("incremental_newer_than", ""),
        "scan_start_date": SCAN_START_DT.strftime("%Y-%m-%d"),
        "email_screening_version": EMAIL_SCREENING_VERSION,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
    }
    save_dashboard_catalog(catalog)
    invalidate_dashboard_cache()

    payload = build_dashboard_payload(force_refresh=True)
    orders = payload.get("orders", [])
    emails = payload.get("emails", [])
    payload["briefing"] = build_daily_briefing(connected_email, orders, emails)
    suggested_replies = len([email for email in emails if email.get("reply")])
    debug["visible_orders_after_scan"] = len(orders)
    debug["visible_emails_after_scan"] = len(emails)
    if "_save_scan_debug" in globals():
        _save_scan_debug(debug)

    payload["scan_summary"] = {
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "scan_start": scan_start_used,
        "incremental_newer_than": debug.get("incremental_newer_than", ""),
        "orders_total": len(orders),
        "orders_replied": len([order for order in orders if not order.get("reply")]),
        "auto_orders_sent": auto_orders_sent,
        "order_replies_waiting": order_replies_waiting,
        "failed_orders_skipped": skipped_failed_orders,
        "suggested_replies": suggested_replies,
        "emails_checked": len(email_thread_ids),
        "email_threads_read": debug.get("email_threads_read", 0),
        "emails_accepted": debug.get("email_ai_accepted", 0) + debug.get("email_deterministic_accepted", 0),
        "emails_rejected": debug.get("email_skipped_order_notification", 0) + debug.get("email_skipped_old_date", 0) + debug.get("email_skipped_latest_from_us", 0) + debug.get("email_skipped_automation", 0) + debug.get("email_skipped_prescreen", 0) + debug.get("email_ai_rejected", 0),
        "ai_screenings_used": ai_screenings_used,
        "debug_file": str(SCAN_DEBUG_FILE),
    }
    print(f"[scan] complete | read={debug.get('email_threads_read',0)} | accepted={payload['scan_summary']['emails_accepted']} | suggested={suggested_replies} | rejected={payload['scan_summary']['emails_rejected']} | debug={SCAN_DEBUG_FILE}", flush=True)
    return payload


# Wrap API routes to return clean Gmail sign-in instructions when Gmail token needs re-auth.
def _json_gmail_auth_required():
    return jsonify({
        "ok": False,
        "auth_required": True,
        "auth_type": "gmail",
        "error": "Please sign in again.",
        "auth_url": _gmail_auth_url(request.full_path or "/"),
    }), 401


# Patch the existing API view functions without changing their URLs.
def _convert_gmail_auth_response(result):
    response = app.make_response(result)
    if response.status_code in (401, 403, 500):
        body = response.get_data(as_text=True) or ""
        if "Please sign in again" in body or "GmailAuthRequired" in body:
            return _json_gmail_auth_required()
    return result

_original_api_scan = app.view_functions.get("api_scan")
def api_scan():
    try:
        request_body = request.get_json(silent=True) or {}
        payload = perform_gmail_scan(force_full=bool(request_body.get("force_full", False)))
        summary = payload.get("scan_summary", {})
        return jsonify({
            "ok": True,
            "message": "Refresh complete.",
            "order_replies_waiting": summary.get("order_replies_waiting", len([order for order in payload.get("orders", []) if order.get("reply")])) ,
            "failed_orders_skipped": summary.get("failed_orders_skipped", len([order for order in payload.get("orders", []) if order.get("order_number") == "Unknown"])),
            "suggested_replies": summary.get("suggested_replies", len([email for email in payload.get("emails", []) if email.get("reply")])),
            "auto_orders_sent": summary.get("auto_orders_sent", 0),
            "emails_checked": summary.get("emails_checked", 0),
            "email_threads_read": summary.get("email_threads_read", 0),
            "emails_accepted": summary.get("emails_accepted", 0),
            "emails_rejected": summary.get("emails_rejected", 0),
            "ai_screenings_used": summary.get("ai_screenings_used", 0),
            "debug_file": summary.get("debug_file", ""),
            "scan_start": summary.get("scan_start", ""),
            "incremental_newer_than": summary.get("incremental_newer_than", ""),
            "scan_window": summary.get("scan_window", f"{SCAN_START_DISPLAY} onward"),
        })
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_scan"] = api_scan

_original_api_send_reply = app.view_functions.get("api_send_reply")
def api_send_reply(thread_id: str):
    try:
        return _convert_gmail_auth_response(_original_api_send_reply(thread_id))
    except GmailAuthRequired:
        return _json_gmail_auth_required()
app.view_functions["api_send_reply"] = api_send_reply

_original_api_remove_reply = app.view_functions.get("api_remove_reply")
def api_remove_reply(thread_id: str):
    try:
        return _convert_gmail_auth_response(_original_api_remove_reply(thread_id))
    except GmailAuthRequired:
        return _json_gmail_auth_required()
app.view_functions["api_remove_reply"] = api_remove_reply


# ---------------------------------------------------------------------
# FINAL PATCH: EprepStation account renewal request support
# ---------------------------------------------------------------------
# Include exact renewal notifications as actionable work items. These are separate
# from paid/amount-based renewal/order messages and should generate a new email to
# the student/customer email inside the notification.
RENEWAL_REQUEST_SCREENING_VERSION = "2026-06-renewal-requests-v10"
try:
    EMAIL_SCREENING_VERSION = RENEWAL_REQUEST_SCREENING_VERSION
except Exception:
    pass

_RENEWAL_EXACT_SUBJECT = "account renewal request received from eprepstation.com"


def _renewal_latest_text(thread: Dict, connected_email: str = "") -> Tuple[Dict, str, str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    subject = latest.get("subject", "") or ""
    body = clean_preview_text(latest.get("body", ""), 12000)
    text = f"{subject}\n{body}"
    return latest, subject, body, text


def is_eprepstation_account_renewal_request(thread: Dict, connected_email: str = "") -> bool:
    latest, subject, body, text = _renewal_latest_text(thread, connected_email)
    subject_l = subject.lower().strip()
    text_l = text.lower()
    # User requested the EprepStation notification specifically, not the separate
    # $214 renewal/payment/order email type.
    if "214$" in subject_l or "$214" in subject_l or "214.00" in subject_l:
        return False
    return _RENEWAL_EXACT_SUBJECT in subject_l or (
        "account renewal request received" in subject_l and "eprepstation" in text_l
    )


def _line_after_label(text: str, labels: List[str]) -> str:
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[:\-]\s*(.+?)\s*$"
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return ""


def extract_renewal_request_details(thread: Dict, connected_email: str = "") -> Dict:
    latest, subject, body, text = _renewal_latest_text(thread, connected_email)
    customer_email = extract_customer_email(text, connected_email) or ""

    name = _line_after_label(text, [
        "Name", "Full Name", "Customer Name", "Student Name", "Account Name", "User Name", "User"
    ])
    if not name:
        # Sometimes the body says something like "Account renewal request from John Smith".
        match = re.search(r"(?i)renewal request\s+(?:received\s+)?from\s+([^\n<]+)", text)
        if match:
            name = re.sub(r"\s+", " ", match.group(1)).strip(" :-")
    if not name:
        name = infer_customer_name_from_email(customer_email) or "Customer"
    name = re.sub(r"[<>\[\]\{\}\"]", "", name).strip()
    if not name or name.lower() in ("account renewal request received from eprepstation.com", "customer"):
        name = infer_customer_name_from_email(customer_email) or "Customer"

    course = _line_after_label(text, [
        "Course", "Course Name", "Program", "Product", "Membership", "Plan", "Package", "Account", "Requested Course"
    ])
    if not course:
        course_line_candidates = []
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" -:\t")
            lowered = line.lower()
            if not line or len(line) > 160:
                continue
            if any(token in lowered for token in ["pebc", "osce", "mcq", "evaluating", "qualifying", "course", "prep", "naplex"]):
                if "account renewal request" not in lowered and "eprepstation" not in lowered:
                    course_line_candidates.append(line)
        if course_line_candidates:
            course = course_line_candidates[0]
    course = re.sub(r"\s+", " ", course or "your course").strip()

    return {
        "name": name,
        "email": customer_email,
        "course": course,
        "subject": subject,
        "body": body,
        "latest": latest,
    }


def build_renewal_request_reply(details: Dict) -> Tuple[str, str]:
    name = details.get("name") or "Customer"
    course = details.get("course") or "your course"
    subject = f"Re: Account renewal request for {course}"
    body = f"""Dear {name},

Thank you for submitting your account renewal request for {course}.

We have received your request and will review your account details. Once confirmed, we will update your course access and send you a confirmation by email.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    return subject, body


def build_renewal_request_item(service, thread: Dict, connected_email: str) -> Optional[Dict]:
    if not is_eprepstation_account_renewal_request(thread, connected_email):
        return None
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return None
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    thread_id = thread.get("thread_id", "")
    details = extract_renewal_request_details(thread, connected_email)
    customer_email = details.get("email", "")
    customer_name = details.get("name", "Customer")
    course = details.get("course", "your course")
    title = f"Account renewal request from {customer_name}"
    important_reason = f"{customer_name} submitted an EprepStation account renewal request for {course}; a course-specific renewal reply is ready for review."
    reply = None
    if customer_email:
        subject, body = build_renewal_request_reply(details)
        reply = {
            "thread_id": thread_id,
            "mode": "new_email",
            "to": customer_email,
            "subject": subject,
            "body": body,
        }
    return {
        "thread_id": thread_id,
        "category": "work",
        "title": title,
        "important_reason": important_reason,
        "status": "Needs Reply" if reply else "Filtered Out",
        "filtered_out": False if reply else True,
        "reply_sent_at": "",
        "latest_inbound_id": latest_inbound_message_id(thread, connected_email),
        "sort_ts": latest_inbound_sort_key(thread, connected_email),
        "ai_screened": True,
        "screen_confidence": 1.0,
        "screening_version": EMAIL_SCREENING_VERSION,
        "renewal_request": True,
        "renewal_customer_email": customer_email,
        "renewal_customer_name": customer_name,
        "renewal_course": course,
        "original": {
            "from": latest.get("from", ""),
            "to": latest.get("to", ""),
            "date": latest.get("date", ""),
            "subject": latest.get("subject", ""),
            "body": clean_preview_text(latest.get("body", ""), 1800),
        },
        "reply": reply,
    }


_previous_automation_or_noise_reason_for_renewal = _automation_or_noise_reason
def _automation_or_noise_reason(thread: Dict, connected_email: str) -> str:
    if is_eprepstation_account_renewal_request(thread, connected_email):
        return ""
    return _previous_automation_or_noise_reason_for_renewal(thread, connected_email)


_previous_should_consider_thread_for_dashboard_for_renewal = should_consider_thread_for_dashboard
def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if is_eprepstation_account_renewal_request(thread, connected_email):
        return True
    return _previous_should_consider_thread_for_dashboard_for_renewal(thread, connected_email)


_previous_email_scan_queries_for_renewal = _email_scan_queries
def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    queries = [
        f'{date_clause} "Account Renewal Request Received from EprepStation.com"',
        f'{date_clause} "Account Renewal Request Received" "EprepStation.com"',
    ]
    for query in _previous_email_scan_queries_for_renewal(date_clause, connected_email):
        if query not in queries:
            queries.append(query)
    return queries


_previous_build_general_email_item_for_renewal = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    renewal_item = build_renewal_request_item(service, thread, connected_email)
    if renewal_item:
        try:
            apply_label_to_thread_messages(service, thread, work_label_id)
        except Exception:
            pass
        return renewal_item
    return _previous_build_general_email_item_for_renewal(service, thread, connected_email, personal_label_id, work_label_id)


_previous_api_send_reply_for_renewal = app.view_functions.get("api_send_reply")
def api_send_reply(thread_id: str):
    try:
        if not get_automation_settings().get("auto_reply_enabled", True):
            return jsonify({"ok": False, "error": "Auto Reply is off. Turn it on before sending replies."}), 403
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        body_payload = request.get_json(silent=True) or {}
        subject = (body_payload.get("subject") or "").strip()
        reply_body = (body_payload.get("body") or "").strip()
        thread = read_thread(service, thread_id)

        if is_eprepstation_account_renewal_request(thread, connected_email):
            item = get_catalog_item("emails", thread_id) or build_renewal_request_item(service, thread, connected_email) or {}
            reply = item.get("reply") or {}
            to_email = (reply.get("to") or extract_renewal_request_details(thread, connected_email).get("email") or "").strip()
            if not to_email:
                raise ValueError("Could not find the renewal request customer email address.")
            if not subject:
                subject = reply.get("subject") or "Re: Account renewal request"
            if not reply_body:
                reply_body = reply.get("body") or build_renewal_request_reply(extract_renewal_request_details(thread, connected_email))[1]
            sent = send_new_email(service, to_email, subject, reply_body)
            upsert_catalog_item("emails", thread_id, {
                "status": "Already Replied",
                "reply": None,
                "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
                "renewal_request": True,
            })
            mark_thread_action_processed(thread_action_key(thread, connected_email), "frontend_reply_sent", sent.get("id", ""), renewal_request=True)
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Renewal reply sent successfully.", "sent": sent})

        return _previous_api_send_reply_for_renewal(thread_id)
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

app.view_functions["api_send_reply"] = api_send_reply



_previous_perform_gmail_scan_for_renewal = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    catalog = get_dashboard_catalog()
    meta = catalog.get("meta", {}) if isinstance(catalog, dict) else {}
    # Force one June-forward re-screen after this update so existing renewal
    # notifications already in Gmail can be picked up once. Future refreshes stay incremental.
    if meta.get("email_screening_version") != EMAIL_SCREENING_VERSION:
        force_full = True
    return _previous_perform_gmail_scan_for_renewal(force_full=force_full)



if __name__ == "__main__":
    print("\nPharmacy Prep Gmail Assistant is starting...")
    print("Open this link in your browser:")
    port = int(os.getenv("PORT", "5050"))
    print(f"http://127.0.0.1:{port}")
    print("")
    app.run(host="0.0.0.0", port=port, debug=False)
