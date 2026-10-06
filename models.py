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

    @property
    def recipients(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.recipients_json or "[]")]
        except (ValueError, TypeError):
            return []

    def is_live(self) -> bool:
        return not self.revoked and self.expires_at > datetime.utcnow()

    def can_open(self, user) -> bool:
        return self.is_live() and (user.id == self.owner_id or self.link_access or user.id in self.recipients)
