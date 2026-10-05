"""Web UI for the data collector. Run: python app.py  then open http://localhost:5000"""
import hmac
import os
from datetime import datetime

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory


def _load_env_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

from scraper import ai_extract, extract, places  # noqa: E402  (after .env is loaded)
from scraper.jobs import JOBS, OUTPUT_DIR, REGIONS, start_job  # noqa: E402

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # page edits show up without restarting

# When APP_PASSWORD is set (always on the server), the browser asks for a login before showing anything.
APP_USER = os.environ.get("APP_USER", "team")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", APP_USER) and hmac.compare_digest(auth.password or "", APP_PASSWORD):
        return None
    return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="Data Collector"'})


def _list(text: str) -> list[str]:
    """Split user input on new lines (or commas when it is a single line)."""
    text = (text or "").strip()
    sep = "\n" if "\n" in text else ","
    return list(dict.fromkeys(p.strip() for p in text.split(sep) if p.strip()))


@app.get("/")
def index():
    return render_template(
        "index.html",
        fields=extract.STANDARD_FIELDS,
        categories=list(places.CATEGORIES),
        regions=REGIONS,
        ai_enabled=ai_extract.available(),
    )


@app.post("/api/jobs")
def create_job():
    d = request.get_json(force=True)
    mode = d.get("mode", "web")
    custom = _list(d.get("custom_fields", ""))[:15]
    if custom and not ai_extract.available():
        return jsonify(error="Custom fields need an Anthropic API key in the .env file. Ask IT to add it."), 400
    try:
        max_results = max(1, min(int(d.get("max_results") or 30), 500 if mode == "places" else 200))
    except ValueError:
        return jsonify(error="Max results must be a number."), 400

    if mode == "places":
        locations = list(dict.fromkeys(l.strip() for l in (d.get("locations") or "").splitlines() if l.strip()))
        if not locations:
            return jsonify(error="Enter at least one location."), 400
        if d.get("category") not in places.CATEGORIES:
            return jsonify(error="Choose a category."), 400
        spec = {
            "mode": "places", "category": d["category"], "locations": locations[:30],
            "name_filter": (d.get("name_filter") or "").strip(), "max_results": max_results,
            "enrich": bool(d.get("enrich")) or bool(custom), "custom_fields": custom,
            "file_name": (d.get("file_name") or "").strip(),
        }
    else:
        # One search per line (commas are kept: "temples in Hyderabad, Telangana" is one search).
        queries = list(dict.fromkeys(q.strip() for q in (d.get("queries") or "").splitlines() if q.strip()))[:20]
        if not queries:
            return jsonify(error="Enter at least one search."), 400
        fields = [f for f in d.get("fields", []) if f in extract.STANDARD_FIELDS]
        if not fields and not custom:
            return jsonify(error="Pick at least one field to collect."), 400
        spec = {
            "mode": "web", "queries": queries, "max_results": max_results,
            "region": d.get("region") if d.get("region") in REGIONS else "wt-wt",
            "fields": fields, "custom_fields": custom,
            "follow_contact": bool(d.get("follow_contact")), "one_per_site": bool(d.get("one_per_site")),
            "require": d.get("require") if d.get("require") in ("emails", "phones", "any_contact") else "",
            "file_name": (d.get("file_name") or "").strip(),
        }
    job = start_job(spec)
    return jsonify(id=job.id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify(job.to_dict())


@app.post("/api/jobs/<job_id>/stop")
def stop_job(job_id):
    job = JOBS.get(job_id) or abort(404)
    job.cancelled.set()
    return jsonify(ok=True)


@app.get("/api/files")
def list_files():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    files = []
    for name in os.listdir(OUTPUT_DIR):
        if name.endswith(".xlsx"):
            p = os.path.join(OUTPUT_DIR, name)
            files.append({"name": name, "size_kb": round(os.path.getsize(p) / 1024, 1),
                          "created": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    files.sort(key=lambda f: f["created"], reverse=True)
    return jsonify(files[:100])


@app.get("/download/<path:name>")
def download(name):
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  Data Collector running at http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
