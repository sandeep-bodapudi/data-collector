"""OneBridge Data Collector - web app. Run: python app.py  then open http://localhost:5000"""
import hashlib
import io
import os
import re
import secrets
import tempfile
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

from models import ROLES, AppSetting, User, VaultCredential, db, encrypt  # noqa: E402
from scraper import ai_extract, extract, places, sheets  # noqa: E402
from scraper.excel import write_workbook  # noqa: E402
from scraper.jobs import JOBS, REGIONS, purge_jobs, start_job  # noqa: E402

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


def _resolve_database() -> tuple[str, dict]:
    """Pick the database URL. If the configured database cannot be reached (for example a free Render
    database that has expired), fall back to a local SQLite file so the site stays up for admin sign-in."""
    from sqlalchemy import create_engine, text
    url = _database_url()
    state = {"kind": url.split(":", 1)[0].split("+")[0], "fallback": False, "error": ""}
    if state["kind"] != "sqlite":
        try:
            eng = create_engine(url, connect_args={"connect_timeout": 10})
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            eng.dispose()
        except Exception as e:  # any connection problem
            print(f"[database] cannot reach {state['kind']} database: {type(e).__name__}: {str(e)[:200]}", flush=True)
            os.makedirs(INSTANCE_DIR, exist_ok=True)
            url = f"sqlite:///{os.path.join(INSTANCE_DIR, 'data.db')}"
            state.update(kind="sqlite", fallback=True, error=type(e).__name__)
    return url, state


DB_URL, DB_STATE = _resolve_database()
ON_SERVER = bool(os.environ.get("RENDER"))


app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SQLALCHEMY_DATABASE_URI=DB_URL,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,  # uploads and Excel exports up to 25 MB
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


# ---------------------------------------------------------------- app shell + PWA

STATIC_DIR = os.path.join(BASE_DIR, "static")
ASSET_FILES = ("app.css", "app.js", "sheetops.js", "store.js")


def asset_version() -> str:
    """Changes whenever a front-end file changes, so browsers and the service worker never serve stale code."""
    h = hashlib.sha1()
    for folder, _dirs, files in sorted(os.walk(STATIC_DIR)):
        for name in sorted(files):
            try:
                path = os.path.join(folder, name)
                st = os.stat(path)
                h.update(f"{os.path.relpath(path, STATIC_DIR)}:{st.st_size}".encode())
                h.update(open(path, "rb").read() if st.st_size < 400_000 else str(st.st_mtime_ns).encode())
            except OSError:
                pass
    return h.hexdigest()[:10]


@app.context_processor
def inject_assets():
    return {"v": asset_version()}


@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        user={"id": current_user.id, "username": current_user.username, "role": current_user.role},
        fields=extract.STANDARD_FIELDS,
        categories=list(places.CATEGORIES),
        regions=REGIONS,
        providers={k: {"label": v["label"], "model": v["model"]} for k, v in ai_extract.PROVIDERS.items()},
        restricted=_restricted_enabled(),
    )


@app.get("/manifest.webmanifest")
def manifest():
    shortcut_icon = [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]
    data = {
        "id": "/", "name": "OneBridge Data Collector", "short_name": "Data Collector",
        "description": "Collect public data from the web into spreadsheets, then merge, dedupe and export it.",
        "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
        "background_color": "#ffffff", "theme_color": "#ffffff", "categories": ["business", "productivity"],
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/logo.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
        "shortcuts": [
            {"name": "Search the web", "url": "/#/new/web", "icons": shortcut_icon},
            {"name": "Find places", "url": "/#/new/places", "icons": shortcut_icon},
            {"name": "My sheets", "url": "/#/sheets", "icons": shortcut_icon},
        ],
    }
    resp = jsonify(data)
    resp.mimetype = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/sw.js")
def service_worker():
    """Served from the site root so it can control every page. Never cached by the browser itself."""
    v = asset_version()
    assets = [f"/static/{n}?v={v}" for n in ASSET_FILES] + [
        "/static/logo.svg", "/static/icons/icon-192.png", "/static/icons/icon-512.png",
        "/static/icons/favicon-32.png", "/manifest.webmanifest", "/offline.html"]
    resp = Response(render_template("sw.js", build=v, assets=assets), mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache, max-age=0"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/offline.html")
def offline_page():
    return render_template("offline.html")


# ---------------------------------------------------------------- runs (live only; results go to the browser)

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
    job = start_job(spec, current_user.id)
    return jsonify(id=job.id)


@app.get("/api/jobs/<job_id>")
@login_required
def job_status(job_id):
    purge_jobs()
    return jsonify(_own_job(job_id).to_dict())


@app.post("/api/jobs/<job_id>/stop")
@login_required
def stop_job(job_id):
    _own_job(job_id).cancelled.set()
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/result")
@login_required
def job_result(job_id):
    """The finished rows, for the browser to save in its own IndexedDB."""
    job = _own_job(job_id)
    if job.status in ("queued", "running"):
        return jsonify(error="This run is still going."), 409
    return jsonify(job.result())


@app.delete("/api/jobs/<job_id>")
@login_required
def forget_job(job_id):
    """Called once the browser has saved the rows, so the server keeps nothing."""
    job = _own_job(job_id)
    if job.status in ("queued", "running"):
        return jsonify(error="Stop the run first."), 409
    JOBS.pop(job_id, None)
    return jsonify(ok=True)


# ---------------------------------------------------------------- stateless helpers for sheets

@app.post("/api/parse")
@roles_required("admin", "member")
def parse_file():
    """Reads an uploaded Excel/CSV/JSON file and returns its rows. Nothing is stored."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose a file to import."), 400
    try:
        columns, rows = sheets.parse_upload(f.filename, f.read())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(name=_clean_name(os.path.splitext(f.filename)[0], "Imported"), columns=columns, rows=rows)


@app.post("/api/export/xlsx")
@login_required
def export_xlsx():
    """Builds a formatted .xlsx from rows sent by the browser. Nothing is stored."""
    d = request.get_json(force=True, silent=True) or {}
    columns, rows = d.get("columns"), d.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
        return jsonify(error="Nothing to export."), 400
    columns = [str(c) for c in columns]
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        write_workbook(path, columns, [r for r in rows if isinstance(r, dict)],
                       {"Exported from": "OneBridge Data Collector", "Sheet": _clean_name(d.get("name"), "Sheet")})
        with open(path, "rb") as f:
            data = f.read()
    finally:
        os.remove(path)
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
    return jsonify(allow_restricted=_restricted_enabled(),
                   database={**DB_STATE, "persistent": DB_STATE["kind"] != "sqlite", "on_server": ON_SERVER})


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
