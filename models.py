"""Database models. Works with PostgreSQL, MySQL (XAMPP) or SQLite - set DATABASE_URL.

The database holds ONLY user accounts, per-user settings (encrypted AI keys) and admin switches.
Sheets and run history live in each person's browser (IndexedDB), never on the server.
"""
import base64
import hashlib
import os
import json
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

ROLES = ("admin", "member", "viewer")


# ---------------------------------------------------------------- encryption (PRD 5.13: AES-256-GCM at rest)

def _vault_key() -> bytes:
    secret = os.environ.get("VAULT_KEY") or os.environ["SECRET_KEY"]
    return hashlib.sha256(("vault:" + secret).encode()).digest()  # 32 bytes = AES-256


def encrypt(plain: str) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(_vault_key()).encrypt(nonce, plain.encode(), None)).decode()


def decrypt(token: str) -> str:
    raw = base64.b64decode(token)
    return AESGCM(_vault_key()).decrypt(raw[:12], raw[12:], None).decode()


def iso(dt) -> str:
    """UTC timestamp for the browser, which shows it in the viewer's local time."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


# ---------------------------------------------------------------- models

class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="member")  # admin, member, viewer
    is_active_user = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):  # used by Flask-Login: deactivated users cannot sign in
        return bool(self.is_active_user)

    @property
    def is_admin(self):
        return self.role == "admin"

    def to_dict(self):
        return {"id": self.id, "username": self.username, "email": self.email or "", "role": self.role,
                "active": bool(self.is_active_user),
                "created": iso(self.created_at), "last_login": iso(self.last_login)}


class VaultCredential(db.Model):
    """Per-user secrets (AI keys etc). Stored encrypted; the plain value is never sent back to the browser."""
    __tablename__ = "vault_credentials"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    credential_type = db.Column(db.String(50), nullable=False)
    encrypted_value = db.Column(db.Text, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def value(self) -> str:
        try:
            return decrypt(self.encrypted_value)
        except Exception:  # unreadable (e.g. key changed) - treat as not set
            return ""


class AppSetting(db.Model):
    """Admin-controlled switches (PRD 5.14)."""
    __tablename__ = "app_settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")

    @staticmethod
    def get(key: str, default: str = "") -> str:
        row = db.session.get(AppSetting, key)
        return row.value if row else default

    @staticmethod
    def set(key: str, value: str):
        row = db.session.get(AppSetting, key) or AppSetting(key=key)
        row.value = value
        db.session.add(row)


class Share(db.Model):
    """A copy of one sheet that its owner chose to share. Size-capped and expiring, so it can't fill the database.
    The owner's own sheet stays in their browser; this is a snapshot, not a live link."""
    __tablename__ = "shares"
    id = db.Column(db.String(24), primary_key=True)          # unguessable token, also used in the link
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    row_count = db.Column(db.Integer, default=0)
    columns_json = db.Column(db.Text, nullable=False)
    data = db.Column(db.LargeBinary(length=2 ** 24), nullable=False)   # gzip of the rows as JSON
    size_bytes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    allow_export = db.Column(db.Boolean, default=True)
    link_access = db.Column(db.Boolean, default=False)       # any signed-in user with the link
    recipients_json = db.Column(db.Text, default="[]")       # user ids who can open it
    revoked = db.Column(db.Boolean, default=False)
    # Customer links: a copy for someone outside the company, opened without an account.
    kind = db.Column(db.String(12), default="internal")      # "internal" (colleagues) or "external" (customer link)
    customer = db.Column(db.String(160), nullable=True)      # who it was prepared for
    message = db.Column(db.Text, nullable=True)              # note from the sender, shown on the customer's page
    passcode_hash = db.Column(db.String(255), nullable=True) # optional passcode, hashed: used to check what the customer types
    passcode_enc = db.Column(db.Text, nullable=True)         # the same passcode, encrypted, so the sender can look it up again
    acknowledged = db.Column(db.Boolean, default=False)      # sender confirmed they may share this data
    views = db.Column(db.Integer, default=0)
    downloads = db.Column(db.Integer, default=0)
    first_opened_at = db.Column(db.DateTime, nullable=True)
    last_opened_at = db.Column(db.DateTime, nullable=True)

    @property
    def recipients(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.recipients_json or "[]")]
        except (ValueError, TypeError):
            return []

    def is_live(self) -> bool:
        return not self.revoked and self.expires_at > datetime.utcnow()

    @property
    def is_external(self) -> bool:
        return self.kind == "external"

    def can_open(self, user) -> bool:
        """Signed-in access. A customer link is opened through its public page, so inside the app only its owner sees it."""
        if not self.is_live():
            return False
        if self.is_external:
            return user.id == self.owner_id
        return user.id == self.owner_id or bool(self.link_access) or user.id in self.recipients


class DirectorySource(db.Model):
    """A directory a person has set up to pull entries from: list page addresses and how to read them. The settings
    are data (JSON), so any site can be added without a code change. See scraper/sources.py."""
    __tablename__ = "directory_sources"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(80), nullable=False, default="")
    config_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def config(self) -> dict:
        return json.loads(self.config_json)


class ConnectionHealth(db.Model):
    """Tracks health of each external connection service.

    Stored per-service so an admin can see when things last failed,
    and the app can quickly skip unhealthy services without re-testing.
    """
    __tablename__ = "connection_health"

    service = db.Column(db.String(50), primary_key=True)  # "brave", "google_cse", etc.
    is_healthy = db.Column(db.Boolean, default=False)
    last_checked = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    times_checked = db.Column(db.Integer, default=0)
    times_successful = db.Column(db.Integer, default=0)

    @staticmethod
    def upsert(service: str, healthy: bool, error: str = "") -> "ConnectionHealth":
        row = db.session.get(ConnectionHealth, service) or ConnectionHealth(service=service)
        row.is_healthy = bool(healthy)
        row.last_checked = datetime.utcnow()
        row.error_message = (error or "")[:500]
        row.times_checked = (row.times_checked or 0) + 1
        if healthy:
            row.times_successful = (row.times_successful or 0) + 1
        db.session.add(row)
        return row


class MonthlyBudget(db.Model):
    """Per-user monthly spend tracking for paid services.

    If user_id is NULL, the row is global (single-tenant deployments).
    Otherwise it belongs to a specific user who signed in.
    """
    __tablename__ = "monthly_budget"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    service = db.Column(db.String(50), nullable=False)  # "social", "ai", etc.
    spent_inr = db.Column(db.Integer, default=0)  # in INR
    spent_usd = db.Column(db.Float, default=0.0)  # in USD
    month_start = db.Column(db.DateTime, default=datetime.utcnow)
    reset_on = db.Column(db.String(20), default="monthly")  # "monthly", "rolling"

    def is_over_soft_limit(self, limit_inr: int, limit_usd: float) -> bool:
        """Check if we've exceeded the soft budget limits."""
        return (self.spent_inr >= limit_inr) or (self.spent_usd >= limit_usd)


class JobPayload(db.Model):
    """Shared finished-job store for multi-host deployments (Phase 5B).

    Same gzip-JSON bytes as the disk files in scraper/job_store.py, so either
    backend serves identical payloads. Payloads hold public spec only, never secrets.
    """
    __tablename__ = "job_payloads"

    job_id = db.Column(db.String(32), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    mode = db.Column(db.String(20), default="web")
    status = db.Column(db.String(20), default="done")
    data = db.Column(db.LargeBinary(length=2 ** 24), nullable=False)  # gzip of the payload JSON
    size_bytes = db.Column(db.Integer, default=0)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobCancel(db.Model):
    """Cross-host stop flags (Phase 5B). Single-host deployments use tiny disk
    files instead (see scraper/job_store.py); the store picks per backend."""
    __tablename__ = "job_cancel_flags"

    job_id = db.Column(db.String(32), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class OrgCredential(db.Model):
    """Company-shared secrets an admin makes available by role (Phase 5D).

    Same encryption as personal vault keys. A member's own key always wins over
    the shared one; members never see shared values, only their names.
    """
    __tablename__ = "org_credentials"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    credential_type = db.Column(db.String(50), nullable=False)
    encrypted_value = db.Column(db.Text, nullable=False)
    roles_json = db.Column(db.Text, default='["admin", "member"]')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def value(self) -> str:
        try:
            return decrypt(self.encrypted_value)
        except Exception:
            return ""

    def roles(self) -> list:
        try:
            roles = json.loads(self.roles_json or "[]")
            return [r for r in roles if r in ROLES] or ["admin", "member"]
        except (ValueError, TypeError):
            return ["admin", "member"]


class AuditLog(db.Model):
    """Append-only ops trail (Phase 5D): who ran/shared/spent what. Fire-and-forget
    writes — a failed write must never fail the action it records."""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    username = db.Column(db.String(100), nullable=False, default="")
    action = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def log_audit(user_id: int | None, action: str, detail: str = "") -> None:
    """Record one audit event. Never raises, never returns anything useful."""
    try:
        username = ""
        if user_id is not None:
            user = db.session.get(User, user_id)
            username = user.username if user else ""
        db.session.add(AuditLog(user_id=user_id, username=username,
                                action=(action or "")[:50], detail=(detail or "")[:500]))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


class Schedule(db.Model):
    """A saved run that fires on a cron schedule (Phase 5C).

    `body_json` is the exact POST body `/api/jobs` accepts, so triggers reuse the
    same validation, budgets and caps as manual runs. Nothing fires without passing
    that gate; a blocked trigger is recorded in `last_status`, never charged.
    """
    __tablename__ = "schedules"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    body_json = db.Column(db.Text, nullable=False)
    cron = db.Column(db.String(20), nullable=False, default="0 9 * * *")
    enabled = db.Column(db.Boolean, default=True)
    last_triggered_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def body(self) -> dict:
        try:
            data = json.loads(self.body_json)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
