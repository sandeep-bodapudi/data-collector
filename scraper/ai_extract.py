"""Extract any custom fields (e.g. "main deity, timings") with the AI provider and key each user saves in Settings.

Nothing here uses a company key: every call uses the key that belongs to the person running the job.
"""
import json
import re

import requests

MAX_PAGE_CHARS = 60_000  # cost control: only the first ~60k characters of each page are sent

# provider id -> label, default model, base URL (OpenAI-compatible providers only), and tier:
#   "free" - the provider gives a free API key with a standing free allowance (daily/per-minute limits apply, and some
#            free plans may use prompts to improve their models - Settings says so next to the choice)
#   "paid" - billed to the person's own account
# Free allowances change often; Settings links to each provider's own page rather than quoting numbers that go stale.
PROVIDERS = {
    "gemini":          {"label": "Google Gemini",                   "model": "gemini-2.5-flash",     "base_url": None,                            "tier": "free"},
    "groq":            {"label": "Groq",                            "model": "llama-3.1-8b-instant", "base_url": "https://api.groq.com/openai/v1", "tier": "free"},
    "openrouter_free": {"label": "OpenRouter (free models only)",   "model": "openrouter/free",      "base_url": "https://openrouter.ai/api/v1",   "tier": "free"},
    "mistral":         {"label": "Mistral (free Experiment plan)",  "model": "mistral-small-latest", "base_url": "https://api.mistral.ai/v1",     "tier": "free"},
    "anthropic":       {"label": "Anthropic (Claude)",              "model": "claude-opus-5-5",      "base_url": None,                            "tier": "paid"},
    "openai":          {"label": "OpenAI (ChatGPT)",                "model": "gpt-4.1-mini",         "base_url": None,                            "tier": "paid"},
    "openrouter":      {"label": "OpenRouter (all models)",         "model": "openrouter/auto",      "base_url": "https://openrouter.ai/api/v1",   "tier": "paid"},
    "custom":          {"label": "Other (OpenAI-compatible URL)",   "model": "",                     "base_url": None,                            "tier": "paid"},
}


class AIError(Exception):
    pass


def _prompt(fields, page_text, url, topic):
    return (
        f"The user is collecting data about: {topic}\n"
        f"From the web page below ({url}), extract these fields: {', '.join(fields)}.\n"
        "Use only information stated on the page. If a field is not present, use an empty string. "
        "If a field has several values, join them with '; '. Keep each value concise.\n"
        "Reply with only a JSON object whose keys are exactly the requested field names.\n\n"
        f"<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>"
    )


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AIError("AI did not return JSON")
    return json.loads(text[start:end + 1])


def _anthropic(prompt, fields, key, model, schema=None):
    import anthropic
    schema = schema or {"type": "object", "properties": {f: {"type": "string"} for f in fields},
                        "required": fields, "additionalProperties": False}
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.beta.messages.create(
            model=model, max_tokens=16000 if schema else 4000,  # a whole list page's items need more room than a few fields
            # If a safety check declines a page, Anthropic retries it on a suitable model automatically.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise AIError("invalid API key")
    except anthropic.RateLimitError:
        raise AIError("rate limited - try fewer pages")
    except anthropic.NotFoundError:
        raise AIError(f"model '{model}' not found")
    except anthropic.APIStatusError as e:
        raise AIError(f"HTTP {e.status_code}")
    except anthropic.APIConnectionError:
        raise AIError("connection failed")
    if resp.stop_reason == "refusal":
        return "{}"
    return next((b.text for b in resp.content if b.type == "text"), "{}")


def _openai_compatible(prompt, key, model, base_url):
    import openai
    client = openai.OpenAI(api_key=key, base_url=base_url or None)
    messages = [{"role": "user", "content": prompt}]
    try:
        try:
            resp = client.chat.completions.create(model=model, messages=messages,
                                                  response_format={"type": "json_object"})
        except openai.BadRequestError:
            # Some providers/models don't support JSON mode; the prompt already asks for JSON.
            resp = client.chat.completions.create(model=model, messages=messages)
    except openai.AuthenticationError:
        raise AIError("invalid API key")
    except openai.RateLimitError:
        raise AIError("rate limited or out of credit")
    except openai.NotFoundError:
        raise AIError(f"model '{model}' not found")
    except openai.APIStatusError as e:
        raise AIError(f"HTTP {e.status_code}")
    except openai.APIConnectionError:
        raise AIError("connection failed - check the URL")
    return resp.choices[0].message.content


def _gemini(prompt, key, model):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},  # key in header, never in the URL
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}},
        timeout=120,
    )
    if r.status_code in (400, 401, 403) and "API key" in r.text:
        raise AIError("invalid API key")
    if r.status_code == 404:
        raise AIError(f"model '{model}' not found")
    if r.status_code == 429:
        raise AIError("rate limited or out of quota")
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code}")
    try:
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return "{}"


def call(provider: str, key: str, model: str, base_url: str, prompt: str, fields: list[str], schema=None) -> dict:
    if not key and provider != "custom":
        raise AIError("no API key saved - open Settings")
    info = PROVIDERS.get(provider) or PROVIDERS["anthropic"]
    model = (model or info["model"]).strip()
    if not model:
        raise AIError("enter a model name in Settings")
    if provider == "anthropic":
        text = _anthropic(prompt, fields, key, model, schema)
    elif provider == "gemini":
        text = _gemini(prompt, key, model)
    else:
        if provider == "custom" and not base_url:
            raise AIError("enter the provider URL in Settings")
        text = _openai_compatible(prompt, key or "not-needed", model, base_url or info["base_url"])
    return _parse_json(text)


def ai_available(ai: dict) -> bool:
    """True if AI extraction can run: global kill-switch on + a key (or custom URL)."""
    try:
        from .config import is_enabled

        if not is_enabled("ai"):
            return False
    except Exception:
        pass
    if not isinstance(ai, dict):
        return False
    provider = ai.get("provider", "anthropic")
    if provider == "custom":
        return bool((ai.get("base_url", "") or "").strip() and (ai.get("model", "") or "").strip())
    return bool((ai.get("key", "") or "").strip())


def extract_fields(fields: list[str], page_text: str, url: str, topic: str, ai: dict) -> dict:
    """Return {field: value}. `ai` holds the user's provider, key, model and base_url."""
    if not ai_available(ai):
        return {f: "" for f in fields}
    try:
        data = call(ai.get("provider", "anthropic"), ai.get("key", ""), ai.get("model", ""),
                    ai.get("base_url", ""), _prompt(fields, page_text, url, topic), fields)
    except AIError as e:
        return {f: f"AI error: {e}" for f in fields}
    except Exception as e:  # unexpected provider response
        return {f: f"AI error: {type(e).__name__}" for f in fields}
    return {f: str(data.get(f, "") or "") for f in fields}


# ---------------------------------------------------------------- reading a whole list page ("Top 10 ... in X")
# scraper/listings.py explains why a generic rule-based "find the list on this page" was rejected: on real directory
# pages it mixed real names with site navigation, category links and unrelated people. A language model reads the page
# the way a person would, so it can tell "Digital Medha, Ameerpet - 81061 87989" from a menu link. But a model can also
# invent things. So nothing it returns is trusted on its own: every name, phone and email is kept only if it literally
# appears in the page text (see _verify_items). The model chooses which strings are organisations; the page proves
# the strings are real.

LIST_ITEM_FIELDS = ["name", "address", "phone", "email", "website"]
MAX_LIST_ITEMS = 100
_LIST_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {f: {"type": "string"} for f in LIST_ITEM_FIELDS},
        "required": LIST_ITEM_FIELDS, "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def _list_prompt(page_text: str, url: str, topic: str) -> str:
    return (
        f"The user is building a list of: {topic}\n"
        f"The web page below ({url}) may list several such organisations (a directory, a 'top 10' article, a "
        "category page). List every distinct organisation on it that is itself one of these.\n"
        "Do NOT include: the website or company that published this page, navigation or menu items, course or "
        "category names, people, adverts for unrelated businesses, or organisations only mentioned in passing.\n"
        "For each one give its name, and its address, phone, email and website ONLY if the page states them for "
        "that organisation - copy them exactly as written, otherwise use an empty string. Never guess.\n"
        'Reply with only a JSON object: {"items": [{"name": "", "address": "", "phone": "", "email": "", '
        '"website": ""}]}. Use {"items": []} if the page lists none.\n\n'
        f"<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>"
    )


def _norm_words(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _joined_digits(text: str) -> str:
    """The page with separators inside numbers removed, so "+91 81061-87989" and "8106187989" compare equal."""
    return re.sub(r"(?<=\d)[\s\-.()/]+(?=\d)", "", text or "")


def _verify_items(items, page_text: str) -> list[dict]:
    """Keep only what the page itself contains. A name not on the page is a hallucination - the whole item goes. A
    phone, email or website not on the page is blanked, but the (real) name stays."""
    words = f" {_norm_words(page_text)} "
    digits = _joined_digits(page_text)
    low = (page_text or "").lower()
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = re.sub(r"\s+", " ", str(it.get("name", "") or "")).strip()
        key = _norm_words(name)
        if len(key) < 3 or f" {key} " not in words or key in seen:
            continue
        seen.add(key)
        row = {"name": name[:150], "address": "", "phone": "", "email": "", "website": ""}
        addr = re.sub(r"\s+", " ", str(it.get("address", "") or "")).strip()
        if addr and _norm_words(addr) in _norm_words(page_text):
            row["address"] = addr[:300]
        phones = []
        for p in re.split(r"[;,/]| or ", str(it.get("phone", "") or "")):
            d = re.sub(r"\D", "", p)
            if len(d) >= 8 and d[-10:] in digits:
                phones.append(p.strip())
        row["phone"] = "; ".join(phones)
        emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", str(it.get("email", "") or ""))
                  if e.lower() in low]
        row["email"] = "; ".join(emails)
        site = str(it.get("website", "") or "").strip().rstrip("/")
        host = re.sub(r"^https?://", "", site, flags=re.I).removeprefix("www.").split("/")[0].lower()
        if host and "." in host and host in low:
            row["website"] = site if site.lower().startswith("http") else "https://" + site
        out.append(row)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def extract_list(page_text: str, url: str, topic: str, ai: dict) -> list[dict]:
    """Every organisation of the given kind named on a list page, with only the details the page itself shows.
    Returns [] when AI isn't set up or the call fails - a list page is a bonus source, never a reason to fail a run.
    Raises AIError only for problems the person needs to fix (bad key, no credit), so the run can say so once."""
    if not ai_available(ai):
        return []
    try:
        data = call(ai.get("provider", "anthropic"), ai.get("key", ""), ai.get("model", ""), ai.get("base_url", ""),
                    _list_prompt(page_text, url, topic), ["items"], schema=_LIST_SCHEMA)
    except AIError as e:
        if any(w in str(e) for w in ("invalid API key", "out of credit", "out of quota", "not found", "Settings")):
            raise
        return []
    except Exception:
        return []
    return _verify_items(data.get("items", []), page_text)


def test_connection(ai: dict) -> str:
    """Small request to check the saved key works. Returns a message for the Settings screen."""
    data = call(ai.get("provider", "anthropic"), ai.get("key", ""), ai.get("model", ""), ai.get("base_url", ""),
                'Reply with this JSON object exactly: {"status": "ok"}', ["status"])
    if str(data.get("status", "")).lower() != "ok":
        raise AIError("unexpected reply from the AI")
    return "Connected"
