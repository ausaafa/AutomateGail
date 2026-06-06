import os
import base64
import re
import json
import time
from copy import deepcopy
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from openai import OpenAI
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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
INITIAL_SCAN_DAYS = int(os.getenv("INITIAL_SCAN_DAYS", "180"))
INCREMENTAL_SCAN_DAYS = int(os.getenv("INCREMENTAL_SCAN_DAYS", "21"))
MAX_ORDER_THREADS_PER_SCAN = int(os.getenv("MAX_ORDER_THREADS_PER_SCAN", "120"))
MAX_EMAIL_THREADS_PER_SCAN = int(os.getenv("MAX_EMAIL_THREADS_PER_SCAN", "120"))
MAX_AI_REPLIES_PER_SCAN = int(os.getenv("MAX_AI_REPLIES_PER_SCAN", "20"))
_dashboard_cache = {
    "built_at": 0.0,
    "payload": None,
}
app = Flask(__name__)
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
    if not thread.get("emails"):
        return False
    if latest_email_is_from_connected_account(thread, connected_email):
        return False
    if get_best_order_email_text(thread):
        return False
    if is_obvious_automated_email(thread, connected_email):
        return False
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    subject = (latest.get("subject", "") or "").lower()
    body = clean_preview_text(latest.get("body", ""), 6000).lower()
    text = f"{subject}\n{body}"
    hard_excludes = [
        "order has shipped", "has shipped", "on the way", "out for delivery", "delivered",
        "e-transfer received", "etransfer received", "payment received", "receipt",
        "thanks for your payment", "invoice paid", "charge receipt", "successful payment",
        "your order is confirmed", "tracking number", "shipment",
    ]
    if any(term in text for term in hard_excludes):
        return False
    direct_request_phrases = [
        "?", "can you", "could you", "would you", "please", "let me know", "wondering",
        "i need", "i need help", "i would like", "how do i", "when will", "where is",
        "what is", "what's", "confirm", "clarify", "advise", "help", "question",
        "follow up", "follow-up", "send me", "share", "provide", "update",
        "available", "availability", "looking for", "interested in", "request",
    ]
    conversation_topics = [
        "order number", "mouse", "order", "invoice", "payment", "refund", "course",
        "login", "access", "pebc", "exam", "class", "schedule", "notes", "recording",
        "extension", "renewal", "enroll", "enrol", "registration", "meeting", "call",
        "client", "lawyer", "student", "support",
    ]
    asks_for_action = any(term in text for term in direct_request_phrases)
    on_topic = any(term in text for term in conversation_topics)
    if asks_for_action and on_topic:
        return True
    # Accept short human follow-ups in an existing conversation if they still contain an ask.
    if asks_for_action and len(body.split()) >= 4:
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
    stopwords = {
        "the", "and", "for", "that", "with", "this", "from", "have", "your", "please",
        "could", "would", "about", "there", "their", "them", "they", "what", "when",
        "where", "which", "need", "help", "reply", "email", "thanks", "thank", "hello",
        "regards", "pharmacy", "prep", "course", "order", "number", "student", "message",
    }
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", raw_text)
    priority = []
    for token in tokens:
        token = token.lower().strip()
        if token in stopwords or token.isdigit():
            continue
        if token not in priority:
            priority.append(token)
        if len(priority) >= 8:
            break
    queries = []
    if sender_email:
        queries.append(f'in:anywhere from:{sender_email}')
        queries.append(f'in:anywhere to:{sender_email}')
        queries.append(f'in:anywhere from:{sender_email} (order OR invoice OR payment OR receipt OR login OR access)')
        queries.append(f'in:anywhere to:{sender_email} (order OR invoice OR payment OR receipt OR login OR access)')
    for keyword in priority[:5]:
        if sender_email:
            queries.append(f'in:anywhere from:{sender_email} "{keyword}"')
            queries.append(f'in:anywhere to:{sender_email} "{keyword}"')
        queries.append(f'in:anywhere "{keyword}"')
    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[:8]

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
    # Dashboard inclusion is handled by strict heuristics so email loading stays reliable.
    return None

def compose_reply_with_ai(thread: Dict, connected_email: str, category: str, extra_context: str = "") -> Optional[Dict]:
    latest = latest_inbound_email_for_dashboard(thread, connected_email)
    sender_email = parseaddr(latest.get("from", ""))[1].strip()
    subject = latest.get("subject", "") or "Your email"
    clean_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    latest_body = clean_preview_text(latest.get("body", ""), 9000)
    local_context = search_processed_orders_context(sender_email, f"{latest.get('subject', '')}\n{latest_body}")
    thread_text = format_thread_for_ai(thread)[:15000]
    prompt = f"""
You are writing a real email reply for Pharmacy Prep.
The user should feel like a human carefully read the email and checked Gmail records.

Rules:
1. Never write a vague filler reply like 'we received your message and will get back to you' unless there is truly no other useful content.
2. If the Gmail context contains the answer, use it directly and naturally.
3. If the sender is asking for an order number, invoice, product, payment record, course access detail, prior sent message, or earlier conversation detail, answer using the provided context.
4. If the context is partial, still write a useful professional response that answers what you can and asks at most one concise follow-up question only if absolutely necessary.
5. Do not invent facts that are missing from the thread and context.
6. Keep the reply polished, warm, specific, and professional.
7. For work emails, sign exactly with:
Regards
Pharmacy Prep
Phone: 416-223-PREP (7737)
WhatsApp: 647-221-0457
www.pharmacyprep.com
8. For personal emails, do not use the business signature.

Return JSON only in this exact schema:
{{"subject": "{clean_subject}", "body": "full reply body"}}

Category: {category}
Sender email: {sender_email}
Latest inbound subject: {latest.get("subject", "")}
Latest inbound body:
{latest_body}

Current thread:
{thread_text}

Stored order context:
{local_context or 'None found'}

Related Gmail context:
{extra_context or 'None found'}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = parse_ai_json(response.output_text.strip())
        if parsed and str(parsed.get("body", "")).strip():
            return {
                "subject": str(parsed.get("subject", clean_subject)).strip() or clean_subject,
                "body": str(parsed.get("body", "")).strip(),
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
    subject = (latest.get("subject", "") or "Your email").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    latest_body = clean_preview_text(latest.get("body", ""), 500).replace("\n", " ").strip()
    question_hint = ""
    lowered = latest_body.lower()
    if "order number" in lowered:
        question_hint = "about your order number"
    elif "invoice" in lowered or "receipt" in lowered:
        question_hint = "about your invoice or receipt"
    elif "login" in lowered or "access" in lowered:
        question_hint = "about your login or access"
    elif "payment" in lowered:
        question_hint = "about your payment"
    elif "course" in lowered or "pebc" in lowered or "exam" in lowered:
        question_hint = "about the course"
    elif latest_body:
        question_hint = f"about {latest_body[:90].rstrip(' .,:;')}"
    else:
        question_hint = "about your message"
    if category == "personal":
        body = f"""Hello,

Thank you for your email {question_hint}. I reviewed your message and wanted to reply right away. Based on the information currently in front of me, I may need to double-check one detail before confirming anything further, but I will make sure you receive a proper follow-up.

Regards"""
    else:
        body = f"""Hello,

Thank you for your email {question_hint}. I reviewed your message and checked the available details from our side. Based on the information currently available, I may need to verify one specific detail before confirming anything further, but I wanted to respond right away so you know your request is being handled.

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
    text = f"{latest.get('subject', '')}\n{latest.get('body', '')}".lower()
    if "order number" in text:
        return "Sender is asking for an order number and this likely requires checking prior Gmail records."
    if "invoice" in text or "receipt" in text:
        return "Sender is asking for invoice or receipt details that may exist in Gmail history."
    if "payment" in text:
        return "Sender is asking about a payment-related detail that needs a clear response."
    if "login" in text or "access" in text:
        return "Sender needs account or course access help."
    if "course" in text or "pebc" in text or "exam" in text:
        return "Student is asking a course-related question that needs a direct reply."
    if "meeting" in text or "call" in text or "available" in text:
        return "Conversation contains a scheduling or follow-up request."
    return "Latest inbound email contains a direct question or reply-worthy request."

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

    if category == "personal":
        apply_label_to_thread_messages(service, thread, personal_label_id)
    else:
        apply_label_to_thread_messages(service, thread, work_label_id)

    important_reason = stored_item.get("important_reason") or build_important_reason(thread, connected_email)
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
        if stored_item.get("latest_inbound_id") == latest_inbound_id and stored_item.get("reply"):
            reply = stored_item.get("reply")
        else:
            queries = heuristic_context_queries_for_thread(thread, connected_email)
            extra_context = gather_context_from_gmail(service, queries, current_thread_id=thread_id, max_threads_per_query=3) if queries else ""
            composed = compose_reply_with_ai(thread, connected_email, category, extra_context=extra_context)
            reply = composed and {
                "thread_id": thread_id,
                "mode": "thread_reply",
                "to": parseaddr(latest.get("from", ""))[1].strip(),
                "subject": composed.get("subject", ""),
                "body": composed.get("body", ""),
            }
            if not reply or not str(reply.get("body", "")).strip():
                reply = fallback_reply_for_thread(thread, connected_email, category)
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
    waiting_orders = [order for order in orders if order.get("reply")]
    handled_orders = [order for order in orders if not order.get("reply")]
    work_emails = [email for email in emails if email.get("category") == "work"]
    personal_emails = [email for email in emails if email.get("category") == "personal"]
    lines = [
        "# Daily Gmail Briefing",
        "",
        f"Generated: {now}",
        f"Connected Gmail: {connected_email}",
        "",
        "Quick Summary",
        f"- Orders waiting for approval: {len(waiting_orders)}",
        f"- Orders already handled: {len(handled_orders)}",
        f"- Work replies suggested: {len(work_emails)}",
        f"- Personal replies suggested: {len(personal_emails)}",
        "",
        "Order Queue",
    ]
    if waiting_orders:
        for order in waiting_orders[:10]:
            lines.append(f"- Order #{order.get('order_number')} | {order.get('customer_name')} | Waiting to send")
    else:
        lines.append("- No new order replies are waiting.")
    lines.extend(["", "Actionable Emails"])
    if emails:
        for email in emails[:12]:
            lines.append(f"- [{email.get('category', 'work').title()}] {email.get('title', 'Email')}")
    else:
        lines.append("- No important emails currently need a suggested reply.")
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

def _is_catalog_email_visible(item: Dict) -> bool:
    """Show only meaningful dashboard emails without deleting anything from the persistent catalog."""
    status = item.get("status", "")
    if status in ("Already Replied", "Suggestion Removed", "Needs Reply"):
        return True
    if item.get("reply"):
        return True
    text = "\n".join([
        str(item.get("title", "")),
        str(item.get("important_reason", "")),
        str(item.get("original", {}).get("subject", "")),
        str(item.get("original", {}).get("body", "")),
    ]).lower()
    bad_terms = [
        "order has shipped", "has shipped", "on the way", "out for delivery", "delivered",
        "e-transfer received", "etransfer received", "payment received", "receipt for your payment",
        "charge receipt", "invoice paid", "tracking number", "shipment", "unsubscribe",
        "promotion", "newsletter", "notification",
    ]
    if any(term in text for term in bad_terms):
        return False
    return bool(item.get("important_reason"))

def build_dashboard_payload(force_refresh: bool = False) -> Dict:
    """Return the saved dashboard fast. Gmail/OpenAI work happens in /api/scan, which the UI runs automatically."""
    if not force_refresh and _dashboard_cache["payload"] and (time.time() - _dashboard_cache["built_at"] < DASHBOARD_CACHE_TTL_SECONDS):
        return deepcopy(_dashboard_cache["payload"])

    catalog = get_dashboard_catalog()
    meta = catalog.get("meta", {})
    connected_email = meta.get("connected_email") or DEFAULT_CONNECTED_EMAIL

    orders = list(catalog.get("orders", {}).values())
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]

    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)

    briefing = read_daily_briefing()
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

def _scan_days_for_catalog(catalog: Dict, force_full: bool = False) -> int:
    if force_full or (not catalog.get("orders") and not catalog.get("emails")):
        return INITIAL_SCAN_DAYS
    last_scan = catalog.get("meta", {}).get("last_successful_scan_at", "")
    if not last_scan:
        return min(INITIAL_SCAN_DAYS, 60)
    try:
        parsed = datetime.fromisoformat(last_scan[:19])
        days = (datetime.now() - parsed).days + 2
        return max(2, min(INCREMENTAL_SCAN_DAYS, days))
    except Exception:
        return min(INCREMENTAL_SCAN_DAYS, 7)

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

def _order_scan_queries(days: int) -> List[str]:
    base = f'newer_than:{days}d -in:spam -in:trash'
    return [
        f'{base} "New Order:"',
        f'{base} "[Order #"',
        f'{base} "you have received the following order"',
        f'{base} "you\'ve received the following order"',
        f'{base} "Billing address" "Payment method:" "Total:"',
        f'{base} from:(wordpress OR woocommerce) "Order #"',
    ]

def _email_scan_queries(days: int, connected_email: str) -> List[str]:
    base = f'newer_than:{days}d -in:spam -in:trash -category:promotions -category:social'
    if connected_email:
        base = f'{base} -from:{connected_email}'
    return [
        f'{base} "order number"',
        f'{base} "can you"',
        f'{base} "could you"',
        f'{base} "would you"',
        f'{base} "please"',
        f'{base} "I need"',
        f'{base} "I would like"',
        f'{base} "question"',
        f'{base} "help"',
        f'{base} "login"',
        f'{base} "access"',
        f'{base} "course"',
        f'{base} "PEBC"',
        f'{base} "invoice"',
        f'{base} "refund"',
        f'{base} "extension"',
        f'{base} "schedule"',
        f'{base} "available"',
        f'{base} "availability"',
        f'{base} "mouse"',
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
    days = _scan_days_for_catalog(catalog, force_full=force_full)

    order_thread_ids = _collect_thread_ids(
        service,
        _order_scan_queries(days),
        per_query_limit=max(10, MAX_ORDER_THREADS_PER_SCAN // 4),
        total_limit=MAX_ORDER_THREADS_PER_SCAN,
    )
    email_thread_ids = _collect_thread_ids(
        service,
        _email_scan_queries(days, connected_email),
        per_query_limit=max(8, MAX_EMAIL_THREADS_PER_SCAN // 6),
        total_limit=MAX_EMAIL_THREADS_PER_SCAN,
    )

    auto_orders_sent = 0
    order_replies_waiting = 0
    suggested_replies = 0
    skipped_failed_orders = 0
    processed_order_threads = set()

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
            before = time.time()
            email_item = build_general_email_item(service, thread, connected_email, personal_label_id, work_label_id)
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
        "last_scan_days": days,
    }
    save_dashboard_catalog(catalog)

    orders = list(catalog.get("orders", {}).values())
    emails = [item for item in catalog.get("emails", {}).values() if _is_catalog_email_visible(item)]
    orders.sort(key=_catalog_sort_key, reverse=True)
    emails.sort(key=_catalog_sort_key, reverse=True)
    build_daily_briefing(connected_email, orders, emails)
    invalidate_dashboard_cache()
    payload = build_dashboard_payload(force_refresh=True)
    payload["scan_summary"] = {
        "scan_days": days,
        "orders_checked": len(order_thread_ids),
        "emails_checked": len(email_thread_ids),
        "auto_orders_sent": auto_orders_sent,
        "order_replies_waiting": order_replies_waiting,
        "failed_orders_skipped": skipped_failed_orders,
        "suggested_replies": suggested_replies,
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
            "scan_days": summary.get("scan_days", ""),
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
if __name__ == "__main__":
    print("\nPharmacy Prep Gmail Assistant is starting...")
    print("Open this link in your browser:")
    port = int(os.getenv("PORT", "5050"))
    print(f"http://127.0.0.1:{port}")
    print("")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port)
