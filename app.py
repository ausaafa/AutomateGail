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
APP_LOGIN_USERNAME = os.getenv("APP_LOGIN_USERNAME", "success@pharmacyprep.com")
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "Pharmacy1966")
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
# MINIMAL RENEWAL PATCH: dedupe + simple renewal card original message
# ---------------------------------------------------------------------
# This patch is intentionally small and only affects EprepStation renewal
# request items. It does not alter normal email screening/replies.
import hashlib

RENEWAL_REQUEST_PATCH_VERSION = "2026-06-renewal-dedupe-visual-only-v1"


def _renewal_norm(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def _renewal_subject_matches(subject: str, body: str = "") -> bool:
    text = f"{subject}\n{body}".lower()
    return (
        "account renewal request received" in text
        and ("eprepstation" in text or "your e-mail address" in text or "your email address" in text)
    )


def _renewal_extract_field(text: str, labels: List[str]) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for label in labels:
        # Capture the value on the same line, allowing the HTML-to-text extraction to add spacing.
        pattern = rf"{re.escape(label)}\s*[:\-]?\s*([^\n]+)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" -:\t")
            if value:
                return value
    return ""


def _renewal_extract_email(text: str) -> str:
    explicit = _renewal_extract_field(text, [
        "Your E-mail Address",
        "Your Email Address",
        "E-mail Address",
        "Email Address",
        "Email",
    ])
    if explicit:
        match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", explicit)
        if match:
            return match.group(0).strip()
    # Fallback: choose the first non-PharmacyPrep/EprepStation email from the body.
    for email in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text or ""):
        lowered = email.lower()
        if "pharmacyprep.com" not in lowered and "eprepstation.com" not in lowered and "wordpress" not in lowered:
            return email.strip()
    return ""


def _renewal_clean_name(name: str, email: str = "") -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip(" -:\t")
    bad = {"no reply", "no-reply", "noreply", "wordpress", "eprepstation", "customer", "student"}
    if name and name.lower() not in bad and "@" not in name:
        return name.title() if name.isupper() or name.islower() else name
    return infer_customer_name_from_email(email) or "Customer"


def _renewal_extract_details_from_thread(thread: Dict, connected_email: str = "") -> Optional[Dict]:
    for email in thread.get("emails", []):
        subject = email.get("subject", "") or ""
        body = email.get("body", "") or ""
        text = f"Subject: {subject}\n\n{body}"
        if not _renewal_subject_matches(subject, body):
            continue
        student_email = _renewal_extract_email(text)
        course = _renewal_extract_field(text, [
            "Exam you are taking",
            "Exam your are taking",
            "Exam you are Taking",
            "Course",
            "Course Name",
            "Exam",
        ])
        name = _renewal_extract_field(text, ["Your Name", "Name"])
        username = _renewal_extract_field(text, ["Your User Name", "Your Username", "Username", "User Name"])
        name = _renewal_clean_name(name, student_email)
        if not student_email or not course:
            return None
        course = re.sub(r"\s+", " ", course).strip(" -:\t")
        return {
            "student_name": name,
            "student_email": student_email,
            "username": username,
            "course": course,
            "source_subject": subject,
            "source_from": email.get("from", ""),
            "source_to": email.get("to", ""),
            "source_date": email.get("date", ""),
            "source_thread_id": thread.get("thread_id", ""),
            "source_message_id": email.get("gmail_message_id", ""),
        }
    return None


def _renewal_key(student_email: str, course: str) -> str:
    raw = f"{_renewal_norm(student_email)}|{_renewal_norm(course)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _renewal_stable_thread_id(student_email: str, course: str) -> str:
    return f"renewal_{_renewal_key(student_email, course)}"


def _renewal_original_body(student_email: str, course: str) -> str:
    return f"Student email: {student_email}\nCourse: {course}"


def _renewal_reply_body(name: str, course: str) -> str:
    first_name = (name or "there").strip().split()[0] if (name or "").strip() else "there"
    return f"""Hello {first_name},

Thank you for submitting your account renewal request for {course}. We have received the request and will review the account details connected to your course access.

We will follow up shortly with the renewal status and any next steps needed to restore or extend your access.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""


def _build_renewal_catalog_item(details: Dict, existing: Optional[Dict] = None) -> Dict:
    existing = existing or {}
    name = details.get("student_name") or "Customer"
    student_email = details.get("student_email", "")
    course = details.get("course", "")
    stable_id = _renewal_stable_thread_id(student_email, course)
    already_replied = existing.get("status") == "Already Replied" or bool(existing.get("reply_sent_at"))
    reply = None if already_replied else {
        "thread_id": stable_id,
        "mode": "new_email",
        "to": student_email,
        "subject": f"Account renewal request - {course}",
        "body": _renewal_reply_body(name, course),
    }
    return {
        **existing,
        "thread_id": stable_id,
        "category": "work",
        "title": f"Account renewal request from {name}",
        "important_reason": f"{name} submitted an EprepStation account renewal request for {course}.",
        "status": "Already Replied" if already_replied else "Needs Reply",
        "reply_sent_at": existing.get("reply_sent_at", ""),
        "latest_inbound_id": details.get("source_message_id", existing.get("latest_inbound_id", "")),
        "sort_ts": email_date_to_sort_key(details.get("source_date", "")) or existing.get("sort_ts", ""),
        "ai_screened": True,
        "screen_confidence": 1.0,
        "screening_version": EMAIL_SCREENING_VERSION,
        "is_renewal_request": True,
        "renewal_patch_version": RENEWAL_REQUEST_PATCH_VERSION,
        "renewal_details": {
            "student_name": name,
            "student_email": student_email,
            "username": details.get("username", ""),
            "course": course,
            "source_thread_id": details.get("source_thread_id", ""),
        },
        "filtered_out": False,
        "original": {
            "from": details.get("source_from", ""),
            "to": details.get("source_to", ""),
            "date": details.get("source_date", ""),
            "subject": details.get("source_subject", "Account Renewal Request Received from EprepStation.com"),
            "body": _renewal_original_body(student_email, course),
        },
        "reply": reply,
    }


def _item_renewal_details(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    details = item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
    title = item.get("title", "") or ""
    subject = original.get("subject", "") or ""
    body = original.get("body", "") or ""
    looks_like = bool(item.get("is_renewal_request")) or "account renewal request" in title.lower() or _renewal_subject_matches(subject, body)
    if not looks_like:
        return None
    student_email = details.get("student_email") or reply.get("to") or _renewal_extract_email(body)
    course = details.get("course") or _renewal_extract_field(body, ["Course", "Exam you are taking", "Exam your are taking", "Exam"])
    name = details.get("student_name") or re.sub(r"^account renewal request from\s+", "", title, flags=re.IGNORECASE).strip()
    name = _renewal_clean_name(name, student_email)
    if not student_email or not course:
        return None
    return {
        "student_name": name,
        "student_email": student_email,
        "course": course,
        "username": details.get("username", ""),
    }


def _normalize_renewal_item_for_display(item: Dict) -> Dict:
    details = _item_renewal_details(item)
    if not details:
        return item
    normalized = deepcopy(item)
    stable_id = _renewal_stable_thread_id(details["student_email"], details["course"])
    normalized["thread_id"] = stable_id
    normalized["category"] = "work"
    normalized["title"] = f"Account renewal request from {details['student_name']}"
    normalized["important_reason"] = f"{details['student_name']} submitted an EprepStation account renewal request for {details['course']}."
    normalized["status"] = normalized.get("status") or "Needs Reply"
    normalized["filtered_out"] = False
    normalized["ai_screened"] = True
    normalized["screen_confidence"] = 1.0
    normalized["screening_version"] = EMAIL_SCREENING_VERSION
    normalized["is_renewal_request"] = True
    normalized["renewal_patch_version"] = RENEWAL_REQUEST_PATCH_VERSION
    normalized["renewal_details"] = details
    original = normalized.setdefault("original", {})
    original["body"] = _renewal_original_body(details["student_email"], details["course"])
    if not original.get("subject"):
        original["subject"] = "Account Renewal Request Received from EprepStation.com"
    if normalized.get("status") != "Already Replied":
        normalized["reply"] = {
            "thread_id": stable_id,
            "mode": "new_email",
            "to": details["student_email"],
            "subject": f"Account renewal request - {details['course']}",
            "body": _renewal_reply_body(details["student_name"], details["course"]),
        }
    return normalized


def _normalize_catalog_renewals(catalog: Dict) -> Tuple[Dict, int]:
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        catalog["emails"] = {}
        return catalog, 0
    normalized_bucket = {}
    changed = 0
    for key, item in list(emails_bucket.items()):
        details = _item_renewal_details(item)
        if not details:
            normalized_bucket[key] = item
            continue
        normalized = _normalize_renewal_item_for_display(item)
        stable_id = _renewal_stable_thread_id(details["student_email"], details["course"])
        existing = normalized_bucket.get(stable_id)
        if existing:
            # Keep an already-replied version if one exists; otherwise keep the newest sort timestamp.
            existing_replied = existing.get("status") == "Already Replied" or bool(existing.get("reply_sent_at"))
            normalized_replied = normalized.get("status") == "Already Replied" or bool(normalized.get("reply_sent_at"))
            if normalized_replied and not existing_replied:
                normalized_bucket[stable_id] = normalized
            elif normalized.get("sort_ts", "") > existing.get("sort_ts", "") and existing_replied == normalized_replied:
                normalized_bucket[stable_id] = normalized
            changed += 1
        else:
            normalized_bucket[stable_id] = normalized
            if key != stable_id:
                changed += 1
    if changed:
        catalog["emails"] = normalized_bucket
    return catalog, changed


def _renewal_scan_queries() -> List[str]:
    base = f"after:{SCAN_START_GMAIL_AFTER}"
    return [
        f'{base} "Account Renewal Request Received"',
        f'{base} "Account Renewal Request Received from EprepStation.com"',
        f'{base} "Your E-mail Address" "Exam"',
        f'{base} "Exam your are taking"',
        f'{base} "Exam you are taking"',
    ]


def _scan_and_upsert_renewal_requests(service, catalog: Dict, connected_email: str) -> int:
    found = 0
    accepted = 0
    thread_ids = _collect_thread_ids(service, _renewal_scan_queries(), per_query_limit=50, total_limit=250)
    for thread_id in thread_ids:
        try:
            thread = read_thread(service, thread_id)
            details = _renewal_extract_details_from_thread(thread, connected_email)
            if not details:
                continue
            stable_id = _renewal_stable_thread_id(details["student_email"], details["course"])
            existing = catalog.setdefault("emails", {}).get(stable_id, {})
            catalog["emails"][stable_id] = _build_renewal_catalog_item(details, existing=existing)
            found += 1
            accepted += 1
        except Exception:
            continue
    catalog, changed = _normalize_catalog_renewals(catalog)
    if found or changed:
        save_dashboard_catalog(catalog)
    print(f"[renewal-minimal] candidates={len(thread_ids)} accepted={accepted} deduped={changed}", flush=True)
    return accepted


# Override visibility only for renewal items so no-reply/EprepStation automation rules do not hide them.
_previous_is_catalog_email_visible_for_renewal = _is_catalog_email_visible
def _is_catalog_email_visible(item: Dict) -> bool:
    details = _item_renewal_details(item)
    if details:
        if not _item_is_on_or_after_scan_start(item):
            return False
        item = _normalize_renewal_item_for_display(item)
        return bool(item.get("reply")) or item.get("status") == "Already Replied"
    return _previous_is_catalog_email_visible_for_renewal(item)


_previous_build_dashboard_payload_for_renewal = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    catalog = get_dashboard_catalog()
    catalog, changed = _normalize_catalog_renewals(catalog)
    if changed:
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    payload = _previous_build_dashboard_payload_for_renewal(force_refresh=force_refresh)
    seen = set()
    cleaned_emails = []
    for item in payload.get("emails", []):
        details = _item_renewal_details(item)
        if details:
            item = _normalize_renewal_item_for_display(item)
            key = _renewal_key(details["student_email"], details["course"])
            if key in seen:
                continue
            seen.add(key)
        cleaned_emails.append(item)
    payload["emails"] = cleaned_emails
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "work_emails": len([email for email in cleaned_emails if email.get("category") == "work"]),
        "personal_emails": len([email for email in cleaned_emails if email.get("category") == "personal"]),
    }
    payload["briefing"] = build_daily_briefing(payload.get("connected_email", DEFAULT_CONNECTED_EMAIL), payload.get("orders", []), cleaned_emails)
    return payload


_previous_perform_gmail_scan_for_renewal = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _previous_perform_gmail_scan_for_renewal(force_full=force_full)
    try:
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        catalog = get_dashboard_catalog()
        added = _scan_and_upsert_renewal_requests(service, catalog, connected_email)
        invalidate_dashboard_cache()
        payload = build_dashboard_payload(force_refresh=True)
        summary = payload.setdefault("scan_summary", {})
        summary["renewal_added"] = added
    except GmailAuthRequired:
        raise
    except Exception as error:
        print(f"[renewal-minimal] skipped due to error: {error}", flush=True)
    return payload


# Override send only for renewal items, because they are synthetic dashboard items and should send a new email to the student.
_previous_api_send_reply_for_renewal = app.view_functions.get("api_send_reply")
def api_send_reply(thread_id: str):
    try:
        item = get_catalog_item("emails", thread_id)
        details = _item_renewal_details(item)
        if details and item.get("reply"):
            if not get_automation_settings().get("auto_reply_enabled", True):
                return jsonify({"ok": False, "error": "Auto Reply is off. Turn it on before sending replies."}), 403
            service = get_gmail_service()
            body = request.get_json(silent=True) or {}
            reply = item.get("reply", {})
            subject = (body.get("subject") or reply.get("subject") or f"Account renewal request - {details['course']}").strip()
            reply_body = (body.get("body") or reply.get("body") or _renewal_reply_body(details["student_name"], details["course"])).strip()
            sent = send_new_email(service, details["student_email"], subject, reply_body)
            upsert_catalog_item("emails", thread_id, {
                **item,
                "status": "Already Replied",
                "reply": None,
                "reply_sent_at": datetime.now().isoformat(timespec="seconds"),
            })
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})
        return _previous_api_send_reply_for_renewal(thread_id)
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_send_reply"] = api_send_reply



# ---------------------------------------------------------------------
# FINAL PATCH: wider personal inbox capture, strict work/personal split,
# favorites, static templates, daily paragraph briefing, and safer thread updates.
# ---------------------------------------------------------------------
EMAIL_SCREENING_VERSION = "2026-06-personal-favorites-templates-v14"
MAX_EMAIL_THREADS_PER_SCAN = int(os.getenv("MAX_EMAIL_THREADS_PER_SCAN", "700"))
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "260"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "120"))
FAVORITE_SCAN_LIMIT = int(os.getenv("FAVORITE_SCAN_LIMIT", "300"))
GMAIL_TEMPLATES = [
  {
    "title": "Info Qualifying MCQ QBank and MOCK course",
    "body": "Welcome to pharmacy prep. Now we are enrolling in a qualifying MCQ Bank and MOCK course prep course as soon as you enroll, we will provide you with books and a study plan so you can begin your preparations.\n\nWe are pleased to inform you that; we have been offering highly structured study material for over 25 years and trained nearly 20,000 pharmacy students for licensing exam preparations.\n\nWith this package, you will gain access to online Q&A Bank and mock tests access for 1 year. This is a self-paced program so you decide when you study.\n\nPharmacist MCQ bank and MOCK course Package Includes;\n\n●        4000+ QBank Questions: Pharmacist Qualifying Exam style questions with a clinical vignette, multiple choice answers and rationales (presented as chapters). Continuous updates to the questions and explanations.\n●        25+ Timed Exam Simulations (MOCKS): Computer-based tests Simulate real pharmacy exams to prepare students for the test environment. Accessible anytime for self-paced attempts.\n●        Custom Quiz Builder: Allow students to generate custom quizzes on topics where they need the most practice.\n●      Online access to Qualifying Exam Review and Guide\n●        QBank organized chapter-wise across the 6 core competencies of the syllabus:\n1. Providing Care\n1A Clinical Care\n1 B Drug distribution\n2. Communication and Collaboration~\n3. Professionalism\n4. Knowledge and Expertise\n5. Leadership and Stewardship\n\nCourse fee;$690+tax\nPlz find link below with details\nhttps://www.pharmacyprep.com/store/category/pebc-qualifying-exam-mcq-courses-and-books/qualifying-exam-mcq-crash-course/\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Evaluating Exam QBank and MOCK course",
    "body": "Welcome to pharmacy prep; Now we are enrolling for pharmacist evaluating exam crash course with QBank and MOCK exams and the prep course is updated new blueprint.\n\nWe are pleased to inform that; we have been offering highly structured study material over 25 years and trained nearly 20,000 pharmacy students for licensing exam preparations.\n\nThis course package includes; We provide online access to our platform that enables access to:\n4000+ Q Bank questions and answers include 4 formats of Q&A\nChapter-wise Practice Q&A (covering entire syllabus must read topics)\nTest mode: COMPUTER BASED TESTs (like a real test)\nMOCK EXAMS Reading mode questions and detail answers\nTest yourself and score cards\nVideo lectures for each chapter\nDigital Evaluating review books and Q&A Books\nDigital clinical pharmacology books\nCourse is valid for 1 year.\nHow to ENROLL\nCan enroll online at;\nhttps://pharmacyprep.com/store/PEBC-Evaluating-Exam-Courses-amp-Books/Evaluating-Exam-In-class-Courses/Evaluating-Exam-In-Class-Crash-Course-c595/\nOR\nCAN ENROLL BY SENDING COURSE Fee by E-transfer\nTo pay for the online access please send an e-transfer. Please email your etransfer to \"success@pharmacyprep.com\" and please email us the password created for etransfer\n\nWe hope the information is sufficient to answer all your questions, if you still have any questions, please do not hesitate to e-mail of CALL/TEXT/SMS us at 647-221-0457\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Info OSPE  Prep Course",
    "body": "Dear Angelina\nWelcome to Pharmacy Prep!\nWe are now enrolling for the Pharmacy Technician OSPE Preparation Course.\nThe course is available in two formats:\nOnline live interactive classes\nIn-person classes at Pharmacy Prep locations\nCourse Features\nLive lectures once per week with interactive OSPE role-play sessions\nAccess to recorded lectures for review anytime\nTechnician OSPE books covering 100% of the syllabus\nAccess to our ePrepStation online platform with OSPE video library\nSimulated OSPE MOCK exams designed like the real exam\nOnline and on-campus learning options available\nONLINE LIVE CLASS SCHEDULE\nStart Date: June 21, 2026\nDay: Sunday\nTime: 4:00 PM – 8:00 PM (Toronto Time)\nTo enroll, please use the link below:\nhttps://www.pharmacyprep.com/store/books/ospe-home-study-plus-online/\nThank you once again, and we look forward to hearing from you.\nRegards,\nPharmacy Prep"
  },
  {
    "title": "Welcome. Enrolled Prep Course",
    "body": "Dear\nWelcome. Enrolled in the Prep course. We will email you the course login details soon.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome.Enrolled Pharm MCQ-Prep Course",
    "body": "Dear  Janvier\nWelcome. We have enrolled you in the Pharmacist Qualifying MCQ online home study course.. Please find below the login for an online exam prep station that enables access to  STUDY PLAN, live lecture, recorded lectures, QBANK, MOCK EXAMS AND DIGITAL BOOKS chapter-wise lecture notes.\n\nPlease find online access at www.pharmacyprep.com\n\nlogin:\n\npassword:\n\nTo log in to  Pharmacist MCQ Prep course from the  registered courses on the eprepstation home page . Select Pharmacist Qualifying MCQ Prep course\n\nQBANK links are in  6 competencies of the syllabus:\nCompetency 1a: Providing Patient Care\nCompetency 1b  Providing care: Drug distribution\nCompetency 2 : Knowledge and Expertise\nCompetency 3  Communication and Collaboration\nCompetency 4:   Leadership and Stewardship\nCompetency 5  Professionalism\n\nCan select the Chapterwise Q&A, Computer based tests and MOCK exams section to practice simulated mock exams.\nComputer-Based Tests (simulate actual exams).\n\nWe will be happy to set up a virtual meeting to guide you and give you a study plan. Please let us know\n\nWe are mailing Qualifying review book to your address.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Info: Pharmacist MCQ & OSCE Prep Course",
    "body": "Welcome. to pharmacy prep. Thank you for your interest in the PEBC Qualifying Exam (QE) Part I – MCQ and Part II – OSCE courses. We are currently enrolling, and upon registration, you receive full access to all materials.\nWhat You Get\nComprehensive prep system with case-based videos + thousands of exam-style questions to build clinical thinking\nPerformance tracking & readiness assessment (mastered / needs improvement + pass prediction)\nPersonalized study plans  with day-by-day guidance\nDetailed rationales + visuals + expert walkthroughs for every question\nContinuous improvement tools: Qbank analytics, missed-item review, webinars & study groups\n\nMCQ Course Highlights\nStructured study plan covering all competencies\nLive classes (Thu & Sat, 4–8 PM) + recorded lectures\nChapter-wise Qbank with explanations\nComputer-based mock exam\n1-year access\n\nOSCE Course Highlights\nWeekly live pharmacist-led role plays (Sun 4–8 PM)\nStructured cases with feedback\nFull-day OSCE mock\nRecorded sessions + 1-year access\n\nWhy Choose Us\n25+ years of experience\n20,000+ pharmacists trained\nStructured, guided, exam-focused preparation\n\nEnrollment\nRegister here:\nhttps://www.pharmacyprep.com/store/books/pharmacy-qualifying-exams-part-i-2-combo-home-study-plus-online-mcqosce/\nSupport: 647.221.0457 (Call/WhatsApp/Text)\nSeats are limited. Please confirm schedules on the official website before registering.\nBest regards,\nPharmacy Prep\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome.Enrolled Pharm MCQ-QBank and MOCK course",
    "body": "Dear\nWelcome. We have enrolled you in the Pharmacist Qualifying MCQ QBANK and MOCK course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, recorded lectures, QBANK, MOCK EXAMS AND DIGITAL BOOKS chapter-wise lecture notes.\n\nPlease find online access at www.pharmacyprep.com\n\nlogin\n\npassword:\n\nTo log in to  Pharmacist MCQ BANK and MOCK course from the  registered courses on the eprepstation home page\n\nQBANK links are in  6 competencies of the syllabus:\nCompetency 1a: Patient Care & Therapeutic Decision-Making\nCompetency 1b  Pharmaceutical Calculations\nCompetency 2  Drug Information & Evidence-Based Practice\nCompetency 3  Medication Safety & Quality Assurance\nCompetency 4:  Communication & Patient Education\nCompetency 5  Professional Practice, Ethics & Legal\nCompetency 6: Health Promotion & Public Health\n\nCan select the MOCK exams section to practice simulated mock exams.\nComputer-Based Tests (simulate actual exams).\n\nWe will be happy to set up a virtual meeting to guide you and give you a study plan. Please let us know\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Info Qualifying MCQ QBank and MOCK course",
    "body": "I really apologize for missing your email .\nPlz find details yes Qualifying QBank courses include chapter wise quizzes and competency wise and then final all competency wise quizzes includes in course\n\nWelcome to pharmacy prep. Now we are enrolling in a qualifying MCQ Bank and MOCK course prep course as soon as you enroll, we will provide you with books and a study plan so you can begin your preparations.\n\nWe are pleased to inform you that; we have been offering highly structured study material for over 24 years and trained nearly 10,000 pharmacy students for licensing exam preparations. Pharmacy Prep Online Plus Home Study helps you to get real results. The home study package contains the most recent updates of high-yield material and covers every topic in depth that gets you real success. With this package, you will gain access to online Q&A and mock tests access for 1 year. This is a self-paced program so you decide when you study.\n\nPharmacist MCQ course Package Includes;\n\n●        4000+ QBank Questions: Pharmacist Qualifying Exam style questions with a clinical vignette, multiple choice answers and rationales (presented as chapters). Continuous updates to the questions and explanations.\n●        25+ Timed Exam Simulations (MOCKS): Computer-based tests Simulate real pharmacy exams to prepare students for the test environment. Accessible anytime for self-paced attempts.\n●        Custom Quiz Builder: Allow students to generate custom quizzes on topics where they need the most practice.\n●        Weekly 2-day live lectures are scheduled on Saturday and Thursday 4 pm to 8bpm and they are recorded and uploaded to the study plan accessible any time after live lectures.\n●        A Qualifying Exam Review and Guide 2025\n●        QBank organized chapter-wise across the 9 core competencies of the syllabus:\n1. Providing Care\n1A Clinical Care\n1 B Drug distribution\n2. Communication and Collaboration~\n3. Professionalism\n4. Knowledge and Expertise\n5. Leadership and Stewardship\n\nCourse fee;$690+tax\nPlz find link below with details\nhttps://www.pharmacyprep.com/store/category/pebc-qualifying-exam-mcq-courses-and-books/qualifying-exam-mcq-crash-course/\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Info: FPGEE Prep Courses",
    "body": "Now enrolling for FPGEE prep courses\n\nWe offer two types of fpgee prep courses\n1. FPGEE self study course\nWhich include. Online access to complete syllabus FPGEE complete review book, chapter-wise Q&A, (QBank) and Mock exams (simulate real exams). Recorded lecture for each topic, flashcards on high yield points.\nCourse fee US$290\nPlz find link below to enroll\nhttps://buy.stripe.com/eVa29JfT167ha8UfZR\n\n2.  FPGEE home study course\nWhich include above all content and additional weekly online live interactive lectures\nCourse fee US$790+tax\n\nhttps://buy.stripe.com/14k01B7mvgLVgxi3d9\n\nShould you need more information please feel free to contact us\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome . Received order for Clinical Pharmacology and Pharmacy Practice Review Book",
    "body": "Dear\nWelcome . Received order for Clinical Pharmacology and Pharmacy Practice Review Book.\nPlz text or whatsapp on 647.221.0457 to schedule book pick up from pharmacy prep locations.\n\nThe digital access of book can access with below login details\nhttps://www.pharmacyprep.com/\n\nlogin:\n\nPassword:\n\nShould you need more information. Plz feel free to email us\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enroled MCQ & OSCE Prep course",
    "body": "Dear\nWelcome. We have enrolled you in the Pharmacist Qualifying MCQ and OSCE online home study course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES< Recorded lectures, QBANK, MOCK EXAMS AND DIGITAL BOOKS chapter-wise lecture notes.\n\nPlease find online access at www.pharmacyprep.com\n\nlogin:\n\npassword:\n\nTo log in to  Pharmacist qualifying MCQ course or Pharmacist OSCE prep course on registered/my courses from the eprepstation home page and select the qualifying MCQ Course\n\nQBANK links are in  9 competencies of the syllabus:\nCOMPETENCY 1: Assume Ethical, Legal and Professional Responsibilities\nCOMPETENCY 2: Patient Care\nCOMPETENCY 3: Product Distribution\nCOMPETENCY 4: Practice Setting\nCOMPETENCY 5: Health Promotion\nCOMPETENCY 6: Access, Retrieve, Evaluate and Disseminate Relevant Information\nCOMPETENCY 7: Communication Skills in Pharmacy Practice\nCOMPETENCY 8: Collaboration with healthcare professionals and teamwork.\nCOMPETENCY 9: Quality assurance\n\nCan select the MOCK exams section to practice simulated mock exams.\nComputer-Based Tests (simulate actual exams).\n\nOnline Class scheduled for MCQ Course Saturday and Thursday 4pm to 8pm\nOnline Class schedule for OSCE course Sunday 4pm to 8pm\n\nWe are mailing books to your mailing address.\nWe will be happy to set up a virtual meeting to guide you and give you a study plan. Please let us know,\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Info MCQ and OSCE combined course",
    "body": "Welcome to pharmacy prep.\nNow we are enrolling in upcoming classes as soon as you enroll, we provide you with books and a study plan so you can begin your preparations.\n\nPlease find complete details of QE Part I (MCQ) and II (OSCE) schedule for the online home study course with online live lectures:\nHome Study Plus Online\nWe are pleased to inform that; we have been offering highly structured study material for over 25 years and trained nearly 20,000 pharmacy students for licensing exam preparations. Pharmacy Prep Online Plus Home Study helps you to get real results. Along with home study books, you will also get online access to class lecture highlighting key points in every chapter (text based). Question Bank, and MOCK Exams through our website. The home study package contains most recent updates of high yield material and covers every topic in depth that gets you real success. With this package, you will gain access to online Q&A and mock tests access for 1 year. This is a self-paced program so you decide when you study.\n\nHome study course package includes\nLIVE ONLINE TUTORIAL CLASSES 3 days/wks. till the exam\n         Weekly LIVE online OSCE role plays with the pharmacist\n         Chapter-wise Question BANK\n         MOCK exam simulated in Computer based test like actual exams.\n         Continuous updates on Q&A with explanation.\n         Updated to new practice guidelines\n         Weekly recorded lectures\n\nMCQ Classes Schedule:\nWeekly 2 days in-class lecture live stream tutorial\nSaturday, 4:00 am-8:00 pm;  Thursday 4:00 pm - 8:00 pm\n\nOSCE Classes Schedule: Sunday 4:00 pm-8:00 pm\n\nCan enroll at our website\nhttps://www.pharmacyprep.com/store/books/pharmacy-qualifying-exams-part-i-2-combo-home-study-plus-online-mcqosce/\n\nAlternatively, can enroll by etransfer method to our email address “success@pharmacyprep.com”\n\nThe complete course fee for:\n\nCombine for QE Part I (MCQ) + Part II OSCE = $2080+ tax + shipping\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737.\n\nThank you once again and look forward to hearing from you.\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nWelcome. Enrolled FPGEE prep course\nDear\nWelcome.  Enrolled you in the FPGEE self-paced prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, RECORDED LECTURES, Q&A chapter-wise, MOCK EXAMS\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nCan log in to eprepstation. On the online portal, the eprepstation home page can select FPGEE rep courses to begin preparing.\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\n\nTo log in to QBank and model (mock) exams. Click on registered courses from the eprepstation home page and select the computer-based tests and  Mock exams.\n\nQBank can access each chapter-wise which is linked in below six sections of the syllabus and can be selected from the cover page of the sections.\n  Biomedical Science\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n  Clinical pharmacology\n  Calculations\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep"
  },
  {
    "title": "Welcome Enrolled Tech MCQ Prep course",
    "body": "Welcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:   erikatakahashi04@gmail.com\nPassword: etechmcqprep2026\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\nContent is grouped in competencies\n         Ethical, Legal and Professional Responsibilities\n         Patient Care\n         Product Distribution\n         Practice Setting\n         Health Promotion\n         Knowledge and Research Application\n         Communication and Education\n         Intra and Inter-Professional Collaboration\n         Quality and Safety\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures. and select the class in link in calendar.\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes start date Jan 31 2026  Live Lecture Saturdays 4pm to 8pm\n\nPlz text on 647.221.0457 to schedule book pick up from pharmacy prep Brampton location. https://maps.app.goo.gl/ActEPYQcigzGfA83A\n\nWe can set up online meeting to walk you through prep course and give you a study plan. Please let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome . Enrolled Tech MCQ and OSPE prep course",
    "body": "Welcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ and OSPE prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\nContent is grouped in competencies\n         Ethical, Legal and Professional Responsibilities\n         Patient Care\n         Product Distribution\n         Practice Setting\n         Health Promotion\n         Knowledge and Research Application\n         Communication and Education\n         Intra and Inter-Professional Collaboration\n         Quality and Safety\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes on Live Lecture Saturdays 4pm to 8pm\n\nand\nOSPE classes on live lecture Sunday 4pm to 8pm\n\nWe can set up online meeting to walk you through prep course and give you a study plan.\n\nWe are mailing books to your address.\n\nPlease let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled  In.person Evaluating exam prep course",
    "body": "Welcome. Enrolled you in the pharmacy prep evaluating exam online prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE/ & RECORDED LECTURES, Question Bank, MOCK EXAMS AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nYou can log in to eprepstation. On the online portal, the eprepstation home page. From the main bar, select the registered courses. Click the Evaluating Exam Prep course.\nThe evaluation exam home page. Click on each section and chapter, and each chapter has lecture notes, Q&A, tips and recorded videos. Additionally, Mock exams.The computer-based tests on the left main bar\n\nThe evaluation exam course has three sections of the syllabus below, and can be selected from the cover page of the sections.\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nDigital Evaluating review books and clinical pharmacology books.\n\nClasses are on Saturday and Sunday, 10 am to 2 pm (Toronto time)\n\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\n\nShould you need further assistance, please do not hesitate to contact us.\n\nRegards,\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled OSCE self learning prep coruse",
    "body": "Welcome. We enrolled you in the pharmacist OSCE self-study prep course. Please find below the login for an online exam prep station that enables access to  OSCE VIDEO BANK AND OSCE CASES.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo log in to eprepstation, click on registered courses from eprepstation home page and select the Tips Pharmacist  OSCE prep course.\n\nShould you need help with a study plan . Please email us we can set up an online meeting schedule to discuss your OSCE study plan.\n\nWe are mailing OSCE Review Book to your address.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled OPRA self paced prep course",
    "body": "Welcome. Enrolled you in the OPRA Exam prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, RECORDED LECTURES, Question Bank, MOCK EXAMS AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\n\nYou can log in to eprepstation. On the online portal, the eprepstation home page. From the main bar, select course registered courses. Click the Australian Pharmacy Exam Prep course.\nOn the Australian Pharmacy Exam Prep course. Click on each section and chapter and each chapter has lecture notes, Q&A, tips and recorded videos. Additionally  Mock exams.the computer-based tests on the left main bar\nThe OPRA Exam course has five sections of the syllabus below and can be selected from the cover page of sections.\nBiomedical Sciences\nPharmacokinetics and Pharmacodynamics\nMedicinal Chemistry and Pharmaceutics\nPharmacology: Drug classes based on mechanism of action (Astrx)\nTherapeutic and Patient Care\n\nEach section's content is covered as per the syllabus of Biomedical Sciences (20%), Medicinal Chemistry & Biopharmaceutics (20%), Pharmacokinetics & Pharmacodynamics (20%), Pharmacology & Toxicology (20%), and Therapeutics & Patient Care (20%)\n\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Pharmacist MCQ Bank and MOCK Course",
    "body": "Welcome to Pharmacy Prep! We are currently accepting enrollments for our Pharmacist MCQ Bank and MOCK exams.\nFor over 25 years, we have provided highly structured study materials that have helped thousands of students excel in qualifying exams.\n\nThe Pharmacist MCQ QBank and MOCK EXAMS package includes the following:\n●        4000+ QBank Questions: Pharmacist Qualifying Exam style questions with clinical vignette, multiple choice answers and rationales (presented as chapterwise).\n●        25+ Timed Exam Simulations (MOCKS): Computer-Based Tests Simulate real pharmacy exams to prepare students for the test environment.\n●        Custom Quiz Builder: Allow students to generate custom quizzes on topics where they need the most practice.\n●        Continuous updates to the questions and explanations.\n●        Weekly live lectures that are recorded and uploaded to the study plan\n●        A digital Qualifying Exam Review and Guide 2025\nQBank is organized chapter-wise across the 9 core competencies of the syllabus:\nCOMPETENCY 1: Assume Ethical, Legal and Professional Responsibilities\nCOMPETENCY 2: Patient Care\nCOMPETENCY 3: Product Distribution\nCOMPETENCY 4: Practice Setting\nCOMPETENCY 5: Health Promotion\nCOMPETENCY 6: Access, Retrieve, Evaluate and Disseminate Relevant Information\nCOMPETENCY 7: Communication Skills in Pharmacy Practice\nCOMPETENCY 8: Collaboration with healthcare professionals and teamwork.\nCOMPETENCY 9: Quality assurance\n\nThe prep course is valid for 1 year.\n\nThe course fee for this package is $690+ Tax. Enrollment is now open, and you can register using the link below: https://www.pharmacyprep.com/store/category/pebc-qualifying-exam-mcq-courses-and-books/qualifying-exam-mcq-crash-course/\n\nIf you have any concerns, please do not hesitate to email or call us at 416-223-7737. Thank you for considering Pharmacy Prep, and we look forward to hearing from you.\nRegards,\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nPharmacy prep OSCE prep course\nDear Haitam\nNow we are enrolling in upcoming classes as soon as you enroll, we provide you online access with books and a study plan so you can begin your preparations.\n\nHome Study Plus Online\n\nWe are pleased to inform you that we have been offering highly structured study material for over 25 years and have trained nearly 10,000 pharmacy students for licensing exam preparations. Pharmacy Prep Online Plus Home Study helps you to get real results.\n\nThe OSCE Online plus MOCK EXAM features the following:\n\n  Weekly Live OSCE Roleplay with a licensed pharmacist for interactive practice.\n  Recorded Sessions are available for flexible access if you miss the live class. http://pharmacyprep.com/osce-review-classes.html\n  Comprehensive OSCE Guidebook: \"OSCE: A Step-by-Step Review Guide\".\n  Extensive Video Library featuring licensed Canadian pharmacists roleplaying various cases.\n  One Full-Day Mock Exam included, with 1-year online access to all course materials.\n\nLIVE LECTURE/ROLE PLAY:  August 30, 2025, Sunday, 4 pm to 8 pm\nOnce you sign up and pay the fees, the package will be mailed to you by express post, and a tracking number will be emailed to you for tracing it. The professors are available to help and guide you during the course of your preparation. You can anytime correspond BY Email or phone. The course fee of the package is $990 +tax+ shipping.\n\nPlease can you enroll by the link below\nhttps://www.pharmacyprep.com/store/books/pebc-osce-home-study-course-plus-osce-video-library/\n\nThis course can be upgraded to one-on-one training online with a Pharm. D. licensed pharmacist. This will be an additional cost based on the number of hours of training.\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737.\n\nThank you once again, and look forward to hearing from you.\n\nRegards,\n\nPharmacy Prep"
  },
  {
    "title": "Welcome. Enrolled OSCE Prep Course",
    "body": "Welcome. We enrolled you in the Pharmacy Prep Qualifying  OSCE prep online course. Please find below the login for an online exam prep station that enables access to LIVE LECTURE, STUDY PLAN,  OSCE VIDEO BANK, AND OSCE CASES.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo login to eprepstation click on registered courses from the eprepstation home page and select the Pharmacist qualifying  OSCE prep course.\n\nTo join an interactive online lecture. Please connect to the \"LIVE LECTURE\" link during class hours. This link can be found on the eprepstation home page.\n\nPlease join the live interactive OSCE weekly role-play scheduled start date on August 31 2025 SUNDAY 4 pm to 8 pm (Eastern Time).\n\nshould you need help in study plan . Please email us we can set up an online meeting schedule to discuss your OSCE study plan.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. OSPE MOCKS Course",
    "body": "Dear\nWe have enrolled you in the OSPE crash course  on\nSeptember 01 Monday, Time 10:00 AM to 3:00 PM\nSeptember 02, 2025 Tuesday, Time 10:00 AM to 3:00 PM\n\nPharmacy prep\nLocation:\nHoliday Inn Toronto Airport East\nAddress: 600 Dixon Road\nToronto, ON M9W 1J1\n\nPharmacy Prep 416-223-7737/647-221-0457\n\nPlz can login at online ospe cases at www.pharmacyprep.com\nlogin:\nPassword:\n\nMock Simulation exams for the students preparing for Pharmacy Technician OSPE. The whole approach of providing you with Quality stations and knowledgeable assessors goes a big way in supporting you and improving your confidence level in clearing the same.\n\nEach day Fourteen OSPE stations includes Interactive and Non-interactive Station.\n\nEach Stations: Comprised of;\n30 seconds: for students to read the information displayed outside the suites and get prepared\n\n6 minutes: For the students to perform (Buzzer will sound at the end of 6 minutes). There are no professional actors for these tests. So, the assessor plays the assessor and Standardized patient’s role\n2 minutes: feedback by the assessor. Please ensure the feedback forms are handed over to the students with actual grading of:\nCase outcome: solved, marginally solved, marginally unsolved, Unsolved\nCommunication: excellent, good, Marginally unacceptable, unacceptable.\n\nShould you need more information. Please feel free to contact us.\nregards\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled FPGEE Self-paced prep course",
    "body": "Welcome.  Enrolled you in the FPGEE self-paced prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, RECORDED LECTURES, Q&A chapter-wise, MOCK EXAMS\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nCan log in to eprepstation. On the online portal, the eprepstation home page can select FPGEE rep courses to begin preparing.\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\n\nTo log in to QBank and model (mock) exams. Click on registered courses from the eprepstation home page and select the computer-based tests and  Mock exams.\n\nQBank can access each chapter-wise which is linked in below six sections of the syllabus and can be selected from the cover page of the sections.\n  Biomedical Science\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n  Clinical pharmacology\n  Calculations\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome . Enrolled tech OSPE prep course",
    "body": "Dear  Gifty\nWelcome. We have enrolled you in the Pharmacy prep tech qualifying OSPE prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES,  Chapter-wise OSPE cases, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name: maflex99@yahoo.com\nPassword: gbospeprep2025\n\nTo begin preparing, select registered courses and select Pharmcy tech OSPE prep course.\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nOSPE classes on Live Lecture Sundays 4pm to 8pm\n\nWe can set up online meeting to walk you through prep course and give you a study plan.\n\nPlz text on 647.221.0457 to schedule book pick up from the pharmacy prep location in Toronto.\n\nPlease let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nPharmacy Prep"
  },
  {
    "title": "Pharmacist QUALIFYING MCQ",
    "body": "Welcome to pharmacy prep. Now we are enrolling in a qualifying MCQ prep course as soon as you enroll, we will provide you with books and a study plan so you can begin your preparations.\n\nWe are pleased to inform you that; we have been offering highly structured study material for over 24 years and trained nearly 10,000 pharmacy students for licensing exam preparations. Pharmacy Prep Online Plus Home Study helps you to get real results. The home study package contains the most recent updates of high-yield material and covers every topic in depth that gets you real success. With this package, you will gain access to online Q&A and mock tests access for 1 year. This is a self-paced program so you decide when you study.\n\nPharmacist MCQ course Package Includes;\n\n●        4000+ QBank Questions: Pharmacist Qualifying Exam style questions with a clinical vignette, multiple choice answers and rationales (presented as chapters). Continuous updates to the questions and explanations.\n●        25+ Timed Exam Simulations (MOCKS): Computer-based tests Simulate real pharmacy exams to prepare students for the test environment. Accessible anytime for self-paced attempts.\n●        Custom Quiz Builder: Allow students to generate custom quizzes on topics where they need the most practice.\n●        Weekly 2-day live lectures are scheduled on Saturday and Thursday 4 pm to 8bpm and they are recorded and uploaded to the study plan accessible any time after live lectures.\n●        A Qualifying Exam Review and Guide 2025\n●        QBank organized chapter-wise across the 9 core competencies of the syllabus:\n●        COMPETENCY 1: Assume Ethical, Legal and Professional Responsibilities\n●        COMPETENCY 2: Patient Care\n●        COMPETENCY 3: Product Distribution\n●        COMPETENCY 4: Practice Setting\n●        COMPETENCY 5: Health Promotion\n●        COMPETENCY 6: Access, Retrieve, Evaluate and Disseminate Relevant Information\n●        COMPETENCY 7: Communication Skills in Pharmacy Practice\n●        COMPETENCY 8: Collaboration with healthcare professionals and teamwork.\n●        COMPETENCY 9: Quality assurance\n●\nCourse fee;\n$990 includes& digital access to the digital online Qualifying Exam Review book (covers all competencies and Q&A bank\nor\n$1190 includes Qualifying Exam Review book (covers all competencies Q&A books mailed to you – also included digital access to all books and QBank.\n\nOnce you sign up and pay the fees, the package will be mailed to you by express post and a tracking number will be emailed to you for the track it. The professors are available to help and guide you during the course of your preparation.\nhttps://pharmacyprep.com/store/PEBC-Qualifying-MCQ-Courses-amp-Books/PEBC-Qualifying-Exam-MCQ-Home-Study/PEBC-Qualifying-Exam-Home-Study-Plus-Online-Course-with-physical-Q-A-books-p1559.html\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737.\nThank you once again and look forward to hearing from you.\n\nRegards,"
  },
  {
    "title": "Welcome Tech MCQ Prep course",
    "body": "Dear Diljot\nWelcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes on Live Lecture Saturdays 4pm to 8pm\n\nWe can set up online meeting to walk you through prep course and give you a study plan. Please let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome.Enrolled Pharm MCQ Bank and MOCK course",
    "body": "Welcome. We have enrolled you in the Pharmacist Qualifying MCQ QBANK and MOCK course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, recorded lectures, QBANK, MOCK EXAMS AND DIGITAL BOOKS chapter-wise lecture notes.\n\nPlease find online access at www.pharmacyprep.com\n\nlogin:\n\npassword:\n\nWe will be happy to set up a virtual meeting to guide you and give you a study plan. Please let us know,\n\nTo log in to  Pharmacist MCQ BANK and MOCK course from the  registered courses on the eprepstation home page\n\nQBANK links are in  9 competencies of the syllabus:\nCOMPETENCY 1: Assume Ethical, Legal and Professional Responsibilities\nCOMPETENCY 2: Patient Care\nCOMPETENCY 3: Product Distribution\nCOMPETENCY 4: Practice Setting\nCOMPETENCY 5: Health Promotion\nCOMPETENCY 6: Access, Retrieve, Evaluate and Disseminate Relevant Information\nCOMPETENCY 7: Communication Skills in Pharmacy Practice\nCOMPETENCY 8: Collaboration with healthcare professionals and teamwork.\nCOMPETENCY 9: Quality assurance\n\nCan select the MOCK exams section to practice simulated mock exams.\nComputer-Based Tests (simulate actual exams).\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome Tech MCQ and OSPE",
    "body": "--\nWelcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ and OSPE prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes on Live Lecture Saturdays 4pm to 8pm\n\nWe can set up online meeting to walk you through prep course and give you a study plan. Please let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nPharmacy Prep"
  },
  {
    "title": "Info TECH MCQ Course: Pharmacy technician mcq",
    "body": "Dear Dona\nWelcome to pharmacy prep. Now we are enrolling for Technician MCQ prep courses. Now enrolling for upcoming PEBC tech MCQ and OSPE prep courses.\nThe review for the course contains,\n         Live lectures once a week\n         Tech MCQ  covering 100% syllabus. Updated to new syllabus 2025\n         Online access to our eprepstation platform\n         Must pass QBANK; Practice Q&A for each chapter\n          COMPUTER BASED TESTS like a real test practice and top 20 MOCK Exams\n         OSPE simulated MOCK exams practice like real exam\n         Weekly recorded lectures\nOnline/on-campus\nONLINE LIVE Classes SCHEDULE:\nMCQ classes:     Saturday 4:00 pm to 8:00 pm\n\nPharmacy Prep has been offering for the past 25years the most comprehensive prep course in Canada. As we are 100% committed to your success. To keep you on track, and on top of lectures, we give you more practice tests and practice stations than any other prep company. Our format will keep you energized, and focused and familiarize you with how quickly to decipher and successfully pass your exam.\nMaterial Provided: books and lecture notes will be provided during the course.\nOnce you sign up and pay the fees, the package will be mailed to you by express post and a tracking number will be emailed to you for tracing it. The professors are available to help and guide you during the course of your preparation. You can anytime correspond BY Email or phone. The MCQ  course fee is $790 +tax+ shipping. Also, the 1-year access will be active 1-2 days after your purchase for one year.\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or\nPlz find details below\nhttps://www.pharmacyprep.com/store/books/pebc-pharmacy-technician-qualifying-exam-home-study-package/\n\nThank you once again and look forward to hearing from you.\nRegards,\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled EE QBANK and MOCK Course",
    "body": "Welcome. Enrolled you in the pharmacy prep evaluating exam QBank, and MOCK course. Please find below the login for an online exam prep station that enables access to STUDY PLAN,  RECORDED LECTURES, Question Bank, MOCK EXAMS AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nYou can log in to eprepstation. On the online portal, the eprepstation home page. From the main bar, select the registered courses. Click the Evaluating Exam QBank and MOCK course.\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\nThe evaluation exam home page. Click on each section and chapter, and each chapter has lecture notes, Q&A, tips and recorded videos. Additionally, Mock exams.The computer-based tests on the left main bar\n\nThe evaluation exam course has three sections of the syllabus below, and can be selected from the cover page of the sections.\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nDigital Evaluating review books and clinical pharmacology books.\n\nShould you need further assistance, please do not hesitate to contact us.\n\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nPharmacy Prep\n\nPharmacy Prep\n\nPharmacy Prep"
  },
  {
    "title": "Tech MCQ and OSPE Prep course",
    "body": "Welcome to pharmacy prep. Now we are enrolling for Technician MCQ and OSPE prep courses. Now enrolling for upcoming PEBC tech MCQ and OSPE prep courses.\nThe review for the course contains,\n         Live lectures 2 days/wks\n         Tech MCQ and OSPE Books covering 100% syllabus. Updated to new syllabus 2025\n         Online access to our eprepstation platform\n         Must pass QBANK; Practice Q&A for each chapter\n          COMPUTER BASED TESTs like a real test practice and top 20 MOCK Exams\n         OSPE simulated MOCK exams practice like real exam\n         Weekly recorded lectures\nOnline/on-campus\nONLINE LIVE Classes SCHEDULE:\nMCQ classes:     Saturday 4:00 pm to 8:00 pm\n\nOSPE classes:   Sunday 4:00 pm to 8:00 pm\n\nPharmacy Prep has been offering for the past 24 years the most comprehensive prep course in Canada. As we are 100% committed to your success. To keep you on track, and on top of lectures, we give you more practice tests and practice stations than any other prep company. Our format will keep you energized, and focused and familiarize you with how quickly to decipher and successfully pass your exam.\nMaterial Provided: books and lecture notes will be provided during the course.\nOnce you sign up and pay the fees, the package will be mailed to you by express post and a tracking number will be emailed to you for tracing it. The professors are available to help and guide you during the course of your preparation. You can anytime correspond BY Email or phone. The combined MCQ and OSPE course fee is $1190 +tax+ shipping. Also, the 1-year access will be active 1-2 days after your purchase for one year.\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737 or visit https://www.pharmacyprep.com/store/books/pharmacy-technician-qualifying-exam-mcq-ospe-combined-online-course/\n\nThank you once again and look forward to hearing from you.\nRegards,\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nWelcome. Enrolled Prep Course\nWelcome. Enrolled in evaluating exam home study course. We will email you the prep course login details soon.\nregards\n--\nPharmacy Prep"
  },
  {
    "title": "EE QBank and MOCK course info",
    "body": "Welcome to pharmacy prep. Now we are enrolling for pharmacist MCQ Bank and MOCK exams.\n\nWe are pleased to inform you that; we have been offering highly structured study material for over 24 years.\n\nPharmacy prep offers Pharmacist MCQ QBANK with thousands of Q&A as chapter-wise from all 9 NAPRA competencies.  Our pharmacist MCQ question bank and mock exams are only the best way to get real success.\nPharmacist MCQ QBank and MOCK EXAMS package includes THE FOLLOWING:\nOnline access to Q Bank; Pharmacist MCQ question bank with answers and explanations.\nQBank Format: Q&A is competency and chapter wise Q&A 5000+\nComputer based tests-CBT Format(Timed)  designed to simulate an actual exam practice in COMPUTER-BASED TESTs.\nMOCK EXAMS Format are designed to learn fast pace Q&A\nWeekly live lectures are recorded and uploaded in the study plan\nDigital Qualifying Exam Review and Guide.\nThe prep course is valid for 1 year.\nCourse Fee: $690+ Tax\n\nENROLL NOW by the link below:\nhttps://www.pharmacyprep.com/store/category/pebc-qualifying-exam-mcq-courses-and-books/qualifying-exam-mcq-crash-course/\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737.\n\nThank you once again and look forward to hearing from you.\n\nRegards,\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled Kroll Pharmacy Software self-learning video course",
    "body": "Welcome. Enrolled in the Pharmacy software training Kroll prep course. The self-paced learning course features expert-led recorded videos and instructions to help you master the software's functionalities. This comprehensive training enables users of all skill levels to navigate the interface smoothly, optimize workflows, and tackle challenges effectively. Ideal for beginners and experienced users alike, the course keeps you updated with the latest updates and enhances your proficiency.\n\nPlease log in to our online platform eprepstation with the details below. On the online portal, on the eprepstation home page, click registered courses and select the pharmacy software Kroll course.\n\nTo login Please visit https://www.pharmacyprep.com/\n\nLogin:\n\nPassword:\n\nThe video modules cover:\nHow to Create a New Patient Profile (data)\nHow to Search for a Patient (data)\nHow to search for a prescriber (data)\nHow to Create a new prescriber record (data)\nHow to search for a Doctor or Dentist’s Registration number (data)\nHow to search for your family doctor’s registration number on CPSO’s website (data)\nHow to enter drug discount cards (Kroll)\nExplanation of plans/insurance & How to Enter Patient’s insurance information(data)\nAdjudication issues and Intervention codes (data)\nDrug search & Generic equivalents (data)\nHow to enter drug mixtures into the patient profiles (Data)\nHow to Enter a new Rx (data)\nHow to Scan Rx’s and hard copies (data)\nRefill, Inactivate, Reactivate, Cancel, Modify or Reprint a Rx (data)\nHow to Fax for Rx refill or LU codes (data)\nReceiving & Entering a Rx Transfer from another Pharmacy (data)\nTransferring Rx from your Pharmacy to another Pharmacy (data)\nSet up patients for blister packs (data)\n\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Enrolled Pharmacist MCQ Prep Course",
    "body": "Welcome. We have enrolled you in the Pharmacist Qualifying MCQ online home study course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES< Recorded lectures, QBANK, MOCK EXAMS AND DIGITAL BOOKS chapter-wise lecture notes.\n\nPlease find online access at www.pharmacyprep.com\n\nlogin:\n\npassword:\n\nWe will be happy to set up a virtual meeting to guide you and give you a study plan. Please let us know,\n\nTo log in to  Pharmacist qualifying MCQ course k on registered courses from the eprepstation home page and select the qualifying MCQ Course\n\nQBANK links are in  9 competencies of the syllabus:\nCOMPETENCY 1: Assume Ethical, Legal and Professional Responsibilities\nCOMPETENCY 2: Patient Care\nCOMPETENCY 3: Product Distribution\nCOMPETENCY 4: Practice Setting\nCOMPETENCY 5: Health Promotion\nCOMPETENCY 6: Access, Retrieve, Evaluate and Disseminate Relevant Information\nCOMPETENCY 7: Communication Skills in Pharmacy Practice\nCOMPETENCY 8: Collaboration with healthcare professionals and teamwork.\nCOMPETENCY 9: Quality assurance\n\nCan select the MOCK exams section to practice simulated mock exams.\nComputer-Based Tests (simulate actual exams).\n\nShould you need further assistance, please do not hesitate to contact us.\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome (1). Enrolled Pharmacy Prep Course",
    "body": "Welcome. We have enrolled you in the Pharmacy prep course. We will email you course login details soon.\nregards\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com\n\nPharmacy Prep"
  },
  {
    "title": "Evaluating Exam QBank and MOCK course",
    "body": "Welcome to pharmacy prep. Now we are enrolling for pharmacist MCQ Bank and MOCK exams.\n\nWe are pleased to inform you that; we have been offering highly structured study material for over 24 years.\n\nPharmacy prep offers Pharmacist MCQ QBANK with thousands of Q&A as chapter-wise from all 9 NAPRA competencies.  Our pharmacist MCQ question bank and mock exams are only the best way to get real success.\nPharmacist MCQ QBank and MOCK EXAMS package includes THE FOLLOWING:\nOnline access to Q Bank; Pharmacist MCQ question bank with answers and explanations.\nQBank Format: Q&A is competency and chapter wise Q&A 5000+\nComputer based tests-CBT Format(Timed)  designed to simulate an actual exam practice in COMPUTER-BASED TESTs.\nMOCK EXAMS Format are designed to learn fast pace Q&A\nWeekly live lectures are recorded and uploaded in the study plan\nDigital Qualifying Exam Review and Guide.\nThe prep course is valid for 1 year.\nCourse Fee: $690+ Tax\n\nENROLL NOW by the link below:\nhttps://www.pharmacyprep.com/store/category/pebc-qualifying-exam-mcq-courses-and-books/qualifying-exam-mcq-crash-course/\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or call us at 416-223-7737.\n\nThank you once again and look forward to hearing from you.\n\nRegards,"
  },
  {
    "title": "Welcome. Enrolled Evaluating exam QBank and MOCK course",
    "body": "Dear Jagdip\nWelcome. Enrolled you in the pharmacy prep evaluating exam QBank and MOCKs course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, & RECORDED LECTURES, Question Bank, MOCK EXAMS AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nYou can log in to eprepstation. On the online portal, the eprepstation home page. From the main bar, select the registered courses. Click the Evaluating Exam QBank and MOCK course.\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\nThe evaluation exam home page. Click on each section and chapter, and each chapter has lecture notes, Q&A, tips and recorded videos. Additionally, Mock exams.The computer-based tests on the left main bar\n\nThe evaluation exam course has three sections of the syllabus below, and can be selected from the cover page of the sections.\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nDigital Evaluating review books and clinical pharmacology books.\n\nShould you need further assistance, please do not hesitate to contact us.\n\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled pharmacy tech MCQ prep course",
    "body": "Welcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ  prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\n\nPlease click the LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes on Live Lecture Saturdays 4pm to 8pm\n\nWe are mailing books to your address.\n\nWe can set up online meeting to walk you through prep course and give you a study plan. Please let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Received Book order",
    "body": "Welcome. Received Book order\n\nWe are mailing book to your address soon.\nregards\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Kroll Self Paced Video training course",
    "body": "Welcome to Pharmacy\nRe: Pharmacy software Kroll - Training Videos Self-paced learning course\n\nThe self-paced learning course features expert-led recorded videos and instructions, designed to help you master the software's functionalities. This comprehensive training enables users of all skill levels to navigate the interface smoothly, optimize workflows, and tackle challenges effectively. Ideal for beginners and experienced users alike, the course keeps you updated with the latest updates and enhances your proficiency.\n\nVideo modules cover:\nHow to Create a New Patient Profile (data)\nHow to Search for a Patient (data)\nHow to search for a prescriber (data)\nHow to Create a new prescriber record (data)\nHow to search for a Doctor or Dentist’s Registration number (data)\nHow to search for your family doctor’s registration number on CPSO’s website (data)\nHow to enter drug discount cards (Kroll)\nExplanation of plans/insurance & How to Enter Patient’s insurance information(data)\nAdjudication issues and Intervention codes (data)\nDrug search & Generic equivalents (data)\nHow to enter drug mixtures into the patient profiles (Data)\nHow to Enter a new Rx (data)\nHow to Scan Rx’s and hard copies (data)\nRefill, Inactivate, Reactivate, Cancel, Modify or Reprint a Rx (data)\nHow to Fax for Rx refill or LU codes (data)\nReceiving & Entering a Rx Transfer from another Pharmacy (data)\nTransferring Rx from your Pharmacy to another Pharmacy (data)\nSet up patients for blister packs (data)\n\nCourse Fee $190 +tax\nCourse can enroll by the  link below\n\nhttps://www.pharmacyprep.com/store/books/pharmacy-software-training/\n\nShould you need more details plz let us know\n\nregards\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "PEBC Evaluating Exam Prep course details",
    "body": "Welcome to pharmacy prep; Now we are enrolling in prep classes as soon as you enroll, we provide you with books and a study plan so you can begin your preparations.\n\nNow prep course and books updated with new blueprint 2025.\n\nHome Study Plus Online\nWe are pleased to inform you that; we have been offering highly structured study material for over 24 years and trained nearly 10,000 pharmacy students for licensing exam preparations. Pharmacy Prep Online Plus Home Study helps you to get real results. Now along with home study books, you will also get online access to class lectures highlighting key points in every chapter (text-based). Question Bank, and MOCK Exams through our website.\n\nThe home study course package includes\n         LIVE ONLINE CLASSES: 2 days/week till the exam  and also access to the weekly recorded lecture\n         BOOKS updated to new syllabus (blueprint): EVALUATING EXAM REVIEW BOOK 2025.  Total 4 Hard copy Books covering 100% of the new PEBC syllabus (3 books are Questions and Answers Books\n         Must pass QBank: Practice Q&A for each chapter with solution strategies\n         COMPUTER-BASED TESTs; practice like the real exam.  Along with the top 25 mock exams\n\nCLASSES SCHEDULE\n\nLive Lecture SCHEDULE: (each group is taught twice a week)\nGroup 1: (ONLINE and In-person)\nSaturday 10:00 am-2:00 pm and Sunday 10:00 am-2:00 pm\n\nGroup 2: (ONLINE in-person)\nTuesday 4:00 pm-8:00 pm AND Wednesday 4:00 pm-8:00 pm\n\nOnce you sign up and pay the fees, the package (the books) will be mailed to you by expedited post and a tracking number will be emailed to you for tracking it. The professors are available to help and guide you during the course of your preparation. You can anytime correspond by email or phone.\n\nThe course fee $1190 + tax + shipping all hard copy books will mail to your address alternatively digital only course fee $990 +tax.\n\nEnroll here \nhttps://www.pharmacyprep.com/store/category/pebc-evaluating-exam-courses-books/evaluating-exam-home-study/\n\nWe hope the information is sufficient to answer all your questions, if you still have any concerns, please do not hesitate to email or Whatsup at 647.221.0457 or 416-223-7737.\n\nThank you once again and look forward to hearing from you.\nRegards,\n--\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled Evaluating exam home study course",
    "body": "Welcome. Enrolled you in the pharmacy prep evaluating exam online prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE/ & RECORDED LECTURES, Question Bank, MOCK EXAMS AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\nLogin:\npassword:\nYou can log in to eprepstation. On the online portal, the eprepstation home page. From the main bar, select the registered courses. Click the Evaluating Exam Prep course.\nIf you need help making your study plan, we can set an online meeting to walk you through it so you can begin preparing.\nThe evaluation exam home page. Click on each section and chapter, and each chapter has lecture notes, Q&A, tips and recorded videos. Additionally, Mock exams.The computer-based tests on the left main bar\n\nThe evaluation exam course has three sections of the syllabus below, and can be selected from the cover page of the sections.\n  Pharmaceutical Science\n  Social Behavioural, Administrative Sciences\n  Pharmacy practice\n\nCan select the MOCK exams section to practice simulated Computer-based test mock exams.\n\nDigital Evaluating review books and clinical pharmacology books.\n\nOnline classes are on Saturday and Sunday, 10 am to 2 pm (Toronto time)\n\nWe are mailing books to your address\n\nShould you need further assistance, please do not hesitate to contact us.\n\nregards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Pharmacy prep Course Renewal",
    "body": "Welcome to pharmacy prep\nThe Pharmacy prep course and online account access can be extended for a year with an extension fee $190+tax=$214.70. Throughout the year, you will be able to get new mock tests, class notes, and recorded videos and be able to join the live online interactive lectures. To pay for the online access, please send an e-transfer. Please email your e-transfer to \"success@pharmacyprep.com\" and please email us the password created for the e-transfer.\nPlease create e-Transfer\nSecurity Answer as “success”\nWe hope the information is sufficient to answer all your questions, if you still have any questions, please do not hesitate to e-mail of CALL/TEXT/SMS us at 416-223-7737 / 647.221.0457\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  },
  {
    "title": "Welcome. Enrolled Pharmacy tech MCQ prep course",
    "body": "Dear Manoj\nWelcome. We have enrolled you in the Pharmacy prep tech qualifying MCQ  prep course. Please find below the login for an online exam prep station that enables access to STUDY PLAN, LIVE LECTURES, QBANK, MOCK EXAMS, COMPUTER-BASED TESTS, AND DIGITAL BOOKS.\n\nPlease find online access at www.pharmacyprep.com\n\nUser Name:\nPassword:\n\nTo begin preparing, select registered courses and select tech-qualifying MCQ prep course.\n\nPlease click LIVE LECTURE LINK TO JOIN online lectures on\n\nMCQ classes on Live Lecture Start Date: June 21 2025: Saturdays 4pm to 8pm\n\nWe are mailing books to your address.\n\nWe can set up online meeting to walk you through prep course and give you a study plan. Please let us know if you have any questions.\n\nRegards\n\nPharmacy Prep\nPhone:416-223-PREP(7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
  }
]


def _final_lower_thread_text(thread: Dict, connected_email: str = "") -> str:
    try:
        return combined_thread_text(thread).lower()
    except Exception:
        latest = latest_inbound_email_for_dashboard(thread, connected_email)
        return f"{latest.get('subject','')}\n{latest.get('body','')}".lower()


def _final_is_pharmacyprep_related_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = _final_lower_thread_text(thread, connected_email)
    strong_terms = [
        "pharmacy prep", "pharmacyprep", "eprepstation", "success@pharmacyprep.com",
        "pebc", "opra", "fpgee", "naplex", "osce", "ospe", "qbank", "mock exam",
        "mock exams", "qualifying exam", "evaluating exam", "mcq", "pharmacist", "pharmacy technician",
        "technician mcq", "technician ospe", "pharmacy exam", "prep course", "home study",
        "live lecture", "recorded lecture", "study plan", "course access", "course login",
        "online exam prep station", "exam prep station", "registered courses", "login details",
    ]
    if any(term in text for term in strong_terms):
        return True
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    course_context = ["course", "class", "student", "exam", "prep", "lecture", "recording", "notes", "books", "book", "enroll", "enrol", "registration"]
    support_context = ["login", "access", "password", "extension", "renewal", "schedule", "qbank", "mock"]
    return any(a in text for a in course_context) and any(b in text for b in support_context)


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return "work" if _final_is_pharmacyprep_related_thread(thread, connected_email) else "personal"


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return dashboard_category_for_thread(thread, connected_email)


def _final_is_known_noise(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 7000).lower()
    text = f"{subject}\n{body}"
    has_direct_request = any(term in text for term in [
        "?", "can you", "could you", "would you", "please send", "please provide", "please confirm",
        "i need", "need help", "i would like", "how do i", "when will", "where is", "what is",
        "let me know", "follow up", "checking in", "not received", "still waiting", "unable to",
        "can't", "cannot", "issue", "problem", "refund", "order number"
    ])
    status_terms = ["has shipped", "on the way", "out for delivery", "delivered", "tracking number", "shipment", "shipping confirmation", "payment received", "e-transfer received", "etransfer received", "interac e-transfer", "receipt for your payment", "charge receipt", "invoice paid", "successful payment", "order confirmation", "your order is confirmed"]
    if any(term in text for term in status_terms) and not has_direct_request:
        return "status/payment/shipping confirmation"
    fyi_terms = ["for your information", "fyi", "no action required", "no need to reply", "please be advised", "notice to residents", "building notice", "master key", "unit door", "contractor requires access", "thank you for signing", "document has been signed", "completed document", "signed document"]
    if any(term in text for term in fyi_terms) and not has_direct_request:
        return "FYI/confirmation"
    marketing_terms = ["unsubscribe", "manage your preferences", "view this email in your browser", "newsletter", "promotion", "limited time", "sale ends", "special offer", "digest"]
    if any(term in text for term in marketing_terms) and not has_direct_request:
        return "marketing/newsletter"
    system_terms = ["please moderate", "comment awaiting moderation", "new question submitted", "security alert", "verification code", "password reset", "delivery status notification", "undeliverable", "mail delivery", "this is an automated message", "do not reply to this email"]
    if any(term in text for term in system_terms) and not has_direct_request:
        return "system notification"
    no_reply_senders = ["mailer-daemon", "postmaster", "marketing@", "newsletter", "security@"]
    if any(term in sender for term in no_reply_senders) and not has_direct_request:
        return "automated sender"
    return ""


def _final_request_score(thread: Dict, connected_email: str) -> int:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 9000).lower()
    text = f"{subject}\n{body}"
    score = 0
    direct = ["?", "can you", "could you", "would you", "can i", "could i", "do you", "should i", "please", "i need", "need help", "i would like", "i want", "how do i", "how can i", "when will", "where is", "what is", "what's", "let me know", "advise", "clarify", "confirm", "question", "help", "send me", "share", "provide", "update me", "follow up", "follow-up", "checking in", "not received", "still waiting", "missing", "issue", "problem", "unable to", "can't", "cannot", "available", "availability", "request"]
    if any(term in text for term in direct):
        score += 4
    if "?" in text:
        score += 3
    if len(thread.get("emails", [])) >= 2:
        score += 2
    if len(body.split()) >= 5:
        score += 1
    if len(body.split()) >= 18:
        score += 1
    if sender and not any(x in sender for x in ["noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "wordpress", "woocommerce", "marketing@", "newsletter"]):
        score += 2
    if _final_is_pharmacyprep_related_thread(thread, connected_email):
        score += 2
    return score


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _final_is_known_noise(thread, connected_email):
        return False
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    body = clean_preview_text(latest.get("body", ""), 9000)
    human_sender = bool(sender) and not any(blocked in sender for blocked in ["noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "wordpress", "woocommerce", "notifications@", "marketing@", "newsletter"])
    if _final_request_score(thread, connected_email) >= 4:
        return True
    if human_sender and len(thread.get("emails", [])) >= 2 and len(body.split()) >= 2:
        return True
    if human_sender and len(body.split()) >= 10:
        return True
    return False


def _final_personal_fallback_reply(thread: Dict, connected_email: str, category: str) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting_name = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    if category == "work":
        body = f"""Hello {greeting_name},

Thank you for your email. We will review this and reply with the correct details shortly.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting_name},

Thanks for your email. I will take a look and get back to you shortly.

Regards"""
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def _final_build_favorited_item(thread: Dict, connected_email: str, stored_item: Dict, gmail_starred: bool = False) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    category = dashboard_category_for_thread(thread, connected_email)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)
    sender_name = sender_display_name(latest.get("from", ""), parseaddr(latest.get("from", ""))[1])
    subject = latest.get("subject", "") or stored_item.get("title", "Favorited email")
    reason = stored_item.get("important_reason") or f"{sender_name} is favorited, so this thread stays on the dashboard."
    reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
    if not reply and not latest_email_is_from_connected_account(thread, connected_email) and not _final_is_known_noise(thread, connected_email):
        reply = _final_personal_fallback_reply(thread, connected_email, category)
    return {**stored_item, "thread_id": thread.get("thread_id", ""), "category": category, "title": stored_item.get("title") or subject, "important_reason": reason, "status": "Needs Reply" if reply else (stored_item.get("status") or "Favorited"), "favorite": True, "gmail_starred": bool(gmail_starred or stored_item.get("gmail_starred")), "filtered_out": False, "ai_screened": bool(stored_item.get("ai_screened", False)), "screening_version": EMAIL_SCREENING_VERSION, "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""), "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)}, "reply": reply}


def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str, force_include: bool = False, gmail_starred: bool = False) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    thread_id = thread.get("thread_id", "")
    thread_key = thread_action_key(thread, connected_email)
    action = get_thread_action(thread_key)
    stored_item = get_catalog_item("emails", thread_id)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)
    category = dashboard_category_for_thread(thread, connected_email)
    favorite = bool(force_include or gmail_starred or stored_item.get("favorite") or stored_item.get("gmail_starred"))
    if favorite:
        item = _final_build_favorited_item(thread, connected_email, stored_item, gmail_starred=gmail_starred)
        try:
            apply_label_to_thread_messages(service, thread, personal_label_id if item.get("category") == "personal" else work_label_id)
        except Exception:
            pass
        return item
    if latest_email_is_from_connected_account(thread, connected_email):
        if not stored_item:
            return None
        return {**stored_item, "thread_id": thread_id, "category": category, "status": "Already Replied", "reply": None, "reply_sent_at": stored_item.get("reply_sent_at") or datetime.now().isoformat(timespec="seconds"), "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""), "screening_version": EMAIL_SCREENING_VERSION, "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)}}
    if action.get("action_type") == "dismissed":
        return {**stored_item, "thread_id": thread_id, "category": category, "status": "Suggestion Removed", "reply": None, "screening_version": EMAIL_SCREENING_VERSION}
    if action.get("action_type") == "frontend_reply_sent":
        return {**stored_item, "thread_id": thread_id, "category": category, "status": "Already Replied", "reply": None, "reply_sent_at": action.get("processed_at", ""), "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""), "screening_version": EMAIL_SCREENING_VERSION}
    noise = _final_is_known_noise(thread, connected_email)
    if noise:
        return {"thread_id": thread_id, "category": category, "title": latest.get("subject", "Filtered email"), "important_reason": noise, "status": "Filtered Out", "filtered_out": True, "ai_screened": False, "screening_version": EMAIL_SCREENING_VERSION, "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts, "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)}, "reply": None}
    candidate = should_consider_thread_for_dashboard(thread, connected_email)
    if not candidate and not stored_item:
        return None
    screening = analyze_dashboard_thread_with_ai(thread, connected_email) if candidate else None
    if screening and not screening.get("include") and not stored_item:
        return {"thread_id": thread_id, "category": category, "title": latest.get("subject", "Filtered email"), "important_reason": screening.get("reason", "Not actionable"), "status": "Filtered Out", "filtered_out": True, "ai_screened": True, "screen_confidence": screening.get("confidence", 0), "screening_version": EMAIL_SCREENING_VERSION, "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts, "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)}, "reply": None}
    title = latest.get("subject", "") or stored_item.get("title") or "Email needs review"
    if screening and screening.get("title") and 3 <= len(screening.get("title", "").split()) <= 12:
        title = screening.get("title", "").strip()
    important_reason = (screening or {}).get("summary", "").strip()
    if summary_is_generic(important_reason):
        important_reason = stored_item.get("important_reason") if stored_item.get("important_reason") and not summary_is_generic(stored_item.get("important_reason")) else build_important_reason(thread, connected_email)
    try:
        apply_label_to_thread_messages(service, thread, personal_label_id if category == "personal" else work_label_id)
    except Exception:
        pass
    latest_clean_body = clean_preview_text(latest.get("body", ""), 6000)
    cached_reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
    cached_ok = bool(cached_reply) and not reply_needs_regeneration(cached_reply.get("body", ""), latest_clean_body, category)
    reply = cached_reply if cached_ok else None
    if not reply:
        try:
            queries = heuristic_context_queries_for_thread(thread, connected_email)
            extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=4) if queries else ""
            composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        except Exception:
            composed = None
        if composed:
            if composed.get("summary") and not summary_is_generic(composed.get("summary", "")):
                important_reason = composed.get("summary", "").strip()
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                title = composed.get("title", "").strip()
            reply = {"thread_id": thread_id, "mode": "thread_reply", "to": parseaddr(latest.get("from", ""))[1].strip(), "subject": composed.get("subject", ""), "body": composed.get("body", "")}
    if not reply:
        reply = _final_personal_fallback_reply(thread, connected_email, category)
    return {"thread_id": thread_id, "category": category, "title": title, "important_reason": important_reason, "status": "Needs Reply", "reply_sent_at": stored_item.get("reply_sent_at", ""), "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""), "ai_screened": bool(screening) or candidate, "screen_confidence": (screening or {}).get("confidence", stored_item.get("screen_confidence", 0)), "screening_version": EMAIL_SCREENING_VERSION, "favorite": bool(stored_item.get("favorite", False)), "gmail_starred": bool(gmail_starred or stored_item.get("gmail_starred", False)), "filtered_out": False, "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)}, "reply": reply}


def _final_start_for_scan(catalog: Dict, force_full: bool = False) -> datetime:
    if force_full:
        return SCAN_START_DT
    meta = catalog.get("meta", {}) if isinstance(catalog.get("meta", {}), dict) else {}
    raw = meta.get("last_successful_scan_at") or meta.get("last_scan_at") or ""
    try:
        if raw:
            parsed = datetime.fromisoformat(raw[:19]) - timedelta(days=3)
            return max(parsed, SCAN_START_DT)
    except Exception:
        pass
    return SCAN_START_DT


def _scan_after_clause_for_catalog(catalog: Dict, force_full: bool = False) -> Tuple[str, str]:
    start_dt = _final_start_for_scan(catalog, force_full=force_full)
    after_value = (start_dt - timedelta(days=1)).strftime("%Y/%m/%d")
    return f"after:{after_value}", start_dt.isoformat(timespec="seconds")


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'{date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    return [
        f'{base} is:starred', f'{base} in:inbox', f'{base}',
        f'{base} ("?" OR "please" OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know" OR "follow up")',
        f'{base} ("not received" OR "still waiting" OR "checking in" OR "send me" OR "available" OR "availability")',
        f'{base} (invoice OR receipt OR payment OR refund OR bank OR statement OR document OR appointment OR meeting)',
        f'{base} (PEBC OR OPRA OR FPGEE OR NAPLEX OR OSCE OR OSPE OR QBank OR exam OR course OR class OR schedule OR notes OR recording OR login OR access OR renewal)',
        f'{base} newer_than:30d',
    ]


def _final_strict_order_sent_check(service, customer_email: str, order_number: str) -> bool:
    if not customer_email or not order_number or str(order_number).lower() == "unknown":
        return False
    queries = [f'in:sent newer_than:365d to:{customer_email} "order #{order_number}"', f'in:sent newer_than:365d to:{customer_email} "{order_number}" "Welcome to Pharmacy Prep"', f'in:sent newer_than:365d to:{customer_email} "{order_number}" (enrolled OR enrollment OR login OR access)']
    for query in queries:
        try:
            if gmail_search_any(service, query, max_results=5):
                return True
        except Exception:
            continue
    return False

was_order_message_already_sent = _final_strict_order_sent_check


def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    work_label_id = get_or_create_label(service, WORK_LABEL)
    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {}); catalog.setdefault("orders", {}); catalog.setdefault("emails", {})
    date_clause, scan_start_used = _scan_after_clause_for_catalog(catalog, force_full=force_full)
    debug = _debug_counts_template() if "_debug_counts_template" in globals() else {"errors": []}
    debug["scan_start"] = scan_start_used; debug["force_full"] = force_full
    print(f"[scan] starting Gmail scan | force_full={force_full} | date_clause={date_clause}", flush=True)
    order_thread_ids = _collect_thread_ids(service, _order_scan_queries(date_clause), per_query_limit=max(10, MAX_ORDER_THREADS_PER_SCAN // 4), total_limit=MAX_ORDER_THREADS_PER_SCAN)
    starred_thread_ids = set(_collect_thread_ids(service, [f'after:{SCAN_START_GMAIL_AFTER} is:starred -in:spam -in:trash'], per_query_limit=100, total_limit=FAVORITE_SCAN_LIMIT))
    email_thread_ids = _collect_thread_ids(service, _email_scan_queries(date_clause, connected_email), per_query_limit=max(40, MAX_EMAIL_THREADS_PER_SCAN // 6), total_limit=MAX_EMAIL_THREADS_PER_SCAN)
    catalog_favorites = [tid for tid, item in catalog.get("emails", {}).items() if isinstance(item, dict) and item.get("favorite") and not str(tid).startswith("renewal_")]
    ordered_email_ids = []
    seen = set()
    for tid in list(starred_thread_ids) + catalog_favorites + email_thread_ids:
        if tid and tid not in seen:
            seen.add(tid); ordered_email_ids.append(tid)
    email_thread_ids = ordered_email_ids
    debug["order_threads_found"] = len(order_thread_ids); debug["email_threads_found"] = len(email_thread_ids)
    print(f"[scan] Gmail returned {len(order_thread_ids)} order thread(s), {len(email_thread_ids)} possible email/favorite thread(s), {len(starred_thread_ids)} starred", flush=True)
    auto_orders_sent = 0; order_replies_waiting = 0; skipped_failed_orders = 0; processed_order_threads = set(); ai_screenings_used = 0
    for thread_id in order_thread_ids:
        try:
            thread = read_thread(service, thread_id)
            order_item = build_order_item(service, thread, connected_email)
            if not order_item: continue
            processed_order_threads.add(thread_id)
            if order_item.get("order_number") == "Unknown": skipped_failed_orders += 1
            order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
            if did_send: auto_orders_sent += 1
            if order_item.get("reply"): order_replies_waiting += 1
            _upsert_order_in_catalog(catalog, thread_id, order_item)
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["email_errors"] = debug.get("email_errors", 0) + 1; debug.setdefault("errors", []).append(f"order {thread_id}: {error}")
    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads: continue
        try:
            thread = read_thread(service, thread_id)
            debug["email_threads_read"] = debug.get("email_threads_read", 0) + 1
            if not _thread_is_on_or_after_scan_start(thread, connected_email):
                debug["email_skipped_old_date"] = debug.get("email_skipped_old_date", 0) + 1; continue
            if get_best_order_email_text(thread):
                order_item = build_order_item(service, thread, connected_email)
                if order_item:
                    order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
                    if did_send: auto_orders_sent += 1
                    if order_item.get("reply"): order_replies_waiting += 1
                    _upsert_order_in_catalog(catalog, thread_id, order_item)
                continue
            favorite = thread_id in starred_thread_ids or bool(catalog.get("emails", {}).get(thread_id, {}).get("favorite"))
            item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id, force_include=favorite, gmail_starred=thread_id in starred_thread_ids)
            if item:
                _upsert_email_in_catalog(catalog, thread_id, item)
                if item.get("reply") and not item.get("filtered_out"): debug["email_ai_accepted"] = debug.get("email_ai_accepted", 0) + 1
                elif item.get("filtered_out"): debug["email_ai_rejected"] = debug.get("email_ai_rejected", 0) + 1
                if item.get("ai_screened"): ai_screenings_used += 1
            else:
                debug["email_skipped_prescreen"] = debug.get("email_skipped_prescreen", 0) + 1
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug["email_errors"] = debug.get("email_errors", 0) + 1
            if len(debug.get("errors", [])) < 20: debug.setdefault("errors", []).append(f"email {thread_id}: {error}")
    try:
        added = _scan_and_upsert_renewal_requests(service, catalog, connected_email) if "_scan_and_upsert_renewal_requests" in globals() else 0
    except Exception as error:
        added = 0; print(f"[renewal] skipped: {error}", flush=True)
    now = datetime.now().isoformat(timespec="seconds")
    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog["meta"] = {**catalog.get("meta", {}), "connected_email": connected_email, "last_successful_scan_at": now, "last_scan_start": scan_start_used, "scan_window": f"{SCAN_START_DISPLAY} onward", "scan_start_date": SCAN_START_DT.strftime("%Y-%m-%d"), "email_screening_version": EMAIL_SCREENING_VERSION, "last_briefing_date": datetime.now().strftime("%Y-%m-%d")}
    save_dashboard_catalog(catalog)
    if "_save_scan_debug" in globals():
        try: _save_scan_debug(debug)
        except Exception: pass
    invalidate_dashboard_cache()
    payload = build_dashboard_payload(force_refresh=True)
    emails = payload.get("emails", []); orders = payload.get("orders", [])
    suggested_replies = len([email for email in emails if email.get("reply")])
    payload["scan_summary"] = {"scan_window": f"{SCAN_START_DISPLAY} onward", "scan_start": scan_start_used, "orders_checked": len(order_thread_ids), "emails_checked": len(email_thread_ids), "email_threads_read": debug.get("email_threads_read", 0), "emails_accepted": len([e for e in emails if e.get("reply") or e.get("favorite") or e.get("gmail_starred")]), "emails_rejected": debug.get("email_ai_rejected", 0) + debug.get("email_skipped_prescreen", 0) + debug.get("email_skipped_old_date", 0), "auto_orders_sent": auto_orders_sent, "order_replies_waiting": order_replies_waiting, "failed_orders_skipped": skipped_failed_orders, "suggested_replies": suggested_replies, "ai_screenings_used": ai_screenings_used, "renewal_added": added, "debug_file": str(SCAN_DEBUG_FILE) if "SCAN_DEBUG_FILE" in globals() else ""}
    print(f"[scan] complete | read={payload['scan_summary']['email_threads_read']} | visible_emails={len(emails)} | suggested={suggested_replies} | personal={len([e for e in emails if e.get('category') == 'personal'])} | work={len([e for e in emails if e.get('category') == 'work'])}", flush=True)
    return payload


def _final_email_visible(item: Dict) -> bool:
    if not _item_is_on_or_after_scan_start(item): return False
    if item.get("favorite") or item.get("gmail_starred"):
        return item.get("category") in ("work", "personal")
    if item.get("filtered_out") or item.get("status") == "Filtered Out": return False
    if item.get("category") not in ("work", "personal"): return False
    if item.get("screening_version") != EMAIL_SCREENING_VERSION: return False
    has_reply = bool(item.get("reply"))
    # The older unimportant check was too broad for personal finance messages because words like
    # "receipt" or "invoice" can be part of a real question. Only use it to hide non-reply items.
    if _catalog_item_looks_unimportant(item) and not has_reply: return False
    handled = item.get("status") in ("Already Replied", "Suggestion Removed") and bool(item.get("important_reason"))
    return has_reply or handled

_is_catalog_email_visible = _final_email_visible


def _brief_sender_name(item: Dict) -> str:
    original = item.get("original", {}) or {}
    name, addr = parseaddr(original.get("from", ""))
    clean = re.sub(r"[\"']", "", name or "").strip()
    if clean and clean.lower() not in ("no reply", "noreply", "unknown"):
        return clean.split()[0].title() if len(clean.split()) <= 3 else clean.title()
    return infer_customer_name_from_email(addr) or "Someone"


def _brief_topic(item: Dict) -> str:
    text = _catalog_text(item)
    checks = [(["renewal", "extension"], "renewals/extensions"), (["login", "access", "password"], "login/access"), (["schedule", "class", "lecture", "availability"], "classes or scheduling"), (["book", "materials", "notes"], "books/materials"), (["invoice", "payment", "receipt", "refund", "bank", "statement"], "payments/finances"), (["order number", "order #"], "order details"), (["exam", "pebc", "opra", "osce", "ospe", "mcq"], "exam/course questions"), (["meeting", "call", "appointment"], "meetings/appointments")]
    for words, label in checks:
        if any(w in text for w in words): return label
    subj = (item.get("original", {}) or {}).get("subject", "") or item.get("title", "message")
    return re.sub(r"^(re|fw|fwd):\s*", "", subj, flags=re.IGNORECASE).strip()[:60] or "messages"


def build_daily_briefing(connected_email: str, orders: List[Dict], emails: List[Dict]) -> str:
    now_dt = datetime.now(); now = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
    orders = [order for order in orders if _is_catalog_order_visible(order)]
    emails = [email for email in emails if _is_catalog_email_visible(email)]
    orders.sort(key=_catalog_sort_key, reverse=True); emails.sort(key=_catalog_sort_key, reverse=True)
    seen_orders = set(); deduped_orders = []
    for order in orders:
        key = (str(order.get("order_number", "")).lower(), str(order.get("customer_email", "")).lower())
        if key in seen_orders: continue
        seen_orders.add(key); deduped_orders.append(order)
    work = [e for e in emails if e.get("category") == "work"]; personal = [e for e in emails if e.get("category") == "personal"]
    waiting_orders = [o for o in deduped_orders if o.get("reply")]; handled_orders = [o for o in deduped_orders if not o.get("reply")]
    names = []
    for item in emails:
        nm = _brief_sender_name(item)
        if nm and nm not in names and nm.lower() not in ("no", "reply", "someone"):
            names.append(nm)
        if len(names) >= 4: break
    topic_counts = {}
    for e in emails:
        topic = _brief_topic(e); topic_counts[topic] = topic_counts.get(topic, 0) + 1
    top_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    topic_phrase = ", ".join([f"{count} {topic}" for topic, count in top_topics]) if top_topics else "no major email themes"
    names_phrase = ", ".join(names) if names else "the newest visible threads"
    newest_order = deduped_orders[0] if deduped_orders else {}
    greeting = "Good morning" if now_dt.hour < 12 else "Good afternoon" if now_dt.hour < 17 else "Good evening"
    order_phrase = ("mostly handled" if not waiting_orders else f"waiting on approval for {len(waiting_orders)} item(s)")
    newest_order_phrase = (f"the newest visible order is #{newest_order.get('order_number', 'Unknown')} for {newest_order.get('customer_name', 'Customer')}." if newest_order else "no June-forward orders are visible yet.")
    paragraph = f"{greeting} — here’s the inbox picture. There are {len(work)} Pharmacy Prep/work thread(s) and {len(personal)} personal thread(s) needing attention, with the main themes being {topic_phrase}. Start with {names_phrase}, since those are among the newest visible conversations. Orders are {order_phrase}; {newest_order_phrase} Favorited or starred threads stay on the dashboard even when they would normally be filtered, and this briefing regenerates whenever the dashboard refreshes."
    lines = ["# AI Summary", "", f"Generated: {now}", f"Connected Gmail: {connected_email}", f"Scan window: {SCAN_START_DISPLAY} onward", "", "Daily Briefing", paragraph, "", "Snapshot", f"- Work: {len(work)}", f"- Personal: {len(personal)}", f"- Orders waiting: {len(waiting_orders)}", f"- Orders handled: {len(handled_orders)}"]
    briefing = "\n".join(lines)
    BRIEFING_FILE.write_text(briefing, encoding="utf-8")
    return briefing


def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    catalog = get_dashboard_catalog(); catalog.setdefault("meta", {}); catalog.setdefault("orders", {}); catalog.setdefault("emails", {})
    try:
        if "_normalize_catalog_renewals" in globals():
            catalog, changed = _normalize_catalog_renewals(catalog)
            if changed: save_dashboard_catalog(catalog)
    except Exception: pass
    connected_email = catalog.get("meta", {}).get("connected_email") or DEFAULT_CONNECTED_EMAIL
    orders = [item for item in catalog.get("orders", {}).values() if _is_catalog_order_visible(item)]
    emails = []
    for item in catalog.get("emails", {}).values():
        if not isinstance(item, dict): continue
        if item.get("is_renewal_request") and "_normalize_renewal_item_for_display" in globals():
            try: item = _normalize_renewal_item_for_display(item)
            except Exception: pass
        if item.get("category") not in ("work", "personal"):
            item["category"] = "work" if item.get("is_renewal_request") else item.get("category", "personal")
        if _is_catalog_email_visible(item): emails.append(item)
    orders.sort(key=_catalog_sort_key, reverse=True); emails.sort(key=_catalog_sort_key, reverse=True)
    briefing = build_daily_briefing(connected_email, orders, emails)
    pending_reply_ids = [item["thread_id"] for item in emails if item.get("reply")] + [item["thread_id"] for item in orders if item.get("reply")]
    return {"ok": True, "connected_email": connected_email, "orders": orders, "emails": emails, "pending_replies": pending_reply_ids, "briefing": briefing, "templates_count": len(GMAIL_TEMPLATES), "automation_settings": {**get_automation_settings(), "last_auto_scan_at": catalog.get("meta", {}).get("last_successful_scan_at", "")}, "stats": {"orders_replied": len([order for order in orders if not order.get("reply")]), "pending_replies": len(pending_reply_ids), "work_emails": len([email for email in emails if email.get("category") == "work"]), "personal_emails": len([email for email in emails if email.get("category") == "personal"])}}


def api_gmail_templates_final():
    return jsonify({"ok": True, "templates": GMAIL_TEMPLATES})
try:
    app.add_url_rule("/api/gmail-templates", "api_gmail_templates_final", api_gmail_templates_final, methods=["GET"])
except Exception:
    app.view_functions["api_gmail_templates_final"] = api_gmail_templates_final


def api_toggle_favorite_final(thread_id: str):
    try:
        catalog = get_dashboard_catalog()
        item = catalog.setdefault("emails", {}).get(thread_id) or catalog.setdefault("orders", {}).get(thread_id)
        if not item: return jsonify({"ok": False, "error": "Could not find this email/order in the dashboard catalog yet."}), 404
        item["favorite"] = True; item["filtered_out"] = False; item["screening_version"] = EMAIL_SCREENING_VERSION
        if item.get("category") not in ("work", "personal"):
            item["category"] = "work" if item.get("is_renewal_request") else "personal"
        save_dashboard_catalog(catalog); invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Favorited."})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


def api_unfavorite_final(thread_id: str):
    try:
        catalog = get_dashboard_catalog()
        item = catalog.setdefault("emails", {}).get(thread_id) or catalog.setdefault("orders", {}).get(thread_id)
        if not item: return jsonify({"ok": False, "error": "Could not find this email/order in the dashboard catalog yet."}), 404
        item["favorite"] = False; save_dashboard_catalog(catalog); invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Removed from favorites."})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

try:
    app.add_url_rule("/api/favorites/<thread_id>", "api_toggle_favorite_final", api_toggle_favorite_final, methods=["POST"])
    app.add_url_rule("/api/favorites/<thread_id>", "api_unfavorite_final", api_unfavorite_final, methods=["DELETE"])
except Exception:
    app.view_functions["api_toggle_favorite_final"] = api_toggle_favorite_final
    app.view_functions["api_unfavorite_final"] = api_unfavorite_final


def api_scan():
    try:
        request_body = request.get_json(silent=True) or {}
        payload = perform_gmail_scan(force_full=bool(request_body.get("force_full", False)))
        summary = payload.get("scan_summary", {})
        return jsonify({"ok": True, "message": "Full scan complete." if request_body.get("force_full") else "Refresh complete.", "order_replies_waiting": summary.get("order_replies_waiting", len([order for order in payload.get("orders", []) if order.get("reply")])), "failed_orders_skipped": summary.get("failed_orders_skipped", 0), "suggested_replies": summary.get("suggested_replies", len([email for email in payload.get("emails", []) if email.get("reply")])), "auto_orders_sent": summary.get("auto_orders_sent", 0), "emails_checked": summary.get("emails_checked", 0), "email_threads_read": summary.get("email_threads_read", 0), "emails_accepted": summary.get("emails_accepted", 0), "emails_rejected": summary.get("emails_rejected", 0), "ai_screenings_used": summary.get("ai_screenings_used", 0), "renewal_added": summary.get("renewal_added", 0), "debug_file": summary.get("debug_file", ""), "scan_start": summary.get("scan_start", ""), "scan_window": summary.get("scan_window", f"{SCAN_START_DISPLAY} onward")})
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_scan"] = api_scan

if __name__ == "__main__":
    print("\nPharmacy Prep Gmail Assistant is starting...")
    print("Open this link in your browser:")
    port = int(os.getenv("PORT", "5050"))
    print(f"http://127.0.0.1:{port}")
    print("")
    app.run(host="0.0.0.0", port=port, debug=False)
