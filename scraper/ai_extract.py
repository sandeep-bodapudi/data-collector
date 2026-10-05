"""Extract custom fields using various AI providers."""
import json
import os

MAX_PAGE_CHARS = 60_000

def available() -> bool:
    return True

def extract_fields(fields: list[str], page_text: str, url: str, topic: str, ai_provider: str="anthropic", ai_api_key: str="", ai_model: str="", ai_base_url: str="") -> dict:
    if not ai_api_key:
        return {f: "Error: No API key provided" for f in fields}

    prompt = (
        f"The user is collecting data about: {topic}\n"
        f"From the web page below ({url}), extract these fields: {', '.join(fields)}.\n"
        "Use only information stated on the page. If a field is not present, return an empty string. "
        "If a field has several values, join them with '; '. Keep each value concise.\n"
        "Return the output as a valid JSON object with the requested fields as keys. Do not include any markdown formatting like ```json.\n\n"
        f"<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>"
    )

    try:
        if ai_provider == "openai":
            import openai
            client = openai.OpenAI(api_key=ai_api_key)
            resp = client.chat.completions.create(
                model=ai_model or "gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content
        elif ai_provider == "openrouter":
            import openai
            client = openai.OpenAI(api_key=ai_api_key, base_url="https://openrouter.ai/api/v1")
            resp = client.chat.completions.create(
                model=ai_model or "openai/gpt-3.5-turbo",
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content
        elif ai_provider in ("groq", "custom"):
            import openai
            base_url = "https://api.groq.com/openai/v1" if ai_provider == "groq" else (ai_base_url or None)
            client = openai.OpenAI(api_key=ai_api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=ai_model or ("llama3-8b-8192" if ai_provider == "groq" else "gpt-3.5-turbo"),
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content
        elif ai_provider == "gemini":
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{ai_model or 'gemini-1.5-flash'}:generateContent?key={ai_api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}
            }
            resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload)
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        else: # anthropic
            import anthropic
            client = anthropic.Anthropic(api_key=ai_api_key)
            schema = {
                "type": "object",
                "properties": {f: {"type": "string"} for f in fields},
                "required": fields,
                "additionalProperties": False,
            }
            try:
                resp = client.messages.create(
                    model=ai_model or "claude-3-haiku-20240307",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt + "\n\nPlease output the JSON object requested."}],
                    tools=[{"name": "extract", "description": "Extract fields", "input_schema": schema}],
                    tool_choice={"type": "tool", "name": "extract"}
                )
                for block in resp.content:
                    if block.type == "tool_use":
                        text = json.dumps(block.input)
                        break
                else:
                    text = "{}"
            except anthropic.AuthenticationError:
                return {f: "AI error: invalid API key" for f in fields}
            except anthropic.RateLimitError:
                return {f: "AI error: rate limited" for f in fields}
            except anthropic.APIStatusError as e:
                return {f: f"AI error: HTTP {e.status_code}" for f in fields}
            except anthropic.APIConnectionError:
                return {f: "AI error: connection failed" for f in fields}

        data = json.loads(text.strip("` \n").removeprefix("json\n"))
        return {f: str(data.get(f, "") or "") for f in fields}
    except Exception as e:
        return {f: f"AI error: {type(e).__name__}" for f in fields}
