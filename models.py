"""Database models. Works with MySQL (XAMPP), PostgreSQL or SQLite - set DATABASE_URL."""
import base64
import hashlib
import json
import os
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


class Run(db.Model):
    __tablename__ = "runs"
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(255), nullable=True)
    connector = db.Column(db.String(50), nullable=False)  # web, places
    status = db.Column(db.String(20), default="queued")   # queued, running, done, error, cancelled
    spec_json = db.Column(db.Text, nullable=False)        # input parameters, secrets removed
    total_items = db.Column(db.Integer, default=0)
    done_items = db.Column(db.Integer, default=0)
    row_count = db.Column(db.Integer, default=0)
    error = db.Column(db.Text, nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self, owner_name=""):
        end = self.completed_at or datetime.utcnow()
        return {"id": self.id, "name": self.name or "Untitled run", "connector": self.connector,
                "status": self.status, "rows": self.row_count or 0, "owner": owner_name,
                "started": iso(self.started_at),
                "duration": int((end - self.started_at).total_seconds()) if self.started_at else 0,
                "error": self.error or "", "spec": json.loads(self.spec_json or "{}")}


class Sheet(db.Model):
    __tablename__ = "sheets"
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(50), default="run")      # run, merge, import
    run_id = db.Column(db.String(36), db.ForeignKey("runs.id"), nullable=True)
    row_count = db.Column(db.Integer, default=0)
    columns_json = db.Column(db.Text, nullable=True)
    file_path = db.Column(db.String(255), nullable=True)
    file_data = db.Column(db.LargeBinary(length=(2 ** 32) - 1), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, size=None):
        return {"id": self.id, "name": self.name, "source": self.source or "run", "rows": self.row_count or 0,
                "columns": json.loads(self.columns_json or "[]"),
                "size_kb": round((size if size is not None else len(self.file_data or b"")) / 1024, 1),
                "created": iso(self.created_at)}


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
