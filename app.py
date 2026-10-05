"""OneBridge Data Collector - web app. Run: python app.py  then open http://localhost:5000"""
import csv
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _load_env_file():
    path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _secret_key() -> str:
    """SECRET_KEY signs logins and encrypts the vault. Use the env var, else a random key kept on disk."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        os.environ["SECRET_KEY"] = f.read().strip()
    return os.environ["SECRET_KEY"]


_load_env_file()
_secret_key()

from models import ROLES, AppSetting, Run, Sheet, User, VaultCredential, db, encrypt  # noqa: E402
from scraper import ai_extract, extract, places, sheets  # noqa: E402
from scraper.jobs import JOBS, REGIONS, start_job  # noqa: E402

def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        return f"sqlite:///{os.path.join(INSTANCE_DIR, 'data.db')}"
    # Render/Heroku give "postgres://..." - SQLAlchemy needs the driver named explicitly.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SQLALCHEMY_DATABASE_URI=_database_url(),
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,  # imports up to 25 MB
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), REMEMBER_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify(error="Your session has ended. Please sign in again."), 401
    return redirect(url_for("login", next=request.path))


def _upgrade_schema():
    """Add columns introduced after a table was first created (create_all never alters tables)."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                ddl = col.type.compile(db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))


with app.app_context():
    db.create_all()
    _upgrade_schema()
    # On a server, the first admin comes from APP_USER / APP_PASSWORD so nobody else can claim it.
    if not User.query.first() and os.environ.get("APP_PASSWORD"):
        admin = User(username=os.environ.get("APP_USER", "admin"), role="admin")
        admin.set_password(os.environ["APP_PASSWORD"])
        db.session.add(admin)
        db.session.commit()


# ---------------------------------------------------------------- helpers

def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def inner(*a, **kw):
            if current_user.role not in roles:
                return jsonify(error="You don't have permission to do this."), 403
            return fn(*a, **kw)
        return inner
    return deco


def _vault(user_id: int) -> dict:
    return {c.name: c.value() for c in VaultCredential.query.filter_by(owner_id=user_id).all()}


def _restricted_enabled() -> bool:
    return AppSetting.get("allow_restricted", "0") == "1"


def _mask(v: str) -> str:
    return ("•" * 8 + v[-4:]) if v and len(v) > 8 else ("•" * 8 if v else "")


def _own_sheet(sheet_id: str) -> Sheet:
    s = db.session.get(Sheet, sheet_id)
    if not s or (s.owner_id != current_user.id and not current_user.is_admin):
        abort(404)
    return s


def _own_job(job_id: str):
    job = JOBS.get(job_id)
    if not job or (job.owner_id != current_user.id and not current_user.is_admin):
        abort(404)
    return job


def _clean_name(name: str, default: str) -> str:
    name = re.sub(r"[^\w\- .()]+", "", (name or "").strip())[:80].strip()
    return name or default


def _list(text: str) -> list[str]:
    text = (text or "").strip()
    sep = "\n" if "\n" in text else ","
    return list(dict.fromkeys(p.strip() for p in text.split(sep) if p.strip()))


# ---------------------------------------------------------------- login

MAX_FAILED, LOCK_SECONDS = 5, 600
_failed: dict[str, list[float]] = {}


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()


def _locked(ip: str) -> bool:
    recent = [t for t in _failed.get(ip, []) if time.time() - t < LOCK_SECONDS]
    _failed[ip] = recent
    return len(recent) >= MAX_FAILED


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    is_setup = User.query.count() == 0  # very first start on a local PC: create the admin account
    error, username = None, ""
    if request.method == "POST":
        ip = _client_ip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if _locked(ip):
            error = "Too many wrong attempts. Please wait 10 minutes and try again."
        elif is_setup:
            if len(username) < 3 or len(password) < 8:
                error = "Choose a username (3+ characters) and a password of at least 8 characters."
            else:
                user = User(username=username, role="admin", last_login=datetime.utcnow())
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                login_user(user, remember=True)
                return redirect(url_for("index"))
        else:
            user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
            if user and user.check_password(password) and user.is_active:
                _failed.pop(ip, None)
                user.last_login = datetime.utcnow()
                db.session.commit()
                login_user(user, remember=bool(request.form.get("remember")))
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            _failed.setdefault(ip, []).append(time.time())
            error = "This account is deactivated. Ask your admin." if user and not user.is_active and user.check_password(password) \
                else "Wrong username or password."
    return render_template("login.html", error=error, username=username, is_setup=is_setup)


@app.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.post("/api/me/password")
@login_required
def change_password():
    d = request.get_json(force=True)
    if not current_user.check_password(d.get("current") or ""):
        return jsonify(error="Your current password is not correct."), 400
    if len(d.get("new") or "") < 8:
        return jsonify(error="The new password must have at least 8 characters."), 400
    current_user.set_password(d["new"])
    db.session.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- app shell

@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        user={"username": current_user.username, "role": current_user.role},
        fields=extract.STANDARD_FIELDS,
        categories=list(places.CATEGORIES),
        regions=REGIONS,
        providers={k: {"label": v["label"], "model": v["model"]} for k, v in ai_extract.PROVIDERS.items()},
        restricted=_restricted_enabled(),
    )


@app.get("/api/dashboard")
@login_required
def dashboard():
    runs = Run.query.filter_by(owner_id=current_user.id)
    sheets_q = Sheet.query.filter_by(owner_id=current_user.id)
    week = datetime.utcnow() - timedelta(days=7)
    return jsonify(
        runs_total=runs.count(),
        runs_week=runs.filter(Run.started_at >= week).count(),
        running=sum(1 for j in JOBS.values() if j.owner_id == current_user.id and j.status in ("queued", "running")),
        sheets_total=sheets_q.count(),
        rows_total=int(db.session.query(db.func.coalesce(db.func.sum(Sheet.row_count), 0))
                       .filter(Sheet.owner_id == current_user.id).scalar() or 0),
        ai_ready=bool(_vault(current_user.id).get("ai_api_key")),
    )


# ---------------------------------------------------------------- runs

@app.post("/api/jobs")
@roles_required("admin", "member")
def create_job():
    d = request.get_json(force=True)
    mode = d.get("mode", "web")
    custom = _list(d.get("custom_fields", ""))[:15]
    vault = _vault(current_user.id)
    ai = {"provider": vault.get("ai_provider", "anthropic"), "key": vault.get("ai_api_key", ""),
          "model": vault.get("ai_model", ""), "base_url": vault.get("ai_base_url", "")}
    if custom and not ai["key"] and ai["provider"] != "custom":
        return jsonify(error="AI details need your own AI key. Add it in Settings → AI provider."), 400
    try:
        max_results = max(1, min(int(d.get("max_results") or 30), 500 if mode == "places" else 200))
    except ValueError:
        return jsonify(error="Max results must be a number."), 400

    restricted = _restricted_enabled()
    common = {
        "custom_fields": custom, "max_results": max_results,
        "file_name": _clean_name(d.get("file_name"), ""),
        "ai": ai, "allow_restricted": restricted,
        "li_at_cookie": vault.get("li_at_cookie", "") if restricted else "",
        "fb_cookie": vault.get("fb_cookie", "") if restricted else "",
    }
    if mode == "places":
        locations = list(dict.fromkeys(l.strip() for l in (d.get("locations") or "").splitlines() if l.strip()))
        if not locations:
            return jsonify(error="Add at least one location."), 400
        if d.get("category") not in places.CATEGORIES:
            return jsonify(error="Choose a category."), 400
        spec = {"mode": "places", "category": d["category"], "locations": locations[:30],
                "name_filter": (d.get("name_filter") or "").strip(),
                "enrich": bool(d.get("enrich")) or bool(custom), **common}
    else:
        queries = list(dict.fromkeys(q.strip() for q in (d.get("queries") or "").splitlines() if q.strip()))[:20]
        if not queries:
            return jsonify(error="Add at least one search."), 400
        fields = [f for f in d.get("fields", []) if f in extract.STANDARD_FIELDS]
        if not fields and not custom:
            return jsonify(error="Pick at least one detail to collect."), 400
        platforms = [p for p in (d.get("platforms") or ["web"])
                     if p in ("web", "linkedin.com", "facebook.com", "instagram.com", "twitter.com")] or ["web"]
        spec = {"mode": "web", "queries": queries,
                "region": d.get("region") if d.get("region") in REGIONS else "wt-wt",
                "fields": fields, "platforms": platforms,
                "follow_contact": bool(d.get("follow_contact", True)), "one_per_site": bool(d.get("one_per_site")),
                "require": d.get("require") if d.get("require") in ("emails", "phones", "any_contact") else "",
                **common}
    job = start_job(spec, app, current_user.id)
    return jsonify(id=job.id)


@app.get("/api/jobs/<job_id>")
@login_required
def job_status(job_id):
    return jsonify(_own_job(job_id).to_dict())


@app.post("/api/jobs/<job_id>/stop")
@login_required
def stop_job(job_id):
    _own_job(job_id).cancelled.set()
    return jsonify(ok=True)


@app.get("/api/runs")
@login_required
def list_runs():
    q = Run.query
    if not (current_user.is_admin and request.args.get("all") == "1"):
        q = q.filter_by(owner_id=current_user.id)
    runs = q.order_by(Run.started_at.desc()).limit(200).all()
    names = {u.id: u.username for u in User.query.all()}
    sheet_by_run = {s.run_id: s.id for s in Sheet.query.filter(Sheet.run_id.in_([r.id for r in runs])).all()} if runs else {}
    out = []
    for r in runs:
        d = r.to_dict(names.get(r.owner_id, ""))
        live = JOBS.get(r.id)
        if live and live.status in ("queued", "running"):
            d["status"], d["rows"] = live.status, len(live.rows)
        elif r.status in ("queued", "running") and not live:
            d["status"] = "error"  # the server restarted while this run was going
            d["error"] = d["error"] or "Interrupted by a server restart"
        d["sheet_id"] = sheet_by_run.get(r.id)
        out.append(d)
    return jsonify(out)


@app.get("/api/runs/<run_id>")
@login_required
def get_run(run_id):
    r = db.session.get(Run, run_id)
    if not r or (r.owner_id != current_user.id and not current_user.is_admin):
        abort(404)
    d = r.to_dict()
    sheet = Sheet.query.filter_by(run_id=run_id).first()
    d["sheet_id"] = sheet.id if sheet else None
    live = JOBS.get(run_id)
    d["live"] = live.to_dict() if live else None
    if not live and r.status in ("queued", "running"):
        d["status"], d["error"] = "error", d["error"] or "Interrupted by a server restart"
    return jsonify(d)


@app.delete("/api/runs/<run_id>")
@login_required
def delete_run(run_id):
    r = db.session.get(Run, run_id)
    if not r or (r.owner_id != current_user.id and not current_user.is_admin):
        abort(404)
    if (job := JOBS.get(run_id)) and job.status in ("queued", "running"):
        return jsonify(error="Stop the run before deleting it."), 400
    Sheet.query.filter_by(run_id=run_id).update({"run_id": None})  # keep its sheet
    db.session.delete(r)
    db.session.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- sheets

@app.get("/api/sheets")
@login_required
def list_sheets():
    rows = (db.session.query(Sheet, db.func.length(Sheet.file_data))
            .filter(Sheet.owner_id == current_user.id, Sheet.file_data.isnot(None))
            .order_by(Sheet.created_at.desc()).limit(500).all())
    return jsonify([s.to_dict(size) for s, size in rows])


@app.get("/api/sheets/<sheet_id>/rows")
@login_required
def sheet_rows(sheet_id):
    s = _own_sheet(sheet_id)
    columns, rows = sheets.read(s)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if any(q in str(r.get(c, "")).lower() for c in columns)]
    offset = max(0, int(request.args.get("offset", 0)))
    limit = max(1, min(500, int(request.args.get("limit", 100))))
    return jsonify(name=s.name, columns=columns, total=len(rows), rows=rows[offset:offset + limit])


@app.get("/api/sheets/<sheet_id>/export")
@login_required
def export_sheet(sheet_id):
    s = _own_sheet(sheet_id)
    fmt = request.args.get("format", "xlsx")
    base = os.path.splitext(s.name)[0]
    if fmt == "xlsx":
        return Response(s.file_data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{base}.xlsx"'})
    columns, rows = sheets.read(s)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",  # BOM so Excel reads Indian scripts correctly
                        headers={"Content-Disposition": f'attachment; filename="{base}.csv"'})
    if fmt == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=2, default=str), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{base}.json"'})
    abort(400)


@app.patch("/api/sheets/<sheet_id>")
@roles_required("admin", "member")
def rename_sheet(sheet_id):
    s = _own_sheet(sheet_id)
    name = _clean_name(request.get_json(force=True).get("name"), "")
    if not name:
        return jsonify(error="Enter a name."), 400
    s.name = name if name.lower().endswith(".xlsx") else name + ".xlsx"
    db.session.commit()
    return jsonify(ok=True, name=s.name)


@app.delete("/api/sheets/<sheet_id>")
@roles_required("admin", "member")
def delete_sheet(sheet_id):
    db.session.delete(_own_sheet(sheet_id))
    db.session.commit()
    sheets.forget(sheet_id)
    return jsonify(ok=True)


@app.post("/api/sheets/import")
@roles_required("admin", "member")
def import_sheet():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose a file to import."), 400
    try:
        columns, rows = sheets.parse_upload(f.filename, f.read())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    s = sheets.save(f"{_clean_name(os.path.splitext(f.filename)[0], 'Imported')}.xlsx", columns, rows,
                    current_user.id, source="import", info={"Imported from": f.filename})
    return jsonify(ok=True, id=s.id, rows=len(rows))


@app.post("/api/merge")
@roles_required("admin", "member")
def merge_sheets():
    d = request.get_json(force=True)
    ids = d.get("sheets") or []
    if len(ids) < 1:
        return jsonify(error="Choose at least one sheet."), 400
    chosen = [_own_sheet(i) for i in ids]
    try:
        result = sheets.merge(chosen, keys=d.get("keys") or [], match=d.get("match") or {},
                              keep=d.get("keep", "first"), fill_empty=bool(d.get("fill_empty")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if d.get("preview"):
        return jsonify(result.summary())
    name = _clean_name(d.get("output_name"), "Merged") + f"_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    s = sheets.save(name, result.columns, result.rows, current_user.id, source="merge",
                    info={"Merged from": ", ".join(c.name for c in chosen), "Duplicate keys": ", ".join(d.get("keys") or []) or "none",
                          "Rows in": result.rows_in, "Duplicates removed": result.removed})
    if d.get("save_removed") and result.removed_rows:
        sheets.save(name.replace(".xlsx", "_removed_duplicates.xlsx"), result.columns, result.removed_rows,
                    current_user.id, source="merge", info={"Duplicates removed from": name})
    return jsonify(ok=True, id=s.id, name=s.name, **result.summary())


# ---------------------------------------------------------------- settings (each user's own keys)

@app.get("/api/settings")
@login_required
def get_settings():
    v = _vault(current_user.id)
    return jsonify(
        ai_provider=v.get("ai_provider", "anthropic"), ai_model=v.get("ai_model", ""), ai_base_url=v.get("ai_base_url", ""),
        ai_key_mask=_mask(v.get("ai_api_key", "")),
        restricted=_restricted_enabled(),
        li_cookie_mask=_mask(v.get("li_at_cookie", "")), fb_cookie_mask=_mask(v.get("fb_cookie", "")),
    )


@app.post("/api/settings")
@login_required
def save_settings():
    d = request.get_json(force=True)
    allowed = {"ai_provider", "ai_model", "ai_base_url", "ai_api_key"}
    if _restricted_enabled():
        allowed |= {"li_at_cookie", "fb_cookie"}
    for key, value in d.items():
        if key not in allowed:
            continue
        value = (value or "").strip()
        if key == "ai_provider" and value not in ai_extract.PROVIDERS:
            continue
        if key in ("ai_api_key", "li_at_cookie", "fb_cookie") and value == "__keep__":
            continue  # the browser never sees the saved secret; "__keep__" means "leave it unchanged"
        cred = VaultCredential.query.filter_by(owner_id=current_user.id, name=key).first()
        if not value:
            if cred:
                db.session.delete(cred)
            continue
        if not cred:
            cred = VaultCredential(owner_id=current_user.id, name=key,
                                   platform="ai" if key.startswith("ai_") else "social",
                                   credential_type="api_key" if key == "ai_api_key" else "setting")
            db.session.add(cred)
        cred.encrypted_value = encrypt(value)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/settings/test")
@login_required
def test_ai():
    v = _vault(current_user.id)
    try:
        msg = ai_extract.test_connection({"provider": v.get("ai_provider", "anthropic"), "key": v.get("ai_api_key", ""),
                                          "model": v.get("ai_model", ""), "base_url": v.get("ai_base_url", "")})
    except ai_extract.AIError as e:
        msg = str(e)
        return jsonify(ok=False, error=msg[:1].upper() + msg[1:])
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}")
    return jsonify(ok=True, message=msg)


# ---------------------------------------------------------------- admin

@app.get("/api/admin/users")
@roles_required("admin")
def admin_users():
    return jsonify([u.to_dict() for u in User.query.order_by(User.created_at).all()])


@app.post("/api/admin/users")
@roles_required("admin")
def admin_add_user():
    d = request.get_json(force=True)
    username = (d.get("username") or "").strip()
    if not re.fullmatch(r"[\w.\-@]{3,100}", username):
        return jsonify(error="Username: 3+ letters, numbers, dots or dashes."), 400
    if User.query.filter(db.func.lower(User.username) == username.lower()).first():
        return jsonify(error="That username is taken."), 400
    if len(d.get("password") or "") < 8:
        return jsonify(error="Password must have at least 8 characters."), 400
    u = User(username=username, email=(d.get("email") or "").strip() or None,
             role=d.get("role") if d.get("role") in ROLES else "member")
    u.set_password(d["password"])
    db.session.add(u)
    db.session.commit()
    return jsonify(u.to_dict())


@app.patch("/api/admin/users/<int:user_id>")
@roles_required("admin")
def admin_edit_user(user_id):
    u = db.session.get(User, user_id) or abort(404)
    d = request.get_json(force=True)
    admins = User.query.filter_by(role="admin", is_active_user=True).count()
    removing_admin = u.role == "admin" and ((d.get("role") and d["role"] != "admin") or d.get("active") is False)
    if removing_admin and admins <= 1:
        return jsonify(error="Keep at least one active admin."), 400
    if d.get("role") in ROLES:
        u.role = d["role"]
    if "active" in d:
        u.is_active_user = bool(d["active"])
    if d.get("password"):
        if len(d["password"]) < 8:
            return jsonify(error="Password must have at least 8 characters."), 400
        u.set_password(d["password"])
    db.session.commit()
    return jsonify(u.to_dict())


@app.get("/api/admin/settings")
@roles_required("admin")
def admin_get_settings():
    return jsonify(allow_restricted=_restricted_enabled())


@app.post("/api/admin/settings")
@roles_required("admin")
def admin_save_settings():
    d = request.get_json(force=True)
    if "allow_restricted" in d:
        AppSetting.set("allow_restricted", "1" if d["allow_restricted"] else "0")
    db.session.commit()
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="That file is too large (max 25 MB)."), 413


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  OneBridge Data Collector running at http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
