#!/usr/bin/env python3
"""
Continuous client activity simulator for a Fineract instance.

Use cases simulated (weighted-random, one per loop iteration):
  1. Client onboarding      - create a new active client
  2. Open savings account   - submit/approve/activate + opening deposit
  3. Savings transaction    - random deposit or withdrawal on an active account
  4. Loan application       - submit/approve/disburse a loan
  5. Loan repayment         - pay the next installment on an active loan
  6. View client            - read a random existing client (broadens GET coverage)
  7. KYC document upload    - attach a synthetic KYC document to a random existing client
  8. KYC identifier capture - record an identity-document identifier on a random existing client
  9. KYC case note          - log a gap/request/audit-style note on a random existing client
  10. KYC document review   - list the documents already on file for a random existing client

Actions 6-10 operate on the broader pre-existing client population (fetched live via
the API, not just entities this script created) rather than growing the client count -
loosely modeled on the "Client Onboarding Document Intake (KYC)" agentic pattern:
documents arrive, get captured against the case record (identifiers, notes, documents),
and get reviewed. This script is a scripted HTTP client, not an actual document-parsing
agent, so the DECIDE/FAN-OUT/REFLECT/LOOP steps in that pattern aren't modeled - only the
concrete Fineract API calls a human analyst's work would eventually produce are.

State (which clients/accounts/loans this script created) is kept in a local JSON file so the
simulator can be stopped and restarted without losing track of what it created.
"""
import argparse
import base64
import json
import os
import random
import signal
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# All connection settings come from the environment so that no credential is
# ever stored in this file (it lives in a shared repo). Defaults match the
# stock docker-compose stack.
BASE_URL = os.environ.get("FINERACT_BASE_URL", "https://localhost:8443/fineract-provider/api/v1")
USERNAME = os.environ.get("FINERACT_USERNAME", "mifos")
PASSWORD = os.environ.get("FINERACT_PASSWORD", "password")
TENANT = os.environ.get("FINERACT_TENANT", "default")

# Long-run bounds. The simulator creates records forever; it must not remember
# them forever. Server-side records are unaffected -- we simply stop tracking
# the oldest ones, which bounds both state.json and the per-iteration scans.
MAX_TRACKED_CLIENTS = int(os.environ.get("SIM_MAX_CLIENTS", "500"))
MAX_TRACKED_SAVINGS = int(os.environ.get("SIM_MAX_SAVINGS", "500"))
MAX_TRACKED_LOANS = int(os.environ.get("SIM_MAX_LOANS", "500"))

# Network resilience: the app restarts (redeploys, OOM) and the simulator must
# ride through it rather than burning a tight error loop.
MAX_RETRIES = int(os.environ.get("SIM_MAX_RETRIES", "5"))

# Local dev container uses a self-signed certificate.
_INSECURE_SSL_CONTEXT = ssl.create_default_context()
_INSECURE_SSL_CONTEXT.check_hostname = False
_INSECURE_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

DATE_FORMAT = "dd MMMM yyyy"
LOCALE = "en"

# Overwritten by bootstrap_estate() when it detects an empty estate and creates
# fresh records; otherwise it picks up whatever the instance already has.
LOAN_PRODUCT_ID = 1  # "Custom Schedule LP": 10k-100k principal, 12 monthly installments @ 12%
SAVINGS_PRODUCT_ID = 1  # "Regular Saving product": min opening balance 100
PAYMENT_TYPE_ID = 1  # "Money Transfer"
CURRENCY_CODE = "USD"

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Amara", "Kwame", "Chidi", "Fatima",
    "Wanjiru", "Kip", "Zola", "Themba", "Aditi", "Rohan", "Priya", "Arjun",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas", "Taylor",
    "Okafor", "Mwangi", "Achebe", "Diallo", "Kimani", "Ndlovu", "Sharma", "Patel",
]

# Fineract's identifier-document-type codes, from GET /clients/{id}/identifiers/template.
IDENTIFIER_DOCUMENT_TYPES = [
    (1, "Passport", lambda: "P" + "".join(random.choices("0123456789", k=8))),
    (2, "Id", lambda: "".join(random.choices("0123456789", k=10))),
    (3, "Drivers License", lambda: "DL" + "".join(random.choices("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ", k=7))),
    (4, "Any Other Id Type", lambda: "OTH" + "".join(random.choices("0123456789", k=6))),
]

# Fineract's content-store whitelist only allows these extensions/mime types by default.
KYC_DOCUMENT_TYPES = [
    ("Passport", "application/pdf", "pdf"),
    ("ProofOfAddress", "application/pdf", "pdf"),
    ("SourceOfFunds", "application/pdf", "pdf"),
    ("CertificateOfIncorporation", "application/pdf", "pdf"),
    ("OwnershipDeclaration", "application/pdf", "pdf"),
    ("TaxForm", "application/pdf", "pdf"),
]

KYC_NOTES = [
    "KYC: proof of address older than 3 months - document update requested",
    "KYC: ownership chart missing - request sent to client",
    "KYC: all required documents verified, no gaps identified",
    "KYC: identity document expiring soon - renewal flagged",
    "KYC: politically exposed person (PEP) declaration on file - enhanced due diligence note added",
    "KYC: source-of-funds evidence received and reconciled against application",
    "KYC: conflicting address across submitted documents - both sources cited, resolution pending",
    "KYC: record reviewed and passed to screening and rules stage",
]

KYC_ADDRESSES = [
    "14 Birchwood Lane, Springfield", "88 Harbor View Road, Rivermouth",
    "221 Baker Street, Elmsworth", "7 Market Square, Dunholt", "56 Pine Ridge Drive, Fairhaven",
]
KYC_FUNDS_SOURCES = ["Salary / employment income", "Business income", "Inheritance", "Investment proceeds", "Sale of property"]


def _random_document_number():
    return "".join(random.choices("0123456789", k=9))


def _pdf_text_lines_for(doc_name, client_name, document_number):
    """Short lines of fake-but-plausible body text for a given KYC document type, tied to
    the client it's being attached to and a freshly generated document number."""
    today = today_str()
    if doc_name == "Passport":
        return ["PASSPORT", f"Name: {client_name}", f"Document No: {document_number}",
                "Nationality: SIMULATED", f"Date of Issue: {today}"]
    if doc_name == "ProofOfAddress":
        return ["PROOF OF ADDRESS", f"Name: {client_name}",
                f"Address: {random.choice(KYC_ADDRESSES)}", f"Date: {today}"]
    if doc_name == "SourceOfFunds":
        return ["SOURCE OF FUNDS DECLARATION", f"Name: {client_name}",
                f"Declared Source: {random.choice(KYC_FUNDS_SOURCES)}", f"Date: {today}"]
    if doc_name == "CertificateOfIncorporation":
        return ["CERTIFICATE OF INCORPORATION", f"Entity Name: {client_name}",
                f"Registration No: {document_number}", f"Date of Incorporation: {today}"]
    if doc_name == "OwnershipDeclaration":
        return ["OWNERSHIP AND CONTROL DECLARATION", f"Entity Name: {client_name}",
                f"Beneficial Owner: {random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
                f"Ownership Percentage: {random.randint(10, 100)}%"]
    return ["TAX FORM", f"Name: {client_name}", f"Tax ID: {document_number}",
            f"Tax Year: {date.today().year}"]


def generate_kyc_pdf_bytes(doc_name, client_name, document_number):
    """Builds a small, genuinely valid single-page PDF with the KYC lines above rendered
    as real text - so the "document" is actually openable and readable, not just a filler
    byte string wearing a .pdf extension."""
    lines = _pdf_text_lines_for(doc_name, client_name, document_number)
    text_ops = ["BT", "/F1 14 Tf", "72 720 Td", "16 TL"]
    for i, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i > 0:
            text_ops.append("T*")
        text_ops.append(f"({escaped}) Tj")
    text_ops.append("ET")
    stream_data = "\n".join(text_ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream_data)).encode() + b" >>\nstream\n" + stream_data + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(out)


def today_str():
    return date.today().strftime("%d %B %Y")


class FineractClient:
    def __init__(self, base_url, username, password, tenant):
        self.base_url = base_url
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Fineract-Platform-TenantId": tenant,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, params=None, body=None, content_type="application/json", raw_body=None):
        url = f"{self.base_url}{path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        if raw_body is not None:
            data = raw_body
        else:
            data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = content_type

        # HTTPError means the server answered and rejected us: a retry would
        # just repeat the same rejection, so it propagates immediately.
        # Everything else (connection refused, DNS, timeout, 5xx-as-URLError)
        # means the server is unreachable or restarting, so back off and retry.
        delay = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15, context=_INSECURE_SSL_CONTEXT) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail[:500]}") from None
            except Exception as e:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"{method} {path} -> unreachable after {MAX_RETRIES} tries: {e}") from None
                log(f"{method} {path} failed ({e}); retry {attempt}/{MAX_RETRIES} in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, params=None, body=None):
        return self._request("POST", path, params=params, body=body)

    def put(self, path, params=None, body=None):
        return self._request("PUT", path, params=params, body=body)

    def post_multipart(self, path, fields, file_field_name, file_name, file_bytes, file_content_type):
        boundary = f"----fineract-sim-{random.randint(10**15, 10**16 - 1)}"
        parts = []
        for key, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{file_field_name}\"; filename=\"{file_name}\"\r\n"
                f"Content-Type: {file_content_type}\r\n\r\n"
            ).encode()
            + file_bytes
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return self._request(
            "POST", path, raw_body=body, content_type=f"multipart/form-data; boundary={boundary}",
        )


class State:
    """Bounded, crash-safe view of what this simulator has created.

    Three properties matter for a process that runs for months:
      * bounded   - each collection is a ring buffer, so the file and the
                    per-iteration scans stop growing (see MAX_TRACKED_*).
      * atomic    - written via a temp file + os.replace, so a kill -9 or a
                    full disk can never leave a half-written file behind.
      * throttled - flushed every save_every iterations rather than on every
                    one, because rewriting the whole file per action is
                    quadratic in total write volume over a long run.
    """

    def __init__(self, path, save_every=25):
        self.path = Path(path)
        self.save_every = max(1, save_every)
        self._dirty = 0
        self.data = {"clients": [], "savings": [], "loans": []}
        # Not persisted - just an in-process cache of the live system's total client
        # count, used to pick a random *existing* client for the KYC/read actions.
        self.remote_client_total = None
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                for key in self.data:
                    if isinstance(loaded.get(key), list):
                        self.data[key] = loaded[key]
            except (ValueError, OSError) as e:
                # A corrupt state file must not stop a 24/7 run: the records
                # still exist server-side, we just lose track of them.
                log(f"State file {self.path} unreadable ({e}); starting fresh")

    def _trim(self):
        """Drop the oldest tracked records once a collection exceeds its cap.

        Closed loans go first -- they are dead weight, since only active loans
        are ever selected for repayment.
        """
        self.data["loans"] = [l for l in self.data["loans"] if l["status"] == "active"]
        for key, cap in (("clients", MAX_TRACKED_CLIENTS),
                         ("savings", MAX_TRACKED_SAVINGS),
                         ("loans", MAX_TRACKED_LOANS)):
            if len(self.data[key]) > cap:
                self.data[key] = self.data[key][-cap:]
        # Never keep a savings/loan whose client we have already forgotten.
        live = {c["id"] for c in self.data["clients"]}
        self.data["savings"] = [s for s in self.data["savings"] if s["client_id"] in live]
        self.data["loans"] = [l for l in self.data["loans"] if l["client_id"] in live]

    def save(self, force=False):
        self._dirty += 1
        if not force and self._dirty < self.save_every:
            return
        self._dirty = 0
        self._trim()
        payload = json.dumps(self.data, indent=2)
        tmp_name = None
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
            with os.fdopen(tmp_fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(self.path))
        except OSError as e:
            # State is only a cache -- every record it tracks also exists
            # server-side. A process meant to run for months must not die
            # because one flush failed (bad path, full disk, read-only mount),
            # so this warns and keeps going.
            log(f"WARNING: could not persist state to {self.path}: {e}")
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def add_client(self, client_id, office_id, name):
        self.data["clients"].append({"id": client_id, "office_id": office_id, "name": name})

    def clients_without_savings(self):
        with_savings = {s["client_id"] for s in self.data["savings"]}
        return [c for c in self.data["clients"] if c["id"] not in with_savings]

    def clients_without_active_loan(self):
        active_loan_clients = {l["client_id"] for l in self.data["loans"] if l["status"] == "active"}
        return [c for c in self.data["clients"] if c["id"] not in active_loan_clients]

    def active_savings(self):
        return [s for s in self.data["savings"] if s["status"] == "active"]

    def active_loans(self):
        return [l for l in self.data["loans"] if l["status"] == "active"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_offices(api):
    offices = api.get("/offices")
    return [o["id"] for o in offices]


def fetch_random_existing_client(api, state):
    """Picks a random client from the *entire* live population (not just ones this
    script created) - used by the read-only and KYC actions, per the deliberate
    choice to exercise those against the existing dataset rather than only the
    clients this run has onboarded."""
    if state.remote_client_total is None:
        page = api.get("/clients", params={"limit": 1})
        state.remote_client_total = page.get("totalFilteredRecords", 0)
    total = state.remote_client_total
    if not total:
        return None
    offset = random.randint(0, total - 1)
    page = api.get("/clients", params={"limit": 1, "offset": offset})
    items = page.get("pageItems", [])
    return items[0] if items else None


def onboard_client(api, state, office_ids):
    office_id = random.choice(office_ids)
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    body = {
        "officeId": office_id,
        "firstname": first,
        "lastname": last,
        "legalFormId": 1,
        "active": True,
        "activationDate": today_str(),
        "submittedOnDate": today_str(),
        "dateFormat": DATE_FORMAT,
        "locale": LOCALE,
    }
    result = api.post("/clients", body=body)
    client_id = result["clientId"]
    state.add_client(client_id, office_id, f"{first} {last}")
    log(f"Onboarded client #{client_id}: {first} {last} (office {office_id})")


def open_savings(api, state):
    candidates = state.clients_without_savings()
    if not candidates:
        return False
    client = random.choice(candidates)
    body = {
        "clientId": client["id"],
        "productId": SAVINGS_PRODUCT_ID,
        "submittedOnDate": today_str(),
        "locale": LOCALE,
        "dateFormat": DATE_FORMAT,
    }
    result = api.post("/savingsaccounts", body=body)
    savings_id = result["savingsId"]

    api.post(f"/savingsaccounts/{savings_id}", params={"command": "approve"}, body={
        "approvedOnDate": today_str(), "locale": LOCALE, "dateFormat": DATE_FORMAT,
    })
    api.post(f"/savingsaccounts/{savings_id}", params={"command": "activate"}, body={
        "activatedOnDate": today_str(), "locale": LOCALE, "dateFormat": DATE_FORMAT,
    })

    opening_deposit = round(random.uniform(500, 5000), 2)
    api.post(f"/savingsaccounts/{savings_id}/transactions", params={"command": "deposit"}, body={
        "transactionDate": today_str(), "transactionAmount": opening_deposit,
        "paymentTypeId": PAYMENT_TYPE_ID, "locale": LOCALE, "dateFormat": DATE_FORMAT,
    })

    state.data["savings"].append({
        "id": savings_id, "client_id": client["id"], "status": "active", "balance": opening_deposit,
    })
    log(f"Opened savings #{savings_id} for client #{client['id']} ({client['name']}), "
        f"opening deposit {opening_deposit}")
    return True


def savings_transaction(api, state):
    accounts = state.active_savings()
    if not accounts:
        return False
    account = random.choice(accounts)
    is_deposit = account["balance"] < 200 or random.random() < 0.65
    amount = round(random.uniform(50, 1500), 2)

    command = "deposit" if is_deposit else "withdrawal"
    try:
        api.post(f"/savingsaccounts/{account['id']}/transactions", params={"command": command}, body={
            "transactionDate": today_str(), "transactionAmount": amount,
            "paymentTypeId": PAYMENT_TYPE_ID, "locale": LOCALE, "dateFormat": DATE_FORMAT,
        })
    except RuntimeError as e:
        log(f"Savings {command} of {amount} on #{account['id']} failed (likely lock-in/insufficient funds): {e}")
        return False

    account["balance"] += amount if is_deposit else -amount
    log(f"Savings #{account['id']}: {command} {amount} -> balance {account['balance']:.2f}")
    return True


def apply_loan(api, state):
    candidates = state.clients_without_active_loan()
    if not candidates:
        return False
    client = random.choice(candidates)
    principal = round(random.uniform(10000, 100000), 2)
    body = {
        "clientId": client["id"],
        "productId": LOAN_PRODUCT_ID,
        "loanType": "individual",
        "principal": principal,
        "loanTermFrequency": 12,
        "loanTermFrequencyType": 2,
        "numberOfRepayments": 12,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": 12,
        "amortizationType": 1,
        "interestType": 0,
        "interestCalculationPeriodType": 0,
        "transactionProcessingStrategyCode": "creocore-strategy",
        "expectedDisbursementDate": today_str(),
        "submittedOnDate": today_str(),
        "locale": LOCALE,
        "dateFormat": DATE_FORMAT,
    }
    result = api.post("/loans", body=body)
    loan_id = result["loanId"]

    api.post(f"/loans/{loan_id}", params={"command": "approve"}, body={
        "approvedOnDate": today_str(),
        "expectedDisbursementDate": today_str(), "locale": LOCALE, "dateFormat": DATE_FORMAT,
    })
    api.post(f"/loans/{loan_id}", params={"command": "disburse"}, body={
        "actualDisbursementDate": today_str(),
        "paymentTypeId": PAYMENT_TYPE_ID, "locale": LOCALE, "dateFormat": DATE_FORMAT,
    })

    schedule = api.get(f"/loans/{loan_id}", params={"associations": "repaymentSchedule"})
    periods = [p for p in schedule["repaymentSchedule"]["periods"] if p.get("principalDue") is not None]
    installment = periods[0]["totalDueForPeriod"] if periods else round(principal / 12, 2)

    state.data["loans"].append({
        "id": loan_id, "client_id": client["id"], "status": "active",
        "principal": principal, "installment": round(installment, 2), "installments_paid": 0,
    })
    log(f"Disbursed loan #{loan_id} for client #{client['id']} ({client['name']}): {principal}")
    return True


def loan_repayment(api, state):
    loans = state.active_loans()
    if not loans:
        return False
    loan = random.choice(loans)
    amount = loan["installment"]
    try:
        result = api.post(f"/loans/{loan['id']}/transactions", params={"command": "repayment"}, body={
            "transactionDate": today_str(), "transactionAmount": amount,
            "paymentTypeId": PAYMENT_TYPE_ID, "locale": LOCALE, "dateFormat": DATE_FORMAT,
        })
    except RuntimeError as e:
        log(f"Repayment on loan #{loan['id']} failed: {e}")
        return False

    loan["installments_paid"] += 1
    details = api.get(f"/loans/{loan['id']}")
    if details.get("status", {}).get("id") == 600 or details.get("status", {}).get("closedObligationsMet"):
        loan["status"] = "closed"
        log(f"Loan #{loan['id']} fully repaid and closed after {loan['installments_paid']} installments")
    else:
        log(f"Loan #{loan['id']}: repayment {amount} ({loan['installments_paid']}/12 installments)")
    return True


def view_client(api, state):
    client = fetch_random_existing_client(api, state)
    if client is None:
        return False
    details = api.get(f"/clients/{client['id']}")
    log(f"Viewed client #{client['id']} ({details.get('displayName')}) - status {details.get('status', {}).get('value')}")
    return True


def upload_client_document(api, state):
    client = fetch_random_existing_client(api, state)
    if client is None:
        return False
    doc_name, mime_type, ext = random.choice(KYC_DOCUMENT_TYPES)
    client_name = client.get("displayName", "Unknown Client")
    document_number = _random_document_number()
    file_bytes = generate_kyc_pdf_bytes(doc_name, client_name, document_number)
    file_name = f"{doc_name.lower()}_{document_number}.{ext}"
    try:
        api.post_multipart(
            f"/clients/{client['id']}/documents",
            fields={"name": doc_name, "description": f"KYC: {doc_name} captured during onboarding intake"},
            file_field_name="file",
            file_name=file_name,
            file_bytes=file_bytes,
            file_content_type=mime_type,
        )
    except RuntimeError as e:
        log(f"Document upload ({doc_name}) for client #{client['id']} failed: {e}")
        return False
    log(f"Uploaded {doc_name} document for client #{client['id']} ({client_name})")
    return True


def add_client_identifier(api, state):
    client = fetch_random_existing_client(api, state)
    if client is None:
        return False
    doc_type_id, doc_type_name, key_generator = random.choice(IDENTIFIER_DOCUMENT_TYPES)
    document_key = key_generator()
    try:
        api.post(f"/clients/{client['id']}/identifiers", body={
            "documentTypeId": doc_type_id,
            "documentKey": document_key,
            "status": "ACTIVE",
            "description": f"KYC: {doc_type_name} captured during onboarding intake",
            "locale": LOCALE,
        })
    except RuntimeError as e:
        log(f"Identifier capture ({doc_type_name}) for client #{client['id']} failed: {e}")
        return False
    log(f"Recorded {doc_type_name} identifier for client #{client['id']} ({client.get('displayName')})")
    return True


def add_client_kyc_note(api, state):
    client = fetch_random_existing_client(api, state)
    if client is None:
        return False
    note = random.choice(KYC_NOTES)
    api.post(f"/clients/{client['id']}/notes", body={"note": note})
    log(f"Added KYC note to client #{client['id']} ({client.get('displayName')}): {note}")
    return True


def review_client_documents(api, state):
    client = fetch_random_existing_client(api, state)
    if client is None:
        return False
    documents = api.get(f"/clients/{client['id']}/documents")
    log(f"Reviewed documents for client #{client['id']} ({client.get('displayName')}): {len(documents)} on file")
    return True


ACTIONS = [
    ("onboard", onboard_client, 3),
    ("open_savings", open_savings, 3),
    ("savings_tx", savings_transaction, 5),
    ("apply_loan", apply_loan, 2),
    ("loan_repayment", loan_repayment, 4),
    ("view_client", view_client, 3),
    ("upload_document", upload_client_document, 3),
    ("add_identifier", add_client_identifier, 3),
    ("add_kyc_note", add_client_kyc_note, 2),
    ("review_documents", review_client_documents, 2),
]


def bootstrap_estate(api):
    """Idempotently create every record the simulator relies on.

    Order matters: currency must be permitted before products can reference it;
    payment types must exist before any deposit/withdrawal/repayment transaction.
    Sets module-level LOAN_PRODUCT_ID / SAVINGS_PRODUCT_ID / PAYMENT_TYPE_ID.
    """
    global LOAN_PRODUCT_ID, SAVINGS_PRODUCT_ID, PAYMENT_TYPE_ID

    # 1) Currency: ensure CURRENCY_CODE is in the permitted list.
    currencies = api.get("/currencies") or {}
    selected = [c["code"] for c in currencies.get("selectedCurrencyOptions", [])]
    if CURRENCY_CODE not in selected:
        new_list = sorted(set(selected + [CURRENCY_CODE]))
        api.put("/currencies", body={"currencies": new_list})
        log(f"Permitted currency {CURRENCY_CODE} (was: {selected or 'none'})")
    else:
        log(f"Currency {CURRENCY_CODE} already permitted")

    # 2) Payment type: prefer one named 'Money Transfer'; otherwise reuse first,
    # otherwise create one.
    payment_types = api.get("/paymenttypes") or []
    chosen = next((p for p in payment_types if p.get("name") == "Money Transfer"), None) \
        or (payment_types[0] if payment_types else None)
    if chosen:
        PAYMENT_TYPE_ID = chosen["id"]
        log(f"Using existing payment type #{PAYMENT_TYPE_ID}: {chosen.get('name')}")
    else:
        result = api.post("/paymenttypes", body={
            "name": "Money Transfer",
            "description": "Money Transfer",
            "isCashPayment": False,
            "position": 1,
        })
        PAYMENT_TYPE_ID = result.get("resourceId")
        log(f"Bootstrapped payment type #{PAYMENT_TYPE_ID}: Money Transfer")

    # 3) Loan product.
    loan_products = api.get("/loanproducts") or []
    if loan_products:
        LOAN_PRODUCT_ID = loan_products[0]["id"]
        log(f"Using existing loan product #{LOAN_PRODUCT_ID}: {loan_products[0].get('name')}")
    else:
        body = {
            "name": "Simulate Demo Loan Product",
            "shortName": "SIM",
            "currencyCode": CURRENCY_CODE,
            "digitsAfterDecimal": 2,
            "inMultiplesOf": 0,
            "principal": 50000,
            "minPrincipal": 1000,
            "maxPrincipal": 500000,
            "numberOfRepayments": 12,
            "minNumberOfRepayments": 3,
            "maxNumberOfRepayments": 60,
            "repaymentEvery": 1,
            "repaymentFrequencyType": 2,       # Months
            "interestRatePerPeriod": 12,
            "minInterestRatePerPeriod": 0,
            "maxInterestRatePerPeriod": 40,
            "interestRateFrequencyType": 3,    # Per year
            "amortizationType": 1,             # Equal installments
            "interestType": 0,                 # Declining balance
            "interestCalculationPeriodType": 1,  # Same as repayment period
            "transactionProcessingStrategyCode": "creocore-strategy",
            "daysInMonthType": 30,
            "daysInYearType": 365,
            "accountingRule": 1,               # None (no GL setup required)
            "isInterestRecalculationEnabled": False,
            "multiDisburseLoan": False,
            "allowVariableInstallments": False,
            "canDefineInstallmentAmount": False,
            "holdGuaranteeFunds": False,
            "canUseForTopup": False,
            "locale": LOCALE,
            "dateFormat": DATE_FORMAT,
        }
        result = api.post("/loanproducts", body=body)
        LOAN_PRODUCT_ID = result.get("resourceId") or result.get("loanProductId")
        log(f"Bootstrapped loan product #{LOAN_PRODUCT_ID}: {body['name']}")

    # 4) Savings product.
    savings_products = api.get("/savingsproducts") or []
    if savings_products:
        SAVINGS_PRODUCT_ID = savings_products[0]["id"]
        log(f"Using existing savings product #{SAVINGS_PRODUCT_ID}: {savings_products[0].get('name')}")
    else:
        body = {
            "name": "Simulate Demo Savings Product",
            "shortName": "SIMS",
            "currencyCode": CURRENCY_CODE,
            "digitsAfterDecimal": 2,
            "inMultiplesOf": 0,
            "nominalAnnualInterestRate": 0,
            "interestCompoundingPeriodType": 1,     # Daily
            "interestPostingPeriodType": 4,         # Monthly
            "interestCalculationType": 1,           # Daily balance
            "interestCalculationDaysInYearType": 365,
            "minRequiredOpeningBalance": 100,
            "accountingRule": 1,                    # None
            "locale": LOCALE,
        }
        result = api.post("/savingsproducts", body=body)
        SAVINGS_PRODUCT_ID = result.get("resourceId") or result.get("savingsProductId")
        log(f"Bootstrapped savings product #{SAVINGS_PRODUCT_ID}: {body['name']}")


_STOP = False


def _request_stop(signum, _frame):
    """SIGTERM/SIGINT: finish the current action, flush state, exit cleanly.

    Needed so `systemctl stop` does not kill the process mid-iteration and
    lose the un-flushed part of the state ring buffer.
    """
    global _STOP
    _STOP = True
    log(f"Signal {signum} received; finishing current action then stopping.")


def run(iterations, min_delay, max_delay, state_path, save_every=25):
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    api = FineractClient(BASE_URL, USERNAME, PASSWORD, TENANT)
    state = State(state_path, save_every=save_every)
    bootstrap_estate(api)
    office_ids = fetch_offices(api)
    if not office_ids:
        raise RuntimeError(
            "No offices exist in this Fineract instance. Fineract normally auto-creates "
            "Head Office at schema init; verify the DB was seeded correctly."
        )
    log(f"Connected. {len(office_ids)} offices, {len(state.data['clients'])} clients already tracked.")

    # Built once: the weighting never changes, so rebuilding it per iteration
    # was pure waste in a loop that runs millions of times.
    weighted = []
    for name, fn, weight in ACTIONS:
        weighted.extend([(name, fn)] * weight)

    count = 0
    consecutive_errors = 0
    while (iterations is None or count < iterations) and not _STOP:
        name, fn = random.choice(weighted)
        try:
            if name == "onboard":
                fn(api, state, office_ids)
            else:
                did_something = fn(api, state)
                if did_something is False:
                    log(f"Skipped {name}: no eligible clients/accounts yet")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR during {name}: {e}")
            # Stop hammering (and stop filling the log) while the app is down.
            if consecutive_errors >= 5:
                backoff = min(30 * consecutive_errors, 300)
                log(f"{consecutive_errors} consecutive failures; sleeping {backoff}s")
                time.sleep(backoff)
        state.save()
        count += 1
        time.sleep(random.uniform(min_delay, max_delay))

    state.save(force=True)
    log(f"Stopped after {count} actions. Tracking {len(state.data['clients'])} clients, "
        f"{len(state.data['savings'])} savings, {len(state.data['loans'])} active loans.")


def main():
    parser = argparse.ArgumentParser(description="Simulate continuous client activity against local Fineract")
    parser.add_argument("--iterations", type=int, default=None, help="Stop after N actions (default: run forever)")
    parser.add_argument("--min-delay", type=float, default=2.0, help="Minimum seconds between actions")
    parser.add_argument("--max-delay", type=float, default=6.0, help="Maximum seconds between actions")
    parser.add_argument("--state-file", default=str(Path(__file__).parent / "state.json"))
    parser.add_argument("--save-every", type=int, default=25,
                        help="Flush state.json every N actions (default: 25). Lower = more durable, "
                             "more disk writes; the file is always flushed on clean shutdown.")
    parser.add_argument("--bootstrap-only", action="store_true",
                        help="Create the demo loan + savings products if missing, then exit.")
    args = parser.parse_args()

    if args.bootstrap_only:
        api = FineractClient(BASE_URL, USERNAME, PASSWORD, TENANT)
        bootstrap_estate(api)
        return

    try:
        run(args.iterations, args.min_delay, args.max_delay, args.state_file, args.save_every)
    except KeyboardInterrupt:
        log("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
