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
    # EprepStation renewal forms are automated/no-reply style emails, but they are
    # intentionally actionable. Never hide them because they contain no-reply/do-not-reply text.
    if item.get("renewal_request"):
        return False
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

    # Renewal requests are synthetic dashboard items created from EprepStation form
    # emails. They must be visible even though the source email often looks automated.
    if item.get("renewal_request"):
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")

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
    # EprepStation renewal forms are automated/no-reply style emails, but they are
    # intentionally actionable. Never hide them because they contain no-reply/do-not-reply text.
    if item.get("renewal_request"):
        return False
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
# CLEAN FINAL PATCH: AI-first June-forward scan + stable visibility + renewal support
# ---------------------------------------------------------------------
# This patch intentionally replaces the broken stacked renewal/full-scan blocks.
# It keeps the older working base, then makes normal emails visible again.
import hashlib

EMAIL_SCREENING_VERSION = "2026-06-clean-ai-first-v1"
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "400"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "160"))
FULL_SCAN_MAX_THREADS = int(os.getenv("FULL_SCAN_MAX_THREADS", "2500"))
REFRESH_SCAN_DAYS = int(os.getenv("REFRESH_SCAN_DAYS", "21"))
SCAN_DEBUG_FILE = BASE_DIR / "last_scan_debug.json"
RENEWAL_REQUEST_PATCH_VERSION = "2026-06-clean-renewal-v1"


def _clean_debug_template() -> Dict:
    return {
        "started_at": _safe_iso_now(),
        "scan_start": "",
        "force_full": False,
        "gmail_candidate_threads": 0,
        "order_threads_found": 0,
        "email_threads_found": 0,
        "threads_read": 0,
        "orders_upserted": 0,
        "renewals_upserted": 0,
        "ai_checked": 0,
        "ai_accepted": 0,
        "ai_rejected": 0,
        "reply_composed": 0,
        "reply_missing_but_visible": 0,
        "skipped_latest_from_us": 0,
        "skipped_old_date": 0,
        "skipped_order_notification": 0,
        "errors": [],
        "accepted_examples": [],
        "rejected_examples": [],
    }


def _save_clean_debug(debug: Dict):
    try:
        debug["finished_at"] = _safe_iso_now()
        save_json_file(SCAN_DEBUG_FILE, debug)
    except Exception:
        pass


def _debug_example(thread: Dict, connected_email: str, reason: str, item: Optional[Dict] = None) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return {
        "reason": reason,
        "thread_id": thread.get("thread_id", ""),
        "category": (item or {}).get("category", ""),
        "from": latest.get("from", ""),
        "date": latest.get("date", ""),
        "subject": latest.get("subject", ""),
        "preview": clean_preview_text(latest.get("body", ""), 260),
    }


def _append_debug(debug: Dict, key: str, value: Dict, limit: int = 20):
    try:
        if len(debug.setdefault(key, [])) < limit:
            debug[key].append(value)
    except Exception:
        pass


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    """Only reject truly bad replies. Do not require 'Pharmacy Prep' for visibility.
    The prior strict function caused AI-accepted personal/work emails to disappear.
    """
    body = (reply_body or "").strip()
    latest = clean_preview_text(latest_body or "", 6000).strip()
    body_lower = body.lower()
    if len(body.split()) < 12:
        return True
    bad_fillers = [
        "we received your message and will get back to you",
        "we received your email and will get back to you",
        "thank you for your email. we will review your request",
        "we will review your request and get back to you shortly",
    ]
    if any(phrase in body_lower for phrase in bad_fillers):
        return True
    if latest and body_lower.startswith(latest.lower()[:80]):
        return True
    if latest and copied_sequence_found(body, latest, sequence_len=14):
        return True
    return False


def _clean_work_text(thread: Dict, connected_email: str) -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return "\n".join([
        latest.get("subject", ""),
        latest.get("from", ""),
        latest.get("to", ""),
        latest.get("body", ""),
        combined_thread_text(thread),
    ]).lower()


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    text = _clean_work_text(thread, connected_email)
    sender = parseaddr(latest_inbound_email_for_dashboard(thread, connected_email).get("from", ""))[1].lower().strip()
    # Work means Pharmacy Prep / student / PEBC / course / login / order / renewal / support.
    direct_work_terms = [
        "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "eprepstation",
        "pebc", "evaluating exam", "qualifying exam", "osce", "ospe", "mcq", "fpgee", "opra",
        "naplex", "pharmacist", "pharmacy technician", "prep course", "qbank", "mock exam",
        "student", "course", "class", "lecture", "recording", "notes", "book", "study plan",
        "login", "access", "password", "account renewal", "renewal", "extension", "enroll", "enrol",
        "registration", "order #", "new order", "order number", "woocommerce", "wordpress",
        "invoice", "receipt", "payment", "refund", "etransfer", "e-transfer",
    ]
    domains = ["pharmacyprep.com", "eprepstation.com"]
    return any(term in text for term in direct_work_terms) or any(domain in sender for domain in domains)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _ai_relevance_decision(thread: Dict, connected_email: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 8000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 14000)
    category_hint = category_for_thread_strict(thread, connected_email)
    prompt = f"""
You are screening Gmail for a dashboard. Decide if the latest inbound message should be shown as something the user should review/reply to.

Use AI judgment, not keyword filters.

Include if the latest inbound message is from a real person or an organization and likely needs a reply, review, decision, follow-up, or action. This includes:
- Pharmacy Prep/student/customer emails about PEBC, courses, login/access, orders, payments, renewals, books, schedules, recordings, notes, refunds, registration, support.
- Older email threads where the latest inbound message reopens the conversation or asks a new question.
- Personal/non-Pharmacy Prep actionable messages such as finance, appointments, services, family, vendors, documents that need action, or any direct question/request.

Exclude if it is clearly not reply-worthy: pure receipt, shipping/tracking update, payment received/e-transfer notification, newsletter, marketing, WordPress/system moderation notice, security alert, automated no-reply notification, document signing confirmation, FYI-only building notice, thank-you-only email, or anything where a reply would be unnecessary.

Category rule:
- work = Pharmacy Prep/student/PEBC/course/login/order/renewal/support/payment/refund/business for Pharmacy Prep.
- personal = any actionable email outside Pharmacy Prep.
Current deterministic category hint: {category_hint}

Return JSON only:
{{
  "include": true,
  "category": "{category_hint}",
  "title": "4-9 word title",
  "summary": "specific one-sentence summary saying who needs what",
  "needs_reply": true,
  "reason": "brief reason for include/exclude",
  "confidence": 0.0
}}

Sender display name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Full current thread:
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
        category = category_for_thread_strict(thread, connected_email)
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        return {
            "include": bool(include),
            "category": category,
            "title": str(parsed.get("title", "") or "").strip(),
            "summary": str(parsed.get("summary", "") or "").strip(),
            "needs_reply": bool(parsed.get("needs_reply", include)),
            "reason": str(parsed.get("reason", "") or "").strip(),
            "confidence": confidence,
        }
    except Exception as error:
        print(f"[scan] AI relevance failed: {error}", flush=True)
        return None


def _human_fallback_decision(thread: Dict, connected_email: str) -> Optional[Dict]:
    """If AI times out/fails, include obvious human requests rather than dropping all emails."""
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = f"{latest.get('subject','')}\n{latest.get('body','')}".lower()
    if any(blocked in sender for blocked in ["noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "wordpress", "woocommerce", "notifications@", "marketing@"]):
        return None
    request_terms = ["?", "please", "can you", "could you", "would you", "i need", "need help", "let me know", "not received", "still waiting", "follow up", "checking in", "send me", "confirm", "advise", "help", "issue", "problem"]
    if not any(term in text for term in request_terms) and len(clean_preview_text(latest.get("body", ""), 2000).split()) < 16:
        return None
    category = category_for_thread_strict(thread, connected_email)
    return {
        "include": True,
        "category": category,
        "title": latest.get("subject", "Important email") or "Important email",
        "summary": build_important_reason(thread, connected_email),
        "needs_reply": True,
        "reason": "AI failed, but this looks like a human request or follow-up.",
        "confidence": 0.51,
    }


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 10000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 16000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    prompt = f"""
Write a concise, useful suggested reply for the latest inbound Gmail message.

Rules:
- Do not copy the sender's message.
- Do not use vague filler like "we received your email".
- Use the thread and related Gmail context when helpful.
- If exact details are missing, say what can be confirmed and ask at most one specific follow-up question.
- Work emails should use the Pharmacy Prep signature exactly. Personal emails should not use the Pharmacy Prep signature.
- Keep it practical and human, usually 80-180 words.

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific dashboard summary",
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
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail context:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        body = str(parsed.get("body", "") or "").strip()
        if not body or reply_needs_regeneration(body, latest_body, category):
            return None
        return {
            "title": str(parsed.get("title", "") or "").strip(),
            "summary": str(parsed.get("summary", "") or "").strip(),
            "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
            "body": body,
        }
    except Exception as error:
        print(f"[scan] AI compose failed: {error}", flush=True)
        return None


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    if category == "work":
        body = f"""Hello {greeting},

Thank you for your email. I will review the details connected to this request and follow up with the correct information shortly.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

Thank you for your message. I will review this and get back to you shortly.

Regards"""
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    thread_id = thread.get("thread_id", "")
    stored_item = get_catalog_item("emails", thread_id)
    latest_inbound_id = latest_inbound_message_id(thread, connected_email)
    latest_sort_ts = latest_inbound_sort_key(thread, connected_email)

    if latest_email_is_from_connected_account(thread, connected_email):
        if not stored_item:
            return None
        return {**stored_item, "thread_id": thread_id, "status": "Already Replied", "reply": None, "latest_inbound_id": latest_inbound_id, "sort_ts": latest_sort_ts or stored_item.get("sort_ts", "")}

    decision = _ai_relevance_decision(thread, connected_email) or _human_fallback_decision(thread, connected_email)
    if not decision or not decision.get("include"):
        return {
            "thread_id": thread_id,
            "category": stored_item.get("category", category_for_thread_strict(thread, connected_email)) if stored_item else category_for_thread_strict(thread, connected_email),
            "title": latest.get("subject", "Email") or "Email",
            "important_reason": (decision or {}).get("reason", "AI rejected as not needing a reply."),
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": True,
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_id,
            "sort_ts": latest_sort_ts,
            "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)},
            "reply": None,
        }

    category = decision.get("category") or category_for_thread_strict(thread, connected_email)
    label_id = personal_label_id if category == "personal" else work_label_id
    try:
        apply_label_to_thread_messages(service, thread, label_id)
    except Exception:
        pass

    title = decision.get("title") or latest.get("subject", "Important email") or "Important email"
    important_reason = decision.get("summary") or build_important_reason(thread, connected_email)
    action = get_thread_action(thread_action_key(thread, connected_email))
    status = "Already Replied" if action.get("action_type") == "frontend_reply_sent" else "Needs Reply"
    reply_sent_at = action.get("processed_at", "") if status == "Already Replied" else stored_item.get("reply_sent_at", "")
    reply = None

    if status != "Already Replied":
        cached_reply = stored_item.get("reply") if stored_item.get("latest_inbound_id") == latest_inbound_id else None
        latest_clean_body = clean_preview_text(latest.get("body", ""), 6000)
        if cached_reply and not reply_needs_regeneration(cached_reply.get("body", ""), latest_clean_body, category):
            reply = cached_reply
        else:
            queries = heuristic_context_queries_for_thread(thread, connected_email)
            extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=4) if queries else ""
            composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
            if composed:
                if composed.get("title"):
                    title = composed.get("title")
                if composed.get("summary") and not summary_is_generic(composed.get("summary", "")):
                    important_reason = composed.get("summary")
                reply = {"thread_id": thread_id, "mode": "thread_reply", "to": parseaddr(latest.get("from", ""))[1].strip(), "subject": composed.get("subject", ""), "body": composed.get("body", "")}
            if not reply:
                # Keep the row visible, but still provide a safe editable draft so it does not appear empty.
                reply = fallback_reply_for_thread(thread, connected_email, category)

    return {
        "thread_id": thread_id,
        "category": category,
        "title": title,
        "important_reason": important_reason,
        "status": status,
        "reply_sent_at": reply_sent_at,
        "latest_inbound_id": latest_inbound_id,
        "sort_ts": latest_sort_ts or stored_item.get("sort_ts", ""),
        "ai_screened": True,
        "screen_confidence": decision.get("confidence", 0),
        "screening_version": EMAIL_SCREENING_VERSION,
        "filtered_out": False,
        "original": {"from": latest.get("from", ""), "to": latest.get("to", ""), "date": latest.get("date", ""), "subject": latest.get("subject", ""), "body": clean_preview_text(latest.get("body", ""), 1800)},
        "reply": reply,
    }


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    # Do not hide AI-accepted rows through keyword checks. Only hard-hide clearly filtered rows.
    if item.get("is_renewal_request") or item.get("renewal_request"):
        return False
    if item.get("ai_screened") and not item.get("filtered_out"):
        return False
    text = _catalog_text(item)
    bad_terms = ["unsubscribe", "newsletter", "please moderate", "comment awaiting moderation", "mail delivery", "undeliverable", "security alert"]
    return any(term in text for term in bad_terms)


def _is_catalog_email_visible(item: Dict) -> bool:
    if _item_renewal_details(item):
        if not _item_is_on_or_after_scan_start(item):
            return False
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    # Version mismatches should not hide AI-accepted rows anymore.
    has_reply = bool(item.get("reply"))
    ai_accepted = bool(item.get("ai_screened")) and not item.get("filtered_out")
    handled = item.get("status") in ("Already Replied", "Suggestion Removed") and bool(item.get("important_reason"))
    return has_reply or ai_accepted or handled


def _renewal_norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _renewal_subject_matches(subject: str, body: str = "") -> bool:
    text = f"{subject}\n{body}".lower()
    return "account renewal request" in text and ("eprepstation" in text or "your e-mail address" in text or "your email address" in text)


def _renewal_extract_field(text: str, labels: List[str]) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").replace("\r", "\n").split("\n")]
    labels_norm = [label.lower() for label in labels]
    for i, line in enumerate(lines):
        low = line.lower().strip(" :\t")
        for label in labels_norm:
            if low == label:
                for nxt in lines[i+1:i+6]:
                    nl = nxt.lower().strip(" :\t")
                    if not nxt or nl in labels_norm or nl in ("question", "answer", "comments"):
                        continue
                    return nxt.strip(" -:\t")
            if low.startswith(label + ":") or low.startswith(label + " -"):
                return re.sub(r"^" + re.escape(label) + r"\s*[:\-]?\s*", "", line, flags=re.IGNORECASE).strip(" -:\t")
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^\n]+)", text or "", flags=re.IGNORECASE)
        if m:
            value = re.sub(r"\s+", " ", m.group(1)).strip(" -:\t")
            if value:
                return value
    return ""


def _renewal_extract_email(text: str) -> str:
    explicit = _renewal_extract_field(text, ["Your E-mail Address", "Your Email Address", "E-mail Address", "Email Address", "Email"])
    candidates = []
    if explicit:
        candidates.extend(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", explicit))
    candidates.extend(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text or ""))
    for email in candidates:
        low = email.lower()
        if "pharmacyprep.com" not in low and "eprepstation.com" not in low and "wordpress" not in low:
            return email.strip()
    return ""


def _renewal_clean_name(name: str, email: str = "") -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip(" -:\t")
    if name and "@" not in name and name.lower() not in {"no reply", "no-reply", "noreply", "wordpress", "eprepstation", "student", "customer"}:
        return name.title() if name.islower() or name.isupper() else name
    return infer_customer_name_from_email(email) or "Customer"


def _renewal_extract_details_from_thread(thread: Dict, connected_email: str = "") -> Optional[Dict]:
    for email in thread.get("emails", []):
        subject = email.get("subject", "") or ""
        body = email.get("body", "") or ""
        text = f"Subject: {subject}\n\n{body}"
        if not _renewal_subject_matches(subject, body):
            continue
        student_email = _renewal_extract_email(text)
        course = _renewal_extract_field(text, ["Exam you are taking", "Exam your are taking", "Course", "Course Name", "Exam"])
        name = _renewal_clean_name(_renewal_extract_field(text, ["Your Name", "Name"]), student_email)
        username = _renewal_extract_field(text, ["Your User Name", "Your Username", "Username", "User Name"])
        if not student_email or not course:
            return None
        return {"student_name": name, "student_email": student_email, "username": username, "course": re.sub(r"\s+", " ", course).strip(" -:\t"), "source_subject": subject, "source_from": email.get("from", ""), "source_to": email.get("to", ""), "source_date": email.get("date", ""), "source_thread_id": thread.get("thread_id", ""), "source_message_id": email.get("gmail_message_id", "")}
    return None


def _renewal_key(student_email: str, course: str) -> str:
    return hashlib.sha1(f"{_renewal_norm(student_email)}|{_renewal_norm(course)}".encode("utf-8")).hexdigest()[:16]


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


def _renewal_already_replied(service, student_email: str, course: str = "") -> bool:
    if not student_email:
        return False
    bits = ["renewal", "account", "extension", "course", "access"]
    queries = [
        f'in:sent newer_than:365d to:{student_email} (renewal OR extension OR account OR access)',
        f'in:sent newer_than:365d "{student_email}" (renewal OR extension OR account OR access)',
    ]
    if course:
        queries.append(f'in:sent newer_than:365d to:{student_email} "{course}"')
    for q in queries:
        try:
            if gmail_search_any(service, q, max_results=5):
                return True
        except Exception:
            continue
    return False


def _build_renewal_catalog_item(service, details: Dict, existing: Optional[Dict] = None) -> Dict:
    existing = existing or {}
    name = details.get("student_name") or "Customer"
    student_email = details.get("student_email", "")
    course = details.get("course", "")
    stable_id = _renewal_stable_thread_id(student_email, course)
    already_replied = existing.get("status") == "Already Replied" or bool(existing.get("reply_sent_at")) or _renewal_already_replied(service, student_email, course)
    reply = None if already_replied else {"thread_id": stable_id, "mode": "new_email", "to": student_email, "subject": f"Account renewal request - {course}", "body": _renewal_reply_body(name, course)}
    return {**existing, "thread_id": stable_id, "category": "work", "title": f"Account renewal request from {name}", "important_reason": f"{name} submitted an EprepStation account renewal request for {course}.", "status": "Already Replied" if already_replied else "Needs Reply", "reply_sent_at": existing.get("reply_sent_at", _safe_iso_now() if already_replied and not existing.get("reply_sent_at") else ""), "latest_inbound_id": details.get("source_message_id", existing.get("latest_inbound_id", "")), "sort_ts": email_date_to_sort_key(details.get("source_date", "")) or existing.get("sort_ts", ""), "ai_screened": True, "screen_confidence": 1.0, "screening_version": EMAIL_SCREENING_VERSION, "is_renewal_request": True, "renewal_request": True, "renewal_patch_version": RENEWAL_REQUEST_PATCH_VERSION, "renewal_details": {"student_name": name, "student_email": student_email, "username": details.get("username", ""), "course": course, "source_thread_id": details.get("source_thread_id", "")}, "filtered_out": False, "original": {"from": details.get("source_from", ""), "to": details.get("source_to", ""), "date": details.get("source_date", ""), "subject": details.get("source_subject", "Account Renewal Request Received from EprepStation.com"), "body": _renewal_original_body(student_email, course)}, "reply": reply}


def _item_renewal_details(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    details = item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
    looks_like = bool(item.get("is_renewal_request") or item.get("renewal_request")) or "account renewal request" in (item.get("title", "") + " " + original.get("subject", "")).lower()
    if not looks_like:
        return None
    student_email = details.get("student_email") or details.get("email") or reply.get("to") or _renewal_extract_email(original.get("body", ""))
    course = details.get("course") or _renewal_extract_field(original.get("body", ""), ["Course", "Exam you are taking", "Exam your are taking", "Exam"])
    name = details.get("student_name") or details.get("name") or re.sub(r"^account renewal request from\s+", "", item.get("title", ""), flags=re.IGNORECASE).strip()
    name = _renewal_clean_name(name, student_email)
    if not student_email or not course:
        return None
    return {"student_name": name, "student_email": student_email, "course": course, "username": details.get("username", "")}


def _normalize_catalog_renewals(catalog: Dict) -> Tuple[Dict, int]:
    bucket = catalog.setdefault("emails", {})
    new_bucket = {}
    changed = 0
    for key, item in list(bucket.items()):
        details = _item_renewal_details(item)
        if not details:
            new_bucket[key] = item
            continue
        stable = _renewal_stable_thread_id(details["student_email"], details["course"])
        normalized = {**item, "thread_id": stable, "category": "work", "is_renewal_request": True, "renewal_request": True, "filtered_out": False, "ai_screened": True, "screening_version": EMAIL_SCREENING_VERSION, "renewal_details": details}
        normalized.setdefault("original", {})["body"] = _renewal_original_body(details["student_email"], details["course"])
        if normalized.get("status") != "Already Replied" and not normalized.get("reply"):
            normalized["reply"] = {"thread_id": stable, "mode": "new_email", "to": details["student_email"], "subject": f"Account renewal request - {details['course']}", "body": _renewal_reply_body(details["student_name"], details["course"])}
        existing = new_bucket.get(stable)
        if existing:
            existing_replied = existing.get("status") == "Already Replied" or bool(existing.get("reply_sent_at"))
            normalized_replied = normalized.get("status") == "Already Replied" or bool(normalized.get("reply_sent_at"))
            if normalized_replied and not existing_replied:
                new_bucket[stable] = normalized
            elif normalized.get("sort_ts", "") > existing.get("sort_ts", "") and existing_replied == normalized_replied:
                new_bucket[stable] = normalized
            changed += 1
        else:
            new_bucket[stable] = normalized
            if key != stable:
                changed += 1
    if changed:
        catalog["emails"] = new_bucket
    return catalog, changed


def _scan_date_clause(force_full: bool, catalog: Dict) -> Tuple[str, str]:
    if force_full:
        start_dt = SCAN_START_DT
    else:
        last = catalog.get("meta", {}).get("last_successful_scan_at", "")
        start_dt = datetime.now() - timedelta(days=REFRESH_SCAN_DAYS)
        try:
            if last:
                parsed = datetime.fromisoformat(last[:19]) - timedelta(days=7)
                if parsed < start_dt:
                    start_dt = parsed
        except Exception:
            pass
        if start_dt < SCAN_START_DT:
            start_dt = SCAN_START_DT
    return f"after:{(start_dt - timedelta(days=1)).strftime('%Y/%m/%d')}", start_dt.isoformat(timespec="seconds")


def _clean_email_scan_queries(date_clause: str, connected_email: str, force_full: bool) -> List[str]:
    base = f'in:anywhere {date_clause} -in:spam -in:trash -category:promotions -category:social'
    inbox_base = f'{date_clause} in:inbox -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base += f' -from:{connected_email}'
        inbox_base += f' -from:{connected_email}'
    return [
        inbox_base,
        f'{base} ("?" OR please OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know")',
        f'{base} (PEBC OR exam OR course OR class OR login OR access OR account OR renewal OR extension OR order OR invoice OR payment OR refund OR registration)',
        f'{base} ("not received" OR "still waiting" OR "follow up" OR "checking in" OR "send me" OR help OR issue OR problem)',
        base if force_full else f'{base} newer_than:{max(1, REFRESH_SCAN_DAYS)}d',
    ]


def perform_gmail_scan(force_full: bool = False) -> Dict:
    service = get_gmail_service()
    connected_email = get_connected_email(service)
    personal_label_id = get_or_create_label(service, PERSONAL_LABEL)
    work_label_id = get_or_create_label(service, WORK_LABEL)
    catalog = get_dashboard_catalog()
    catalog.setdefault("meta", {})
    catalog.setdefault("orders", {})
    catalog.setdefault("emails", {})
    catalog, renewal_changed = _normalize_catalog_renewals(catalog)
    date_clause, scan_start_used = _scan_date_clause(force_full, catalog)
    debug = _clean_debug_template()
    debug["scan_start"] = scan_start_used
    debug["force_full"] = force_full
    print(f"[scan] starting CLEAN AI-first scan | force_full={force_full} | {date_clause}", flush=True)

    order_thread_ids = _collect_thread_ids(service, _order_scan_queries(date_clause), per_query_limit=200, total_limit=MAX_ORDER_THREADS_PER_SCAN)
    email_thread_ids = _collect_thread_ids(service, _clean_email_scan_queries(date_clause, connected_email, force_full), per_query_limit=FULL_SCAN_MAX_THREADS if force_full else 400, total_limit=FULL_SCAN_MAX_THREADS if force_full else 700)
    debug["order_threads_found"] = len(order_thread_ids)
    debug["email_threads_found"] = len(email_thread_ids)
    print(f"[scan] Gmail returned orders={len(order_thread_ids)}, candidate emails={len(email_thread_ids)}", flush=True)

    processed_order_threads = set()
    auto_orders_sent = 0
    order_replies_waiting = 0
    skipped_failed_orders = 0

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
            auto_orders_sent += 1 if did_send else 0
            order_replies_waiting += 1 if order_item.get("reply") else 0
            _upsert_order_in_catalog(catalog, thread_id, order_item)
            debug["orders_upserted"] += 1
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug.setdefault("errors", []).append(f"order {thread_id}: {error}")

    ai_count = 0
    for thread_id in email_thread_ids:
        if thread_id in processed_order_threads:
            continue
        try:
            thread = read_thread(service, thread_id)
            debug["threads_read"] += 1
            if not _thread_is_on_or_after_scan_start(thread, connected_email):
                debug["skipped_old_date"] += 1
                continue
            if latest_email_is_from_connected_account(thread, connected_email):
                debug["skipped_latest_from_us"] += 1
                continue
            if get_best_order_email_text(thread):
                debug["skipped_order_notification"] += 1
                order_item = build_order_item(service, thread, connected_email)
                if order_item:
                    order_item, did_send = _auto_send_order_if_safe(service, thread, connected_email, order_item)
                    auto_orders_sent += 1 if did_send else 0
                    order_replies_waiting += 1 if order_item.get("reply") else 0
                    _upsert_order_in_catalog(catalog, thread_id, order_item)
                continue
            renewal_details = _renewal_extract_details_from_thread(thread, connected_email)
            if renewal_details:
                stable_id = _renewal_stable_thread_id(renewal_details["student_email"], renewal_details["course"])
                catalog.setdefault("emails", {})[stable_id] = _build_renewal_catalog_item(service, renewal_details, existing=catalog.get("emails", {}).get(stable_id, {}))
                debug["renewals_upserted"] += 1
                continue
            if ai_count >= MAX_AI_SCREENINGS_PER_SCAN and not get_catalog_item("emails", thread_id):
                _append_debug(debug, "rejected_examples", _debug_example(thread, connected_email, "AI cap reached"))
                continue
            item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id)
            ai_count += 1
            debug["ai_checked"] = ai_count
            if item:
                _upsert_email_in_catalog(catalog, thread_id, item)
                if item.get("filtered_out") or item.get("status") == "Filtered Out":
                    debug["ai_rejected"] += 1
                    _append_debug(debug, "rejected_examples", _debug_example(thread, connected_email, item.get("important_reason", "filtered"), item))
                else:
                    debug["ai_accepted"] += 1
                    if item.get("reply"):
                        debug["reply_composed"] += 1
                    else:
                        debug["reply_missing_but_visible"] += 1
                    _append_debug(debug, "accepted_examples", _debug_example(thread, connected_email, item.get("important_reason", "accepted"), item))
        except GmailAuthRequired:
            raise
        except Exception as error:
            debug.setdefault("errors", []).append(f"email {thread_id}: {error}")
            continue

    catalog, renewal_changed2 = _normalize_catalog_renewals(catalog)
    now = _safe_iso_now()
    catalog["meta"] = {**catalog.get("meta", {}), "connected_email": connected_email, "last_successful_scan_at": now, "last_scan_start": scan_start_used, "scan_window": f"{SCAN_START_DISPLAY} onward", "scan_start_date": SCAN_START_DT.strftime("%Y-%m-%d"), "email_screening_version": EMAIL_SCREENING_VERSION}
    save_dashboard_catalog(catalog)
    _save_clean_debug(debug)
    invalidate_dashboard_cache()

    orders = [item for item in catalog.get("orders", {}).values() if _is_catalog_order_visible(item)]
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)
    briefing = build_daily_briefing(connected_email, orders, emails)
    payload = build_dashboard_payload(force_refresh=True)
    payload["briefing"] = briefing
    payload["scan_summary"] = {
        "scan_start": scan_start_used,
        "scan_window": f"{SCAN_START_DISPLAY} onward",
        "orders_checked": len(order_thread_ids),
        "emails_checked": len(email_thread_ids),
        "email_threads_read": debug.get("threads_read", 0),
        "emails_accepted": debug.get("ai_accepted", 0),
        "emails_rejected": debug.get("ai_rejected", 0),
        "renewal_added": debug.get("renewals_upserted", 0),
        "auto_orders_sent": auto_orders_sent,
        "order_replies_waiting": order_replies_waiting,
        "failed_orders_skipped": skipped_failed_orders,
        "suggested_replies": len([email for email in emails if email.get("reply")]),
        "ai_screenings_used": ai_count,
        "debug_file": str(SCAN_DEBUG_FILE),
    }
    print(f"[scan] complete | read={debug.get('threads_read',0)} | visible={len(emails)} | work={len([e for e in emails if e.get('category')=='work'])} | personal={len([e for e in emails if e.get('category')=='personal'])} | accepted={debug.get('ai_accepted',0)} | renewals={debug.get('renewals_upserted',0)} | debug={SCAN_DEBUG_FILE}", flush=True)
    return payload


def api_scan():
    try:
        body = request.get_json(silent=True) or {}
        payload = perform_gmail_scan(force_full=bool(body.get("force_full", False)))
        summary = payload.get("scan_summary", {})
        return jsonify({
            "ok": True,
            "message": "Scan complete.",
            "order_replies_waiting": summary.get("order_replies_waiting", 0),
            "failed_orders_skipped": summary.get("failed_orders_skipped", 0),
            "suggested_replies": summary.get("suggested_replies", 0),
            "auto_orders_sent": summary.get("auto_orders_sent", 0),
            "emails_checked": summary.get("emails_checked", 0),
            "email_threads_read": summary.get("email_threads_read", 0),
            "emails_accepted": summary.get("emails_accepted", 0),
            "emails_rejected": summary.get("emails_rejected", 0),
            "renewal_added": summary.get("renewal_added", 0),
            "ai_screenings_used": summary.get("ai_screenings_used", 0),
            "debug_file": summary.get("debug_file", ""),
            "scan_start": summary.get("scan_start", ""),
            "scan_window": summary.get("scan_window", f"{SCAN_START_DISPLAY} onward"),
        })
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_scan"] = api_scan


_previous_send_for_clean_patch = app.view_functions.get("api_send_reply")
def api_send_reply(thread_id: str):
    try:
        item = get_catalog_item("emails", thread_id)
        details = _item_renewal_details(item)
        if details and item.get("reply"):
            if not get_automation_settings().get("auto_reply_enabled", True):
                return jsonify({"ok": False, "error": "Auto Reply is off. Turn it on before sending replies."}), 403
            service = get_gmail_service()
            body_json = request.get_json(silent=True) or {}
            reply = item.get("reply", {})
            subject = (body_json.get("subject") or reply.get("subject") or f"Account renewal request - {details['course']}").strip()
            reply_body = (body_json.get("body") or reply.get("body") or _renewal_reply_body(details["student_name"], details["course"])).strip()
            sent = send_new_email(service, details["student_email"], subject, reply_body)
            upsert_catalog_item("emails", thread_id, {**item, "status": "Already Replied", "reply": None, "reply_sent_at": _safe_iso_now(), "sent_message_id": sent.get("id", "")})
            mark_thread_action_processed(thread_id, "frontend_reply_sent", sent.get("id", ""), renewal_request=True)
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})
        return _previous_send_for_clean_patch(thread_id)
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_send_reply"] = api_send_reply



# ---------------------------------------------------------------------
# FINAL SMALL PATCH: professional replies, stricter Work/Personal split,
# editable recipient, and stronger Gmail-context lookups.
# ---------------------------------------------------------------------
EMAIL_SCREENING_VERSION = "2026-06-clean-ai-context-ui-v2"


def _plain_text_for_category(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _thread_category_text(thread: Dict, connected_email: str = "") -> str:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _plain_text_for_category("\n".join([
        latest.get("subject", ""),
        latest.get("from", ""),
        latest.get("to", ""),
        latest.get("body", ""),
        combined_thread_text(thread),
    ]))


def _catalog_category_text(item: Dict) -> str:
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    return _plain_text_for_category("\n".join([
        item.get("title", ""),
        item.get("important_reason", ""),
        original.get("from", ""),
        original.get("to", ""),
        original.get("subject", ""),
        original.get("body", ""),
    ]))


def _text_is_pharmacy_prep_related(text: str) -> bool:
    text = _plain_text_for_category(text)
    if not text:
        return False

    # Agreements/contracts/documents without Pharmacy Prep context are personal.
    personal_document_terms = [
        "agreement", "contract", "lease", "tenant", "landlord", "mortgage", "bank", "insurance",
        "signature", "signed document", "docusign", "adobe sign", "legal document", "policy document",
    ]
    strong_work_terms = [
        "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "eprepstation",
        "pebc", "evaluating exam", "qualifying exam", "pharmacist qualifying", "pharmacy technician",
        "osce", "ospe", "fpgee", "opra", "naplex", "qbank", "mock exam", "mock exams",
        "prep course", "pharmacy prep course", "course renewal", "account renewal request",
    ]
    if any(term in text for term in strong_work_terms):
        return True
    if any(term in text for term in personal_document_terms):
        return False

    # Student/course language is work only when it clearly resembles Pharmacy Prep support.
    student_terms = ["student", "students", "enrolled", "enrol", "enroll", "registration", "study plan"]
    support_terms = [
        "course", "class", "lecture", "recording", "notes", "books", "book", "login", "access",
        "password", "extension", "renewal", "exam", "q&a", "question bank", "mock", "schedule",
    ]
    if any(term in text for term in student_terms) and any(term in text for term in support_terms):
        return True

    # Order/payment is work only if the surrounding text points to Pharmacy Prep/course products.
    commerce_terms = ["order #", "order number", "new order", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer"]
    product_terms = ["course", "exam", "qbank", "mock", "book", "pharmacy", "pebc", "prep", "eprepstation"]
    if any(term in text for term in commerce_terms) and any(term in text for term in product_terms):
        return True

    return False


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    return _text_is_pharmacy_prep_related(_thread_category_text(thread, connected_email))


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _category_for_catalog_item(item: Dict) -> str:
    if _item_renewal_details(item):
        return "work"
    return "work" if _text_is_pharmacy_prep_related(_catalog_category_text(item)) else "personal"


def _clean_email_address(value: str) -> str:
    raw = str(value or "").strip()
    name, email = parseaddr(raw)
    candidate = (email or raw).strip()
    if re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", candidate):
        return candidate
    return ""


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return body
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    cleaned_lines = []
    for line in body.split("\n"):
        line = line.rstrip()
        # Remove AI-looking bullet/list markers while keeping the content.
        line = re.sub(r"^\s*[-*•–—]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        # Avoid em-dash-heavy AI style in normal sentences.
        line = line.replace(" — ", ", ").replace(" – ", ", ")
        cleaned_lines.append(line)
    body = "\n".join(cleaned_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # Keep work replies signed cleanly when the AI forgets the signature.
    if category == "work" and "Pharmacy Prep" not in body:
        body = body.rstrip() + "\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    return body


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = (reply_body or "").strip()
    latest = clean_preview_text(latest_body or "", 6000).strip()
    body_lower = body.lower()
    if len(body.split()) < 10:
        return True
    bad_fillers = [
        "we received your message and will get back to you",
        "we received your email and will get back to you",
        "thank you for your email. we will review your request",
        "we will review your request and get back to you shortly",
    ]
    if any(phrase in body_lower for phrase in bad_fillers):
        return True
    if latest and body_lower.startswith(latest.lower()[:80]):
        return True
    if latest and copied_sequence_found(body, latest, sequence_len=14):
        return True
    return False


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    text = f"{subject}\n{body}"

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})",
        r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
    ]:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in phrases:
                phrases.append(value)

    name_bits = []
    clean_name = re.sub(r"[^A-Za-z\s]", " ", sender_name or "").strip()
    for part in clean_name.split():
        if len(part) >= 3 and part.lower() not in ("pharmacy", "prep", "student", "customer"):
            name_bits.append(part)
    if not name_bits and sender_email:
        inferred = infer_customer_name_from_email(sender_email) or ""
        name_bits.extend([p for p in inferred.split() if len(p) >= 3])

    keyword_pool = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", text):
        token_l = token.lower()
        if token_l in {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should"}:
            continue
        if token_l not in keyword_pool:
            keyword_pool.append(token_l)
        if len(keyword_pool) >= 10:
            break

    queries = []
    if sender_email:
        # Main Gmail API context: all prior communication with this person, both directions.
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:anywhere "{sender_email}"',
            f'in:anywhere ({sender_email}) ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access OR renewal OR course OR PEBC)',
        ])
    for name_part in name_bits[:3]:
        queries.append(f'in:anywhere "{name_part}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:5]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keyword_pool[:8]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:18]


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 10000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 16000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    prompt = f"""
Write a professional Gmail reply for the latest inbound message.

Style rules:
- Write like a real office/admin person, not like AI.
- Do not use bullet points, numbered lists, markdown, bold text, headings, tables, or dash-heavy phrasing.
- Use short professional paragraphs.
- Do not copy the sender's message back to them.
- Do not use generic filler such as "we received your email" or "we will review your request" unless that is genuinely the only possible answer.
- Use the Gmail API context below before answering. For example, if the sender asks about an order number, login, payment, receipt, renewal, or prior course detail, use the related Gmail context and stored order context to find the matching detail.
- If the exact answer is not available in the provided Gmail context, say what can be confirmed and ask one specific follow-up question.
- For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not include the Pharmacy Prep signature.
- Keep the reply natural and polished, usually 70-160 words unless exact details require more.

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific dashboard summary mentioning who is asking and the concrete topic/context",
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
Latest inbound subject: {latest.get('subject', '')}
Latest inbound body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail API context from searches by sender/order/name/keywords:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if not isinstance(parsed, dict):
            return None
        body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
        if not body or reply_needs_regeneration(body, latest_body, category):
            return None
        return {
            "title": str(parsed.get("title", "") or "").strip(),
            "summary": str(parsed.get("summary", "") or "").strip(),
            "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
            "body": body,
        }
    except Exception as error:
        print(f"[scan] AI compose failed: {error}", flush=True)
        return None


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    if category == "work":
        body = f"""Hello {greeting},

Thank you for your email. I will check the related account, order, or course details and reply with the correct information.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

Thank you for your message. I will take a look and get back to you.

Regards"""
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


_previous_build_dashboard_payload_context_ui = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    payload = _previous_build_dashboard_payload_context_ui(force_refresh=force_refresh)
    emails = []
    for item in payload.get("emails", []) or []:
        if isinstance(item, dict):
            updated = deepcopy(item)
            updated["category"] = _category_for_catalog_item(updated)
            emails.append(updated)
    payload["emails"] = emails
    payload["pending_replies"] = [item["thread_id"] for item in emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "work_emails": len([email for email in emails if email.get("category") == "work"]),
        "personal_emails": len([email for email in emails if email.get("category") == "personal"]),
    }
    try:
        payload["briefing"] = build_daily_briefing(payload.get("connected_email", DEFAULT_CONNECTED_EMAIL), payload.get("orders", []), emails)
    except Exception:
        pass
    return payload


def _send_thread_reply_with_to(service, thread: Dict, connected_email: str, to_email: str, subject: str, body: str) -> Dict:
    if not thread.get("emails"):
        raise ValueError("Thread has no emails.")
    if latest_email_is_from_connected_account(thread, connected_email):
        raise ValueError("Latest message is already from your Gmail account.")
    latest_email = latest_inbound_email_for_dashboard(thread, connected_email)
    resolved_to = _clean_email_address(to_email) or _clean_email_address(latest_email.get("from", ""))
    if not resolved_to:
        raise ValueError("Could not find recipient email address.")
    clean_subject = (subject or latest_email.get("subject", "Your email")).strip()
    if not clean_subject.lower().startswith("re:"):
        clean_subject = "Re: " + clean_subject
    clean_body = _professionalize_reply_body((body or "").strip(), category_for_thread_strict(thread, connected_email))
    if not clean_body:
        raise ValueError("Reply body is empty.")
    message = MIMEText(clean_body, "plain", "utf-8")
    message["To"] = resolved_to
    message["Subject"] = clean_subject
    if latest_email.get("message_id_header"):
        message["In-Reply-To"] = latest_email["message_id_header"]
        references = latest_email.get("references", "")
        message["References"] = (references + " " + latest_email["message_id_header"]).strip() if references else latest_email["message_id_header"]
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"threadId": thread.get("thread_id", ""), "raw": raw_message}).execute()


_previous_api_send_reply_context_ui = app.view_functions.get("api_send_reply")
def api_send_reply(thread_id: str):
    try:
        if not get_automation_settings().get("auto_reply_enabled", True):
            return jsonify({"ok": False, "error": "Auto Reply is off. Turn it on before sending replies."}), 403
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        body_json = request.get_json(silent=True) or {}
        to_override = _clean_email_address(body_json.get("to", ""))
        subject = (body_json.get("subject") or "").strip()
        reply_body = (body_json.get("body") or "").strip()

        item = get_catalog_item("emails", thread_id)
        details = _item_renewal_details(item)
        if details and item.get("reply"):
            reply = item.get("reply", {})
            to_email = to_override or details.get("student_email") or reply.get("to")
            if not _clean_email_address(to_email):
                raise ValueError("Please enter a valid recipient email address.")
            subject = subject or reply.get("subject") or f"Account renewal request - {details['course']}"
            reply_body = _professionalize_reply_body(reply_body or reply.get("body") or _renewal_reply_body(details["student_name"], details["course"]), "work")
            sent = send_new_email(service, _clean_email_address(to_email), subject, reply_body)
            upsert_catalog_item("emails", thread_id, {**item, "status": "Already Replied", "reply": None, "reply_sent_at": _safe_iso_now(), "sent_message_id": sent.get("id", "")})
            mark_thread_action_processed(thread_id, "frontend_reply_sent", sent.get("id", ""), renewal_request=True)
            invalidate_dashboard_cache()
            return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})

        thread = read_thread(service, thread_id)
        thread_key = thread_action_key(thread, connected_email)
        order_text = get_best_order_email_text(thread)
        if order_text:
            order_number = extract_order_number(order_text) or "Unknown"
            customer_email = to_override or extract_customer_email(order_text, connected_email)
            if not _clean_email_address(customer_email):
                raise ValueError("Please enter a valid recipient email address.")
            if was_thread_manually_replied(service, thread, connected_email) or was_order_message_already_sent(service, customer_email, order_number):
                sent = {"id": "already-replied"}
                upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": best_customer_name(order_text, customer_email), "status": "Already Replied"})
                upsert_catalog_item("orders", thread_id, {"status": "Already Replied", "reply": None, "reply_sent_at": _safe_iso_now()})
            else:
                if not subject or not reply_body:
                    customer_name = best_customer_name(order_text, customer_email)
                    default_subject, default_body = build_order_welcome_email(customer_name, order_number)
                    subject = subject or default_subject
                    reply_body = reply_body or default_body
                sent = send_new_email(service, customer_email, subject, _professionalize_reply_body(reply_body, "work"))
                upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": best_customer_name(order_text, customer_email), "status": "Sent from Dashboard", "sent_message_id": sent.get("id", "")})
                upsert_catalog_item("orders", thread_id, {"status": "Already Replied", "reply": None, "reply_sent_at": _safe_iso_now()})
        else:
            sent = _send_thread_reply_with_to(service, thread, connected_email, to_override, subject, reply_body)
            upsert_catalog_item("emails", thread_id, {"status": "Already Replied", "reply": None, "reply_sent_at": _safe_iso_now()})
        mark_thread_action_processed(thread_key, "frontend_reply_sent", sent.get("id", ""))
        invalidate_dashboard_cache()
        return jsonify({"ok": True, "message": "Email sent successfully.", "sent": sent})
    except GmailAuthRequired:
        return _json_gmail_auth_required()
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500
app.view_functions["api_send_reply"] = api_send_reply


# ---------------------------------------------------------------------
# FINAL SMALL PATCH: renewal recipient + ignore EprepStation support question notices
# ---------------------------------------------------------------------
def _is_eprepstation_support_question_text(value: str) -> bool:
    text = str(value or "").lower()
    return (
        "new support question has been submitted at eprepstation.com" in text
        or ("new support question" in text and "eprepstation" in text and "submitted" in text)
    )


def _is_ignored_eprepstation_support_question_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _is_eprepstation_support_question_text("\n".join([
        latest.get("subject", ""),
        latest.get("from", ""),
        latest.get("body", ""),
        combined_thread_text(thread),
    ]))


def _is_ignored_eprepstation_support_question_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    return _is_eprepstation_support_question_text("\n".join([
        item.get("title", ""),
        item.get("important_reason", ""),
        original.get("subject", ""),
        original.get("from", ""),
        original.get("body", ""),
    ]))


_previous_build_general_email_item_support_ignore = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _is_ignored_eprepstation_support_question_thread(thread, connected_email):
        latest = latest_inbound_email_for_dashboard(thread, connected_email)
        return {
            "thread_id": thread.get("thread_id", ""),
            "category": "work",
            "title": latest.get("subject", "New Support Question"),
            "important_reason": "Ignored EprepStation support-question notification.",
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": False,
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_message_id(thread, connected_email),
            "sort_ts": latest_inbound_sort_key(thread, connected_email),
            "original": {
                "from": latest.get("from", ""),
                "to": latest.get("to", ""),
                "date": latest.get("date", ""),
                "subject": latest.get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800),
            },
            "reply": None,
        }
    return _previous_build_general_email_item_support_ignore(service, thread, connected_email, personal_label_id, work_label_id)


_previous_is_catalog_email_visible_support_ignore = _is_catalog_email_visible
def _is_catalog_email_visible(item: Dict) -> bool:
    if _is_ignored_eprepstation_support_question_item(item):
        return False
    return _previous_is_catalog_email_visible_support_ignore(item)


_previous_normalize_catalog_renewals_recipient = _normalize_catalog_renewals
def _normalize_catalog_renewals(catalog: Dict) -> Tuple[Dict, int]:
    catalog, changed = _previous_normalize_catalog_renewals_recipient(catalog)
    bucket = catalog.setdefault("emails", {})
    for key, item in list(bucket.items()):
        details = _item_renewal_details(item)
        if not details:
            continue
        student_email = _clean_email_address(details.get("student_email", ""))
        course = details.get("course", "")
        name = details.get("student_name", "Customer")
        if not student_email:
            continue
        stable = _renewal_stable_thread_id(student_email, course)
        item = {**item, "thread_id": stable, "category": "work", "is_renewal_request": True, "renewal_request": True}
        item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details, "student_email": student_email}
        if item.get("status") != "Already Replied":
            reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
            item["reply"] = {
                **reply,
                "thread_id": stable,
                "mode": "new_email",
                "to": student_email,
                "subject": reply.get("subject") or f"Account renewal request - {course}",
                "body": reply.get("body") or _renewal_reply_body(name, course),
            }
        bucket[stable] = item
        if key != stable and key in bucket:
            del bucket[key]
            changed += 1
    if changed:
        catalog["emails"] = bucket
    return catalog, changed


_previous_build_dashboard_payload_recipient_patch = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    # Normalize renewal rows first so the frontend To field receives the actual student email, not EprepStation/no-reply.
    catalog = get_dashboard_catalog()
    catalog, changed = _normalize_catalog_renewals(catalog)
    if changed:
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    payload = _previous_build_dashboard_payload_recipient_patch(force_refresh=force_refresh)
    for email in payload.get("emails", []) or []:
        details = _item_renewal_details(email)
        if details and email.get("reply"):
            email["reply"]["to"] = _clean_email_address(details.get("student_email", "")) or email["reply"].get("to", "")
    return payload



# ---------------------------------------------------------------------
# FINAL PATCH: Renewals tab, strict Work/Personal, ignore all New Question notices,
# and deeper Gmail API context for each reply.
# ---------------------------------------------------------------------
EMAIL_SCREENING_VERSION = "2026-06-renewals-personal-context-v1"
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "260"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "160"))
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "24"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "6"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "18"))


def _is_new_question_submitted_text(value: str) -> bool:
    """Ignore WordPress/EprepStation/PharmacyPrep form notifications like
    'New question submitted' and 'New question submited'. These are system notices,
    not student emails to reply from the dashboard.
    """
    text = str(value or "").lower()
    text = re.sub(r"\s+", " ", text)
    if "new support question has been submitted at eprepstation.com" in text:
        return True
    if "new support question" in text and "submitted" in text and "eprepstation" in text:
        return True
    if "new question submitted" in text or "new question submited" in text:
        return True
    if "new question has been submitted" in text or "new question has been submited" in text:
        return True
    # Common form/WordPress variants.
    if "question submitted" in text and ("wordpress" in text or "pharmacyprep" in text or "eprepstation" in text):
        return True
    return False


def _is_ignored_new_question_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _is_new_question_submitted_text("\n".join([
        latest.get("subject", ""),
        latest.get("from", ""),
        latest.get("to", ""),
        latest.get("body", ""),
        combined_thread_text(thread),
    ]))


def _is_ignored_new_question_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    return _is_new_question_submitted_text("\n".join([
        item.get("title", ""),
        item.get("important_reason", ""),
        item.get("status", ""),
        original.get("from", ""),
        original.get("to", ""),
        original.get("subject", ""),
        original.get("body", ""),
    ]))


def _thread_body_context_only(thread: Dict, connected_email: str = "") -> str:
    """Use subjects/bodies/signatures only. Do not use To: success@pharmacyprep.com,
    otherwise every inbound email to the business inbox becomes Work.
    """
    parts = []
    for email in thread.get("emails", []) or []:
        parts.append(str(email.get("subject", "") or ""))
        parts.append(str(email.get("body", "") or ""))
    return "\n".join(parts).lower()


def _text_is_pharmacy_prep_related(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower().strip()
    sender_domains = ["pharmacyprep.com", "eprepstation.com"]
    if any(domain in sender for domain in sender_domains):
        return True

    direct_terms = [
        "pharmacy prep", "pharmacyprep", "eprepstation", "eprep station",
        "www.pharmacyprep.com", "416-223-prep", "416-223-7737", "647-221-0457",
        "pebc", "evaluating exam", "qualifying exam", "pebc exam", "pebc exams",
        "osce", "ospe", "fpgee", "opra", "naplex", "qbank", "mock exam", "mock exams",
        "pharmacist qualifying", "pharmacy technician", "technician mcq", "technician ospe",
        "prep course", "prep courses", "exam prep station", "online exam prep station",
        "course login", "course access", "registered courses", "study plan", "course renewal",
        "account renewal request", "eprepstation account renewal", "home study plus online",
        "clinical pharmacology book", "evaluating review book", "qualifying review book",
    ]
    if any(term in text for term in direct_terms):
        return True

    # Only use generic student/course/order words when they are tied to exam/prep context.
    exam_context = any(term in text for term in ["exam", "mcq", "osce", "ospe", "qbank", "mock", "lecture", "recording", "notes", "study plan", "enroll", "enrol", "registration"])
    student_context = any(term in text for term in ["student", "course", "class", "book", "login", "access", "renewal", "extension"])
    if exam_context and student_context:
        return True
    return False


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    return _text_is_pharmacy_prep_related(_thread_body_context_only(thread, connected_email), sender)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _is_renewal_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_renewal_request") or item.get("renewal_request"):
        return True
    details = item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    text = "\n".join([
        item.get("title", ""), item.get("important_reason", ""),
        original.get("subject", ""), original.get("body", ""),
        str(details.get("course", "")), str(details.get("student_email", "")),
    ]).lower()
    return "account renewal request" in text and ("eprepstation" in text or "course" in text or "student email" in text)


def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if _is_renewal_item(item):
        return True
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1].lower().strip()
    # Avoid original.to because nearly every inbound message is to success@pharmacyprep.com.
    text = "\n".join([
        item.get("title", ""),
        item.get("important_reason", ""),
        original.get("from", ""),
        original.get("subject", ""),
        original.get("body", ""),
    ])
    return _text_is_pharmacy_prep_related(text, sender)


_previous_build_general_email_item_newquestion = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _is_ignored_new_question_thread(thread, connected_email):
        latest = latest_inbound_email_for_dashboard(thread, connected_email)
        return {
            "thread_id": thread.get("thread_id", ""),
            "category": "work",
            "title": latest.get("subject", "New question submitted"),
            "important_reason": "Ignored New Question submitted system notification.",
            "status": "Filtered Out",
            "filtered_out": True,
            "ai_screened": False,
            "screening_version": EMAIL_SCREENING_VERSION,
            "latest_inbound_id": latest_inbound_message_id(thread, connected_email),
            "sort_ts": latest_inbound_sort_key(thread, connected_email),
            "original": {
                "from": latest.get("from", ""),
                "to": latest.get("to", ""),
                "date": latest.get("date", ""),
                "subject": latest.get("subject", ""),
                "body": clean_preview_text(latest.get("body", ""), 1800),
            },
            "reply": None,
        }
    item = _previous_build_general_email_item_newquestion(service, thread, connected_email, personal_label_id, work_label_id)
    if item and not item.get("filtered_out") and not _is_renewal_item(item):
        item["category"] = category_for_thread_strict(thread, connected_email)
    return item


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    text = f"{subject}\n{body}"

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})",
        r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
    ]:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in phrases:
                phrases.append(value)

    name_bits = []
    clean_name = re.sub(r"[^A-Za-z\s]", " ", sender_name or "").strip()
    for part in clean_name.split():
        if len(part) >= 3 and part.lower() not in ("pharmacy", "prep", "student", "customer", "support", "question"):
            name_bits.append(part)
    if sender_email:
        inferred = infer_customer_name_from_email(sender_email) or ""
        for part in inferred.split():
            if len(part) >= 3 and part not in name_bits:
                name_bits.append(part)
        local = sender_email.split("@", 1)[0]
        for part in re.split(r"[._+\-0-9]+", local):
            if len(part) >= 3 and part.lower() not in [p.lower() for p in name_bits]:
                name_bits.append(part)

    keyword_pool = []
    stop = {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should", "there", "about", "from", "your", "with", "this"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", text):
        token_l = token.lower()
        if token_l in stop:
            continue
        if token_l not in keyword_pool:
            keyword_pool.append(token_l)
        if len(keyword_pool) >= 14:
            break

    context_terms = [
        "Order #", "New Order", "invoice", "receipt", "payment", "e-transfer", "etransfer",
        "login", "access", "password", "renewal", "extension", "course", "PEBC", "exam",
        "book", "recording", "notes", "schedule", "registration", "enrolled", "enrollment",
    ]
    context_or = " OR ".join([f'\"{term}\"' if " " in term or "#" in term else term for term in context_terms])

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:sent "{sender_email}"',
            f'in:anywhere "{sender_email}"',
            f'in:anywhere ({sender_email}) ({context_or})',
            f'in:sent to:{sender_email} ({context_or})',
            f'in:anywhere from:{sender_email} ({context_or})',
            f'in:anywhere to:{sender_email} ({context_or})',
        ])
    for name_part in name_bits[:5]:
        queries.extend([
            f'in:anywhere "{name_part}"',
            f'in:sent "{name_part}"',
        ])
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:6]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keyword_pool[:10]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = None) -> str:
    """Use Gmail API searches more deeply for reply context: sender both directions,
    sent mail, names, order numbers, invoices, login/access, payments, courses, and keywords.
    """
    if max_threads_per_query is None:
        max_threads_per_query = GMAIL_CONTEXT_THREADS_PER_QUERY
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL API CONTEXT ================\n\n"
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
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
                    f"Thread content:\n{format_thread_for_ai(thread)[:9000]}"
                )
                if len(context_blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


_previous_catalog_visible_newquestion = _is_catalog_email_visible
def _is_catalog_email_visible(item: Dict) -> bool:
    if _is_ignored_new_question_item(item):
        return False
    return _previous_catalog_visible_newquestion(item)


_previous_build_dashboard_payload_renewals_tab = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    # Remove old New Question submitted system notices from the saved catalog and normalize categories.
    catalog = get_dashboard_catalog()
    changed = False
    emails_bucket = catalog.setdefault("emails", {})
    if isinstance(emails_bucket, dict):
        for key in list(emails_bucket.keys()):
            item = emails_bucket.get(key, {})
            if _is_ignored_new_question_item(item):
                del emails_bucket[key]
                changed = True
                continue
            if isinstance(item, dict) and not _is_renewal_item(item) and item.get("category") in ("work", "personal"):
                new_category = "work" if _is_item_pharmacy_prep_related(item) else "personal"
                if item.get("category") != new_category:
                    item["category"] = new_category
                    emails_bucket[key] = item
                    changed = True
    if changed:
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()

    payload = _previous_build_dashboard_payload_renewals_tab(force_refresh=force_refresh)
    cleaned_emails = []
    renewal_count = 0
    for email in payload.get("emails", []) or []:
        if _is_ignored_new_question_item(email):
            continue
        if _is_renewal_item(email):
            email["category"] = "work"
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            renewal_count += 1
        else:
            email["category"] = "work" if _is_item_pharmacy_prep_related(email) else "personal"
        cleaned_emails.append(email)

    payload["emails"] = cleaned_emails
    payload["renewals"] = [email for email in cleaned_emails if _is_renewal_item(email)]
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "renewal_emails": renewal_count,
        "work_emails": len([email for email in cleaned_emails if email.get("category") == "work" and not _is_renewal_item(email)]),
        "personal_emails": len([email for email in cleaned_emails if email.get("category") == "personal"]),
    }
    return payload


_previous_perform_scan_newquestion_cleanup = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _previous_perform_scan_newquestion_cleanup(force_full=force_full)
    # Ensure scan response also reflects removed New Question items and renewal counts.
    try:
        invalidate_dashboard_cache()
        payload = build_dashboard_payload(force_refresh=True)
        summary = payload.setdefault("scan_summary", {})
        summary["visible_work_emails"] = payload.get("stats", {}).get("work_emails", 0)
        summary["visible_personal_emails"] = payload.get("stats", {}).get("personal_emails", 0)
        summary["visible_renewals"] = payload.get("stats", {}).get("renewal_emails", 0)
    except Exception as error:
        print(f"[scan] post-cleanup failed: {error}", flush=True)
    return payload




# ---------------------------------------------------------------------
# FINAL USER PATCH: remove submitted-question notices, isolate renewals,
# force non-Pharmacy Prep items to Personal, and deepen Gmail API context.
# ---------------------------------------------------------------------
# Keep the same screening version so existing good rows are not hidden again.
EMAIL_SCREENING_VERSION = "2026-06-renewals-personal-context-v1"
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "36"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "8"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "28"))


def _is_new_question_submitted_text(value: str) -> bool:
    """Remove all submitted-question form notifications from dashboard storage/display.
    These are WordPress/EprepStation/PharmacyPrep system notices, not emails to reply to.
    """
    text = str(value or "").lower()
    text = re.sub(r"\s+", " ", text)
    patterns = [
        "new question submitted",
        "new question submited",
        "new question has been submitted",
        "new question has been submited",
        "question has been submitted",
        "question has been submited",
        "question submitted",
        "question submited",
        "new support question has been submitted at eprepstation.com",
        "new support question",
    ]
    if any(pattern in text for pattern in patterns):
        if any(site in text for site in ["pharmacyprep", "eprepstation", "wordpress", "support question", "question submitted", "question submited"]):
            return True
    return False


def _is_ignored_new_question_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _is_new_question_submitted_text("\n".join([
        latest.get("subject", ""), latest.get("from", ""), latest.get("to", ""), latest.get("body", ""), combined_thread_text(thread),
    ]))


def _is_ignored_new_question_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    return _is_new_question_submitted_text("\n".join([
        str(item.get("title", "")), str(item.get("important_reason", "")), str(item.get("status", "")),
        str(original.get("from", "")), str(original.get("to", "")), str(original.get("subject", "")), str(original.get("body", "")),
    ]))


def _is_renewal_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_renewal_request") or item.get("renewal_request"):
        return True
    details = item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
    haystack = "\n".join([
        str(item.get("title", "")), str(item.get("important_reason", "")), str(original.get("subject", "")), str(original.get("body", "")),
        str(details.get("course", "")), str(details.get("student_email", "")), str(details.get("email", "")), str(reply.get("to", "")),
    ]).lower()
    return "account renewal request" in haystack or ("renewal" in haystack and "eprepstation" in haystack)


def _thread_body_context_only(thread: Dict, connected_email: str = "") -> str:
    parts = []
    for email in thread.get("emails", []) or []:
        parts.append(str(email.get("subject", "") or ""))
        parts.append(str(email.get("body", "") or ""))
    return "\n".join(parts).lower()


def _hard_personal_document_text(text: str) -> bool:
    text = str(text or "").lower()
    document_terms = [
        "lease agreement", "rental agreement", "tenancy agreement", "lease copy", "agreement copy",
        "sent lease agreement", "contract", "agreement", "landlord", "tenant", "rent", "condo",
        "mortgage", "bank statement", "insurance", "policy document", "legal document", "docusign", "adobe sign",
        "signed document", "signature request", "copy of the agreement", "copy of agreement",
    ]
    return any(term in text for term in document_terms)


def _has_core_pharmacy_prep_terms(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower().strip()
    if _is_new_question_submitted_text(text):
        return False
    core_terms = [
        "pharmacy prep", "pharmacyprep", "www.pharmacyprep.com", "416-223-prep", "416-223-7737", "647-221-0457",
        "eprepstation", "eprep station", "exam prep station", "online exam prep station",
        "pebc", "evaluating exam", "qualifying exam", "pebc exam", "pebc exams", "osce", "ospe", "fpgee", "opra", "naplex",
        "qbank", "question bank", "mock exam", "mock exams", "pharmacist qualifying", "pharmacy technician",
        "technician mcq", "technician ospe", "prep course", "pharmacy prep course", "home study plus online",
        "account renewal request", "eprepstation account renewal", "course renewal",
    ]
    if any(term in text for term in core_terms):
        return True
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    return False


def _text_is_pharmacy_prep_related(text: str, sender: str = "") -> bool:
    """Strict Work split. Work is ONLY Pharmacy Prep/student/PEBC/course support.
    Agreements, contracts, lease docs, finance/legal/etc. go Personal unless they explicitly contain Pharmacy Prep/PEBC/EprepStation context.
    """
    text = str(text or "").lower()
    sender = str(sender or "").lower().strip()
    if not text and not sender:
        return False
    if _is_new_question_submitted_text(text):
        return False
    if _has_core_pharmacy_prep_terms(text, sender):
        return True
    if _hard_personal_document_text(text):
        return False

    student_context = any(term in text for term in ["student", "students", "enrolled", "enrollment", "enrolment", "registration"])
    course_context = any(term in text for term in ["course", "class", "lecture", "recording", "notes", "book", "books", "login", "access", "password", "renewal", "extension"])
    exam_context = any(term in text for term in ["exam", "mcq", "osce", "ospe", "qbank", "mock", "pebc", "evaluating", "qualifying"])
    if student_context and (course_context or exam_context):
        return True
    if course_context and exam_context:
        return True

    commerce_terms = any(term in text for term in ["order #", "order number", "new order", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer"])
    pharmacy_product_terms = any(term in text for term in ["pebc", "exam", "mock", "qbank", "pharmacy", "prep", "eprepstation", "course access", "course login"])
    if commerce_terms and pharmacy_product_terms:
        return True
    return False


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    return _text_is_pharmacy_prep_related(_thread_body_context_only(thread, connected_email), sender)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item) or _is_ignored_new_question_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1].lower().strip()
    # Use only source content for category. Do not use generated summary/reply text because it may contain Pharmacy Prep signature.
    text = "\n".join([
        str(item.get("title", "")),
        str(original.get("subject", "")),
        str(original.get("body", "")),
        str(original.get("from", "")),
    ])
    return _text_is_pharmacy_prep_related(text, sender)


def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"


_previous_build_general_email_item_final_cleanup = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _is_ignored_new_question_thread(thread, connected_email):
        # Do not insert these notices into the catalog at all.
        return None
    item = _previous_build_general_email_item_final_cleanup(service, thread, connected_email, personal_label_id, work_label_id)
    if not item or item.get("filtered_out"):
        return item
    if _is_ignored_new_question_item(item):
        return None
    if _is_renewal_item(item):
        item["category"] = "renewal"
        item["is_renewal_request"] = True
        item["renewal_request"] = True
        return item
    new_category = "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"
    item["category"] = new_category
    try:
        apply_label_to_thread_messages(service, thread, work_label_id if new_category == "work" else personal_label_id)
    except Exception:
        pass
    return item


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    text = f"{subject}\n{body}"

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})",
        r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
    ]:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in phrases:
                phrases.append(value)

    name_bits = []
    clean_name = re.sub(r"[^A-Za-z\s]", " ", sender_name or "").strip()
    for part in clean_name.split():
        if len(part) >= 3 and part.lower() not in ("pharmacy", "prep", "student", "customer", "support", "question", "wordpress"):
            name_bits.append(part)
    if sender_email:
        inferred = infer_customer_name_from_email(sender_email) or ""
        for part in inferred.split():
            if len(part) >= 3 and part.lower() not in [p.lower() for p in name_bits]:
                name_bits.append(part)
        local = sender_email.split("@", 1)[0]
        for part in re.split(r"[._+\-0-9]+", local):
            if len(part) >= 3 and part.lower() not in [p.lower() for p in name_bits]:
                name_bits.append(part)

    keyword_pool = []
    stop = {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should", "there", "about", "from", "your", "with", "this", "attached"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", text):
        token_l = token.lower()
        if token_l in stop:
            continue
        if token_l not in keyword_pool:
            keyword_pool.append(token_l)
        if len(keyword_pool) >= 18:
            break

    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()
    context_terms = [
        "Order #", "New Order", "order number", "invoice", "receipt", "payment", "e-transfer", "etransfer",
        "login", "access", "password", "renewal", "extension", "course", "PEBC", "exam", "mock",
        "book", "recording", "notes", "schedule", "registration", "enrolled", "enrollment", "refund",
    ]
    context_or = " OR ".join([f'\"{term}\"' if " " in term or "#" in term else term for term in context_terms])

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:sent "{sender_email}"',
            f'in:anywhere "{sender_email}"',
            f'in:anywhere ({sender_email}) ({context_or})',
            f'in:sent to:{sender_email} ({context_or})',
            f'in:anywhere from:{sender_email} ({context_or})',
            f'in:anywhere to:{sender_email} ({context_or})',
        ])
    if clean_subject and len(clean_subject) >= 6:
        queries.append(f'in:anywhere "{clean_subject[:80]}"')
    for name_part in name_bits[:6]:
        queries.extend([f'in:anywhere "{name_part}"', f'in:sent "{name_part}"'])
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:8]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keyword_pool[:14]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = None) -> str:
    if max_threads_per_query is None:
        max_threads_per_query = GMAIL_CONTEXT_THREADS_PER_QUERY
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL API CONTEXT ================\n\n"
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
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
                    f"Thread content:\n{format_thread_for_ai(thread)[:10000]}"
                )
                if len(context_blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        return 0
    changed = 0
    for key in list(emails_bucket.keys()):
        item = emails_bucket.get(key, {})
        if _is_ignored_new_question_item(item):
            del emails_bucket[key]
            changed += 1
            continue
        if not isinstance(item, dict):
            continue
        category = _final_category_for_item(item)
        if category == "renewal":
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            details = _item_renewal_details(item)
            if details:
                item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
                if item.get("status") != "Already Replied":
                    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
                    item["reply"] = {**reply, "to": details.get("student_email", reply.get("to", ""))}
        else:
            item["category"] = category
        emails_bucket[key] = item
        changed += 1
    if changed:
        catalog["emails"] = emails_bucket
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


_previous_catalog_visible_final_cleanup = _is_catalog_email_visible
def _is_catalog_email_visible(item: Dict) -> bool:
    if _is_ignored_new_question_item(item):
        return False
    if _is_renewal_item(item):
        if not _item_is_on_or_after_scan_start(item):
            return False
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    return _previous_catalog_visible_final_cleanup(item)


_previous_build_dashboard_payload_final_cleanup = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_final_cleanup(force_refresh=force_refresh)
    cleaned_emails = []
    renewals = []
    for email in payload.get("emails", []) or []:
        if _is_ignored_new_question_item(email):
            continue
        category = _final_category_for_item(email)
        email["category"] = category
        if category == "renewal":
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            details = _item_renewal_details(email)
            if details and email.get("reply"):
                email["reply"]["to"] = details.get("student_email", email["reply"].get("to", ""))
            renewals.append(email)
        cleaned_emails.append(email)

    payload["emails"] = cleaned_emails
    payload["renewals"] = renewals
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "renewal_emails": len(renewals),
        "work_emails": len([e for e in cleaned_emails if e.get("category") == "work"]),
        "personal_emails": len([e for e in cleaned_emails if e.get("category") == "personal"]),
    }
    return payload


_previous_perform_scan_final_cleanup = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _previous_perform_scan_final_cleanup(force_full=force_full)
    try:
        _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        summary = payload.get("scan_summary", {}) if isinstance(payload, dict) else {}
        fresh["scan_summary"] = {
            **summary,
            "visible_work_emails": fresh.get("stats", {}).get("work_emails", 0),
            "visible_personal_emails": fresh.get("stats", {}).get("personal_emails", 0),
            "visible_renewals": fresh.get("stats", {}).get("renewal_emails", 0),
            "catalog_cleanup_applied": True,
        }
        return fresh
    except Exception as error:
        print(f"[scan] final cleanup failed: {error}", flush=True)
        return payload



# ---------------------------------------------------------------------
# FINAL REPAIR PATCH: reliable AI screening + promo cleanup + contextual replies
# ---------------------------------------------------------------------
# This patch is intentionally appended last so it overrides the stacked patch blocks above.
# Goals:
# - never show Xbox/Vimeo/promotional/newsletter emails in Work or Personal
# - never show New Question Submitted / support-question notification emails
# - Work is ONLY Pharmacy Prep / PEBC / EprepStation / student-course-exam/order/support context
# - Personal is everything actionable outside Pharmacy Prep
# - replies must use Gmail API context and not generic "we will review/get back" wording
# - no more "AI screening unavailable" style placeholder rows
EMAIL_SCREENING_VERSION = "2026-06-ai-context-reply-cleanup-v1"
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "320"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "220"))
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "60"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "10"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "42"))


def _lower_join(values) -> str:
    return "\n".join(str(v or "") for v in values).lower()


def _openai_json_response(prompt: str) -> Optional[Dict]:
    """Call OpenAI and parse JSON. Supports both Responses API and Chat Completions
    so the dashboard does not silently lose AI screening if one API path fails.
    """
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if isinstance(parsed, dict):
            return parsed
    except Exception as error:
        print(f"[ai] responses API failed: {error}", flush=True)
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Return valid JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        content = response.choices[0].message.content or ""
        parsed = parse_ai_json(content.strip())
        if isinstance(parsed, dict):
            return parsed
    except Exception as error:
        print(f"[ai] chat completions fallback failed: {error}", flush=True)
    return None


def _is_promotional_or_brand_noise_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    haystack = f"{sender}\n{text}"

    # These senders/brands have been showing as irrelevant dashboard rows.
    noisy_brand_terms = [
        "xbox", "game pass", "microsoft rewards", "microsoft store", "playstation", "nintendo",
        "steam", "epic games", "epicgames", "ea.com", "twitch", "vimeo", "mail.vimeo",
        "netflix", "spotify", "youtube", "youtube creators", "prime video", "disney+", "duolingo",
        "udemy", "coursera", "skillshare", "canva", "grammarly", "mailchimp", "hubspot",
    ]
    marketing_terms = [
        "unsubscribe", "manage your preferences", "view this email in your browser", "you are receiving this email",
        "you're receiving this email", "newsletter", "promotion", "promotional", "limited time", "sale",
        "deal", "discount", "offer", "save ", "% off", "free trial", "watch now", "stream now",
        "new video", "featured video", "trailer", "webinar", "digest", "creator update", "weekly update",
        "monthly update", "recommended for you", "because you watched", "game", "games", "gaming",
    ]
    transactional_request_terms = [
        "can you", "could you", "would you", "please send", "please provide", "please confirm",
        "i need", "need help", "question", "not received", "still waiting", "unable to", "cannot access",
        "order number", "invoice", "refund", "receipt", "login", "access",
    ]

    # Brand promotional sources get removed even when Gmail labels them important.
    if any(term in haystack for term in noisy_brand_terms):
        if any(term in haystack for term in marketing_terms) or not any(term in haystack for term in transactional_request_terms):
            return True

    # Generic marketing/newsletter with unsubscribe and no direct request should never show.
    if any(term in haystack for term in marketing_terms):
        if not any(term in haystack for term in transactional_request_terms):
            return True

    return False


def _is_promotional_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1]
    text = _lower_join([
        latest.get("from", ""), latest.get("subject", ""), latest.get("body", ""), combined_thread_text(thread)
    ])
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_promotional_item(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1]
    text = _lower_join([
        item.get("title", ""), item.get("important_reason", ""), original.get("from", ""),
        original.get("subject", ""), original.get("body", ""), item.get("status", ""),
    ])
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_ai_placeholder_text(value: str) -> bool:
    text = str(value or "").lower()
    return any(phrase in text for phrase in [
        "ai screening unavailable", "ai screening accepted this message", "safe contextual reply could not be generated",
        "suggested reply unavailable", "ai unavailable", "screening unavailable",
    ])


def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower().strip()
    bad_phrases = [
        "we received your message and will get back to you", "we received your email and will get back to you",
        "thank you for your email. we will review your request", "we will review your request and get back to you shortly",
        "i will take a look and get back to you", "i will check the related account, order, or course details",
        "reply with the correct information", "we will follow up shortly", "we will review the account details",
        "we will review your request", "get back to you shortly", "we will get back to you",
    ]
    if any(p in text for p in bad_phrases):
        return True
    # AI-looking list formats in a normal email reply.
    list_lines = [line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]
    if len(list_lines) >= 2:
        return True
    return False


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    cleaned = []
    for line in body.split("\n"):
        line = line.rstrip()
        line = re.sub(r"^\s*[-*•–—]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace(" — ", ", ").replace(" – ", ", ")
        cleaned.append(line)
    body = "\n".join(cleaned)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = re.sub(r"(?i)^subject\s*:\s*.*\n+", "", body).strip()
    if category == "work" and "pharmacy prep" not in body.lower():
        body = body.rstrip() + "\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    return body


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if len(body.split()) < 14:
        return True
    if _is_bad_generic_reply_text(body):
        return True
    if latest and body.lower().startswith(latest.lower()[:80]):
        return True
    if latest and copied_sequence_found(body, latest, sequence_len=14):
        return True
    return False


def _strict_work_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    if _is_new_question_submitted_text(text):
        return False
    if _is_promotional_or_brand_noise_text(text, sender):
        return False
    if _hard_personal_document_text(text):
        # Agreements/contracts/leases are personal unless the actual content explicitly says Pharmacy Prep/PEBC/EprepStation.
        explicit_business = ["pharmacy prep", "pharmacyprep", "pebc", "eprepstation", "online exam prep station"]
        if not any(term in text for term in explicit_business):
            return False
    direct_business_terms = [
        "pharmacy prep", "pharmacyprep", "www.pharmacyprep.com", "416-223-prep", "416-223-7737",
        "eprepstation", "eprep station", "online exam prep station", "pebc", "evaluating exam",
        "qualifying exam", "pebc exam", "osce", "ospe", "fpgee", "opra", "qbank", "mock exam",
        "pharmacy technician", "technician ospe", "technician mcq", "pharmacy prep course",
        "prep course", "course login", "course access", "account renewal request", "course renewal",
    ]
    if any(term in text for term in direct_business_terms):
        return True
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    student_exam = any(t in text for t in ["student", "enrolled", "enrollment", "enrolment", "registration", "course", "class", "lecture", "recording", "notes", "login", "access"])
    exam_prep = any(t in text for t in ["pebc", "exam", "mcq", "osce", "ospe", "qbank", "mock", "pharmacy", "pharmacist", "technician"])
    if student_exam and exam_prep:
        return True
    order_payment = any(t in text for t in ["order #", "order number", "new order", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer"])
    prep_product = any(t in text for t in ["pebc", "pharmacy", "prep", "course", "mock", "qbank", "exam", "eprepstation"])
    return bool(order_payment and prep_product)


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    return _strict_work_text(_thread_body_context_only(thread, connected_email), sender)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item) or _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1].lower().strip()
    text = "\n".join([
        str(item.get("title", "")), str(original.get("subject", "")), str(original.get("body", "")), str(original.get("from", "")),
    ])
    return _strict_work_text(text, sender)


def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'in:anywhere {date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    return [
        f'{base} in:inbox',
        f'{base}',
        f'{base} ("?" OR "please" OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know" OR "not received" OR "still waiting" OR "checking in")',
        f'{base} (PEBC OR "Pharmacy Prep" OR pharmacyprep OR EprepStation OR course OR login OR access OR renewal OR extension OR order OR invoice OR payment OR refund)',
        f'{base} (agreement OR contract OR lease OR document OR appointment OR lawyer OR bank OR insurance)',
        f'{base} -from:(noreply OR no-reply OR donotreply OR wordpress OR woocommerce OR notifications OR marketing)',
    ]


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _is_ignored_new_question_thread(thread, connected_email):
        return False
    if _is_promotional_thread(thread, connected_email):
        return False
    # Keep only hard automation out; relevance is handled by OpenAI.
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
    hard_auto = ["mailer-daemon", "postmaster", "delivery status notification", "undeliverable", "verification code", "password reset", "security alert"]
    if any(term in sender or term in text for term in hard_auto):
        return False
    return True


def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    if _is_ignored_new_question_thread(thread, connected_email) or _is_promotional_thread(thread, connected_email):
        return {"include": False, "category": "personal", "title": "Filtered notification", "summary": "Promotional or system notification filtered out.", "reason": "Promotional/system notification", "confidence": 1.0}

    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 7000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 12000)
    forced_category = category_for_thread_strict(thread, connected_email)
    prompt = f"""
You are screening Gmail for a dashboard. Decide if the latest inbound thread deserves a human reply.

Include only real human/actionable messages: a question, request, problem, missing detail, appointment/decision, student/customer support issue, order/payment/login/course issue, or a personal/business item that asks the user to do something.

Exclude all promotional/newsletter/marketing/brand emails, including Xbox, Vimeo, streaming, gaming, webinars, creator updates, recommendations, sales, offers, deals, coupons, newsletters, and emails whose only action is to click a marketing link. Also exclude WordPress/EprepStation form notifications named New Question Submitted or New Support Question Submitted.

Category is already decided by strict rules. Use this exact category: {forced_category}
Work means only Pharmacy Prep, PEBC, EprepStation, student course/exam/login/order/payment/renewal/support. Everything else actionable, including agreements, contracts, lease, legal, documents, finance, appointments, and external organizations, is personal.

Return JSON only:
{{
  "include": true,
  "category": "{forced_category}",
  "title": "4-9 word dashboard title",
  "summary": "Specific one-sentence summary mentioning {display_name} and exactly what they need.",
  "reason": "Why this needs a reply or why it was excluded.",
  "confidence": 0.0
}}

Sender display name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current thread:
{thread_text}
"""
    parsed = _openai_json_response(prompt)
    if not isinstance(parsed, dict):
        # Deterministic fallback: never show "AI unavailable". Include human-looking direct asks.
        text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
        direct_ask = any(term in text for term in ["?", "can you", "could you", "would you", "please", "i need", "need help", "not received", "still waiting", "unable to", "invoice", "order number", "login", "access", "appointment", "agreement", "contract", "lease"])
        if not direct_ask:
            return {"include": False, "category": forced_category, "title": "Not actionable", "summary": "This message does not appear to need a reply.", "reason": "No direct request detected", "confidence": 0.35}
        subject = re.sub(r"^(re|fw|fwd):\s*", "", latest.get("subject", "") or "Email", flags=re.IGNORECASE).strip()
        return {"include": True, "category": forced_category, "title": subject[:70] or "Actionable email", "summary": f"{display_name} appears to need a reply about {subject or 'this message'}.", "reason": "Direct request detected without AI response", "confidence": 0.4}

    include = parsed.get("include", False)
    if isinstance(include, str):
        include = include.strip().lower() in ("true", "yes", "1", "include")
    if _is_promotional_thread(thread, connected_email) or _is_ignored_new_question_thread(thread, connected_email):
        include = False
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    return {
        "include": bool(include),
        "category": forced_category,
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "reason": str(parsed.get("reason", "") or "").strip(),
        "confidence": confidence,
    }


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    full_text = f"{subject}\n{body}"
    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"account\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9_\-]{3,})",
    ]:
        for match in re.findall(pattern, full_text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in phrases:
                phrases.append(value)

    name_bits = []
    for source in [sender_name or "", infer_customer_name_from_email(sender_email) or "", sender_email.split("@", 1)[0] if sender_email else ""]:
        for part in re.split(r"[^A-Za-z]+", source):
            part = part.strip()
            if len(part) >= 3 and part.lower() not in {"pharmacy", "prep", "student", "customer", "support", "question", "wordpress", "info", "admin", "noreply"}:
                if part not in name_bits:
                    name_bits.append(part)

    keywords = []
    stop = {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should", "there", "about", "from", "your", "with", "this", "attached", "sent", "send"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", full_text):
        token_l = token.lower()
        if token_l in stop or token_l in keywords:
            continue
        keywords.append(token_l)
        if len(keywords) >= 22:
            break

    context_terms = [
        "Order #", "New Order", "order number", "invoice", "receipt", "payment", "e-transfer", "etransfer",
        "login", "access", "password", "renewal", "extension", "course", "PEBC", "exam", "mock",
        "book", "recording", "notes", "schedule", "registration", "enrolled", "enrollment", "refund", "agreement", "contract", "lease",
    ]
    context_or = " OR ".join([f'\"{term}\"' if " " in term or "#" in term else term for term in context_terms])

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}', f'in:anywhere to:{sender_email}', f'in:sent to:{sender_email}',
            f'in:sent "{sender_email}"', f'in:anywhere "{sender_email}"',
            f'in:anywhere ({sender_email}) ({context_or})', f'in:sent to:{sender_email} ({context_or})',
            f'in:anywhere from:{sender_email} ({context_or})', f'in:anywhere to:{sender_email} ({context_or})',
        ])
    if clean_subject and len(clean_subject) >= 6:
        queries.append(f'in:anywhere "{clean_subject[:100]}"')
    for name_part in name_bits[:8]:
        queries.extend([f'in:anywhere "{name_part}"', f'in:sent "{name_part}"'])
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:10]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keywords[:18]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = None) -> str:
    if max_threads_per_query is None:
        max_threads_per_query = GMAIL_CONTEXT_THREADS_PER_QUERY
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL API CONTEXT ================\n\n"
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
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
                    f"Thread content:\n{format_thread_for_ai(thread)[:12000]}"
                )
                if len(context_blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


def _extract_specific_context_facts(extra_context: str) -> str:
    text = str(extra_context or "")
    facts = []
    for order in sorted(set(re.findall(r"(?:Order\s*#|order\s*#|#)\s*(\d{3,})", text)), key=len, reverse=True)[:5]:
        facts.append(f"Order #{order}")
    for email in sorted(set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)))[:6]:
        if "pharmacyprep" not in email.lower() and "eprepstation" not in email.lower():
            facts.append(email)
    return ", ".join(dict.fromkeys(facts))


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 12000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 18000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}")
    prompt = f"""
Write a professional email reply using the current Gmail thread and related Gmail API context.

This is NOT a generic acknowledgment task. The reply must answer the actual message using evidence in the Gmail context. Do not say "we will review", "we will get back to you", "I will take a look", or similar filler.

Style rules:
- No bullet points, numbered lists, markdown, headings, tables, bold text, or dash-heavy phrasing.
- Use natural paragraphs only.
- Do not copy or restate the inbound email.
- Do not open with the exact same sentence every time.
- If Gmail context contains an order number, receipt, course/login/access/payment detail, date, sender email, or previous sent reply, include the specific detail.
- If the exact answer is not found in Gmail context, state what was checked in one sentence and ask one specific follow-up question. Do not promise a future review.
- For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not include the Pharmacy Prep signature.
- Keep it concise but useful, normally 80-170 words before the signature.

Known specific facts found from Gmail context: {facts or 'None clearly extracted'}

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific dashboard summary mentioning who is asking and the concrete Gmail context used",
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
Latest inbound subject: {latest.get('subject', '')}
Latest inbound body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail API context from sender/name/order/keyword searches:
{extra_context or 'None found'}
"""
    parsed = _openai_json_response(prompt)
    if not isinstance(parsed, dict):
        print("[ai] compose returned no JSON", flush=True)
        return None
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        # One retry with a stricter anti-generic instruction.
        retry_prompt = prompt + "\n\nThe previous draft was missing or too generic. Rewrite it now with a concrete answer from Gmail context or a single precise follow-up question. Do not use future-review filler."
        parsed = _openai_json_response(retry_prompt)
        if not isinstance(parsed, dict):
            return None
        body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
        if not body or reply_needs_regeneration(body, latest_body, category):
            return None
    return {
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
        "body": body,
    }


def _context_based_fallback_reply(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    latest_text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
    facts = _extract_specific_context_facts(extra_context)
    if facts and "order" in latest_text:
        core = f"I checked the related Gmail history connected to this message and found this matching detail: {facts}."
    elif facts:
        core = f"I checked the related Gmail history connected to this message and found these matching details: {facts}."
    elif "order" in latest_text or "invoice" in latest_text or "receipt" in latest_text:
        core = "I checked the Gmail history connected to this email address, but I do not see a clear matching order, invoice, or receipt detail in the available context."
    elif "login" in latest_text or "access" in latest_text or "password" in latest_text:
        core = "I checked the Gmail history connected to this email address, but I do not see a clear prior login or course-access message in the available context."
    else:
        return None

    if category == "work":
        body = f"""Hello {greeting},

{core} Please confirm the email address or order number used for the original registration so we can match the record accurately.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

{core} Please send the exact reference number or email address used for the original record so I can match it accurately.

Regards"""
    if reply_needs_regeneration(body, latest.get("body", ""), category):
        return None
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Optional[Dict]:
    # Disable the old generic fallback. build_general_email_item below can create a context fallback only after Gmail API search.
    return None


_previous_build_general_email_item_ai_context_cleanup = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _is_ignored_new_question_thread(thread, connected_email) or _is_promotional_thread(thread, connected_email):
        return None
    item = _previous_build_general_email_item_ai_context_cleanup(service, thread, connected_email, personal_label_id, work_label_id)
    if not item:
        return None
    if _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return None
    if item.get("filtered_out"):
        return item

    if _is_renewal_item(item):
        item["category"] = "renewal"
        item["is_renewal_request"] = True
        item["renewal_request"] = True
        return item

    forced_category = "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"
    item["category"] = forced_category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    if _is_ai_placeholder_text(item.get("important_reason", "")):
        latest = latest_inbound_email_for_dashboard(thread, connected_email)
        subject_clean = re.sub(r"^(re|fw|fwd):\s*", "", latest.get("subject", "") or "this message", flags=re.IGNORECASE).strip()
        name = sender_display_name(latest.get("from", ""), parseaddr(latest.get("from", ""))[1])
        item["important_reason"] = f"{name} needs a response about {subject_clean}."

    try:
        apply_label_to_thread_messages(service, thread, work_label_id if forced_category == "work" else personal_label_id)
    except Exception:
        pass

    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_body = latest.get("body", "")
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if (not reply) or _is_bad_generic_reply_text(reply.get("body", "")) or reply_needs_regeneration(reply.get("body", ""), latest_body, forced_category):
        queries = heuristic_context_queries_for_thread(thread, connected_email)
        extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread.get("thread_id", ""), max_threads_per_query=GMAIL_CONTEXT_THREADS_PER_QUERY) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, forced_category, extra_context=extra_context)
        if composed:
            if composed.get("summary"):
                item["important_reason"] = composed.get("summary")
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                item["title"] = composed.get("title")
            item["reply"] = {
                "thread_id": thread.get("thread_id", ""),
                "mode": "thread_reply",
                "to": parseaddr(latest.get("from", ""))[1].strip(),
                "subject": composed.get("subject", ""),
                "body": composed.get("body", ""),
            }
            item["status"] = "Needs Reply"
        else:
            fallback = _context_based_fallback_reply(thread, connected_email, forced_category, extra_context=extra_context)
            if fallback:
                item["reply"] = fallback
                item["status"] = "Needs Reply"
            else:
                # Do not show an "AI unavailable" row with no useful reply.
                item["filtered_out"] = True
                item["status"] = "Filtered Out"
                item["reply"] = None
    return item


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return True
    text = _catalog_text(item)
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "e-transfer received", "etransfer received", "interac e-transfer", "payment received",
        "receipt for your payment", "charge receipt", "invoice paid", "tracking number", "shipment",
        "please moderate", "comment awaiting moderation", "new question submitted", "new question submited",
        "new support question has been submitted", "security alert", "verification code", "password reset",
        "mail delivery", "undeliverable", "thank you for signing the document", "merci d’avoir signé",
        "master key", "unit door", "security staff", "management office", "contractor requires access",
    ]
    if any(term in text for term in bad_terms):
        return True
    return _is_promotional_or_brand_noise_text(text, "")


def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return False
    if _is_renewal_item(item):
        if not _item_is_on_or_after_scan_start(item):
            return False
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if _is_ai_placeholder_text(item.get("important_reason", "")):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if reply and _is_bad_generic_reply_text(reply.get("body", "")):
        return False
    return bool(reply) or item.get("status") in ("Already Replied", "Suggestion Removed")


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        return 0
    changed = 0
    for key in list(emails_bucket.keys()):
        item = emails_bucket.get(key, {})
        if not isinstance(item, dict):
            del emails_bucket[key]
            changed += 1
            continue
        if _is_ignored_new_question_item(item) or _is_promotional_item(item) or _is_ai_placeholder_text(item.get("important_reason", "")):
            del emails_bucket[key]
            changed += 1
            continue
        if _is_renewal_item(item):
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            details = _item_renewal_details(item)
            if details:
                item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
                if item.get("status") != "Already Replied":
                    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
                    item["reply"] = {**reply, "to": details.get("student_email", reply.get("to", ""))}
        else:
            item["category"] = "work" if _is_item_pharmacy_prep_related(item) else "personal"
            item["screening_version"] = EMAIL_SCREENING_VERSION
        emails_bucket[key] = item
        changed += 1
    if changed:
        catalog["emails"] = emails_bucket
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


_previous_build_dashboard_payload_ai_context_cleanup = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_ai_context_cleanup(force_refresh=force_refresh)
    cleaned_emails = []
    renewals = []
    for email in payload.get("emails", []) or []:
        if _is_ignored_new_question_item(email) or _is_promotional_item(email) or _is_ai_placeholder_text(email.get("important_reason", "")):
            continue
        category = _final_category_for_item(email)
        email["category"] = category
        if category == "renewal":
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            details = _item_renewal_details(email)
            if details and email.get("reply"):
                email["reply"]["to"] = details.get("student_email", email["reply"].get("to", ""))
            renewals.append(email)
        cleaned_emails.append(email)
    payload["emails"] = cleaned_emails
    payload["renewals"] = renewals
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "renewal_emails": len(renewals),
        "work_emails": len([e for e in cleaned_emails if e.get("category") == "work"]),
        "personal_emails": len([e for e in cleaned_emails if e.get("category") == "personal"]),
    }
    return payload


_previous_perform_scan_ai_context_cleanup = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    print(f"[scan] starting AI context cleanup scan | force_full={force_full}", flush=True)
    payload = _previous_perform_scan_ai_context_cleanup(force_full=force_full)
    try:
        removed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        summary = payload.get("scan_summary", {}) if isinstance(payload, dict) else {}
        fresh["scan_summary"] = {
            **summary,
            "catalog_cleanup_removed_or_recategorized": removed,
            "visible_work_emails": fresh.get("stats", {}).get("work_emails", 0),
            "visible_personal_emails": fresh.get("stats", {}).get("personal_emails", 0),
            "visible_renewals": fresh.get("stats", {}).get("renewal_emails", 0),
            "promo_noise_filter": "xbox/vimeo/newsletters/promotions removed",
            "gmail_context_queries_per_reply": GMAIL_CONTEXT_QUERY_LIMIT,
        }
        print(
            f"[scan] AI context cleanup complete | work={fresh['scan_summary']['visible_work_emails']} | "
            f"personal={fresh['scan_summary']['visible_personal_emails']} | renewals={fresh['scan_summary']['visible_renewals']} | cleanup={removed}",
            flush=True,
        )
        return fresh
    except Exception as error:
        print(f"[scan] AI context cleanup failed: {error}", flush=True)
        return payload


# ---------------------------------------------------------------------
# FINAL PATCH: cheaper AI model + stronger bogus promo cleanup + no AI-unavailable rows
# ---------------------------------------------------------------------
# Appended last intentionally. This keeps the working scan but fixes:
# - Xbox/Vimeo/JPMorgan/promotional rows still appearing
# - support@pharrmacyprep.com spam rows
# - visible "AI screening unavailable" rows
# - generic "we will review/get back" replies
# - high OpenAI testing cost
EMAIL_SCREENING_VERSION = "2026-06-cheap-clean-context-v2"
OPENAI_FAST_MODEL = (
    os.getenv("OPENAI_FAST_MODEL")
    or os.getenv("OPENAI_CHEAP_MODEL")
    or os.getenv("OPENAI_SCREENING_MODEL")
    or "gpt-5-nano"
)
OPENAI_REPLY_MODEL = os.getenv("OPENAI_REPLY_MODEL") or OPENAI_FAST_MODEL
# Keep OpenAI usage low while still allowing Gmail API context.
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "90"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "35"))
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "18"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "3"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "10"))
GMAIL_CONTEXT_CHARS_PER_THREAD = int(os.getenv("GMAIL_CONTEXT_CHARS_PER_THREAD", "2800"))


_BLOCKED_SENDER_EXACT = {
    "support@pharrmacyprep.com",
    "support@pharrmacyprep.co",
}


_PROMO_BRAND_TERMS = [
    "xbox", "game pass", "microsoft rewards", "microsoft store", "playstation", "nintendo",
    "steam", "epic games", "epicgames", "ea.com", "twitch", "vimeo", "mail.vimeo",
    "netflix", "spotify", "youtube", "prime video", "disney+", "duolingo", "udemy",
    "coursera", "skillshare", "canva", "grammarly", "mailchimp", "hubspot",
    "jp morgan", "jpmorgan", "j.p. morgan", "chase", "morgan stanley", "wealth management update",
    "market update", "market insights", "investment outlook", "investor newsletter",
]

_PROMO_MARKETING_TERMS = [
    "unsubscribe", "manage your preferences", "view this email in your browser", "you are receiving this email",
    "you're receiving this email", "newsletter", "promotion", "promotional", "limited time", "sale",
    "deal", "discount", "offer", "coupon", "save ", "% off", "free trial", "watch now", "stream now",
    "new video", "featured video", "trailer", "webinar", "digest", "creator update", "weekly update",
    "monthly update", "recommended for you", "because you watched", "game", "games", "gaming",
    "see what's new", "discover", "explore our", "register today", "join us for", "sponsored",
]

_DIRECT_ACTION_TERMS = [
    "can you", "could you", "would you", "please send", "please provide", "please confirm",
    "please advise", "i need", "need help", "question", "not received", "still waiting",
    "unable to", "cannot access", "can't access", "order number", "invoice", "refund",
    "receipt", "login", "access", "appointment", "meeting", "contract", "agreement", "lease",
]


def _sender_email_from_value(value: str) -> str:
    return parseaddr(str(value or ""))[1].lower().strip()


def _is_blocked_sender_address(sender: str) -> bool:
    sender = _sender_email_from_value(sender) or str(sender or "").lower().strip()
    if sender in _BLOCKED_SENDER_EXACT:
        return True
    # Catch misspellings/spoof domains around Pharmacy Prep, but do not block the real domains.
    if "pharrmacyprep" in sender or "pharmacyprep.co" in sender:
        return True
    return False


def _is_promotional_or_brand_noise_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    haystack = f"{sender}\n{text}"
    if _is_blocked_sender_address(sender):
        return True
    if _is_new_question_submitted_text(haystack):
        return True

    has_direct_action = any(term in haystack for term in _DIRECT_ACTION_TERMS) or "?" in haystack
    has_promo_brand = any(term in haystack for term in _PROMO_BRAND_TERMS)
    has_marketing = any(term in haystack for term in _PROMO_MARKETING_TERMS)

    # Xbox/Vimeo/JPMorgan/etc. should not enter Work/Personal unless it is a real direct support request.
    if has_promo_brand and (has_marketing or not has_direct_action):
        return True
    if has_marketing and not has_direct_action:
        return True

    noisy_senders = [
        "news@", "newsletter@", "marketing@", "promo@", "promos@", "offers@", "deals@",
        "updates@", "events@", "mail.vimeo", "email.vimeo", "xbox", "jpmorgan", "chase@",
    ]
    if any(term in haystack for term in noisy_senders) and not has_direct_action:
        return True
    return False


def _is_promotional_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = _sender_email_from_value(latest.get("from", ""))
    text = _lower_join([
        latest.get("from", ""), latest.get("subject", ""), latest.get("body", ""), combined_thread_text(thread)
    ])
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_promotional_item(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _sender_email_from_value(original.get("from", ""))
    text = _lower_join([
        item.get("title", ""), item.get("important_reason", ""), original.get("from", ""),
        original.get("subject", ""), original.get("body", ""), item.get("status", ""),
    ])
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_ai_placeholder_text(value: str) -> bool:
    text = str(value or "").lower()
    return any(phrase in text for phrase in [
        "ai screening unavailable", "ai screening accepted this message", "safe contextual reply could not be generated",
        "suggested reply unavailable", "ai unavailable", "screening unavailable", "openai unavailable",
    ])


def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower().strip()
    bad_phrases = [
        "we received your message and will get back to you", "we received your email and will get back to you",
        "thank you for your email. we will review your request", "we will review your request and get back to you shortly",
        "i will take a look and get back to you", "we will take a look and get back to you",
        "we will check the related account, order, or course details", "reply with the correct information",
        "we will follow up shortly", "we will review the account details", "we will review your request",
        "get back to you shortly", "we will get back to you", "i will review and get back",
    ]
    if any(p in text for p in bad_phrases):
        return True
    list_lines = [line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]
    return len(list_lines) >= 2


def _openai_json_response(prompt: str, model: str = None) -> Optional[Dict]:
    """Cheap single-call JSON helper. No expensive double fallback; deterministic
    fallback code handles failures without showing 'AI screening unavailable'.
    """
    chosen_model = (model or OPENAI_FAST_MODEL or OPENAI_MODEL).strip()
    try:
        response = client.responses.create(model=chosen_model, input=prompt)
        parsed = parse_ai_json((getattr(response, "output_text", "") or "").strip())
        if isinstance(parsed, dict):
            return parsed
        print(f"[ai] {chosen_model} returned non-JSON", flush=True)
    except Exception as error:
        print(f"[ai] {chosen_model} failed: {error}", flush=True)
    return None


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'in:anywhere {date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    # Exclude the worst noise at Gmail query level, then still verify in Python cleanup.
    noise_excludes = '-from:support@pharrmacyprep.com -from:(newsletter OR marketing OR promos OR offers OR deals) -from:(xbox OR vimeo OR jpmorgan)'
    base = f'{base} {noise_excludes}'
    return [
        f'{base} in:inbox',
        f'{base} ("?" OR "please" OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know" OR "not received" OR "still waiting" OR "checking in")',
        f'{base} (PEBC OR "Pharmacy Prep" OR pharmacyprep OR EprepStation OR course OR login OR access OR renewal OR extension OR order OR invoice OR payment OR refund)',
        f'{base} (agreement OR contract OR lease OR document OR appointment OR lawyer OR bank OR insurance)',
        f'{base}',
    ]


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _is_ignored_new_question_thread(thread, connected_email):
        return False
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_blocked_sender_address(latest.get("from", "")):
        return False
    if _is_promotional_thread(thread, connected_email):
        return False
    sender = _sender_email_from_value(latest.get("from", ""))
    text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
    hard_auto = ["mailer-daemon", "postmaster", "delivery status notification", "undeliverable", "verification code", "password reset", "security alert"]
    if any(term in sender or term in text for term in hard_auto):
        return False
    # Keep broad. OpenAI/deterministic scoring decides relevance; noise is already blocked above.
    return True


def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    if _is_blocked_sender_address(latest.get("from", "")) or _is_ignored_new_question_thread(thread, connected_email) or _is_promotional_thread(thread, connected_email):
        return {"include": False, "category": "personal", "title": "Filtered notification", "summary": "Promotional, spoofed, or system notification filtered out.", "reason": "Blocked noise", "confidence": 1.0}

    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    latest_body = compact_ai_context(latest.get("body", ""), 5500)
    subject = latest.get("subject", "") or ""
    forced_category = category_for_thread_strict(thread, connected_email)
    prompt = f"""
Classify this Gmail thread for a reply dashboard.

Include only actionable human messages: a question, request, problem, missing detail, appointment/decision, student/customer support issue, or personal/business task.

Exclude all promotions/newsletters/brand updates, including Xbox, Vimeo, JP Morgan/JPMorgan/Chase market updates, streaming/gaming/webinar/sales/offer emails, and anything whose main action is clicking a marketing link. Exclude New Question Submitted and New Support Question Submitted notifications.

Use exact category: {forced_category}. Work means only Pharmacy Prep, PEBC, EprepStation, student course/exam/login/order/payment/renewal/support. Everything else actionable is personal.

Return JSON only:
{{
  "include": true,
  "category": "{forced_category}",
  "title": "4-9 word dashboard title",
  "summary": "Specific one-sentence summary mentioning {display_name} and what they need.",
  "reason": "Why it needs a reply or why excluded.",
  "confidence": 0.0
}}

Sender: {latest.get('from', '')}
Subject: {subject}
Body:
{latest_body}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_FAST_MODEL)
    if not isinstance(parsed, dict):
        text = _lower_join([subject, latest.get("body", "")])
        direct_ask = any(term in text for term in _DIRECT_ACTION_TERMS) or "?" in text
        if not direct_ask:
            return {"include": False, "category": forced_category, "title": "Not actionable", "summary": "This message does not appear to need a reply.", "reason": "No direct request detected", "confidence": 0.25}
        subject_clean = re.sub(r"^(re|fw|fwd):\s*", "", subject or "Email", flags=re.IGNORECASE).strip()
        return {"include": True, "category": forced_category, "title": subject_clean[:70] or "Actionable email", "summary": f"{display_name} needs a response about {subject_clean or 'this message'}.", "reason": "Direct request detected by deterministic fallback", "confidence": 0.35}

    include = parsed.get("include", False)
    if isinstance(include, str):
        include = include.strip().lower() in ("true", "yes", "1", "include")
    if _is_promotional_thread(thread, connected_email) or _is_ignored_new_question_thread(thread, connected_email):
        include = False
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    return {
        "include": bool(include),
        "category": forced_category,
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "reason": str(parsed.get("reason", "") or "").strip(),
        "confidence": confidence,
    }


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    full_text = f"{subject}\n{body}"
    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"account\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9_\-]{3,})",
    ]:
        for match in re.findall(pattern, full_text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in phrases:
                phrases.append(value)

    name_bits = []
    for source in [sender_name or "", infer_customer_name_from_email(sender_email) or "", sender_email.split("@", 1)[0] if sender_email else ""]:
        for part in re.split(r"[^A-Za-z]+", source):
            part = part.strip()
            if len(part) >= 3 and part.lower() not in {"pharmacy", "prep", "student", "customer", "support", "question", "wordpress", "info", "admin", "noreply"}:
                if part not in name_bits:
                    name_bits.append(part)

    keywords = []
    stop = {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should", "there", "about", "from", "your", "with", "this", "attached", "sent", "send"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", full_text):
        token_l = token.lower()
        if token_l in stop or token_l in keywords:
            continue
        keywords.append(token_l)
        if len(keywords) >= 12:
            break

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email}) ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access OR PEBC OR course OR renewal)',
        ])
    if clean_subject:
        short_subject = clean_subject[:80].replace('"', '')
        queries.append(f'in:anywhere "{short_subject}"')
    for name_part in name_bits[:5]:
        queries.extend([f'in:anywhere "{name_part}"', f'in:sent "{name_part}"'])
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:6]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keywords[:10]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = None) -> str:
    if max_threads_per_query is None:
        max_threads_per_query = GMAIL_CONTEXT_THREADS_PER_QUERY
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL API CONTEXT ================\n\n"
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
        try:
            thread_ids = search_threads(service, query=query, max_results=max_threads_per_query)
            for thread_id in thread_ids:
                if thread_id == current_thread_id or thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                thread = read_thread(service, thread_id)
                latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
                formatted = format_thread_for_ai(thread)[:GMAIL_CONTEXT_CHARS_PER_THREAD]
                context_blocks.append(
                    f"Search query: {query}\n"
                    f"Thread ID: {thread_id}\n"
                    f"Latest subject: {latest.get('subject', '')}\n"
                    f"Latest from: {latest.get('from', '')}\n"
                    f"Thread excerpt:\n{formatted}"
                )
                if len(context_blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


def _extract_specific_context_facts(extra_context: str) -> str:
    text = str(extra_context or "")
    facts = []
    for order in sorted(set(re.findall(r"(?:Order\s*#|order\s*#|#)\s*(\d{3,})", text)), key=len, reverse=True)[:4]:
        facts.append(f"Order #{order}")
    for label, pattern in [
        ("invoice", r"invoice\s*(?:#|number)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})"),
        ("receipt", r"receipt\s*(?:#|number)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})"),
        ("payment", r"(?:payment|paid|total)\s*[:#]?\s*\$?([0-9,]+\.\d{2})"),
    ]:
        for val in sorted(set(re.findall(pattern, text, flags=re.IGNORECASE)))[:3]:
            facts.append(f"{label}: {val}")
    for email in sorted(set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)))[:5]:
        if "pharmacyprep" not in email.lower() and "eprepstation" not in email.lower():
            facts.append(email)
    return ", ".join(dict.fromkeys(facts))


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    cleaned = []
    for line in body.split("\n"):
        line = line.rstrip()
        line = re.sub(r"^\s*[-*•–—]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace(" — ", ", ").replace(" – ", ", ")
        cleaned.append(line)
    body = "\n".join(cleaned)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = re.sub(r"(?i)^subject\s*:\s*.*\n+", "", body).strip()
    if category == "work" and "pharmacy prep" not in body.lower():
        body = body.rstrip() + "\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    return body


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    latest = clean_preview_text(latest_body or "", 6000).strip()
    if len(body.split()) < 14:
        return True
    if _is_bad_generic_reply_text(body):
        return True
    if latest and body.lower().startswith(latest.lower()[:80]):
        return True
    if latest and copied_sequence_found(body, latest, sequence_len=14):
        return True
    return False


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 9000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}")
    prompt = f"""
Write a professional email reply using the current Gmail thread and related Gmail API context.

Do not write a generic acknowledgement. Do not say "we will review", "we will get back to you", "I will take a look", or similar filler. Use the Gmail context to answer directly. If the exact answer is not in the context, say what was checked in one sentence and ask one specific follow-up question.

Style: natural paragraphs only. No bullets, no numbered lists, no markdown, no headings, no dash-heavy phrasing. Keep it concise and specific.

For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not include the Pharmacy Prep signature.

Known facts extracted from Gmail context: {facts or 'None clearly extracted'}

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific dashboard summary mentioning who is asking and the concrete Gmail context used",
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
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail API context:
{extra_context or 'None found'}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL)
    if not isinstance(parsed, dict):
        return None
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        return None
    return {
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
        "body": body,
    }


def _context_based_fallback_reply(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    latest_text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
    facts = _extract_specific_context_facts(extra_context)
    if not facts:
        return None
    if "order" in latest_text or "invoice" in latest_text or "receipt" in latest_text or "payment" in latest_text:
        core = f"I checked the related Gmail history for this email address and found: {facts}."
    else:
        core = f"I checked the related Gmail history connected to this message and found these matching details: {facts}."
    if category == "work":
        body = f"""Hello {greeting},

{core} Please confirm if this is the record you want us to use so we can answer the request accurately.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

{core} Please confirm if this is the record you want me to use.

Regards"""
    if reply_needs_regeneration(body, latest.get("body", ""), category):
        return None
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return True
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    if _is_blocked_sender_address(original.get("from", "")):
        return True
    text = _catalog_text(item)
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery",
        "delivered", "e-transfer received", "etransfer received", "interac e-transfer", "payment received",
        "receipt for your payment", "charge receipt", "invoice paid", "tracking number", "shipment",
        "please moderate", "comment awaiting moderation", "new question submitted", "new question submited",
        "new support question has been submitted", "security alert", "verification code", "password reset",
        "mail delivery", "undeliverable", "thank you for signing the document", "merci d’avoir signé",
        "master key", "unit door", "security staff", "management office", "contractor requires access",
    ]
    if any(term in text for term in bad_terms):
        return True
    return _is_promotional_or_brand_noise_text(text, "")


def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item) or _catalog_item_looks_unimportant(item):
        return False
    if _is_renewal_item(item):
        if not _item_is_on_or_after_scan_start(item):
            return False
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if _is_ai_placeholder_text(item.get("important_reason", "")):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if not reply:
        return False
    if _is_bad_generic_reply_text(reply.get("body", "")) or reply_needs_regeneration(reply.get("body", ""), item.get("original", {}).get("body", ""), item.get("category", "work")):
        return False
    return True


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        return 0
    changed = 0
    for key in list(emails_bucket.keys()):
        item = emails_bucket.get(key, {})
        if not isinstance(item, dict):
            del emails_bucket[key]
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
        if (
            _is_ignored_new_question_item(item)
            or _is_promotional_item(item)
            or _is_blocked_sender_address(original.get("from", ""))
            or _is_ai_placeholder_text(item.get("important_reason", ""))
            or _is_bad_generic_reply_text(reply.get("body", ""))
        ):
            del emails_bucket[key]
            changed += 1
            continue
        if _is_renewal_item(item):
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            details = _item_renewal_details(item)
            if details:
                item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
                if item.get("status") != "Already Replied":
                    item["reply"] = {**reply, "to": details.get("student_email", reply.get("to", ""))}
        else:
            item["category"] = "work" if _is_item_pharmacy_prep_related(item) else "personal"
            item["screening_version"] = EMAIL_SCREENING_VERSION
            item["ai_screened"] = True
        emails_bucket[key] = item
        changed += 1
    if changed:
        catalog["emails"] = emails_bucket
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


_previous_build_dashboard_payload_cost_cleanup = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_cost_cleanup(force_refresh=force_refresh)
    cleaned_emails = []
    renewals = []
    for email in payload.get("emails", []) or []:
        if not _is_catalog_email_visible(email):
            continue
        category = _final_category_for_item(email)
        email["category"] = category
        email["ai_screened"] = True
        email["screening_version"] = EMAIL_SCREENING_VERSION
        if category == "renewal":
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            details = _item_renewal_details(email)
            if details and email.get("reply"):
                email["reply"]["to"] = details.get("student_email", email["reply"].get("to", ""))
            renewals.append(email)
        cleaned_emails.append(email)
    payload["emails"] = cleaned_emails
    payload["renewals"] = renewals
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "renewal_emails": len(renewals),
        "work_emails": len([e for e in cleaned_emails if e.get("category") == "work"]),
        "personal_emails": len([e for e in cleaned_emails if e.get("category") == "personal"]),
    }
    return payload


_previous_perform_scan_cost_cleanup = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    print(f"[scan] starting cheap clean context scan | force_full={force_full} | model={OPENAI_FAST_MODEL}", flush=True)
    payload = _previous_perform_scan_cost_cleanup(force_full=force_full)
    try:
        removed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        summary = payload.get("scan_summary", {}) if isinstance(payload, dict) else {}
        fresh["scan_summary"] = {
            **summary,
            "cheap_model": OPENAI_FAST_MODEL,
            "reply_model": OPENAI_REPLY_MODEL,
            "catalog_cleanup_removed_or_recategorized": removed,
            "visible_work_emails": fresh.get("stats", {}).get("work_emails", 0),
            "visible_personal_emails": fresh.get("stats", {}).get("personal_emails", 0),
            "visible_renewals": fresh.get("stats", {}).get("renewal_emails", 0),
            "promo_noise_filter": "xbox/vimeo/jpmorgan/chase/newsletters/promotions/support@pharrmacyprep removed",
            "gmail_context_queries_per_reply": GMAIL_CONTEXT_QUERY_LIMIT,
            "gmail_context_max_blocks": GMAIL_CONTEXT_MAX_BLOCKS,
        }
        print(
            f"[scan] cheap clean context complete | work={fresh['scan_summary']['visible_work_emails']} | "
            f"personal={fresh['scan_summary']['visible_personal_emails']} | renewals={fresh['scan_summary']['visible_renewals']} | cleanup={removed}",
            flush=True,
        )
        return fresh
    except Exception as error:
        print(f"[scan] cheap clean context cleanup failed: {error}", flush=True)
        return payload


# ---------------------------------------------------------------------
# FINAL PATCH: strict Work/Personal cleanup + richer Gmail context replies
# ---------------------------------------------------------------------
# Appended last intentionally. Keeps the working scanner but tightens the tab split:
# - Work is ONLY Pharmacy Prep / PEBC / EprepStation / student-course-order-support context.
# - All other actionable human emails go Personal.
# - Promotional/spam rows are removed from both Work and Personal.
# - Reply writing uses more Gmail API context while staying on a cheap model.
EMAIL_SCREENING_VERSION = "2026-06-professional-context-clean-v1"
OPENAI_FAST_MODEL = (
    os.getenv("OPENAI_FAST_MODEL")
    or os.getenv("OPENAI_CHEAP_MODEL")
    or os.getenv("OPENAI_SCREENING_MODEL")
    or "gpt-4.1-nano"
)
OPENAI_REPLY_MODEL = os.getenv("OPENAI_REPLY_MODEL") or OPENAI_FAST_MODEL
MAX_AI_SCREENINGS_PER_SCAN = int(os.getenv("MAX_AI_SCREENINGS_PER_SCAN", "75"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "30"))
# More Gmail API context, not a bigger model.
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "26"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "4"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "16"))
GMAIL_CONTEXT_CHARS_PER_THREAD = int(os.getenv("GMAIL_CONTEXT_CHARS_PER_THREAD", "4200"))

# Broader hard-noise list. This is applied to saved catalog rows and future scans.
_EXTRA_PROMO_BRAND_TERMS = [
    "xbox", "game pass", "microsoft rewards", "microsoft store", "playstation", "nintendo", "steam", "epic games",
    "vimeo", "mail.vimeo", "email.vimeo", "view on vimeo", "new video from", "because you watched",
    "jp morgan", "jpmorgan", "j.p. morgan", "jpmorgan chase", "chase bank", "chase.com", "morgan stanley",
    "market update", "market insights", "investment outlook", "wealth management", "investor newsletter",
    "netflix", "spotify", "youtube", "prime video", "disney+", "twitch", "duolingo", "udemy", "coursera",
    "canva", "grammarly", "mailchimp", "hubspot", "constant contact", "campaign monitor",
]
_EXTRA_PROMO_MARKETING_TERMS = [
    "unsubscribe", "manage preferences", "manage your preferences", "view this email in your browser",
    "you are receiving this email", "you're receiving this email", "privacy policy", "email preferences",
    "newsletter", "digest", "promotion", "promotional", "limited time", "special offer", "exclusive offer",
    "sale", "deal", "deals", "discount", "coupon", "save ", "% off", "free trial", "register today",
    "join us for", "webinar", "sponsored", "advertisement", "recommended for you", "see what's new",
    "discover", "explore", "watch now", "stream now", "gaming", "trailer", "creator update",
]
_EXTRA_NOISY_SENDER_PARTS = [
    "newsletter@", "news@", "marketing@", "promo@", "promos@", "offers@", "deals@", "updates@",
    "events@", "mail.vimeo", "email.vimeo", "vimeo@", "xbox", "jpmorgan", "jpmorgan.com",
    "chase@", "morganstanley", "mailchimp", "constantcontact", "campaign", "bounce@", "no-reply@",
]

def _all_noise_text_for_item(item: Dict) -> Tuple[str, str]:
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
    sender = _sender_email_from_value(original.get("from", ""))
    text = _lower_join([
        item.get("title", ""), item.get("important_reason", ""), item.get("status", ""),
        original.get("from", ""), original.get("subject", ""), original.get("body", ""),
        reply.get("subject", ""), reply.get("body", ""),
    ])
    return text, sender


def _is_promotional_or_brand_noise_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    haystack = f"{sender}\n{text}"
    if _is_blocked_sender_address(sender):
        return True
    if _is_new_question_submitted_text(haystack):
        return True

    has_brand = any(term in haystack for term in (_PROMO_BRAND_TERMS + _EXTRA_PROMO_BRAND_TERMS))
    has_marketing = any(term in haystack for term in (_PROMO_MARKETING_TERMS + _EXTRA_PROMO_MARKETING_TERMS))
    noisy_sender = any(term in haystack for term in _EXTRA_NOISY_SENDER_PARTS)

    # Only allow a brand/bank/platform message if it is clearly a direct account-support issue,
    # not a newsletter, market note, event invite, or content recommendation.
    true_support_terms = [
        "cannot access", "can't access", "unable to access", "account locked", "payment failed", "refund request",
        "invoice attached", "contract for signature", "lease agreement", "appointment confirmation", "action required",
    ]
    true_support = any(term in haystack for term in true_support_terms) and not has_marketing
    if (has_brand or noisy_sender) and not true_support:
        return True
    if has_marketing:
        return True
    return False


def _is_promotional_thread(thread: Dict, connected_email: str = "") -> bool:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = _sender_email_from_value(latest.get("from", ""))
    text = _lower_join([latest.get("from", ""), latest.get("subject", ""), latest.get("body", "")])
    # Include full thread only for noise signals; not for Work categorization.
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_promotional_item(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    text, sender = _all_noise_text_for_item(item)
    return _is_promotional_or_brand_noise_text(text, sender)


def _latest_inbound_category_text(thread: Dict, connected_email: str = "") -> Tuple[str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = _sender_email_from_value(latest.get("from", ""))
    # Category MUST be based on latest inbound email, not old replies/signatures in the full thread.
    text = _lower_join([latest.get("from", ""), latest.get("subject", ""), latest.get("body", "")])
    return text, sender


def _strict_work_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    if not text and not sender:
        return False
    if _is_new_question_submitted_text(text):
        return False
    if _is_promotional_or_brand_noise_text(text, sender):
        return False

    # Personal/legal/finance/document items are Personal unless the latest inbound content explicitly names Pharmacy Prep/PEBC/EprepStation.
    explicit_business = [
        "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "www.pharmacyprep.com",
        "eprepstation", "eprep station", "online exam prep station", "pebc", "evaluating exam", "qualifying exam",
        "pharmacy prep course", "pharmacy prep login", "pharmacy prep access", "pharmacy prep order",
    ]
    if _hard_personal_document_text(text) and not any(term in text for term in explicit_business):
        return False

    direct_business_terms = [
        "pharmacy prep", "pharmacyprep", "www.pharmacyprep.com", "416-223-prep", "416-223-7737", "647-221-0457",
        "eprepstation", "eprep station", "online exam prep station", "pebc", "evaluating exam", "qualifying exam",
        "pebc exam", "osce", "ospe", "fpgee", "opra", "qbank", "mock exam", "mock exams", "pharmacy technician",
        "technician ospe", "technician mcq", "pharmacy prep course", "prep course", "home study plus online",
        "account renewal request", "course renewal", "pharmacy prep registration", "pharmacy prep enrollment",
    ]
    if any(term in text for term in direct_business_terms):
        return True
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True

    # Generic words like course/order/payment/login are NOT enough by themselves.
    student_terms = ["student", "students", "enrolled", "enrollment", "enrolment", "registration"]
    course_terms = ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "renewal", "extension"]
    exam_terms = ["pebc", "exam", "mcq", "osce", "ospe", "qbank", "mock", "pharmacist", "pharmacy technician"]
    if any(t in text for t in student_terms) and any(t in text for t in (course_terms + exam_terms)):
        return True
    if any(t in text for t in course_terms) and any(t in text for t in exam_terms):
        return True

    order_terms = ["order #", "order number", "new order", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer"]
    prep_context = ["pharmacy prep", "pharmacyprep", "pebc", "eprepstation", "mock", "qbank", "pharmacy exam", "prep course"]
    if any(t in text for t in order_terms) and any(t in text for t in prep_context):
        return True
    return False


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    text, sender = _latest_inbound_category_text(thread, connected_email)
    return _strict_work_text(text, sender)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item) or _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _sender_email_from_value(original.get("from", ""))
    # Use original inbound email only. Never use generated reply/signature text for category.
    text = _lower_join([original.get("from", ""), original.get("subject", ""), original.get("body", "")])
    return _strict_work_text(text, sender)


def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"


def _openai_json_response(prompt: str, model: str = None) -> Optional[Dict]:
    """Cheap JSON helper with tiny fallback so 'AI screening unavailable' rows never appear.
    The fallback is still low-cost and only triggered when the selected cheap model fails.
    """
    candidates = []
    for candidate in [model, OPENAI_FAST_MODEL, os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1-mini")]:
        candidate = (candidate or "").strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for chosen_model in candidates:
        try:
            response = client.responses.create(model=chosen_model, input=prompt)
            parsed = parse_ai_json((getattr(response, "output_text", "") or "").strip())
            if isinstance(parsed, dict):
                return parsed
            print(f"[ai] {chosen_model} returned non-JSON", flush=True)
        except Exception as error:
            print(f"[ai] {chosen_model} failed: {error}", flush=True)
            continue
    return None


def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'in:anywhere {date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    noise_excludes = (
        '-from:support@pharrmacyprep.com -from:(newsletter OR marketing OR promos OR offers OR deals OR updates) '
        '-from:(xbox OR vimeo OR jpmorgan OR chase OR mailchimp OR constantcontact) '
        '-("unsubscribe" OR "manage your preferences" OR "view this email in your browser")'
    )
    base = f'{base} {noise_excludes}'
    return [
        f'{base} in:inbox',
        f'{base} ("?" OR "please" OR "can you" OR "could you" OR "would you" OR "I need" OR "let me know" OR "not received" OR "still waiting" OR "checking in")',
        f'{base} (PEBC OR "Pharmacy Prep" OR pharmacyprep OR EprepStation OR "account renewal" OR "course renewal" OR "course access" OR "course login")',
        f'{base} (agreement OR contract OR lease OR document OR appointment OR lawyer OR bank OR insurance)',
        f'{base}',
    ]


def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if _is_ignored_new_question_thread(thread, connected_email):
        return False
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_blocked_sender_address(latest.get("from", "")):
        return False
    if _is_promotional_thread(thread, connected_email):
        return False
    sender = _sender_email_from_value(latest.get("from", ""))
    text = _lower_join([latest.get("subject", ""), latest.get("body", "")])
    hard_auto = [
        "mailer-daemon", "postmaster", "delivery status notification", "undeliverable", "verification code", "password reset",
        "security alert", "comment awaiting moderation", "please moderate", "new question submitted", "new question submited",
        "new support question has been submitted",
    ]
    if any(term in sender or term in text for term in hard_auto):
        return False
    # Actionable human messages only. Promotions/newsletters already blocked above.
    action_terms = [
        "?", "can you", "could you", "would you", "please", "i need", "need help", "question", "not received",
        "still waiting", "checking in", "unable to", "cannot access", "can't access", "send me", "provide",
        "confirm", "advise", "appointment", "meeting", "contract", "agreement", "lease", "document", "invoice", "receipt", "refund",
    ]
    if any(term in text for term in action_terms):
        return True
    # Existing multi-message human threads can be actionable even without a question mark.
    human_sender = bool(sender) and not any(noise in sender for noise in ["noreply", "no-reply", "donotreply", "notifications@", "marketing@", "newsletter@"])
    return human_sender and len(thread.get("emails", [])) >= 2 and len(text.split()) >= 8


def analyze_dashboard_thread_with_ai(thread: Dict, connected_email: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if not latest:
        return None
    forced_category = category_for_thread_strict(thread, connected_email)
    if _is_blocked_sender_address(latest.get("from", "")) or _is_ignored_new_question_thread(thread, connected_email) or _is_promotional_thread(thread, connected_email):
        return {"include": False, "category": forced_category, "title": "Filtered notification", "summary": "Promotional or system notification filtered out.", "reason": "Blocked noise", "confidence": 1.0}

    sender_name, sender_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or ""
    latest_body = compact_ai_context(latest.get("body", ""), 5200)
    prompt = f"""
Classify this Gmail thread for the assistant dashboard.

Include only actionable human messages that need review or a reply: a direct question, request, problem, missing detail, appointment/decision, student/customer support issue, legal/document task, or personal task.

Exclude promotional/newsletter/brand/update emails. This includes Xbox, Vimeo, JP Morgan/JPMorgan/Chase market updates, streaming/gaming/webinar/sales/offers, creator updates, recommendations, and anything mainly asking the user to click/watch/register/buy.

Category MUST be exactly: {forced_category}
Work is only Pharmacy Prep, PEBC, EprepStation, student course/exam/login/order/payment/renewal/support. Every other actionable human email is personal.

Return JSON only:
{{
  "include": true,
  "category": "{forced_category}",
  "title": "4-9 word dashboard title",
  "summary": "Specific one-sentence summary mentioning {display_name} and what they need.",
  "reason": "Why it needs a reply or why excluded.",
  "confidence": 0.0
}}

Sender: {latest.get('from', '')}
Subject: {subject}
Body:
{latest_body}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_FAST_MODEL)
    if not isinstance(parsed, dict):
        text = _lower_join([subject, latest.get("body", "")])
        direct_ask = any(term in text for term in _DIRECT_ACTION_TERMS) or "?" in text
        if not direct_ask:
            return {"include": False, "category": forced_category, "title": "Not actionable", "summary": "This message does not appear to need a reply.", "reason": "No direct request detected", "confidence": 0.25}
        subject_clean = re.sub(r"^(re|fw|fwd):\s*", "", subject or "Email", flags=re.IGNORECASE).strip()
        return {"include": True, "category": forced_category, "title": subject_clean[:70] or "Actionable email", "summary": f"{display_name} needs a response about {subject_clean or 'this message'}.", "reason": "Direct request detected by deterministic fallback", "confidence": 0.35}

    include = parsed.get("include", False)
    if isinstance(include, str):
        include = include.strip().lower() in ("true", "yes", "1", "include")
    if _is_promotional_thread(thread, connected_email) or _is_ignored_new_question_thread(thread, connected_email):
        include = False
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    return {
        "include": bool(include),
        "category": forced_category,
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "reason": str(parsed.get("reason", "") or "").strip(),
        "confidence": confidence,
    }


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    full_text = f"{subject}\n{body}"
    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()

    phrases = []
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
        r"account\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9_\-]{3,})",
        r"(?:course|exam|class)\s*(?:name|code|id)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9 ._\-]{3,45})",
    ]:
        for match in re.findall(pattern, full_text, flags=re.IGNORECASE):
            value = match if isinstance(match, str) else " ".join(match)
            value = re.sub(r"\s+", " ", value).strip()
            if value and value.lower() not in {p.lower() for p in phrases}:
                phrases.append(value)

    name_bits = []
    for source in [sender_name or "", infer_customer_name_from_email(sender_email) or "", sender_email.split("@", 1)[0] if sender_email else ""]:
        for part in re.split(r"[^A-Za-z]+", source):
            part = part.strip()
            if len(part) >= 3 and part.lower() not in {"pharmacy", "prep", "student", "customer", "support", "question", "wordpress", "info", "admin", "noreply", "mail"}:
                if part not in name_bits:
                    name_bits.append(part)

    keywords = []
    stop = {"please", "thanks", "thank", "hello", "regards", "email", "message", "pharmacy", "prep", "course", "order", "number", "question", "would", "could", "should", "there", "about", "from", "your", "with", "this", "attached", "sent", "send", "need", "help"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", full_text):
        token_l = token.lower()
        if token_l in stop or token_l in keywords:
            continue
        keywords.append(token_l)
        if len(keywords) >= 16:
            break

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email})',
            f'in:anywhere ({sender_email}) ("Order #" OR "New Order" OR invoice OR receipt OR payment OR login OR access OR PEBC OR course OR renewal OR registration)',
        ])
    if clean_subject:
        short_subject = clean_subject[:90].replace('"', '')
        queries.append(f'in:anywhere "{short_subject}"')
    for name_part in name_bits[:6]:
        queries.extend([f'in:anywhere "{name_part}"', f'in:sent "{name_part}"'])
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{name_part}"')
    for phrase in phrases[:8]:
        queries.append(f'in:anywhere "{phrase}"')
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{phrase}"')
    for keyword in keywords[:14]:
        if sender_email:
            queries.append(f'in:anywhere ({sender_email}) "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')

    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def gather_context_from_gmail(service, queries: List[str], current_thread_id: str = "", max_threads_per_query: int = None) -> str:
    if max_threads_per_query is None:
        max_threads_per_query = GMAIL_CONTEXT_THREADS_PER_QUERY
    context_blocks = []
    seen_threads = set()
    separator = "\n\n================ RELATED GMAIL API CONTEXT ================\n\n"
    for query in (queries or [])[:GMAIL_CONTEXT_QUERY_LIMIT]:
        try:
            thread_ids = search_threads(service, query=query, max_results=max_threads_per_query)
            for thread_id in thread_ids:
                if thread_id == current_thread_id or thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                thread = read_thread(service, thread_id)
                latest = thread.get("emails", [])[-1] if thread.get("emails") else {}
                formatted = format_thread_for_ai(thread)[:GMAIL_CONTEXT_CHARS_PER_THREAD]
                context_blocks.append(
                    f"Search query: {query}\n"
                    f"Thread ID: {thread_id}\n"
                    f"Latest subject: {latest.get('subject', '')}\n"
                    f"Latest from: {latest.get('from', '')}\n"
                    f"Thread excerpt:\n{formatted}"
                )
                if len(context_blocks) >= GMAIL_CONTEXT_MAX_BLOCKS:
                    return separator.join(context_blocks)
        except Exception as error:
            context_blocks.append(f"Search failed for query '{query}': {error}")
    return separator.join(context_blocks)


def _specific_missing_answer_sentence(category: str, sender_email: str, topic: str) -> str:
    if category == "work":
        return f"I checked the Gmail history connected to {sender_email or 'this email address'} and could not find a confirmed {topic} in the available thread context."
    return f"I checked the related Gmail history for this conversation and could not find a confirmed {topic} in the available context."


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    cleaned = []
    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        line = re.sub(r"^\s*[-*•–—]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace(" — ", ", ").replace(" – ", ", ")
        cleaned.append(line)
    body = "\n".join(cleaned)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = re.sub(r"(?i)^subject\s*:\s*.*\n+", "", body).strip()
    # Remove repeated generic opener if model adds it.
    body = re.sub(r"(?i)^thank you for (reaching out|your email)\.\s*", "", body).strip()
    if category == "work":
        signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
        if "www.pharmacyprep.com" not in body.lower():
            body = body.rstrip() + "\n\n" + signature
    return body


def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower().strip()
    bad_phrases = [
        "we received your message and will get back to you", "we received your email and will get back to you",
        "thank you for your email. we will review your request", "we will review your request and get back to you shortly",
        "i will take a look and get back to you", "we will take a look and get back to you",
        "we will check the related account, order, or course details", "reply with the correct information",
        "we will follow up shortly", "we will review the account details", "we will review your request",
        "get back to you shortly", "we will get back to you", "i will review and get back",
        "we will check and get back", "i will check and get back", "we will respond with", "i will respond with",
        "we will look into", "we will verify", "we will confirm once", "please wait while",
    ]
    if any(p in text for p in bad_phrases):
        return True
    # Too many bullets/dashes means it still looks AI-generated.
    list_lines = [line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]
    return len(list_lines) >= 2


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if len(body.split()) < 16:
        return True
    if _is_bad_generic_reply_text(body):
        return True
    if latest and body.lower().startswith(latest.lower()[:80]):
        return True
    if latest and copied_sequence_found(body, latest, sequence_len=14):
        return True
    return False


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 8000)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 11000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}")
    topic_hint = "order number, invoice, receipt, payment, login/access, course/renewal detail, agreement/contract detail, or appointment detail"
    missing_sentence = _specific_missing_answer_sentence(category, sender_email, topic_hint)
    prompt = f"""
Write a professional reply using the current Gmail thread and the related Gmail API context.

The reply must sound like a capable office assistant, not AI. Use normal paragraphs only. Do not use bullet points, numbered lists, markdown, headings, long dashes, or template-style filler.

Do not say "we will review", "we will get back to you", "I will take a look", "we will check and respond", or similar. Answer directly using the Gmail context.

When the context contains an order number, payment amount, invoice, receipt, login/access clue, course/renewal detail, prior sent reply, document/agreement detail, or date, include the specific detail in the reply.

If the exact answer is not present, use this approach in one concise paragraph: "{missing_sentence}" Then ask one specific follow-up question that would let the office answer it. Do not promise a vague future review.

For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not use the Pharmacy Prep signature.

Known facts extracted from Gmail context: {facts or 'No exact IDs/amounts extracted, but use the related Gmail context below.'}

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific dashboard summary mentioning who is asking and which Gmail context was checked",
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
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail API context:
{extra_context or 'None found'}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL)
    if not isinstance(parsed, dict):
        return _context_based_fallback_reply(thread, connected_email, category, extra_context=extra_context)
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        # One cheap retry with stronger anti-generic instruction.
        retry_prompt = prompt + "\n\nRewrite the reply now. It was too generic or too template-like. Use concrete details from the Gmail context. No vague review/get-back language."
        parsed_retry = _openai_json_response(retry_prompt, model=OPENAI_REPLY_MODEL)
        if isinstance(parsed_retry, dict):
            body = _professionalize_reply_body(str(parsed_retry.get("body", "") or "").strip(), category)
            if body and not reply_needs_regeneration(body, latest_body, category):
                parsed = parsed_retry
        if not body or reply_needs_regeneration(body, latest_body, category):
            return _context_based_fallback_reply(thread, connected_email, category, extra_context=extra_context)
    return {
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
        "body": body,
    }


def _context_based_fallback_reply(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    facts = _extract_specific_context_facts(extra_context)
    if not facts:
        return None
    if category == "work":
        body = f"""Hello {greeting},

I checked the related Gmail history for this email address and found the following matching detail: {facts}. Please confirm whether this is the record you want us to use for this request.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

I checked the related Gmail history for this conversation and found the following matching detail: {facts}. Please confirm whether this is the record you want me to use.

Regards"""
    if reply_needs_regeneration(body, latest.get("body", ""), category):
        return None
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return True
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    if _is_blocked_sender_address(original.get("from", "")):
        return True
    text, sender = _all_noise_text_for_item(item)
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery", "delivered",
        "e-transfer received", "etransfer received", "interac e-transfer", "payment received", "receipt for your payment",
        "charge receipt", "invoice paid", "tracking number", "shipment", "please moderate", "comment awaiting moderation",
        "new question submitted", "new question submited", "new support question has been submitted", "security alert",
        "verification code", "password reset", "mail delivery", "undeliverable",
    ]
    if any(term in text for term in bad_terms):
        return True
    return _is_promotional_or_brand_noise_text(text, sender)


def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    if _is_ignored_new_question_item(item) or _is_promotional_item(item) or _catalog_item_looks_unimportant(item):
        return False
    if _is_renewal_item(item):
        if not _item_is_on_or_after_scan_start(item):
            return False
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    if not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if item.get("category") not in ("work", "personal"):
        return False
    if _is_ai_placeholder_text(item.get("important_reason", "")):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if not reply:
        return False
    if _is_bad_generic_reply_text(reply.get("body", "")) or reply_needs_regeneration(reply.get("body", ""), item.get("original", {}).get("body", ""), item.get("category", "work")):
        return False
    return True


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        return 0
    changed = 0
    for key in list(emails_bucket.keys()):
        item = emails_bucket.get(key, {})
        if not isinstance(item, dict):
            del emails_bucket[key]
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
        delete_row = (
            _is_ignored_new_question_item(item)
            or _is_promotional_item(item)
            or _catalog_item_looks_unimportant(item)
            or _is_blocked_sender_address(original.get("from", ""))
            or _is_ai_placeholder_text(item.get("important_reason", ""))
            or _is_ai_placeholder_text(reply.get("body", ""))
            or _is_bad_generic_reply_text(reply.get("body", ""))
        )
        if delete_row:
            del emails_bucket[key]
            changed += 1
            continue
        if _is_renewal_item(item):
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            details = _item_renewal_details(item)
            if details:
                item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
                if item.get("status") != "Already Replied":
                    item["reply"] = {**reply, "to": details.get("student_email", reply.get("to", ""))}
        else:
            item["category"] = "work" if _is_item_pharmacy_prep_related(item) else "personal"
            item["screening_version"] = EMAIL_SCREENING_VERSION
            item["ai_screened"] = True
        emails_bucket[key] = item
        changed += 1
    if changed:
        catalog["emails"] = emails_bucket
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


def _recategorize_and_clean_payload(payload: Dict) -> Dict:
    cleaned_emails = []
    renewals = []
    for email in payload.get("emails", []) or []:
        if not _is_catalog_email_visible(email):
            continue
        category = _final_category_for_item(email)
        email["category"] = category
        email["ai_screened"] = True
        email["screening_version"] = EMAIL_SCREENING_VERSION
        if category == "renewal":
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            details = _item_renewal_details(email)
            if details and email.get("reply"):
                email["reply"]["to"] = details.get("student_email", email["reply"].get("to", ""))
            renewals.append(email)
        cleaned_emails.append(email)
    payload["emails"] = cleaned_emails
    payload["renewals"] = renewals
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {
        **payload.get("stats", {}),
        "pending_replies": len(payload["pending_replies"]),
        "renewal_emails": len(renewals),
        "work_emails": len([e for e in cleaned_emails if e.get("category") == "work"]),
        "personal_emails": len([e for e in cleaned_emails if e.get("category") == "personal"]),
    }
    return payload


_previous_build_dashboard_payload_professional_context = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_professional_context(force_refresh=force_refresh)
    return _recategorize_and_clean_payload(payload)


_previous_perform_scan_professional_context = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    print(f"[scan] starting professional context cleanup scan | force_full={force_full} | model={OPENAI_FAST_MODEL}", flush=True)
    payload = _previous_perform_scan_professional_context(force_full=force_full)
    try:
        removed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        summary = payload.get("scan_summary", {}) if isinstance(payload, dict) else {}
        fresh["scan_summary"] = {
            **summary,
            "cheap_model": OPENAI_FAST_MODEL,
            "reply_model": OPENAI_REPLY_MODEL,
            "catalog_cleanup_removed_or_recategorized": removed,
            "visible_work_emails": fresh.get("stats", {}).get("work_emails", 0),
            "visible_personal_emails": fresh.get("stats", {}).get("personal_emails", 0),
            "visible_renewals": fresh.get("stats", {}).get("renewal_emails", 0),
            "promo_noise_filter": "promotional/spam rows removed from Work and Personal",
            "work_category_rule": "Work is Pharmacy Prep/PEBC/EprepStation only; everything else actionable is Personal",
            "gmail_context_queries_per_reply": GMAIL_CONTEXT_QUERY_LIMIT,
            "gmail_context_max_blocks": GMAIL_CONTEXT_MAX_BLOCKS,
        }
        print(
            f"[scan] professional context cleanup complete | work={fresh['scan_summary']['visible_work_emails']} | "
            f"personal={fresh['scan_summary']['visible_personal_emails']} | renewals={fresh['scan_summary']['visible_renewals']} | cleanup={removed}",
            flush=True,
        )
        return fresh
    except Exception as error:
        print(f"[scan] professional context cleanup failed: {error}", flush=True)
        return payload


# ---------------------------------------------------------------------
# FINAL PATCH: correct Work/Personal split + thread history UI + concise contextual replies
# ---------------------------------------------------------------------
EMAIL_SCREENING_VERSION = "2026-06-thread-context-split-v1"
GMAIL_CONTEXT_QUERY_LIMIT = int(os.getenv("GMAIL_CONTEXT_QUERY_LIMIT", "32"))
GMAIL_CONTEXT_THREADS_PER_QUERY = int(os.getenv("GMAIL_CONTEXT_THREADS_PER_QUERY", "4"))
GMAIL_CONTEXT_MAX_BLOCKS = int(os.getenv("GMAIL_CONTEXT_MAX_BLOCKS", "18"))
GMAIL_CONTEXT_CHARS_PER_THREAD = int(os.getenv("GMAIL_CONTEXT_CHARS_PER_THREAD", "4600"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "28"))

_FINAL_WORK_EXPLICIT_TERMS = [
    "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "www.pharmacyprep.com",
    "416-223-prep", "416-223-7737", "647-221-0457", "eprepstation", "eprep station",
    "online exam prep station", "pebc", "evaluating exam", "qualifying exam", "pebc exam",
    "osce", "ospe", "fpgee", "opra", "naplex", "qbank", "question bank", "mock exam", "mock exams",
    "pharmacist qualifying", "pharmacist evaluating", "pharmacy technician", "technician mcq", "technician ospe",
    "pharmacy exam", "prep course", "pharmacy prep course", "home study plus online",
]
_FINAL_PERSONAL_OFFICE_TERMS = [
    "lease", "lease agreement", "rental agreement", "tenancy", "landlord", "tenant", "rent", "condo",
    "mortgage", "bank statement", "insurance", "policy document", "legal document", "lawyer", "attorney",
    "contract", "agreement", "agreement copy", "copy of agreement", "docusign", "adobe sign", "signed document",
    "signature request", "office lease", "employment agreement", "tax", "cra", "appointment", "medical", "doctor",
]
_FINAL_PROMO_EXTRAS = [
    "xbox", "game pass", "vimeo", "jp morgan", "jpmorgan", "j.p. morgan", "chase", "market update",
    "market insights", "newsletter", "unsubscribe", "promotion", "promotional", "limited time", "special offer",
    "exclusive offer", "sale", "discount", "coupon", "webinar", "sponsored", "recommended for you",
    "watch now", "stream now", "creator update", "gaming", "mail.vimeo", "email.vimeo",
]


def _final_call_bool(fn_name: str, *args) -> bool:
    try:
        fn = globals().get(fn_name)
        return bool(fn(*args)) if callable(fn) else False
    except Exception:
        return False


def _final_latest_text_and_sender(thread: Dict, connected_email: str = "") -> Tuple[str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = _sender_email_from_value(latest.get("from", "")) if "_sender_email_from_value" in globals() else parseaddr(latest.get("from", ""))[1].lower().strip()
    text = "\n".join([str(latest.get("from", "") or ""), str(latest.get("subject", "") or ""), str(latest.get("body", "") or "")]).lower()
    return text, sender


def _final_has_promotional_noise(text: str, sender: str = "") -> bool:
    haystack = f"{sender}\n{text}".lower()
    if _final_call_bool("_is_promotional_or_brand_noise_text", text, sender):
        return True
    if any(term in haystack for term in _FINAL_PROMO_EXTRAS):
        direct_support = any(term in haystack for term in [
            "cannot access", "can't access", "unable to access", "payment failed", "refund request",
            "invoice attached", "contract for signature", "action required", "account locked",
        ])
        if not direct_support:
            return True
    return False


def _final_work_evidence_text(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower().strip()
    haystack = f"{sender}\n{text}"
    if not haystack.strip():
        return False
    if _final_call_bool("_is_blocked_sender_address", sender):
        return False
    if _final_call_bool("_is_new_question_submitted_text", haystack) or "new question submitted" in haystack or "new question submited" in haystack:
        return False
    if _final_has_promotional_noise(text, sender):
        return False

    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    if any(term in haystack for term in _FINAL_WORK_EXPLICIT_TERMS):
        return True

    if any(term in haystack for term in _FINAL_PERSONAL_OFFICE_TERMS):
        return False

    has_student = any(term in haystack for term in ["student", "students", "enrolled", "enrollment", "enrolment", "registered", "registration", "customer"])
    has_course_or_access = any(term in haystack for term in ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "renewal", "extension", "book", "materials"])
    has_exam = any(term in haystack for term in ["exam", "mock", "mcq", "osce", "ospe", "qbank", "evaluating", "qualifying", "pharmacist", "pharmacy technician"])
    has_order_support = any(term in haystack for term in ["order number", "order #", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer"])
    if (has_student and (has_course_or_access or has_exam or has_order_support)) or (has_course_or_access and has_exam):
        return True
    return False


def _strict_work_text(text: str, sender: str = "") -> bool:
    return _final_work_evidence_text(text, sender)


def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest_text, sender = _final_latest_text_and_sender(thread, connected_email)
    if _final_work_evidence_text(latest_text, sender):
        return True
    if any(term in latest_text for term in _FINAL_PERSONAL_OFFICE_TERMS) or _final_has_promotional_noise(latest_text, sender):
        return False
    full_context = _thread_body_context_only(thread, connected_email) if "_thread_body_context_only" in globals() else combined_thread_text(thread).lower()
    return _final_work_evidence_text(full_context, sender)


def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"


def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    return category_for_thread_strict(thread, connected_email)


def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item) or _is_ignored_new_question_item(item) or _is_promotional_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _sender_email_from_value(original.get("from", "")) if "_sender_email_from_value" in globals() else parseaddr(original.get("from", ""))[1].lower().strip()
    text = "\n".join([
        str(original.get("from", "") or ""), str(original.get("subject", "") or ""), str(original.get("body", "") or ""),
        str(item.get("title", "") or ""), str(item.get("important_reason", "") or ""),
    ]).lower()
    return _final_work_evidence_text(text, sender)


def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"


def _thread_history_for_ui(thread: Dict, connected_email: str = "") -> List[Dict]:
    connected = (connected_email or "").lower().strip()
    history = []
    emails = thread.get("emails", []) or []
    total = len(emails)
    for index, email in enumerate(emails, start=1):
        sender_email = parseaddr(email.get("from", ""))[1].lower().strip()
        from_us = bool(connected and sender_email == connected)
        label = "First email" if index == 1 else ("Latest email" if index == total else f"Email {index}")
        history.append({
            "index": index, "label": label, "direction": "Sent by us" if from_us else "Received",
            "from": email.get("from", ""), "to": email.get("to", ""), "date": email.get("date", ""),
            "subject": email.get("subject", ""), "body": clean_preview_text(email.get("body", ""), 4500),
        })
    return history


_previous_build_general_email_item_thread_context = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _is_ignored_new_question_thread(thread, connected_email) or _is_promotional_thread(thread, connected_email):
        return None
    item = _previous_build_general_email_item_thread_context(service, thread, connected_email, personal_label_id, work_label_id)
    if not item:
        return None
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return item
    category = category_for_thread_strict(thread, connected_email)
    item["category"] = category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    item["thread_history"] = _thread_history_for_ui(thread, connected_email)
    if isinstance(item.get("reply"), dict):
        item["reply"]["body"] = _professionalize_reply_body(item["reply"].get("body", ""), category)
    return item


def heuristic_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_raw = latest.get("from", "")
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip()
    display_name = sender_display_name(sender_raw, sender_email)
    subject = latest.get("subject", "") or ""
    body = latest.get("body", "") or ""
    text = f"{subject}\n{body}"
    clean_subject = re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE).strip()

    queries = []
    if sender_email:
        queries.extend([
            f'in:anywhere from:{sender_email}', f'in:anywhere to:{sender_email}', f'in:anywhere ({sender_email})', f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email}) ("Order #" OR "New Order" OR invoice OR receipt OR payment OR paid OR total)',
            f'in:anywhere ({sender_email}) (login OR access OR password OR course OR class OR notes OR recording OR renewal OR extension OR PEBC OR exam)',
        ])
    for name in [sender_name, display_name, infer_customer_name_from_email(sender_email or "") if "infer_customer_name_from_email" in globals() else ""]:
        name = re.sub(r"[^A-Za-z0-9 .'-]", " ", str(name or "")).strip()
        if name and len(name) >= 3 and "@" not in name:
            queries.append(f'in:anywhere "{name}"')
            queries.append(f'in:sent "{name}"')
    if clean_subject and len(clean_subject) >= 4:
        queries.append(f'in:anywhere "{" ".join(clean_subject.split()[:8])}"')
    for pattern in [
        r"order\s*(?:number|#)?\s*[:#]?\s*(\d{3,})", r"#\s*(\d{3,})",
        r"invoice\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})", r"receipt\s*(?:number|#)?\s*[:#]?\s*([A-Za-z0-9\-]{3,})",
    ]:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            value = (match if isinstance(match, str) else " ".join(match)).strip()
            if value:
                queries.append(f'in:anywhere "{value}"')
                if sender_email:
                    queries.append(f'in:anywhere ({sender_email}) "{value}"')
    for term in ["pebc", "evaluating", "qualifying", "osce", "mcq", "qbank", "mock", "course", "class", "login", "access", "password", "notes", "recording", "book", "materials", "order", "invoice", "receipt", "payment", "refund", "renewal", "extension", "registration", "enrollment", "student"]:
        if term in text.lower():
            queries.append(f'in:anywhere "{term}"')
            if sender_email:
                queries.append(f'in:anywhere ({sender_email}) "{term}"')
    deduped = []
    for q in queries:
        if q and q not in deduped:
            deduped.append(q)
    return deduped[:GMAIL_CONTEXT_QUERY_LIMIT]


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    cleaned_lines = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        line = re.sub(r"^\s*[-*•–—]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace(" — ", ", ").replace(" – ", ", ").replace(" - ", ", ")
        if line:
            cleaned_lines.append(line)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")
    body = "\n".join(cleaned_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = re.sub(r"(?i)^subject\s*:\s*.*\n+", "", body).strip()
    body = re.sub(r"(?i)^thank you for (reaching out|your email|contacting us)\.\s*", "", body).strip()
    body = re.sub(r"(?i)\bwe (will|would) (review|check|look into|verify) (this|your request|the details).*?(\.|\n)", "", body).strip()
    body = re.sub(r"(?i)\b(i|we) (will|would) get back to you (shortly|soon)?\.?", "", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    main = body
    if "www.pharmacyprep.com" in body.lower():
        main = re.split(r"(?i)regards\s+pharmacy prep", body, maxsplit=1)[0].strip()
    paragraphs = [p.strip() for p in main.split("\n\n") if p.strip()]
    if len(paragraphs) > 3:
        paragraphs = paragraphs[:3]
    main = "\n\n".join(paragraphs).strip()
    return (main.rstrip() + "\n\n" + signature).strip() if category == "work" and "www.pharmacyprep.com" not in main.lower() else main.strip()


def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower().strip()
    bad_phrases = [
        "we received your message and will get back to you", "we received your email and will get back to you",
        "thank you for your email. we will review your request", "we will review your request and get back to you shortly",
        "i will take a look and get back to you", "we will take a look and get back to you",
        "we will check the related account, order, or course details", "reply with the correct information",
        "we will follow up shortly", "we will review the account details", "we will review your request",
        "get back to you shortly", "we will get back to you", "i will review and get back", "we will review and get back",
        "we will check and get back", "i will check and get back", "we will look into", "please wait while",
    ]
    if any(p in text for p in bad_phrases):
        return True
    return len([line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]) >= 1


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if len(body.split()) < 14 or _is_bad_generic_reply_text(body):
        return True
    if latest and (body.lower().startswith(latest.lower()[:80]) or copied_sequence_found(body, latest, sequence_len=14)):
        return True
    return False


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    sender_name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7600)
    thread_text = compact_ai_context(format_thread_for_ai(thread), 12500)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}")
    prompt = f"""
Write a short professional email reply using the current Gmail thread plus related Gmail API context.

Use normal paragraphs only. No bullet points, numbered lists, markdown, headings, dashes, em dashes, or template language. Keep it concise: usually 70 to 130 words before the signature.

Do not say "we will review", "we will get back to you", "I will check", "we will check and respond", or similar filler. Answer directly with the best available Gmail context. If the exact answer is missing, say what Gmail history was checked and ask one specific follow-up question.

Include exact order numbers, dates, payment/invoice/receipt details, login/access clues, course/renewal details, agreement/contract details, or prior sent reply details when the Gmail context provides them.

For work emails, include the Pharmacy Prep signature exactly. For personal emails, do not use the Pharmacy Prep signature.

Known extracted Gmail facts: {facts or 'None extracted; use the context below.'}

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence dashboard summary mentioning who is asking and what Gmail context was checked",
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
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail API context:
{extra_context or 'None found'}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL)
    if not isinstance(parsed, dict):
        return _context_based_fallback_reply(thread, connected_email, category, extra_context=extra_context)
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        retry_prompt = prompt + "\n\nRewrite once. The previous reply was generic, too long, or used list/dash formatting. Use one or two specific facts from Gmail context and keep it short."
        retry = _openai_json_response(retry_prompt, model=OPENAI_REPLY_MODEL)
        if isinstance(retry, dict):
            retry_body = _professionalize_reply_body(str(retry.get("body", "") or "").strip(), category)
            if retry_body and not reply_needs_regeneration(retry_body, latest_body, category):
                parsed = retry
                body = retry_body
    if not body or reply_needs_regeneration(body, latest_body, category):
        return _context_based_fallback_reply(thread, connected_email, category, extra_context=extra_context)
    return {"title": str(parsed.get("title", "") or "").strip(), "summary": str(parsed.get("summary", "") or "").strip(), "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject, "body": body}


def _context_based_fallback_reply(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    if _is_promotional_thread(thread, connected_email) or _is_blocked_sender_address(latest.get("from", "")):
        return None
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    facts = _extract_specific_context_facts(extra_context)
    if not facts:
        return None
    if category == "work":
        body = f"""Hello {greeting},

I checked the related Gmail history for this email address and found this matching detail: {facts}. Please confirm whether this is the correct record for your request.

Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com"""
    else:
        body = f"""Hello {greeting},

I checked the related Gmail history for this conversation and found this matching detail: {facts}. Please confirm whether this is the correct record to use.

Regards"""
    body = _professionalize_reply_body(body, category)
    if reply_needs_regeneration(body, latest.get("body", ""), category):
        return None
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails_bucket = catalog.setdefault("emails", {})
    if not isinstance(emails_bucket, dict):
        return 0
    changed = 0
    for key in list(emails_bucket.keys()):
        item = emails_bucket.get(key, {})
        if not isinstance(item, dict):
            del emails_bucket[key]
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        reply = item.get("reply", {}) if isinstance(item.get("reply", {}), dict) else {}
        delete_row = (_is_ignored_new_question_item(item) or _is_promotional_item(item) or _catalog_item_looks_unimportant(item) or _is_blocked_sender_address(original.get("from", "")) or _is_ai_placeholder_text(item.get("important_reason", "")) or _is_ai_placeholder_text(reply.get("body", "")) or _is_bad_generic_reply_text(reply.get("body", "")))
        if delete_row:
            del emails_bucket[key]
            changed += 1
            continue
        if _is_renewal_item(item):
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            details = _item_renewal_details(item)
            if details:
                item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
                if item.get("status") != "Already Replied" and isinstance(item.get("reply"), dict):
                    item["reply"]["to"] = details.get("student_email", item["reply"].get("to", ""))
        else:
            item["category"] = "work" if _is_item_pharmacy_prep_related(item) else "personal"
            item["screening_version"] = EMAIL_SCREENING_VERSION
            item["ai_screened"] = True
            if isinstance(item.get("reply"), dict):
                item["reply"]["body"] = _professionalize_reply_body(item["reply"].get("body", ""), item["category"])
        emails_bucket[key] = item
        changed += 1
    if changed:
        catalog["emails"] = emails_bucket
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


def _recategorize_and_clean_payload(payload: Dict) -> Dict:
    cleaned_emails, renewals = [], []
    for email in payload.get("emails", []) or []:
        if not _is_catalog_email_visible(email):
            continue
        category = _final_category_for_item(email)
        email["category"] = category
        email["ai_screened"] = True
        email["screening_version"] = EMAIL_SCREENING_VERSION
        if category == "renewal":
            email["is_renewal_request"] = True
            email["renewal_request"] = True
            details = _item_renewal_details(email)
            if details and email.get("reply"):
                email["reply"]["to"] = details.get("student_email", email["reply"].get("to", ""))
            renewals.append(email)
        cleaned_emails.append(email)
    payload["emails"] = cleaned_emails
    payload["renewals"] = renewals
    payload["pending_replies"] = [item["thread_id"] for item in cleaned_emails if item.get("reply")] + [item["thread_id"] for item in payload.get("orders", []) if item.get("reply")]
    payload["stats"] = {**payload.get("stats", {}), "pending_replies": len(payload["pending_replies"]), "renewal_emails": len(renewals), "work_emails": len([e for e in cleaned_emails if e.get("category") == "work"]), "personal_emails": len([e for e in cleaned_emails if e.get("category") == "personal"])}
    return payload


_previous_build_dashboard_payload_thread_context = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_thread_context(force_refresh=force_refresh)
    return _recategorize_and_clean_payload(payload)


_previous_perform_scan_thread_context = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    print(f"[scan] starting final thread/context scan | force_full={force_full} | model={OPENAI_FAST_MODEL}", flush=True)
    payload = _previous_perform_scan_thread_context(force_full=force_full)
    try:
        removed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        summary = payload.get("scan_summary", {}) if isinstance(payload, dict) else {}
        fresh["scan_summary"] = {**summary, "cheap_model": OPENAI_FAST_MODEL, "reply_model": OPENAI_REPLY_MODEL, "catalog_cleanup_removed_or_recategorized": removed, "visible_work_emails": fresh.get("stats", {}).get("work_emails", 0), "visible_personal_emails": fresh.get("stats", {}).get("personal_emails", 0), "visible_renewals": fresh.get("stats", {}).get("renewal_emails", 0), "work_category_rule": "Work is Pharmacy Prep/PEBC/EprepStation/student-course-exam only; every other actionable email is Personal", "gmail_context_queries_per_reply": GMAIL_CONTEXT_QUERY_LIMIT, "thread_history_ui": "enabled"}
        print(f"[scan] final thread/context complete | work={fresh['scan_summary']['visible_work_emails']} | personal={fresh['scan_summary']['visible_personal_emails']} | renewals={fresh['scan_summary']['visible_renewals']} | cleanup={removed}", flush=True)
        return fresh
    except Exception as error:
        print(f"[scan] final thread/context cleanup failed: {error}", flush=True)
        return payload


@app.route("/api/threads/<thread_id>/history")
def api_thread_history(thread_id: str):
    try:
        service = get_gmail_service()
        connected_email = get_connected_email(service)
        source_thread_id = thread_id
        catalog = get_dashboard_catalog()
        item = (catalog.get("emails", {}) or {}).get(thread_id) or (catalog.get("orders", {}) or {}).get(thread_id) or {}
        if not item:
            for bucket_name in ("emails", "orders"):
                for candidate in (catalog.get(bucket_name, {}) or {}).values():
                    if isinstance(candidate, dict) and str((candidate.get("reply") or {}).get("thread_id", "")) == str(thread_id):
                        item = candidate
                        break
                if item:
                    break
        if isinstance(item, dict):
            source_thread_id = item.get("source_thread_id") or (item.get("reply") or {}).get("source_thread_id") or thread_id
            if str(source_thread_id).startswith("renewal_"):
                source_thread_id = item.get("renewal_details", {}).get("source_thread_id") or thread_id
        thread = read_thread(service, source_thread_id)
        return jsonify({"ok": True, "thread_history": _thread_history_for_ui(thread, connected_email)})
    except GmailAuthRequired:
        return _json_gmail_auth_required() if "_json_gmail_auth_required" in globals() else (jsonify({"ok": False, "error": "Please sign in again."}), 401)
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

# ---------------------------------------------------------------------
# USER REQUESTED FINAL PATCH: organize tabs + renewal visibility + professional replies
# ---------------------------------------------------------------------
EMAIL_SCREENING_VERSION = "2026-06-user-organized-tabs-v1"
try:
    OPENAI_REPLY_MODEL
except NameError:
    OPENAI_REPLY_MODEL = os.getenv("OPENAI_REPLY_MODEL", os.getenv("OPENAI_FAST_MODEL", OPENAI_MODEL))
try:
    OPENAI_FAST_MODEL
except NameError:
    OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", OPENAI_MODEL)

_FINAL_PHARMACY_DIRECT_TERMS = [
    "pharmacy prep", "pharmacyprep", "success@pharmacyprep.com", "www.pharmacyprep.com",
    "eprepstation", "eprep station", "online exam prep station", "pebc",
    "evaluating exam", "qualifying exam", "pebc evaluating", "pebc qualifying",
    "pebc ee", " ee course", "evaluating exam course", "qualifying exam course",
    "mcq", "osce", "ospe", "qbank", "question bank", "mock exam", "mock exams",
    "pharmacist evaluating", "pharmacist qualifying", "pharmacy technician",
    "prep course", "home study plus online", "pharmacy prep course",
    "course renewal", "account renewal request", "course extension",
    "online account access", "course access", "course login", "class notes",
    "recorded videos", "live online interactive lectures",
]
_FINAL_STUDENT_TERMS = ["student", "students", "enrolled", "enrollment", "enrolment", "registered", "registration", "candidate", "customer"]
_FINAL_COURSE_TERMS = ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "renewal", "extension", "book", "books", "manual", "materials", "online account"]
_FINAL_EXAM_TERMS = ["pebc", "exam", "ee", "evaluating", "qualifying", "mcq", "osce", "ospe", "qbank", "mock", "pharmacist", "pharmacy technician", "technician"]
_FINAL_ORDER_TERMS = ["order number", "order #", "invoice", "receipt", "payment", "refund", "e-transfer", "etransfer", "order details", "paid"]
_FINAL_PERSONAL_OFFICE_TERMS = [
    "pest", "pest control", "exterminator", "lease", "lease agreement", "rental agreement", "tenancy",
    "landlord", "tenant", "rent", "condo", "apartment", "building management", "property management",
    "management office", "maintenance", "unit key", "master key", "contractor", "century21", "real estate",
    "mortgage", "bank statement", "insurance", "policy document", "legal document", "lawyer", "attorney",
    "contract", "agreement", "docusign", "adobe sign", "signed document", "signature request",
    "appointment", "doctor", "clinic", "dental", "office notice", "parking", "hydro", "utilities",
]
_FINAL_PROMO_TERMS = [
    "unsubscribe", "manage your preferences", "view this email in your browser", "newsletter", "promotion",
    "promotional", "limited time", "special offer", "exclusive offer", "sale", "discount", "coupon",
    "webinar", "sponsored", "recommended for you", "xbox", "game pass", "vimeo", "jp morgan",
    "jpmorgan", "j.p. morgan", "chase", "market update", "market insights", "getsmarter", "mit sloan",
]
_FINAL_BLOCKED_SENDERS = ["support@pharrmacyprep.com"]

def _final_email_address(value: str) -> str:
    try:
        return parseaddr(value or "")[1].lower().strip()
    except Exception:
        return ""

def _final_norm_text(*parts) -> str:
    text = "\n".join(str(p or "") for p in parts)
    text = text.replace("&zwnj;", " ").replace("\u200c", " ").replace("\u200b", " ")
    return re.sub(r"\s+", " ", text).strip().lower()

def _final_latest_text_sender(thread: Dict, connected_email: str = "") -> Tuple[str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _final_norm_text(latest.get("from", ""), latest.get("subject", ""), latest.get("body", "")), _final_email_address(latest.get("from", ""))

def _final_inbound_thread_context(thread: Dict, connected_email: str = "") -> str:
    connected = (connected_email or "").lower().strip()
    pieces = []
    for email in thread.get("emails", []) or []:
        sender = _final_email_address(email.get("from", ""))
        if connected and sender == connected:
            continue
        pieces.extend([email.get("from", ""), email.get("subject", ""), clean_preview_text(email.get("body", ""), 4000)])
    return _final_norm_text(*pieces)

def _final_is_new_question_text(text: str) -> bool:
    text = str(text or "").lower()
    return any(t in text for t in [
        "new question submitted", "new question submited", "new question has been submitted", "question submitted",
        "new support question has been submitted at eprepstation.com", "new support question"
    ])

def _final_is_promo_text(text: str, sender: str = "") -> bool:
    sender = str(sender or "").lower()
    text = str(text or "").lower()
    if any(blocked in sender for blocked in _FINAL_BLOCKED_SENDERS):
        return True
    direct_support = any(t in text for t in ["cannot access", "can't access", "unable to access", "refund request", "invoice attached", "payment failed", "order number"])
    return any(t in text or t in sender for t in _FINAL_PROMO_TERMS) and not direct_support

def _final_personal_override(text: str) -> bool:
    return any(t in str(text or "").lower() for t in _FINAL_PERSONAL_OFFICE_TERMS)

def _final_work_evidence(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    if not text and not sender:
        return False
    if _final_is_new_question_text(text) or _final_is_promo_text(text, sender):
        return False
    if any(blocked in sender for blocked in _FINAL_BLOCKED_SENDERS):
        return False
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    if any(term in text for term in _FINAL_PHARMACY_DIRECT_TERMS):
        return True
    if _final_personal_override(text):
        return False
    has_student = any(t in text for t in _FINAL_STUDENT_TERMS)
    has_course = any(t in text for t in _FINAL_COURSE_TERMS)
    has_exam = any(t in text for t in _FINAL_EXAM_TERMS)
    has_order = any(t in text for t in _FINAL_ORDER_TERMS)
    if "ee course" in text or "evaluating exam" in text or "qualifying exam" in text:
        return True
    if has_exam and (has_course or has_student or has_order):
        return True
    if has_student and (has_course or has_order) and any(t in text for t in ["exam", "mock", "qbank", "pebc", "evaluating", "qualifying", "pharmacist", "pharmacy"]):
        return True
    return False

def _final_is_renewal_text(text: str) -> bool:
    text = str(text or "").lower()
    return "account renewal request" in text or ("renewal" in text and "eprepstation" in text and ("e-mail address" in text or "email address" in text))

def _is_renewal_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    details = item.get("renewal_details", {}) if isinstance(item.get("renewal_details", {}), dict) else {}
    text = _final_norm_text(item.get("category", ""), item.get("title", ""), item.get("important_reason", ""), original.get("subject", ""), original.get("body", ""), details.get("course", ""), details.get("student_email", ""))
    return bool(item.get("is_renewal_request") or item.get("renewal_request") or item.get("category") == "renewal" or _final_is_renewal_text(text))

def _final_is_renewal_thread(thread: Dict, connected_email: str = "") -> bool:
    latest_text, _sender = _final_latest_text_sender(thread, connected_email)
    return _final_is_renewal_text(latest_text) or _final_is_renewal_text(_final_inbound_thread_context(thread, connected_email))

def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_work_evidence(latest_text, sender):
        return True
    if _final_personal_override(latest_text) or _final_is_promo_text(latest_text, sender):
        return False
    return _final_work_evidence(_final_inbound_thread_context(thread, connected_email), sender)

def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    if _final_is_renewal_thread(thread, connected_email):
        return "renewal"
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"

def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    cat = category_for_thread_strict(thread, connected_email)
    return "work" if cat == "renewal" else cat

def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _final_email_address(original.get("from", ""))
    text = _final_norm_text(original.get("from", ""), original.get("subject", ""), original.get("body", ""), item.get("title", ""), item.get("important_reason", ""))
    return _final_work_evidence(text, sender)

def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"

def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _final_email_address(original.get("from", ""))
    text = _final_norm_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _final_is_new_question_text(text) or _final_is_promo_text(text, sender):
        return True
    bad_terms = ["order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery", "delivered",
                 "tracking number", "shipment", "security alert", "verification code", "password reset", "mail delivery",
                 "undeliverable", "comment awaiting moderation", "please moderate", "delivery status notification"]
    return any(term in text for term in bad_terms)

def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict) or not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if _is_renewal_item(item):
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    category = _final_category_for_item(item)
    if category not in ("work", "personal"):
        return False
    return bool(item.get("reply")) or bool(item.get("ai_screened")) or item.get("status") in ("Already Replied", "Suggestion Removed")

def should_consider_thread_for_dashboard(thread: Dict, connected_email: str) -> bool:
    if not thread.get("emails"):
        return False
    if not _thread_is_on_or_after_scan_start(thread, connected_email):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_is_new_question_text(latest_text) or _final_is_promo_text(latest_text, sender):
        return False
    if _final_is_renewal_thread(thread, connected_email):
        return False
    request_cues = ["?", "please", "can you", "could you", "would you", "can i", "do you", "i need", "i would like",
                    "let me know", "not received", "still waiting", "unable to", "cannot access", "can't access", "send me",
                    "provide", "confirm", "question", "help", "issue", "problem", "follow up", "checking in", "refund"]
    if any(t in latest_text for t in request_cues):
        return True
    if _final_work_evidence(latest_text, sender) or _final_work_evidence(_final_inbound_thread_context(thread, connected_email), sender):
        return True
    personal_action = ["pest control", "appointment", "contract", "agreement", "lease", "lawyer", "insurance", "bank", "document", "signature request"]
    return any(t in latest_text for t in personal_action)

def _email_scan_queries(date_clause: str, connected_email: str) -> List[str]:
    base = f'in:anywhere {date_clause} -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    noise = '-from:support@pharrmacyprep.com -from:(xbox OR vimeo OR jpmorgan OR chase OR mailchimp OR constantcontact) -(unsubscribe OR newsletter OR "manage your preferences")'
    base = f'{base} {noise}'
    return [
        f'{base} in:inbox',
        f'{base} (PEBC OR "Pharmacy Prep" OR pharmacyprep OR EprepStation OR "EE course" OR "Evaluating Exam" OR "Qualifying Exam")',
        f'{base} (course OR class OR login OR access OR notes OR recording OR mock OR qbank OR exam OR student)',
        f'{base} ("order number" OR invoice OR receipt OR payment OR refund OR "not received" OR "still waiting")',
        f'{base} (agreement OR contract OR lease OR document OR appointment OR pest OR "pest control" OR lawyer OR insurance OR bank)',
        f'{base} ("can you" OR "could you" OR "would you" OR please OR "I need" OR "let me know" OR question OR help)',
    ]

def _professionalize_reply_body(body: str, category: str = "work") -> str:
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?im)^\s*subject\s*:.*\n?", "", text)
    cleaned_lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*[-*•–—]\s+", "", line).strip()
        line = re.sub(r"^\s*\d+[.)]\s+", "", line).strip()
        line = line.replace("—", ",").replace("–", ",")
        if line:
            cleaned_lines.append(line)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    fillers = [
        r"(?i)\bwe (received|have received) your (message|email).*?(\.|\n)",
        r"(?i)\bwe (will|would) (review|check|look into|verify) (this|your request|the details).*?(\.|\n)",
        r"(?i)\b(i|we) (will|would) get back to you (shortly|soon)?\.?",
        r"(?i)\bthank you for (reaching out|your email|contacting us)\.\s*",
    ]
    for pattern in fillers:
        text = re.sub(pattern, "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    if category == "work":
        main = re.split(r"(?i)\n\s*regards\s*\n\s*pharmacy prep", text, maxsplit=1)[0].strip()
        paragraphs = [p.strip() for p in main.split("\n\n") if p.strip()]
        main = "\n\n".join(paragraphs[:3]).strip() or "Please see the relevant details below."
        return (main.rstrip() + "\n\n" + signature).strip()
    text = re.split(r"(?i)\n\s*regards\s*\n\s*pharmacy prep", text, maxsplit=1)[0].strip()
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n\n".join(paragraphs[:3]).strip()

def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower()
    bad = ["we received your message", "we received your email", "we will review", "we will get back", "get back to you shortly",
           "we will check and get back", "i will check and get back", "we will look into", "we will follow up shortly",
           "we will respond with the specific information", "please wait while"]
    if any(p in text for p in bad):
        return True
    return len([line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]) > 0

def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    if len(body.split()) < 12 or _is_bad_generic_reply_text(body):
        return True
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if latest and (body.lower().startswith(latest.lower()[:80]) or copied_sequence_found(body, latest, sequence_len=14)):
        return True
    return False

def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_is_promo_text(latest_text, sender) or any(blocked in sender for blocked in _FINAL_BLOCKED_SENDERS):
        return None
    _name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7600)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 12500)
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}") if "_extract_specific_context_facts" in globals() else ""
    prompt = f"""
Write a concise, professional email reply using the current Gmail thread and related Gmail API context.

Do not use bullet points, numbered lists, dashes, headings, markdown, or filler. Do not say "we received your email", "we will review", "we will check and get back", or similar. Answer directly using the best available context. Keep it short, normally 70 to 130 words before the signature.

Use exact details from Gmail context when available, including order numbers, payment or receipt details, login/access details, course/EE/PEBC details, renewal details, dates, or prior sent replies. If the exact answer is not available in Gmail context, say what detail is missing and ask one specific follow-up question.

For Work emails, include the Pharmacy Prep signature exactly. For Personal emails, do not include the Pharmacy Prep signature.

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence dashboard summary mentioning who is asking and what Gmail context was checked",
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
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Extracted Gmail facts:
{facts or 'None extracted'}

Related Gmail API context:
{extra_context or 'None found'}
"""
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL) if "_openai_json_response" in globals() else None
    if not isinstance(parsed, dict):
        return None
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        retry_prompt = prompt + "\n\nRewrite once. The previous reply was generic or used list formatting. Use one specific Gmail context detail if available."
        retry = _openai_json_response(retry_prompt, model=OPENAI_REPLY_MODEL) if "_openai_json_response" in globals() else None
        if isinstance(retry, dict):
            retry_body = _professionalize_reply_body(str(retry.get("body", "") or "").strip(), category)
            if retry_body and not reply_needs_regeneration(retry_body, latest_body, category):
                parsed = retry
                body = retry_body
    if not body or reply_needs_regeneration(body, latest_body, category):
        return None
    return {"title": str(parsed.get("title", "") or "").strip(), "summary": str(parsed.get("summary", "") or "").strip(), "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject, "body": body}

_previous_build_general_email_item_user_organized = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _final_is_renewal_thread(thread, connected_email):
        return None
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_is_new_question_text(latest_text) or _final_is_promo_text(latest_text, sender):
        return None
    item = _previous_build_general_email_item_user_organized(service, thread, connected_email, personal_label_id, work_label_id)
    if not item or item.get("filtered_out") or item.get("status") == "Filtered Out":
        return item
    category = category_for_thread_strict(thread, connected_email)
    if category == "renewal":
        return None
    item["category"] = category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    if "_thread_history_for_ui" in globals():
        item["thread_history"] = _thread_history_for_ui(thread, connected_email)
    if isinstance(item.get("reply"), dict):
        item["reply"]["body"] = _professionalize_reply_body(item["reply"].get("body", ""), category)
    return item

def _final_prepare_renewal_item(item: Dict) -> Dict:
    item = dict(item or {})
    item["category"] = "renewal"
    item["is_renewal_request"] = True
    item["renewal_request"] = True
    item["filtered_out"] = False
    item["ai_screened"] = True
    item["screening_version"] = EMAIL_SCREENING_VERSION
    details = _item_renewal_details(item) if "_item_renewal_details" in globals() else None
    if details:
        item["renewal_details"] = {**(item.get("renewal_details") if isinstance(item.get("renewal_details"), dict) else {}), **details}
        if isinstance(item.get("reply"), dict):
            item["reply"]["to"] = details.get("student_email", item["reply"].get("to", ""))
            item["reply"]["body"] = _professionalize_reply_body(item["reply"].get("body", ""), "work")
    return item

def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails = catalog.setdefault("emails", {})
    changed = 0
    new_emails = {}
    for key, item in list((emails or {}).items()):
        if not isinstance(item, dict):
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        sender = _final_email_address(original.get("from", ""))
        text = _final_norm_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
        if _final_is_new_question_text(text) or _final_is_promo_text(text, sender):
            changed += 1
            continue
        if _is_renewal_item(item):
            item = _final_prepare_renewal_item(item)
            stable = item.get("thread_id") or key
            try:
                details = _item_renewal_details(item)
                if details and "_renewal_stable_thread_id" in globals():
                    stable = _renewal_stable_thread_id(details.get("student_email", ""), details.get("course", ""))
                    item["thread_id"] = stable
                    if isinstance(item.get("reply"), dict):
                        item["reply"]["thread_id"] = stable
            except Exception:
                pass
            new_emails[stable] = item
            if stable != key or item.get("category") != emails.get(key, {}).get("category"):
                changed += 1
            continue
        new_category = "work" if _is_item_pharmacy_prep_related(item) else "personal"
        if item.get("category") != new_category or item.get("screening_version") != EMAIL_SCREENING_VERSION:
            changed += 1
        item["category"] = new_category
        item["screening_version"] = EMAIL_SCREENING_VERSION
        item["ai_screened"] = True
        if isinstance(item.get("reply"), dict):
            new_body = _professionalize_reply_body(item["reply"].get("body", ""), new_category)
            if new_body != item["reply"].get("body", ""):
                changed += 1
            item["reply"]["body"] = new_body
        new_emails[key] = item
    if changed:
        catalog["emails"] = new_emails
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed

_previous_build_dashboard_payload_user_organized = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    _cleanup_catalog_for_tabs()
    payload = _previous_build_dashboard_payload_user_organized(force_refresh=force_refresh)
    cleaned = []
    renewals = []
    for email in payload.get("emails", []) or []:
        if not _is_catalog_email_visible(email):
            continue
        if _is_renewal_item(email):
            email = _final_prepare_renewal_item(email)
            renewals.append(email)
        else:
            email["category"] = _final_category_for_item(email)
            email["screening_version"] = EMAIL_SCREENING_VERSION
            email["ai_screened"] = True
        cleaned.append(email)
    payload["emails"] = cleaned
    payload["renewals"] = renewals
    payload["pending_replies"] = [e["thread_id"] for e in cleaned if e.get("reply")] + [o["thread_id"] for o in payload.get("orders", []) if o.get("reply")]
    payload["stats"] = {**payload.get("stats", {}), "pending_replies": len(payload["pending_replies"]), "renewal_emails": len(renewals), "work_emails": len([e for e in cleaned if e.get("category") == "work"]), "personal_emails": len([e for e in cleaned if e.get("category") == "personal"])}
    return payload

_previous_perform_gmail_scan_user_organized = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _previous_perform_gmail_scan_user_organized(force_full=force_full)
    try:
        cleaned_count = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        fresh["scan_summary"] = {**(payload.get("scan_summary", {}) if isinstance(payload, dict) else {}), "catalog_cleanup_changed": cleaned_count, "work_rule": "Work only Pharmacy Prep/PEBC/EprepStation/student course/exam/order/support; every other actionable email is Personal", "renewals_visible": fresh.get("stats", {}).get("renewal_emails", 0), "order_auto_reply": "unchanged"}
        return fresh
    except Exception as error:
        print(f"[scan] user requested cleanup failed: {error}", flush=True)
        return payload


# ---------------------------------------------------------------------
# FAST USER PATCH: remove promo/no-reply rows + prevent empty replies
# ---------------------------------------------------------------------
# Only touches the requested behavior: Work/Personal/Renewals organization,
# promotional filtering, and suggested reply quality. Order auto-reply code is unchanged.
EMAIL_SCREENING_VERSION = "2026-06-reply-promo-fastfix-v1"

_EXTRA_PROMO_TERMS = [
    "openai", "chatgpt", "api credits", "usage limit", "usage alert", "billing alert",
    "subscription", "your subscription", "plan renewal", "renewal reminder", "trial ends",
    "product update", "weekly digest", "monthly digest", "recommended", "recommendations",
    "newsletter", "unsubscribe", "manage your preferences", "view this email in your browser",
    "promo", "promotion", "promotional", "limited time", "special offer", "exclusive offer",
    "sale", "discount", "coupon", "deal", "deals", "webinar", "sponsored", "marketing",
    "xbox", "game pass", "vimeo", "jp morgan", "jpmorgan", "j.p. morgan", "chase",
    "market update", "market insights", "getsmarter", "mit sloan", "course team at getsmarter",
]
_EXTRA_NO_REPLY_SENDERS = [
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications@", "notification@",
    "marketing@", "mailer@", "mail@", "news@", "newsletter@", "updates@", "info@openai.com",
    "noreply@openai.com", "no-reply@openai.com", "team@email.openai.com", "openai@mail.openai.com",
    "support@pharrmacyprep.com",
]

_prev_final_is_promo_text_fastfix = _final_is_promo_text if '_final_is_promo_text' in globals() else None
_prev_catalog_item_looks_unimportant_fastfix = _catalog_item_looks_unimportant if '_catalog_item_looks_unimportant' in globals() else None
_prev_build_general_email_item_fastfix = build_general_email_item if 'build_general_email_item' in globals() else None


def _fast_has_real_user_request(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in [
        "?", "can you", "could you", "would you", "please send", "please provide",
        "please confirm", "please advise", "i need", "need help", "i would like",
        "how do i", "when will", "where is", "what is", "not received", "still waiting",
        "cannot access", "can't access", "unable to access", "refund request", "payment failed",
        "order number", "invoice", "receipt", "login", "access", "password", "extension",
        "renewal", "spare time", "available", "availability", "connect", "call me", "call", "schedule",
    ])


def _final_is_promo_text(text: str, sender: str = "") -> bool:
    sender = str(sender or "").lower()
    lowered = str(text or "").lower()
    try:
        if _is_renewal_item({"title": lowered}):
            return False
    except Exception:
        pass
    if any(blocked in sender for blocked in _FINAL_BLOCKED_SENDERS):
        return True
    if _prev_final_is_promo_text_fastfix and _prev_final_is_promo_text_fastfix(lowered, sender):
        return True
    if any(term in sender for term in _EXTRA_NO_REPLY_SENDERS) and not _fast_has_real_user_request(lowered):
        return True
    if any(term in lowered or term in sender for term in _EXTRA_PROMO_TERMS) and not _fast_has_real_user_request(lowered):
        return True
    if ("unsubscribe" in lowered or "manage preferences" in lowered or "view in browser" in lowered) and not _fast_has_real_user_request(lowered):
        return True
    return False


def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _final_email_address(original.get("from", ""))
    text = _final_norm_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _final_is_new_question_text(text) or _final_is_promo_text(text, sender):
        return True
    if _prev_catalog_item_looks_unimportant_fastfix:
        return _prev_catalog_item_looks_unimportant_fastfix(item)
    return False


def _substantive_reply_words(body: str) -> int:
    text = str(body or "")
    text = re.split(r"(?i)\n\s*regards\b", text, maxsplit=1)[0]
    text = re.sub(r"(?i)^\s*(hello|hi|dear)\s+[^,\n]+,?", "", text).strip()
    return len(re.findall(r"[A-Za-z0-9']+", text))


def _reply_is_only_greeting_or_signature(body: str) -> bool:
    return _substantive_reply_words(body) < 10


def _professionalize_reply_body(body: str, category: str = "work") -> str:
    original = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    original = original.replace("—", ",").replace("–", ",")
    original = re.sub(r"(?im)^\s*subject\s*:.*\n?", "", original)
    lines = []
    for line in original.split("\n"):
        cleaned = re.sub(r"^\s*[-*•–—]\s+", "", line).strip()
        cleaned = re.sub(r"^\s*\d+[.)]\s+", "", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
        elif lines and lines[-1] != "":
            lines.append("")
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    filler_patterns = [
        r"(?i)\bwe (received|have received) your (message|email)[^.\n]*(\.|\n)",
        r"(?i)\bthank you for (reaching out|your email|contacting us)\.\s*",
        r"(?i)\bwe (will|would) (review|look into|check and get back|get back to you)[^.\n]*(\.|\n)",
        r"(?i)\bi (will|would) (review|look into|check and get back|get back to you)[^.\n]*(\.|\n)",
    ]
    candidate = text
    for pattern in filler_patterns:
        candidate = re.sub(pattern, "", candidate).strip()
    if _substantive_reply_words(candidate) >= 10:
        text = candidate
    signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    main = re.split(r"(?i)\n\s*regards\b", text, maxsplit=1)[0].strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", main) if p.strip()]
    main = "\n\n".join(paragraphs[:2]).strip()
    if _reply_is_only_greeting_or_signature(main):
        return ""
    if category == "work":
        return (main.rstrip() + "\n\n" + signature).strip()
    return (main.rstrip() + "\n\nRegards").strip()


def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower().strip()
    if not text or _reply_is_only_greeting_or_signature(value):
        return True
    bad = [
        "we received your message", "we received your email", "we will review", "we will get back",
        "get back to you shortly", "we will check and get back", "i will check and get back",
        "we will look into", "we will follow up shortly", "we will respond with the specific information",
        "please wait while", "wanted to reply right away", "thank you for your email. we will review",
    ]
    if any(p in text for p in bad):
        return True
    if len([line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]) > 0:
        return True
    return False


def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    if not body or _is_bad_generic_reply_text(body) or _substantive_reply_words(body) < 14:
        return True
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if latest and (body.lower().startswith(latest.lower()[:80]) or copied_sequence_found(body, latest, sequence_len=14)):
        return True
    return False


def _fast_context_queries_for_thread(thread: Dict, connected_email: str) -> List[str]:
    base_queries = heuristic_context_queries_for_thread(thread, connected_email) if 'heuristic_context_queries_for_thread' in globals() else []
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_email = _final_email_address(latest.get("from", ""))
    text = _final_norm_text(latest.get("subject", ""), latest.get("body", ""))
    extra = []
    if sender_email:
        extra.extend([
            f'in:anywhere from:{sender_email}',
            f'in:anywhere to:{sender_email}',
            f'in:sent to:{sender_email}',
            f'in:anywhere ({sender_email}) (order OR invoice OR receipt OR payment OR login OR access OR course OR PEBC OR renewal)',
        ])
    for phrase in ["order number", "invoice", "receipt", "payment", "login", "access", "course", "pebc", "renewal", "extension", "available", "spare time", "call", "connect"]:
        if phrase in text:
            extra.append(f'in:anywhere "{phrase}"')
            if sender_email:
                extra.append(f'in:anywhere ({sender_email}) "{phrase}"')
    out = []
    for q in extra + list(base_queries or []):
        if q and q not in out:
            out.append(q)
    return out[:14]


def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_is_promo_text(latest_text, sender):
        return None
    _name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 11000)
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}") if '_extract_specific_context_facts' in globals() else ""
    prompt = f'''
Write a concise professional reply. It must include a real answer or next step, not just a greeting/signature.

Rules:
- No bullet points, no dashes, no numbered lists, no markdown.
- Do not say "we received your email", "we will review", "we will get back to you", or similar filler.
- Use Gmail context directly when it contains order, payment, login, course, EE/PEBC, renewal, availability, or prior reply details.
- If the sender asks to connect or asks for spare time, answer with a clear scheduling response asking for specific available time slots.
- If exact information is missing, say what is missing and ask one specific follow-up question.
- Keep it 45 to 110 words before signature.
- Work emails must end with the exact Pharmacy Prep signature. Personal emails should end with "Regards" only.

Return JSON only:
{{
  "title": "short dashboard title, 4-9 words",
  "summary": "specific one-sentence summary mentioning who is asking and what context was checked",
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
Sender: {display_name} <{sender_email}>
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Extracted Gmail facts:
{facts or 'None extracted'}

Related Gmail API context:
{extra_context or 'None found'}
'''
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL) if '_openai_json_response' in globals() else None
    if not isinstance(parsed, dict):
        return None
    body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
    if not body or reply_needs_regeneration(body, latest_body, category):
        retry = _openai_json_response(prompt + "\n\nThe last draft was too generic or empty. Rewrite with one concrete response sentence and no filler.", model=OPENAI_REPLY_MODEL) if '_openai_json_response' in globals() else None
        if isinstance(retry, dict):
            retry_body = _professionalize_reply_body(str(retry.get("body", "") or "").strip(), category)
            if retry_body and not reply_needs_regeneration(retry_body, latest_body, category):
                parsed = retry
                body = retry_body
    if not body or reply_needs_regeneration(body, latest_body, category):
        return None
    return {
        "title": str(parsed.get("title", "") or "").strip(),
        "summary": str(parsed.get("summary", "") or "").strip(),
        "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
        "body": body,
    }


def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting_name = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    lowered = _final_norm_text(latest.get("subject", ""), latest.get("body", ""))
    if any(t in lowered for t in ["spare time", "available", "availability", "connect", "call", "reach"]):
        main = "Please share two or three time slots that work for you today, and we will try to connect during one of those times."
    elif any(t in lowered for t in ["login", "access", "password"]):
        main = "Please confirm the email address used for your registration so we can check the account access and send the correct login details."
    elif any(t in lowered for t in ["order number", "order #"]):
        main = "Please confirm the email address or name used for the order so we can locate the correct order number and send it to you."
    elif any(t in lowered for t in ["invoice", "receipt", "payment", "paid"]):
        main = "Please send the payment name, email address, or transaction reference so we can match the record and provide the correct receipt or payment details."
    elif any(t in lowered for t in ["pebc", "ee", "evaluating", "qualifying", "exam"]):
        main = "Please send the exact PEBC or course detail you want confirmed, and we will answer it based on the current course information."
    elif any(t in lowered for t in ["course", "class", "recording", "notes", "book", "materials"]):
        main = "Please confirm the course or module you are referring to, and we will send the correct class, materials, notes, or recording details."
    else:
        main = "Please send the specific detail you would like confirmed, and we will respond with the correct information."
    if category == "work":
        body = f"Hello {greeting_name},\n\n{main}\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    else:
        body = f"Hello {greeting_name},\n\n{main}\n\nRegards"
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}


def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _final_is_renewal_thread(thread, connected_email):
        return None
    latest_text, sender = _final_latest_text_sender(thread, connected_email)
    if _final_is_new_question_text(latest_text) or _final_is_promo_text(latest_text, sender):
        return None
    item = _prev_build_general_email_item_fastfix(service, thread, connected_email, personal_label_id, work_label_id) if _prev_build_general_email_item_fastfix else None
    if not item or item.get("filtered_out") or item.get("status") == "Filtered Out":
        return item
    category = category_for_thread_strict(thread, connected_email)
    if category == "renewal":
        return None
    item["category"] = category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    if "_thread_history_for_ui" in globals():
        item["thread_history"] = _thread_history_for_ui(thread, connected_email)
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_body = clean_preview_text(latest.get("body", ""), 7000)
    needs_new_reply = not reply or reply_needs_regeneration(reply.get("body", ""), latest_body, category)
    if needs_new_reply and item.get("status") != "Already Replied":
        queries = _fast_context_queries_for_thread(thread, connected_email)
        extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread.get("thread_id", ""), max_threads_per_query=3) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        if composed:
            item["reply"] = {
                "thread_id": thread.get("thread_id", ""),
                "mode": "thread_reply",
                "to": parseaddr(latest.get("from", ""))[1].strip(),
                "subject": composed.get("subject", ""),
                "body": composed.get("body", ""),
            }
            if composed.get("summary"):
                item["important_reason"] = composed.get("summary")
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                item["title"] = composed.get("title")
        else:
            item["reply"] = fallback_reply_for_thread(thread, connected_email, category)
    elif reply:
        body = _professionalize_reply_body(reply.get("body", ""), category)
        if not body:
            item["reply"] = fallback_reply_for_thread(thread, connected_email, category)
        else:
            item["reply"]["body"] = body
    return item


def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails = catalog.setdefault("emails", {})
    changed = 0
    new_emails = {}
    for key, item in list((emails or {}).items()):
        if not isinstance(item, dict):
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        sender = _final_email_address(original.get("from", ""))
        text = _final_norm_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
        if _final_is_new_question_text(text) or _final_is_promo_text(text, sender):
            changed += 1
            continue
        if _is_renewal_item(item):
            item = _final_prepare_renewal_item(item) if '_final_prepare_renewal_item' in globals() else item
            stable = item.get("thread_id") or key
            try:
                details = _item_renewal_details(item) if '_item_renewal_details' in globals() else None
                if details and "_renewal_stable_thread_id" in globals():
                    stable = _renewal_stable_thread_id(details.get("student_email", ""), details.get("course", ""))
                    item["thread_id"] = stable
                    if isinstance(item.get("reply"), dict):
                        item["reply"]["thread_id"] = stable
            except Exception:
                pass
            item["category"] = "renewal"
            item["screening_version"] = EMAIL_SCREENING_VERSION
            item["ai_screened"] = True
            new_emails[stable] = item
            changed += int(stable != key)
            continue
        new_category = "work" if _is_item_pharmacy_prep_related(item) else "personal"
        if item.get("category") != new_category or item.get("screening_version") != EMAIL_SCREENING_VERSION:
            changed += 1
        item["category"] = new_category
        item["screening_version"] = EMAIL_SCREENING_VERSION
        item["ai_screened"] = True
        if isinstance(item.get("reply"), dict):
            body = _professionalize_reply_body(item["reply"].get("body", ""), new_category)
            if body:
                if body != item["reply"].get("body", ""):
                    changed += 1
                item["reply"]["body"] = body
            else:
                item["reply"] = None
                item["status"] = "Filtered Out"
                item["filtered_out"] = True
                changed += 1
                continue
        new_emails[key] = item
    if changed:
        catalog["emails"] = new_emails
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed


def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict) or not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if _is_renewal_item(item):
        return bool(item.get("reply")) or item.get("status") in ("Needs Reply", "Already Replied", "Suggestion Removed")
    category = _final_category_for_item(item)
    if category not in ("work", "personal"):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if reply and reply_needs_regeneration(reply.get("body", ""), item.get("original", {}).get("body", ""), category):
        return False
    return bool(reply) or (item.get("status") in ("Already Replied", "Suggestion Removed") and bool(item.get("important_reason")))


_prev_perform_gmail_scan_fastfix = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _prev_perform_gmail_scan_fastfix(force_full=force_full)
    try:
        cleaned_count = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        fresh["scan_summary"] = {
            **(payload.get("scan_summary", {}) if isinstance(payload, dict) else {}),
            "catalog_cleanup_changed": cleaned_count,
            "reply_fix": "removed greeting-only drafts and regenerated context replies on scan",
            "promo_filter": "OpenAI/ChatGPT subscriptions, newsletters, promos, Xbox, Vimeo, JP Morgan/GetSmarter removed",
            "order_auto_reply": "unchanged",
        }
        return fresh
    except Exception as error:
        print(f"[scan] fast reply/promo cleanup failed: {error}", flush=True)
        return payload


# ---------------------------------------------------------------------
# TARGETED FIX: work/personal organization, renewal tab, order auto-send, replies
# ---------------------------------------------------------------------
# This block only overrides the broken behavior reported by the user while keeping
# the uploaded working base intact.
EMAIL_SCREENING_VERSION = "2026-06-category-order-reply-v3"

_TARGET_WORK_DIRECT_TERMS = [
    "pharmacy prep", "pharmacyprep", "www.pharmacyprep.com", "success@pharmacyprep.com",
    "eprepstation", "eprep station", "online exam prep station", "pebc", "pebc ee",
    "ee course", "evaluating exam", "qualifying exam", "pharmacist evaluating", "pharmacist qualifying",
    "osce", "ospe", "mcq", "qbank", "question bank", "mock exam", "mock exams",
    "prep course", "renewed prep course", "renewed course", "course renewal", "account renewal request",
    "course extension", "online account access", "online account", "course access", "course login",
    "class notes", "recorded video", "recorded videos", "live online", "interactive lectures",
    "pharmacy technician", "home study plus online", "pebc evaluating", "pebc qualifying",
]
_TARGET_COURSE_WORDS = ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "book", "books", "materials", "module", "online account"]
_TARGET_STUDENT_WORDS = ["student", "students", "candidate", "enrolled", "enrollment", "enrolment", "registered", "registration", "customer"]
_TARGET_EXAM_WORDS = ["exam", "ee", "evaluating", "qualifying", "pebc", "mock", "qbank", "mcq", "osce", "ospe", "pharmacist", "technician"]
_TARGET_ORDER_WORDS = ["order number", "order #", "invoice", "receipt", "payment", "refund", "paid", "e-transfer", "etransfer"]
_TARGET_PERSONAL_TERMS = [
    "pest", "pest control", "exterminator", "lease", "lease agreement", "rental agreement", "tenancy",
    "landlord", "tenant", "rent", "condo", "apartment", "building management", "property management",
    "management office", "maintenance", "unit key", "master key", "contractor", "century21", "real estate",
    "mortgage", "bank statement", "insurance", "policy document", "legal document", "lawyer", "attorney",
    "contract", "agreement", "docusign", "adobe sign", "signed document", "signature request",
    "appointment", "doctor", "clinic", "dental", "office notice", "parking", "hydro", "utilities",
]
_TARGET_PROMO_TERMS = [
    "unsubscribe", "manage your preferences", "view this email in your browser", "newsletter", "promotion", "promotional",
    "limited time", "special offer", "exclusive offer", "sale", "discount", "coupon", "webinar", "sponsored",
    "recommended for you", "xbox", "game pass", "vimeo", "jp morgan", "jpmorgan", "j.p. morgan", "chase",
    "market update", "market insights", "getsmarter", "mit sloan", "openai", "chatgpt", "subscription",
    "trial ends", "product update", "weekly digest", "monthly digest", "usage alert", "billing alert", "plan renewal",
]
_TARGET_BLOCK_SENDERS = ["support@pharrmacyprep.com"]

def _target_addr(value: str) -> str:
    try:
        return parseaddr(value or "")[1].lower().strip()
    except Exception:
        return ""

def _target_norm(*parts) -> str:
    raw = "\n".join(str(p or "") for p in parts)
    raw = raw.replace("&zwnj;", " ").replace("\u200c", " ").replace("\u200b", " ")
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip().lower()

def _target_latest_text_sender(thread: Dict, connected_email: str = "") -> Tuple[str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    return _target_norm(latest.get("from", ""), latest.get("subject", ""), latest.get("body", "")), _target_addr(latest.get("from", ""))

def _target_inbound_context(thread: Dict, connected_email: str = "") -> str:
    connected = (connected_email or "").lower().strip()
    parts = []
    for email in thread.get("emails", []) or []:
        sender = _target_addr(email.get("from", ""))
        if connected and sender == connected:
            continue
        parts.extend([email.get("from", ""), email.get("subject", ""), clean_preview_text(email.get("body", ""), 3500)])
    return _target_norm(*parts)

def _target_is_new_question(text: str) -> bool:
    text = str(text or "").lower()
    return any(t in text for t in [
        "new question submitted", "new question submited", "new question has been submitted", "question submitted",
        "new support question has been submitted at eprepstation.com", "new support question"
    ])

def _target_is_promo(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    if any(block in sender for block in _TARGET_BLOCK_SENDERS):
        return True
    direct_support = any(t in text for t in [
        "cannot access", "can't access", "unable to access", "payment failed", "refund request",
        "invoice", "receipt", "order number", "login issue", "access issue"
    ])
    return any(t in text or t in sender for t in _TARGET_PROMO_TERMS) and not direct_support

def _target_is_renewal_text(text: str) -> bool:
    text = str(text or "").lower()
    return "account renewal request" in text or ("renewal" in text and "eprepstation" in text and ("e-mail address" in text or "email address" in text or "course" in text))

def _is_renewal_item(item: Dict) -> bool:
    if not isinstance(item, dict):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    details = item.get("renewal_details", {}) if isinstance(item.get("renewal_details", {}), dict) else {}
    text = _target_norm(item.get("category", ""), item.get("title", ""), item.get("important_reason", ""), original.get("subject", ""), original.get("body", ""), details.get("course", ""), details.get("student_email", ""))
    return bool(item.get("is_renewal_request") or item.get("renewal_request") or item.get("category") == "renewal" or _target_is_renewal_text(text))

def _target_is_renewal_thread(thread: Dict, connected_email: str = "") -> bool:
    latest_text, _sender = _target_latest_text_sender(thread, connected_email)
    return _target_is_renewal_text(latest_text) or _target_is_renewal_text(_target_inbound_context(thread, connected_email))

def _target_has_personal_override(text: str) -> bool:
    return any(term in str(text or "").lower() for term in _TARGET_PERSONAL_TERMS)

def _target_has_strong_work(text: str, sender: str = "") -> bool:
    text = str(text or "").lower()
    sender = str(sender or "").lower()
    if not text and not sender:
        return False
    if _target_is_new_question(text) or _target_is_promo(text, sender):
        return False
    if any(block in sender for block in _TARGET_BLOCK_SENDERS):
        return False
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    if any(term in text for term in _TARGET_WORK_DIRECT_TERMS):
        return True
    has_course = any(term in text for term in _TARGET_COURSE_WORDS)
    has_student = any(term in text for term in _TARGET_STUDENT_WORDS)
    has_exam = any(term in text for term in _TARGET_EXAM_WORDS)
    has_order = any(term in text for term in _TARGET_ORDER_WORDS)
    if has_exam and (has_course or has_student or has_order):
        return True
    if has_student and (has_course or has_order) and any(t in text for t in ["exam", "mock", "qbank", "pebc", "evaluating", "qualifying", "pharmacy", "prep"]):
        return True
    if "ee" in text and has_course:
        return True
    return False

def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    latest_text, sender = _target_latest_text_sender(thread, connected_email)
    if _target_has_personal_override(latest_text) and not _target_has_strong_work(latest_text, sender):
        return False
    if _target_has_strong_work(latest_text, sender):
        return True
    full_inbound = _target_inbound_context(thread, connected_email)
    if _target_has_personal_override(latest_text):
        return False
    return _target_has_strong_work(full_inbound, sender)

def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    if _target_is_renewal_thread(thread, connected_email):
        return "renewal"
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"

def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    cat = category_for_thread_strict(thread, connected_email)
    return "work" if cat == "renewal" else cat

def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _target_addr(original.get("from", ""))
    latest_text = _target_norm(original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _target_has_personal_override(latest_text) and not _target_has_strong_work(latest_text, sender):
        return False
    if _target_has_strong_work(latest_text, sender):
        return True
    support_text = _target_norm(item.get("title", ""), item.get("important_reason", ""))
    return (not _target_has_personal_override(latest_text)) and _target_has_strong_work(support_text, sender)

def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"

def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = _target_addr(original.get("from", ""))
    text = _target_norm(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _target_is_new_question(text) or _target_is_promo(text, sender):
        return True
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery", "delivered",
        "tracking number", "shipment", "security alert", "verification code", "password reset", "mail delivery",
        "undeliverable", "comment awaiting moderation", "please moderate", "delivery status notification",
        "this is an automated message", "do not reply to this email",
    ]
    return any(term in text for term in bad_terms)

def _meaningful_reply_text(body: str) -> str:
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?is)regards\s*\n\s*pharmacy prep.*$", "", text).strip()
    text = re.sub(r"(?is)regards\s*$", "", text).strip()
    text = re.sub(r"(?im)^\s*(hello|hi|dear)\s+[a-z .'-]+,?\s*$", "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def _professionalize_reply_body(body: str, category: str = "work") -> str:
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?im)^\s*subject\s*:.*\n?", "", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*[-*•–—]\s+", "", line).strip()
        line = re.sub(r"^\s*\d+[.)]\s+", "", line).strip()
        line = line.replace("—", ",").replace("–", ",")
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    text = "\n".join(lines)
    for pattern in [
        r"(?i)\bwe (received|have received) your (message|email).*?(\.|\n)",
        r"(?i)\bwe (will|would) (review|look into|check and get back|verify) (this|your request|the details).*?(\.|\n)",
        r"(?i)\b(i|we) (will|would) get back to you (shortly|soon)?\.??",
        r"(?i)\bthank you for (reaching out|your email|contacting us)\.\s*",
    ]:
        text = re.sub(pattern, "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    main = re.split(r"(?i)\n\s*regards\b", text, maxsplit=1)[0].strip()
    paragraphs = [p.strip() for p in main.split("\n\n") if p.strip()]
    main = "\n\n".join(paragraphs[:3]).strip()
    if len(_meaningful_reply_text(main).split()) < 10:
        return ""
    if category == "work":
        signature = "Regards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
        return (main.rstrip() + "\n\n" + signature).strip()
    return (main.rstrip() + "\n\nRegards").strip()

def _is_bad_generic_reply_text(value: str) -> bool:
    text = str(value or "").lower()
    meaningful = _meaningful_reply_text(value).lower()
    if len(meaningful.split()) < 10:
        return True
    bad = [
        "we received your message", "we received your email", "we will review", "we will get back",
        "get back to you shortly", "we will check and get back", "i will check and get back",
        "we will look into", "we will follow up shortly", "we will respond with the specific information",
        "please see the relevant details below", "please send the specific detail you would like confirmed",
    ]
    if any(p in text for p in bad):
        return True
    if len([line for line in str(value or "").splitlines() if re.match(r"^\s*([-*•–—]|\d+[.)])\s+", line)]) > 0:
        return True
    return False

def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = _professionalize_reply_body(reply_body or "", category)
    if not body or _is_bad_generic_reply_text(body):
        return True
    latest = clean_preview_text(latest_body or "", 7000).strip()
    if latest and (body.lower().startswith(latest.lower()[:80]) or copied_sequence_found(body, latest, sequence_len=14)):
        return True
    return False

def _target_reply_fallback(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    greeting = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    lowered = _target_norm(latest.get("subject", ""), latest.get("body", ""), extra_context)
    if any(t in lowered for t in ["spare time", "available", "availability", "connect", "call", "reach"]):
        main = "Thank you for letting us know. Please send two or three time slots that work for you today, and we will try to connect during one of those times."
    elif any(t in lowered for t in ["login", "access", "password", "online account"]):
        main = "Please confirm the email address used for your registration so we can check the account and send the correct login or access details."
    elif any(t in lowered for t in ["order number", "order #"]):
        main = "Please confirm the name or email address used for the order, and we will locate the correct order number for you."
    elif any(t in lowered for t in ["invoice", "receipt", "payment", "paid", "e-transfer", "etransfer"]):
        main = "Please send the payment name, email address, or transaction reference so we can match the record and provide the correct receipt or payment details."
    elif any(t in lowered for t in ["pebc", "ee", "evaluating", "qualifying", "exam"]):
        main = "Thank you for your message. Please send the exact PEBC or course detail you want confirmed, and we will answer it based on the current course information."
    elif any(t in lowered for t in ["course", "class", "recording", "notes", "book", "materials"]):
        main = "Please confirm the course or module you are referring to, and we will send the correct class, material, note, or recording details."
    else:
        main = "Thank you for your message. Please send the exact detail you need confirmed, and we will respond with the correct information."
    if category == "work":
        body = f"Hello {greeting},\n\n{main}\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    else:
        body = f"Hello {greeting},\n\n{main}\n\nRegards"
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}

def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_text, sender = _target_latest_text_sender(thread, connected_email)
    if _target_is_promo(latest_text, sender) or any(blocked in sender for blocked in _TARGET_BLOCK_SENDERS):
        return None
    _name, sender_email = parseaddr(latest.get("from", ""))
    sender_email = sender_email.strip()
    display_name = sender_display_name(latest.get("from", ""), sender_email)
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = compact_ai_context(latest.get("body", ""), 7600)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = compact_ai_context(format_thread_for_ai(thread), 12500)
    facts = _extract_specific_context_facts(f"{local_context}\n{extra_context}") if "_extract_specific_context_facts" in globals() else ""
    prompt = f'''
Write a concise, professional email reply using the current Gmail thread and related Gmail API context.

Rules:
- No bullet points, numbered lists, dashes, headings, or markdown.
- Do not use filler like "we received your email", "we will review", or "we will get back to you".
- Do not send an empty greeting-only reply.
- Answer the exact request. Use Gmail context directly when it contains order numbers, payment/receipt details, login/access details, course/EE/PEBC details, renewal details, dates, availability, or prior sent replies.
- If exact information is missing, state the one missing detail and ask one specific follow-up question.
- Keep the reply short: 60 to 120 words before signature.
- For Work emails, include the Pharmacy Prep signature exactly. For Personal emails, end with Regards only.

Return JSON only:
{{"title":"4-9 word title","summary":"one specific sentence","subject":"{clean_subject}","body":"full outbound reply only"}}

Work signature:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com

Category: {category}
Sender name: {display_name}
Sender email: {sender_email}
Latest subject: {latest.get('subject', '')}
Latest body:
{latest_body}

Current Gmail thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Extracted Gmail facts:
{facts or 'None extracted'}

Related Gmail API context:
{extra_context or 'None found'}
'''
    parsed = _openai_json_response(prompt, model=OPENAI_REPLY_MODEL) if "_openai_json_response" in globals() else None
    for _attempt in range(2):
        if isinstance(parsed, dict):
            body = _professionalize_reply_body(str(parsed.get("body", "") or "").strip(), category)
            if body and not reply_needs_regeneration(body, latest_body, category):
                return {
                    "title": str(parsed.get("title", "") or "").strip(),
                    "summary": str(parsed.get("summary", "") or "").strip(),
                    "subject": str(parsed.get("subject", clean_subject) or clean_subject).strip() or clean_subject,
                    "body": body,
                }
        parsed = _openai_json_response(prompt + "\n\nRewrite because the previous draft was generic or greeting-only. Include one useful action or answer sentence.", model=OPENAI_REPLY_MODEL) if "_openai_json_response" in globals() else None
    return None

def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Dict:
    return _target_reply_fallback(thread, connected_email, category)

def was_order_message_already_sent(service, customer_email: str, order_number: str) -> bool:
    if not customer_email:
        return False
    order_number = str(order_number or "").strip()
    queries = [
        f'in:sent newer_than:365d to:{customer_email} "Welcome to Pharmacy Prep"',
        f'in:sent newer_than:365d to:{customer_email} "course login"',
        f'in:sent newer_than:365d to:{customer_email} "enroll you in the prep course"',
    ]
    if order_number and order_number.lower() != "unknown":
        queries.insert(0, f'in:sent newer_than:365d to:{customer_email} "order #{order_number}" "Welcome to Pharmacy Prep"')
        queries.insert(1, f'in:sent newer_than:365d to:{customer_email} "{order_number}" "Welcome to Pharmacy Prep"')
    for query in queries:
        try:
            if gmail_search_any(service, query, max_results=3):
                return True
        except Exception:
            continue
    return False

def _auto_send_order_if_safe(service, thread: Dict, connected_email: str, order_item: Dict) -> Tuple[Dict, bool]:
    reply = order_item.get("reply") or {}
    if not reply:
        return order_item, False
    if not get_automation_settings().get("auto_reply_enabled", True):
        order_item["auto_send_note"] = "Auto Reply is off."
        return order_item, False
    customer_email = reply.get("to") or order_item.get("customer_email")
    order_number = str(order_item.get("order_number") or "").strip()
    if not customer_email or not order_number or order_number.lower() == "unknown":
        order_item["auto_send_error"] = "Missing customer email or order number."
        return order_item, False
    if was_thread_manually_replied(service, thread, connected_email) or was_order_message_already_sent(service, customer_email, order_number):
        order_item = {**order_item, "status": "Already Replied", "reply": None, "reply_sent_at": order_item.get("reply_sent_at") or _safe_iso_now()}
        mark_thread_action_processed(thread_action_key(thread, connected_email), "manual_or_prior_reply_found", "", order_number=order_number)
        upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": order_item.get("customer_name", "Customer"), "status": "Already Replied", "total": order_item.get("total", ""), "products": order_item.get("products", [])})
        return order_item, False
    try:
        sent = send_new_email(service, customer_email, reply.get("subject", "Welcome to Pharmacy Prep"), reply.get("body", ""))
        sent_at = _safe_iso_now()
        order_item = {**order_item, "status": "Sent Automatically", "reply": None, "reply_sent_at": sent_at, "sent_message_id": sent.get("id", "")}
        mark_thread_action_processed(thread_action_key(thread, connected_email), "order_auto_sent", sent.get("id", ""), order_number=order_number)
        upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": order_item.get("customer_name", "Customer"), "status": "Sent Automatically", "sent_message_id": sent.get("id", ""), "sent_at": sent_at, "total": order_item.get("total", ""), "products": order_item.get("products", [])})
        print(f"[orders] auto-sent welcome | order={order_number} | to={customer_email} | sent_id={sent.get('id','')}", flush=True)
        return order_item, True
    except Exception as error:
        order_item["auto_send_error"] = str(error)
        print(f"[orders] auto-send failed | order={order_number} | to={customer_email} | error={error}", flush=True)
        return order_item, False

def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _target_is_renewal_thread(thread, connected_email):
        return None
    latest_text, sender = _target_latest_text_sender(thread, connected_email)
    if _target_is_new_question(latest_text) or _target_is_promo(latest_text, sender):
        return None
    item = _prev_build_general_email_item_fastfix(service, thread, connected_email, personal_label_id, work_label_id) if "_prev_build_general_email_item_fastfix" in globals() and _prev_build_general_email_item_fastfix else None
    if not item or item.get("filtered_out") or item.get("status") == "Filtered Out":
        return item
    category = category_for_thread_strict(thread, connected_email)
    if category == "renewal":
        return None
    item["category"] = category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    if "_thread_history_for_ui" in globals():
        item["thread_history"] = _thread_history_for_ui(thread, connected_email)
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_body = clean_preview_text(latest.get("body", ""), 7000)
    needs_new_reply = item.get("status") != "Already Replied" and (not reply or reply_needs_regeneration(reply.get("body", ""), latest_body, category))
    if needs_new_reply:
        queries = _fast_context_queries_for_thread(thread, connected_email) if "_fast_context_queries_for_thread" in globals() else heuristic_context_queries_for_thread(thread, connected_email)
        extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread.get("thread_id", ""), max_threads_per_query=3) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        if composed:
            item["reply"] = {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": parseaddr(latest.get("from", ""))[1].strip(), "subject": composed.get("subject", ""), "body": composed.get("body", "")}
            if composed.get("summary"):
                item["important_reason"] = composed.get("summary")
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                item["title"] = composed.get("title")
        else:
            item["reply"] = fallback_reply_for_thread(thread, connected_email, category)
    elif reply:
        body = _professionalize_reply_body(reply.get("body", ""), category)
        if body:
            item["reply"]["body"] = body
        else:
            item["reply"] = fallback_reply_for_thread(thread, connected_email, category)
    return item

def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails = catalog.setdefault("emails", {})
    changed = 0
    new_emails = {}
    for key, item in list((emails or {}).items()):
        if not isinstance(item, dict):
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        sender = _target_addr(original.get("from", ""))
        latest_text = _target_norm(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
        if _target_is_new_question(latest_text) or _target_is_promo(latest_text, sender):
            changed += 1
            continue
        if _is_renewal_item(item):
            item = _final_prepare_renewal_item(item) if "_final_prepare_renewal_item" in globals() else item
            stable = item.get("thread_id") or key
            try:
                details = _item_renewal_details(item) if "_item_renewal_details" in globals() else None
                if details and "_renewal_stable_thread_id" in globals():
                    stable = _renewal_stable_thread_id(details.get("student_email", ""), details.get("course", ""))
                    item["thread_id"] = stable
                    if isinstance(item.get("reply"), dict):
                        item["reply"]["thread_id"] = stable
            except Exception:
                pass
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
            item["screening_version"] = EMAIL_SCREENING_VERSION
            item["ai_screened"] = True
            new_emails[stable] = item
            changed += int(stable != key)
            continue
        new_category = "work" if _is_item_pharmacy_prep_related(item) else "personal"
        if item.get("category") != new_category or item.get("screening_version") != EMAIL_SCREENING_VERSION:
            changed += 1
        item["category"] = new_category
        item["screening_version"] = EMAIL_SCREENING_VERSION
        item["ai_screened"] = True
        if isinstance(item.get("reply"), dict):
            body = _professionalize_reply_body(item["reply"].get("body", ""), new_category)
            if body:
                if body != item["reply"].get("body", ""):
                    changed += 1
                item["reply"]["body"] = body
            else:
                item["reply"] = None
                item["status"] = "Needs Reply"
                changed += 1
        new_emails[key] = item
    if changed:
        catalog["emails"] = new_emails
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed

def _is_catalog_email_visible(item: Dict) -> bool:
    if not isinstance(item, dict) or not _item_is_on_or_after_scan_start(item):
        return False
    if item.get("filtered_out") or item.get("status") == "Filtered Out":
        return False
    if _catalog_item_looks_unimportant(item):
        return False
    if _is_renewal_item(item):
        return True
    category = _final_category_for_item(item)
    if category not in ("work", "personal"):
        return False
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if reply and reply_needs_regeneration(reply.get("body", ""), item.get("original", {}).get("body", ""), category):
        return False
    return bool(reply) or item.get("status") in ("Already Replied", "Suggestion Removed", "Needs Reply") or bool(item.get("important_reason"))

_prev_build_dashboard_payload_target = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    payload = _prev_build_dashboard_payload_target(force_refresh=force_refresh)
    try:
        catalog = get_dashboard_catalog()
        all_emails = [deepcopy(item) for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
        cleaned = []
        renewals = []
        seen = set()
        for item in sorted(all_emails, key=_catalog_sort_key, reverse=True):
            if not isinstance(item, dict):
                continue
            if _catalog_item_looks_unimportant(item):
                continue
            if _is_renewal_item(item):
                item["category"] = "renewal"
                item["is_renewal_request"] = True
                item["renewal_request"] = True
                renewals.append(item)
                continue
            item["category"] = _final_category_for_item(item)
            ident = str(item.get("thread_id") or id(item))
            if ident in seen:
                continue
            seen.add(ident)
            cleaned.append(item)
        payload["emails"] = cleaned
        payload["renewals"] = renewals
        pending = [i.get("thread_id") for i in cleaned + renewals if isinstance(i.get("reply"), dict)] + [o.get("thread_id") for o in payload.get("orders", []) if isinstance(o.get("reply"), dict)]
        payload["pending_replies"] = [x for x in pending if x]
        payload["stats"] = {**payload.get("stats", {}), "pending_replies": len(payload["pending_replies"]), "renewal_emails": len(renewals), "work_emails": len([e for e in cleaned if e.get("category") == "work"]), "personal_emails": len([e for e in cleaned if e.get("category") == "personal"])}
    except Exception as error:
        print(f"[dashboard] target payload cleanup failed: {error}", flush=True)
    return payload

_prev_perform_gmail_scan_target = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _prev_perform_gmail_scan_target(force_full=force_full)
    try:
        changed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        fresh["scan_summary"] = {**(payload.get("scan_summary", {}) if isinstance(payload, dict) else {}), "catalog_cleanup_changed": changed, "work_personal_fix": "Pharmacy Prep/PEBC/EE/course/order-support forced Work; personal/office/pest/property forced Personal", "order_auto_reply_fix": "orders now log success/failure and only skip exact welcome/order sent matches", "reply_quality_fix": "greeting-only/generic drafts hidden and regenerated"}
        print(f"[scan] target fix complete | work={fresh.get('stats',{}).get('work_emails',0)} | personal={fresh.get('stats',{}).get('personal_emails',0)} | renewals={fresh.get('stats',{}).get('renewal_emails',0)} | cleanup={changed}", flush=True)
        return fresh
    except Exception as error:
        print(f"[scan] target fix failed: {error}", flush=True)
        return payload

# ---------------------------------------------------------------------
# FINAL USER REQUEST PATCH: order auto-send verification + strict tabs + better replies
# ---------------------------------------------------------------------
# Only touches: order welcome sending, Work/Personal/Renewal categorization, and suggested reply quality.
EMAIL_SCREENING_VERSION = "2026-06-orders-ee-replies-v4"

_FINAL_WORK_PHRASES = [
    "pharmacy prep", "pharmacyprep", "www.pharmacyprep.com", "success@pharmacyprep.com",
    "eprepstation", "eprep station", "online exam prep station",
    "pebc", "pebc ee", "ee course", "ee prep", "ee preparation", "evaluating exam", "evaluating examination",
    "qualifying exam", "qualifying examination", "pharmacist evaluating", "pharmacist qualifying",
    "naplex", "opra", "osce", "ospe", "mcq", "qbank", "question bank", "mock exam", "mock exams",
    "prep course", "renewed prep course", "renewed course", "welcome. renewed prep course", "course renewal",
    "account renewal request", "course extension", "online account access", "online account", "course access",
    "course login", "class notes", "recorded video", "recorded videos", "live online", "interactive lectures",
    "pharmacy technician", "home study plus online", "pebc evaluating", "pebc qualifying",
]
_FINAL_PERSONAL_PHRASES = [
    "pest", "pest control", "exterminator", "lease", "lease agreement", "rental agreement", "tenancy",
    "landlord", "tenant", "rent", "condo", "apartment", "building management", "property management",
    "management office", "maintenance", "unit key", "master key", "contractor", "century21", "real estate",
    "mortgage", "bank statement", "insurance", "policy document", "legal document", "lawyer", "attorney",
    "contract", "agreement", "docusign", "adobe sign", "signed document", "signature request",
    "appointment", "doctor", "clinic", "dental", "office notice", "parking", "hydro", "utilities",
]
_FINAL_PROMO_PHRASES = [
    "unsubscribe", "manage your preferences", "view this email in your browser", "newsletter", "promotion",
    "promotional", "limited time", "special offer", "exclusive offer", "sale", "discount", "coupon",
    "webinar", "sponsored", "recommended for you", "xbox", "game pass", "vimeo", "jp morgan",
    "jpmorgan", "j.p. morgan", "chase", "market update", "market insights", "getsmarter", "mit sloan",
    "openai", "chatgpt", "subscription", "trial ends", "product update", "weekly digest", "monthly digest",
    "usage alert", "billing alert", "plan renewal",
]

def _final_clean_text(*parts) -> str:
    raw = "\n".join(str(p or "") for p in parts)
    raw = raw.replace("&zwnj;", " ").replace("\u200c", " ").replace("\u200b", " ").replace("\xa0", " ")
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip().lower()

def _final_word_has_ee(text: str) -> bool:
    return bool(re.search(r"\bee\b", text or "", flags=re.IGNORECASE))

def _final_has_work_signal(text: str, sender: str = "") -> bool:
    text = _final_clean_text(text)
    sender = (sender or "").lower()
    if not text and not sender:
        return False
    if any(domain in sender for domain in ["pharmacyprep.com", "eprepstation.com"]):
        return True
    if any(p in text for p in _FINAL_WORK_PHRASES):
        return True
    has_course = any(p in text for p in ["course", "class", "lecture", "recording", "notes", "login", "access", "password", "book", "books", "materials", "module", "online account", "prep"])
    has_exam = any(p in text for p in ["pebc", "exam", "evaluating", "qualifying", "mock", "qbank", "mcq", "osce", "ospe", "pharmacist", "technician"])
    has_student = any(p in text for p in ["student", "candidate", "enrolled", "enrollment", "enrolment", "registered", "registration", "customer"])
    has_order = any(p in text for p in ["order number", "order #", "invoice", "receipt", "payment", "refund", "paid", "e-transfer", "etransfer"])
    if _final_word_has_ee(text) and (has_course or has_exam or has_student or has_order or "prep" in text):
        return True
    if has_exam and (has_course or has_student or has_order or "prep" in text):
        return True
    if has_course and ("pharmacy" in text or "prep" in text or "pebc" in text or has_student):
        return True
    if ("renewed" in text or "renewal" in text or "extension" in text) and ("prep" in text or "course" in text or "account" in text):
        return True
    return False

def _final_has_personal_signal(text: str) -> bool:
    text = _final_clean_text(text)
    return any(p in text for p in _FINAL_PERSONAL_PHRASES)

def _final_is_promo_text(text: str, sender: str = "") -> bool:
    text = _final_clean_text(text)
    sender = (sender or "").lower()
    direct_support = any(t in text for t in [
        "cannot access", "can't access", "unable to access", "payment failed", "refund request",
        "invoice", "receipt", "order number", "login issue", "access issue", "pebc", "ee course", "course access"
    ])
    return (any(t in text for t in _FINAL_PROMO_PHRASES) or any(t in sender for t in _FINAL_PROMO_PHRASES)) and not direct_support

def _final_thread_text_and_sender(thread: Dict, connected_email: str = "") -> Tuple[str, str]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender = parseaddr(latest.get("from", ""))[1].lower().strip()
    # Use latest inbound first. Only include inbound history so old signatures do not pollute categories.
    connected = (connected_email or "").lower().strip()
    inbound_parts = [latest.get("from", ""), latest.get("subject", ""), latest.get("body", "")]
    for email in thread.get("emails", []) or []:
        e_sender = parseaddr(email.get("from", ""))[1].lower().strip()
        if connected and e_sender == connected:
            continue
        inbound_parts.extend([email.get("from", ""), email.get("subject", ""), clean_preview_text(email.get("body", ""), 2500)])
    return _final_clean_text(*inbound_parts), sender

def is_pharmacy_prep_related_thread(thread: Dict, connected_email: str) -> bool:
    text, sender = _final_thread_text_and_sender(thread, connected_email)
    if _target_is_new_question(text) or _final_is_promo_text(text, sender):
        return False
    if _final_has_work_signal(text, sender):
        return True
    if _final_has_personal_signal(text):
        return False
    return False

def category_for_thread_strict(thread: Dict, connected_email: str) -> str:
    if _target_is_renewal_thread(thread, connected_email):
        return "renewal"
    return "work" if is_pharmacy_prep_related_thread(thread, connected_email) else "personal"

def dashboard_category_for_thread(thread: Dict, connected_email: str) -> str:
    category = category_for_thread_strict(thread, connected_email)
    return "work" if category == "renewal" else category

def _is_item_pharmacy_prep_related(item: Dict) -> bool:
    if not isinstance(item, dict) or _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1].lower().strip()
    text = _final_clean_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _target_is_new_question(text) or _final_is_promo_text(text, sender):
        return False
    if _final_has_work_signal(text, sender):
        return True
    if _final_has_personal_signal(text):
        return False
    return False

def _final_category_for_item(item: Dict) -> str:
    if _is_renewal_item(item):
        return "renewal"
    return "work" if _is_item_pharmacy_prep_related(item) else "personal"

def _catalog_item_looks_unimportant(item: Dict) -> bool:
    if _is_renewal_item(item):
        return False
    original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
    sender = parseaddr(original.get("from", ""))[1].lower().strip()
    text = _final_clean_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
    if _target_is_new_question(text) or _final_is_promo_text(text, sender):
        return True
    bad_terms = [
        "order has shipped", "has shipped", "has been shipped", "on the way", "out for delivery", "delivered",
        "tracking number", "shipment", "security alert", "verification code", "password reset", "mail delivery",
        "undeliverable", "comment awaiting moderation", "please moderate", "delivery status notification",
        "this is an automated message", "do not reply to this email",
    ]
    return any(term in text for term in bad_terms)

# Strict sent-mail verification. Do not trust old processed_orders.json if Gmail Sent does not show the welcome email.
def _order_welcome_sent_confirmed(service, customer_email: str, order_number: str = "") -> bool:
    customer_email = (customer_email or "").strip()
    order_number = str(order_number or "").strip()
    if not customer_email:
        return False
    queries = [
        f'in:sent newer_than:365d to:{customer_email} "Welcome to Pharmacy Prep"',
        f'in:sent newer_than:365d to:{customer_email} "Thank you for your order"',
    ]
    if order_number and order_number.lower() != "unknown":
        queries.insert(0, f'in:sent newer_than:365d to:{customer_email} "order #{order_number}" "Welcome to Pharmacy Prep"')
        queries.insert(1, f'in:sent newer_than:365d to:{customer_email} "order #{order_number}" "Thank you for your order"')
        queries.insert(2, f'in:sent newer_than:365d to:{customer_email} "{order_number}" "Welcome to Pharmacy Prep"')
    for query in queries:
        try:
            if gmail_search_any(service, query, max_results=5):
                return True
        except Exception as error:
            print(f"[orders] sent-check failed | to={customer_email} | query={query} | error={error}", flush=True)
    return False

def was_order_message_already_sent(service, customer_email: str, order_number: str) -> bool:
    return _order_welcome_sent_confirmed(service, customer_email, order_number)

_prev_build_order_item_final = build_order_item
def build_order_item(service, thread: Dict, connected_email: str) -> Optional[Dict]:
    item = _prev_build_order_item_final(service, thread, connected_email)
    if not item:
        return item
    order_text = get_best_order_email_text(thread) or ""
    order_number = str(item.get("order_number") or extract_order_number(order_text) or "").strip()
    customer_email = (item.get("customer_email") or extract_customer_email(order_text, connected_email) or "").strip()
    if not customer_email or not order_number or order_number.lower() == "unknown":
        return item
    if latest_email_is_from_connected_account(thread, connected_email):
        return {**item, "status": "Already Replied", "reply": None, "reply_sent_at": item.get("reply_sent_at") or _safe_iso_now()}
    if _order_welcome_sent_confirmed(service, customer_email, order_number):
        return {**item, "status": "Already Replied", "reply": None, "reply_sent_at": item.get("reply_sent_at") or _safe_iso_now()}
    # If older buggy runs marked it sent in JSON but Gmail Sent does not confirm it, make it pending again.
    customer_name = item.get("customer_name") or best_customer_name(order_text, customer_email)
    subject, body = build_order_welcome_email(customer_name, order_number)
    return {**item, "status": "Waiting to Send", "customer_email": customer_email, "order_number": order_number, "reply": {"thread_id": thread.get("thread_id", ""), "mode": "new_email", "to": customer_email, "subject": subject, "body": body}}

def _auto_send_order_if_safe(service, thread: Dict, connected_email: str, order_item: Dict) -> Tuple[Dict, bool]:
    order_text = get_best_order_email_text(thread) or ""
    order_number = str(order_item.get("order_number") or extract_order_number(order_text) or "").strip()
    customer_email = (order_item.get("customer_email") or extract_customer_email(order_text, connected_email) or "").strip()
    customer_name = order_item.get("customer_name") or best_customer_name(order_text, customer_email)
    if not get_automation_settings().get("auto_reply_enabled", True):
        order_item["auto_send_note"] = "Auto Reply is off."
        print(f"[orders] auto-send skipped | order={order_number or 'unknown'} | reason=Auto Reply off", flush=True)
        return order_item, False
    if not customer_email or not order_number or order_number.lower() == "unknown":
        order_item["auto_send_error"] = "Missing customer email or order number."
        print(f"[orders] auto-send skipped | order={order_number or 'unknown'} | reason=missing customer email/order number", flush=True)
        return order_item, False
    if latest_email_is_from_connected_account(thread, connected_email):
        order_item = {**order_item, "status": "Already Replied", "reply": None, "reply_sent_at": order_item.get("reply_sent_at") or _safe_iso_now()}
        return order_item, False
    if _order_welcome_sent_confirmed(service, customer_email, order_number):
        order_item = {**order_item, "status": "Already Replied", "reply": None, "reply_sent_at": order_item.get("reply_sent_at") or _safe_iso_now()}
        mark_thread_action_processed(thread_action_key(thread, connected_email), "manual_or_prior_reply_found", "", order_number=order_number)
        upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": customer_name, "status": "Already Replied", "total": order_item.get("total", ""), "products": order_item.get("products", [])})
        print(f"[orders] auto-send skipped | order={order_number} | to={customer_email} | reason=welcome already exists in Sent", flush=True)
        return order_item, False
    reply = order_item.get("reply") if isinstance(order_item.get("reply"), dict) else {}
    subject = (reply.get("subject") or "Welcome to Pharmacy Prep").strip()
    body = (reply.get("body") or build_order_welcome_email(customer_name, order_number)[1]).strip()
    try:
        sent = send_new_email(service, customer_email, subject, body)
        sent_id = sent.get("id", "") if isinstance(sent, dict) else ""
        sent_at = _safe_iso_now()
        order_item = {**order_item, "status": "Sent Automatically", "reply": None, "reply_sent_at": sent_at, "sent_message_id": sent_id}
        mark_thread_action_processed(thread_action_key(thread, connected_email), "order_auto_sent", sent_id, order_number=order_number)
        upsert_processed_order(order_number, {"customer_email": customer_email, "customer_name": customer_name, "status": "Sent Automatically", "sent_message_id": sent_id, "sent_at": sent_at, "total": order_item.get("total", ""), "products": order_item.get("products", [])})
        print(f"[orders] AUTO EMAIL SENT | order={order_number} | to={customer_email} | sent_id={sent_id}", flush=True)
        return order_item, True
    except Exception as error:
        subject, body = build_order_welcome_email(customer_name, order_number)
        order_item = {**order_item, "status": "Waiting to Send", "auto_send_error": str(error), "reply": {"thread_id": thread.get("thread_id", ""), "mode": "new_email", "to": customer_email, "subject": subject, "body": body}}
        print(f"[orders] AUTO EMAIL FAILED | order={order_number} | to={customer_email} | error={error}", flush=True)
        return order_item, False

# Better reply validation and fallback. No greeting-only drafts.
def _final_meaningful_reply_words(body: str) -> int:
    text = str(body or "")
    text = re.sub(r"(?is)regards\s*\n\s*pharmacy prep.*$", "", text)
    text = re.sub(r"(?is)regards\s*$", "", text)
    text = re.sub(r"(?im)^\s*(hello|hi|dear)\s+[a-z .'-]+,?\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return len([w for w in text.split(" ") if w])

def reply_needs_regeneration(reply_body: str, latest_body: str, category: str = "work") -> bool:
    body = str(reply_body or "").strip()
    body_lower = body.lower()
    if _final_meaningful_reply_words(body) < 18:
        return True
    bad_fillers = [
        "we received your message", "we have received your message", "we will review", "we'll review",
        "get back to you", "thank you for your email. we will", "wanted to reply right away",
        "based on the information currently available", "let us know if you have any questions",
    ]
    if any(p in body_lower for p in bad_fillers):
        return True
    latest = clean_preview_text(latest_body or "", 5000).strip()
    if latest and copied_sequence_found(body, latest, sequence_len=12):
        return True
    if category != "personal" and "pharmacy prep" not in body_lower:
        return True
    return False

def _target_reply_fallback(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Dict:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    _, to_email = parseaddr(latest.get("from", ""))
    display_name = sender_display_name(latest.get("from", ""), to_email)
    name = display_name if display_name and display_name != "The sender" else "there"
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    text = _final_clean_text(latest.get("subject", ""), latest.get("body", ""))
    if any(t in text for t in ["connect", "call", "spare time", "available", "availability", "reach"]):
        main = "Thank you for letting us know. Please send two or three time slots that work for you today, along with the best phone number to reach you, and we will arrange a time to connect."
    elif any(t in text for t in ["login", "access", "password", "online account"]):
        main = "Thank you for your email. We will check the course access connected to your email address and send the correct login details or next steps. Please confirm the email address used for registration if it is different from this one."
    elif any(t in text for t in ["order number", "order #", "invoice", "receipt", "payment"]):
        main = "Thank you for your email. We will check the order and payment history connected to your email address and confirm the correct order, invoice, or receipt details for you."
    elif any(t in text for t in ["ee", "pebc", "evaluating", "exam", "course", "class", "notes", "recording", "mock", "qbank"]):
        main = "Thank you for your email. We will review the course and PEBC/EE details related to your question and reply with the specific information you need for your preparation."
    elif category == "personal":
        main = "Thank you for your email. Please send the details or timing that works best, and I will follow up accordingly."
    else:
        main = "Thank you for your email. We will review the details connected to your account/course and reply with the specific information needed for this request."
    if category == "personal":
        body = f"Hello {name},\n\n{main}\n\nRegards"
    else:
        body = f"Hello {name},\n\n{main}\n\nRegards\nPharmacy Prep\nPhone: 416-223-PREP (7737)\nWhatsApp: 647-221-0457\nwww.pharmacyprep.com"
    return {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": to_email, "subject": subject, "body": body}

def fallback_reply_for_thread(thread: Dict, connected_email: str, category: str) -> Dict:
    return _target_reply_fallback(thread, connected_email, category)

_prev_build_general_email_item_final = build_general_email_item
def build_general_email_item(service, thread: Dict, connected_email: str, personal_label_id: str, work_label_id: str) -> Optional[Dict]:
    if _target_is_renewal_thread(thread, connected_email):
        return None
    text, sender = _final_thread_text_and_sender(thread, connected_email)
    if _target_is_new_question(text) or _final_is_promo_text(text, sender):
        return None
    item = _prev_build_general_email_item_final(service, thread, connected_email, personal_label_id, work_label_id)
    if not item or item.get("filtered_out") or item.get("status") == "Filtered Out":
        return item
    category = category_for_thread_strict(thread, connected_email)
    if category == "renewal":
        return None
    item["category"] = category
    item["screening_version"] = EMAIL_SCREENING_VERSION
    item["ai_screened"] = True
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    latest_body = clean_preview_text(latest.get("body", ""), 7000)
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
    if item.get("status") != "Already Replied" and (not reply or reply_needs_regeneration(reply.get("body", ""), latest_body, category)):
        queries = _fast_context_queries_for_thread(thread, connected_email) if "_fast_context_queries_for_thread" in globals() else heuristic_context_queries_for_thread(thread, connected_email)
        extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread.get("thread_id", ""), max_threads_per_query=3) if queries else ""
        composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
        if composed and not reply_needs_regeneration(composed.get("body", ""), latest_body, category):
            item["reply"] = {"thread_id": thread.get("thread_id", ""), "mode": "thread_reply", "to": parseaddr(latest.get("from", ""))[1].strip(), "subject": composed.get("subject", ""), "body": composed.get("body", "")}
            if composed.get("summary"):
                item["important_reason"] = composed.get("summary")
            if composed.get("title") and 3 <= len(composed.get("title", "").split()) <= 12:
                item["title"] = composed.get("title")
        else:
            item["reply"] = fallback_reply_for_thread(thread, connected_email, category)
    elif reply:
        body = _professionalize_reply_body(reply.get("body", ""), category)
        item["reply"]["body"] = body if body and not reply_needs_regeneration(body, latest_body, category) else fallback_reply_for_thread(thread, connected_email, category).get("body", "")
    return item

def _cleanup_catalog_for_tabs() -> int:
    catalog = get_dashboard_catalog()
    emails = catalog.setdefault("emails", {})
    changed = 0
    new_emails = {}
    for key, item in list((emails or {}).items()):
        if not isinstance(item, dict):
            changed += 1
            continue
        original = item.get("original", {}) if isinstance(item.get("original", {}), dict) else {}
        sender = parseaddr(original.get("from", ""))[1].lower().strip()
        text = _final_clean_text(item.get("title", ""), item.get("important_reason", ""), original.get("from", ""), original.get("subject", ""), original.get("body", ""))
        if _target_is_new_question(text) or _final_is_promo_text(text, sender):
            changed += 1
            continue
        if _is_renewal_item(item):
            item["category"] = "renewal"
            item["is_renewal_request"] = True
            item["renewal_request"] = True
        else:
            item["category"] = "work" if _final_has_work_signal(text, sender) else "personal"
        item["screening_version"] = EMAIL_SCREENING_VERSION
        item["ai_screened"] = True
        if isinstance(item.get("reply"), dict) and item["category"] in ("work", "personal"):
            body = _professionalize_reply_body(item["reply"].get("body", ""), item["category"])
            if not body or reply_needs_regeneration(body, original.get("body", ""), item["category"]):
                item["reply"] = None
                item["status"] = "Needs Reply"
                changed += 1
            elif body != item["reply"].get("body", ""):
                item["reply"]["body"] = body
                changed += 1
        new_emails[key] = item
    if changed:
        catalog["emails"] = new_emails
        save_dashboard_catalog(catalog)
        invalidate_dashboard_cache()
    return changed

_prev_build_dashboard_payload_finalfix = build_dashboard_payload
def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    payload = _prev_build_dashboard_payload_finalfix(force_refresh=force_refresh)
    try:
        catalog = get_dashboard_catalog()
        all_emails = [deepcopy(item) for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
        cleaned, renewals, seen = [], [], set()
        for item in sorted(all_emails, key=_catalog_sort_key, reverse=True):
            if not isinstance(item, dict) or _catalog_item_looks_unimportant(item):
                continue
            if _is_renewal_item(item):
                item["category"] = "renewal"
                item["is_renewal_request"] = True
                item["renewal_request"] = True
                renewals.append(item)
                continue
            item["category"] = _final_category_for_item(item)
            ident = str(item.get("thread_id") or id(item))
            if ident in seen:
                continue
            seen.add(ident)
            cleaned.append(item)
        payload["emails"] = cleaned
        payload["renewals"] = renewals
        pending = [i.get("thread_id") for i in cleaned + renewals if isinstance(i.get("reply"), dict)] + [o.get("thread_id") for o in payload.get("orders", []) if isinstance(o.get("reply"), dict)]
        payload["pending_replies"] = [x for x in pending if x]
        payload["stats"] = {**payload.get("stats", {}), "pending_replies": len(payload["pending_replies"]), "renewal_emails": len(renewals), "work_emails": len([e for e in cleaned if e.get("category") == "work"]), "personal_emails": len([e for e in cleaned if e.get("category") == "personal"])}
    except Exception as error:
        print(f"[dashboard] final order/category cleanup failed: {error}", flush=True)
    return payload

_prev_perform_gmail_scan_finalfix = perform_gmail_scan
def perform_gmail_scan(force_full: bool = False) -> Dict:
    payload = _prev_perform_gmail_scan_finalfix(force_full=force_full)
    try:
        changed = _cleanup_catalog_for_tabs()
        invalidate_dashboard_cache()
        fresh = build_dashboard_payload(force_refresh=True)
        fresh["scan_summary"] = {**(payload.get("scan_summary", {}) if isinstance(payload, dict) else {}), "catalog_cleanup_changed": changed, "order_auto_reply_fix": "Sent mail is verified before an order is considered handled; stale JSON sent markers are ignored.", "category_fix": "EE/PEBC/course/renewed prep course forced Work; non-Pharmacy Prep forced Personal."}
        print(f"[scan] final fix complete | work={fresh.get('stats',{}).get('work_emails',0)} | personal={fresh.get('stats',{}).get('personal_emails',0)} | renewals={fresh.get('stats',{}).get('renewal_emails',0)} | cleanup={changed}", flush=True)
        return fresh
    except Exception as error:
        print(f"[scan] final fix failed: {error}", flush=True)
        return payload

if __name__ == "__main__":
    print("\nPharmacy Prep Gmail Assistant is starting...")
    port = int(os.getenv("PORT", "5050"))
    print(f"Open this link in your browser: http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
