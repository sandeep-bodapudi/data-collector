"""Extract any custom fields (e.g. "main deity, timings") with the AI provider and key each user saves in Settings.

Nothing here uses a company key: every call uses the key that belongs to the person running the job.
"""
import json

import requests

MAX_PAGE_CHARS = 60_000  # cost control: only the first ~60k characters of each page are sent

# provider id -> label, default model, base URL (OpenAI-compatible providers only)
PROVIDERS = {
    "anthropic":  {"label": "Anthropic (Claude)",            "model": "claude-opus-5-5",           "base_url": None},
    "openai":     {"label": "OpenAI (ChatGPT)",              "model": "gpt-4.1-mini",              "base_url": None},
    "gemini":     {"label": "Google Gemini",                 "model": "gemini-2.5-flash",          "base_url": None},
    "openrouter": {"label": "OpenRouter (many models)",      "model": "openrouter/auto",           "base_url": "https://openrouter.ai/api/v1"},
    "groq":       {"label": "Groq",                          "model": "llama-3.1-8b-instant",      "base_url": "https://api.groq.com/openai/v1"},
    "custom":     {"label": "Other (OpenAI-compatible URL)", "model": "",                          "base_url": None},
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


def _anthropic(prompt, fields, key, model):
    import anthropic
    schema = {"type": "object", "properties": {f: {"type": "string"} for f in fields},
              "required": fields, "additionalProperties": False}
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.beta.messages.create(
            model=model, max_tokens=4000,
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


def call(provider: str, key: str, model: str, base_url: str, prompt: str, fields: list[str]) -> dict:
    if not key and provider != "custom":
        raise AIError("no API key saved - open Settings")
    info = PROVIDERS.get(provider) or PROVIDERS["anthropic"]
    model = (model or info["model"]).strip()
    if not model:
        raise AIError("enter a model name in Settings")
    if provider == "anthropic":
        text = _anthropic(prompt, fields, key, model)
    elif provider == "gemini":
        text = _gemini(prompt, key, model)
    else:
        if provider == "custom" and not base_url:
            raise AIError("enter the provider URL in Settings")
        text = _openai_compatible(prompt, key or "not-needed", model, base_url or info["base_url"])
    return _parse_json(text)


def extract_fields(fields: list[str], page_text: str, url: str, topic: str, ai: dict) -> dict:
    """Return {field: value}. `ai` holds the user's provider, key, model and base_url."""
    try:
        data = call(ai.get("provider", "anthropic"), ai.get("key", ""), ai.get("model", ""),
                    ai.get("base_url", ""), _prompt(fields, page_text, url, topic), fields)
    except AIError as e:
        return {f: f"AI error: {e}" for f in fields}
    except Exception as e:  # unexpected provider response
        return {f: f"AI error: {type(e).__name__}" for f in fields}
    return {f: str(data.get(f, "") or "") for f in fields}


def test_connection(ai: dict) -> str:
    """Small request to check the saved key works. Returns a message for the Settings screen."""
    data = call(ai.get("provider", "anthropic"), ai.get("key", ""), ai.get("model", ""), ai.get("base_url", ""),
                'Reply with this JSON object exactly: {"status": "ok"}', ["status"])
    if str(data.get("status", "")).lower() != "ok":
        raise AIError("unexpected reply from the AI")
    return "Connected"
