"""Optional: extract any custom fields the user types (e.g. "deity, timings") using Claude.

Enabled only when ANTHROPIC_API_KEY is set (in the environment or the .env file).
"""
import json
import os

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
MAX_PAGE_CHARS = 60_000  # cost control: only the first ~60k characters of each page are sent

_client = None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def extract_fields(fields: list[str], page_text: str, url: str, topic: str) -> dict:
    """Return {field: value} for each requested field. Missing values come back as ""."""
    schema = {
        "type": "object",
        "properties": {f: {"type": "string"} for f in fields},
        "required": fields,
        "additionalProperties": False,
    }
    prompt = (
        f"The user is collecting data about: {topic}\n"
        f"From the web page below ({url}), extract these fields: {', '.join(fields)}.\n"
        "Use only information stated on the page. If a field is not present, return an empty string. "
        "If a field has several values, join them with '; '. Keep each value concise.\n\n"
        f"<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>"
    )
    try:
        resp = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        return {f: "AI error: invalid API key" for f in fields}
    except anthropic.RateLimitError:
        return {f: "AI error: rate limited" for f in fields}
    except anthropic.APIStatusError as e:
        return {f: f"AI error: HTTP {e.status_code}" for f in fields}
    except anthropic.APIConnectionError:
        return {f: "AI error: connection failed" for f in fields}

    if resp.stop_reason == "refusal":
        return {f: "" for f in fields}
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {f: "" for f in fields}
    return {f: str(data.get(f, "") or "") for f in fields}
