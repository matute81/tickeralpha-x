"""Fill prompts/x_ideas.md and ask Anthropic or OpenAI for 3 X ideas."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fetch_fmp_brief import ROOT, load_dotenv

PROMPT_PATH = ROOT / "prompts" / "x_ideas.md"
DEFAULT_PROMPT_PATH = ROOT / "prompts" / "defaults" / "x_ideas.md"
CONTEXT_PATH = ROOT / "prompts" / "context.json"
DRAFTS = ROOT / "drafts"
AI_UNAVAILABLE = "AI is not available. Add CURSOR_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY to .env."
MAX_PROMPT_CHARS = 200_000
CONTEXT_KEYS = ("good_examples", "good_why", "bad_examples", "bad_why")

MODE_TO_STREAM = {
    "morning": "1",
    "close": "2",
    "week-ahead": "3",
}

FOCUS_FILES = {
    "morning": ROOT / "prompts" / "focus-morning.md",
    "close": ROOT / "prompts" / "focus-close.md",
    "week-ahead": ROOT / "prompts" / "focus-week-ahead.md",
}

DEFAULT_FOCUS_FILES = {
    "morning": ROOT / "prompts" / "defaults" / "focus-morning.md",
    "close": ROOT / "prompts" / "defaults" / "focus-close.md",
    "week-ahead": ROOT / "prompts" / "defaults" / "focus-week-ahead.md",
}

STREAM_LABELS = {
    "1": "1 — US data releases, economy, politics, and news",
    "2": "2 — Price movements and stock analysis",
    "3": "3 — What to look for next week (weekend posts for the upcoming week)",
}

STREAM_WINDOWS = {
    "1": "Stream 1 — Economy, politics & news",
    "2": "Stream 2 — Price movements & stock analysis",
    "3": "Stream 3 — Next week preview",
}

TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
CASHTAG_RE = re.compile(r"\$[A-Z]{1,5}\b")
PCT_IN_TEXT = re.compile(r"(?<![\d.])([+-]?)(\d+(?:\.\d+)?)%")
PRICE_IN_TEXT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+\.\d+|\d+\.\d+)(?![\d%])")
MOVER_IN_TEXT = re.compile(
    r"\$[A-Z]{1,5}\s+[+-]?\d+(?:\.\d+)?%(?:\s+to\s+[\d,]+(?:\.\d+)?)?"
)
PERCENT_KEY_RE = re.compile(r"percent", re.I)
PRICE_KEYS = {
    "price",
    "change",
    "previousClose",
    "dayHigh",
    "dayLow",
    "yearHigh",
    "yearLow",
    "open",
    "close",
    "vwap",
}
IDEA_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,3}\s*)?Idea\s+(\d+)\s*[:.]?\s*\n(.*?)(?=(?:^|\n)\s*(?:#{1,3}\s*)?Idea\s+\d+\b|\Z)",
    re.I | re.S,
)
FIELD_RE = re.compile(
    r"\*{0,2}\s*(Title|Short Summary|Post Body|Supporting Data|Source URL)\s*\*{0,2}\s*:\s*\*{0,2}\s*(.*?)(?=\n\s*\*{0,2}\s*(?:Title|Short Summary|Post Body|Supporting Data|Source URL)\s*\*{0,2}\s*:|\Z)",
    re.I | re.S,
)
RAW_DUMP = ROOT / "data" / "last_llm_raw.md"


def _llm_keys() -> tuple[str, str, str]:
    load_dotenv(ROOT / ".env")
    return (
        (os.environ.get("CURSOR_API_KEY") or "").strip(),
        (os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        (os.environ.get("OPENAI_API_KEY") or "").strip(),
    )


def llm_configured() -> bool:
    cursor, anthropic, openai = _llm_keys()
    return bool(cursor or anthropic or openai)


def unavailable_message(detail: str | None = None) -> str:
    if not llm_configured():
        return AI_UNAVAILABLE
    if detail:
        return f"AI is not available. {detail}"
    return "AI is not available."


def slim_fmp_data(brief: dict[str, Any]) -> dict[str, Any]:
    """Inject only fields the prompt can cite. Keep news URLs — ideas need a source URL."""
    news = []
    for item in brief.get("news") or []:
        news.append(
            {
                "publishedDate": item.get("publishedDate"),
                "publisher": item.get("publisher"),
                "title": item.get("title"),
                "text": item.get("text"),
                "url": item.get("url"),
                "symbol": item.get("symbol"),
                "site": item.get("site"),
            }
        )
    payload = {
        "session_date": brief.get("session_date"),
        "timezone": brief.get("timezone") or "America/New_York",
        "mode": brief.get("mode"),
        "week_start": brief.get("week_start"),
        "week_end": brief.get("week_end"),
        "generated_at": brief.get("generated_at"),
        "indexes": brief.get("indexes") or {},
        "overseas": brief.get("overseas") or {},
        "cross_asset": brief.get("cross_asset") or {},
        "treasury": brief.get("treasury"),
        "sectors": (brief.get("sectors") or [])[:12],
        "mega_caps": brief.get("mega_caps") or {},
        "liquid_gainers": brief.get("liquid_gainers") or [],
        "liquid_losers": brief.get("liquid_losers") or [],
        "actives": (brief.get("actives") or [])[:10],
        "economic_calendar": brief.get("economic_calendar") or [],
        "earnings_calendar": brief.get("earnings_calendar") or [],
        "news": news[:15],
    }
    return round_quote_numbers(payload)


def _round_num(value: Any, digits: int) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def round_quote_numbers(obj: Any) -> Any:
    """Round quote percents to 1 decimal and prices to 2 before the LLM sees them."""
    if isinstance(obj, list):
        return [round_quote_numbers(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            out[key] = round_quote_numbers(value)
        elif PERCENT_KEY_RE.search(str(key)):
            out[key] = _round_num(value, 1)
        elif key in PRICE_KEYS:
            out[key] = _round_num(value, 2)
        else:
            out[key] = value
    return out


def _fmt_pct(sign: str, num: str) -> str:
    if "." not in num:
        return f"{sign}{num}%"
    n = float(sign + num)
    rounded = round(n, 1)
    if rounded == 0:
        return "0.0%"
    if rounded < 0:
        return f"-{abs(rounded):.1f}%"
    if sign == "+":
        return f"+{rounded:.1f}%"
    return f"{rounded:.1f}%"


def _fmt_price_token(token: str) -> str:
    raw = token.replace(",", "")
    try:
        n = float(raw)
    except ValueError:
        return token
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    if decimals <= 2 and abs(n) < 1000:
        return token
    if decimals <= 2 and abs(n) >= 1000:
        return f"{n:,.2f}"
    if abs(n) < 10 and decimals <= 3:
        return token
    if abs(n) >= 1000:
        return f"{n:,.2f}"
    return f"{n:.2f}"


def _explode_movers(paragraph: str) -> str:
    stripped = paragraph.strip()
    if re.match(r"^[-*•]\s+", stripped):
        return paragraph
    movers = list(MOVER_IN_TEXT.finditer(stripped))
    if len(movers) < 2:
        return paragraph
    if len(movers) < 3 and "," not in stripped:
        return paragraph
    intro = stripped[: movers[0].start()]
    intro = re.sub(r"(?:,?\s+(?:with|including|and|vs\.?))\s*$", "", intro, flags=re.I)
    intro = intro.rstrip(" :—-")
    tail = stripped[movers[-1].end() :].lstrip(" .;,").strip()
    lines: list[str] = []
    if intro:
        lines.append(intro + ":")
    for match in movers:
        lines.append("- " + match.group(0).strip())
    if tail and not MOVER_IN_TEXT.search(tail):
        lines.append("")
        lines.append(tail)
    return "\n".join(lines)


def _sentence_rows(paragraph: str) -> str:
    if "\n" in paragraph or re.match(r"\s*[-*•]\s+", paragraph):
        return paragraph
    protected = paragraph.replace("vs. ", "\ue000").replace("U.S. ", "\ue001")
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z$“"‘])', protected.strip())
    if len(parts) < 2:
        return paragraph
    return "\n".join(part.replace("\ue000", "vs. ").replace("\ue001", "U.S. ") for part in parts)


def format_post_body(text: str) -> str:
    """Round percents/prices and turn ticker lists into readable rows."""
    if not text:
        return text
    out = PCT_IN_TEXT.sub(lambda m: _fmt_pct(m.group(1), m.group(2)), text)
    out = PRICE_IN_TEXT.sub(lambda m: _fmt_price_token(m.group(1)), out)
    blocks: list[str] = []
    for raw in re.split(r"\n{2,}", out.strip()):
        para = raw.strip()
        if not para:
            continue
        para = _explode_movers(para)
        if "\n" in para:
            blocks.append("\n".join(_sentence_rows(line) if not line.startswith("- ") else line for line in para.split("\n")))
        else:
            blocks.append(_sentence_rows(para))
    return "\n\n".join(blocks)


def format_idea_title(text: str) -> str:
    if not text:
        return text
    return PCT_IN_TEXT.sub(lambda m: _fmt_pct(m.group(1), m.group(2)), text)


def previous_posts(session: date) -> list[dict[str, Any]]:
    """Tickers and titles from drafts in the last two US session dates."""
    wanted = {
        (session - timedelta(days=1)).isoformat(),
        (session - timedelta(days=2)).isoformat(),
    }
    out: list[dict[str, Any]] = []
    if not DRAFTS.exists():
        return out
    for path in sorted(DRAFTS.glob("*.md")):
        name = path.name
        if "template" in name.lower() or name.lower() == "readme.md":
            continue
        day = name[:10]
        if day not in wanted:
            continue
        text = path.read_text(encoding="utf-8")
        tickers = sorted(set(TICKER_RE.findall(text)))
        titles = re.findall(r"\*\*Title:\*\*\s*(.+)", text)
        if not titles:
            titles = re.findall(r"^## (.+)$", text, re.M)
        topic_keys = [t.strip() for t in titles if t.strip()][:12]
        out.append({"date": day, "file": name, "tickers": tickers, "topic_keys": topic_keys})
    return out


def expected_idea_count(mode: str | None) -> int:
    return 4 if mode == "close" else 3


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _write_text(path: Path, text: str) -> None:
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"Text is too long (max {MAX_PROMPT_CHARS} characters).")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_norm(text), encoding="utf-8")


def _or_none(value: str, label: str) -> str:
    text = (value or "").strip()
    return text if text else f"(No {label} provided.)"


def focus_file_for(mode: str) -> Path:
    if mode not in FOCUS_FILES:
        raise KeyError(mode)
    return FOCUS_FILES[mode]


def default_focus_file_for(mode: str) -> Path:
    if mode not in DEFAULT_FOCUS_FILES:
        raise KeyError(mode)
    return DEFAULT_FOCUS_FILES[mode]


def read_full_prompt() -> tuple[str, bool]:
    """Return (text, customized vs defaults/x_ideas.md)."""
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(PROMPT_PATH)
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if DEFAULT_PROMPT_PATH.is_file():
        default = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
        return text, _norm(text) != _norm(default)
    return text, False


def write_full_prompt(text: str) -> None:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Full prompt cannot be empty.")
    _write_text(PROMPT_PATH, stripped + "\n")


def reset_full_prompt() -> None:
    if not DEFAULT_PROMPT_PATH.is_file():
        raise FileNotFoundError(DEFAULT_PROMPT_PATH)
    write_full_prompt(DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"))


def read_focus(mode: str) -> tuple[str, bool]:
    """Return (focus text, customized). Custom file wins; else defaults/focus-*.md."""
    custom = focus_file_for(mode)
    default = default_focus_file_for(mode)
    if custom.is_file():
        return custom.read_text(encoding="utf-8"), True
    if default.is_file():
        return default.read_text(encoding="utf-8"), False
    raise FileNotFoundError(default)


def write_focus(mode: str, text: str) -> None:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Stream focus cannot be empty.")
    _write_text(focus_file_for(mode), stripped + "\n")


def reset_focus(mode: str) -> None:
    path = focus_file_for(mode)
    if path.is_file():
        path.unlink()


def empty_context() -> dict[str, str]:
    return {key: "" for key in CONTEXT_KEYS}


def read_context() -> dict[str, str]:
    out = empty_context()
    if not CONTEXT_PATH.is_file():
        return out
    try:
        raw = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return out
    if not isinstance(raw, dict):
        return out
    for key in CONTEXT_KEYS:
        value = raw.get(key)
        out[key] = value if isinstance(value, str) else ""
    return out


def write_context(payload: dict[str, Any]) -> dict[str, str]:
    out = empty_context()
    for key in CONTEXT_KEYS:
        value = payload.get(key)
        text = value if isinstance(value, str) else ""
        if len(text) > MAX_PROMPT_CHARS:
            raise ValueError(f"{key} is too long (max {MAX_PROMPT_CHARS} characters).")
        out[key] = _norm(text)
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def fill_prompt(
    stream: str,
    fmp_data: dict[str, Any],
    previous: list[dict[str, Any]],
    *,
    mode: str | None = None,
) -> str:
    template, _ = read_full_prompt()
    if mode:
        focus, _ = read_focus(mode)
    else:
        focus = ""
    ctx = read_context()
    label = STREAM_LABELS.get(stream, stream)
    focus_text = focus.strip() or "(No stream focus provided.)"
    good_examples = _or_none(ctx["good_examples"], "good examples")
    good_why = _or_none(ctx["good_why"], "why-good notes")
    bad_examples = _or_none(ctx["bad_examples"], "bad examples")
    bad_why = _or_none(ctx["bad_why"], "why-bad notes")
    data_json = json.dumps(fmp_data, indent=2, default=str)
    prev_json = json.dumps(previous, indent=2, default=str)
    if "## INPUTS" in template:
        body, inputs = template.split("## INPUTS", 1)
    else:
        body, inputs = template, ""
    if "{{STREAM_FOCUS}}" not in template:
        body = body.rstrip() + f"\n\n## STREAM-SPECIFIC FOCUS\n\n{focus_text}\n"
    tokens = {
        "{{STREAM}}": label,
        "{{STREAM_FOCUS}}": focus_text,
        "{{GOOD_EXAMPLES}}": good_examples,
        "{{GOOD_WHY}}": good_why,
        "{{BAD_EXAMPLES}}": bad_examples,
        "{{BAD_WHY}}": bad_why,
    }
    for key, value in tokens.items():
        body = body.replace(key, value)
        inputs = inputs.replace(key, value)
    body = body.replace("{{FMP_DATA}}", "the injected FMP_DATA")
    body = body.replace("{{PREVIOUS_POSTS}}", "the injected PREVIOUS_POSTS")
    inputs = inputs.replace("{{FMP_DATA}}", data_json)
    inputs = inputs.replace("{{PREVIOUS_POSTS}}", prev_json)
    if data_json not in inputs:
        inputs += (
            f"\n\n- STREAM: {label}\n- STREAM_FOCUS:\n{focus_text}\n"
            f"- FMP_DATA:\n{data_json}\n- PREVIOUS_POSTS:\n{prev_json}\n"
        )
    return (
        body
        + "## INPUTS"
        + inputs
        + "\n\nBegin the reply with ### Idea 1. No preamble. Follow the OUTPUT FORMAT exactly. If this is stream 2, continue through ### Idea 4.\n"
    )


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).removesuffix("```").strip()
    return text


def _clean_field(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"^[\s*]+", "", text)
    text = re.sub(r"\s*-{3,}\s*$", "", text)
    text = re.sub(r"\*+$", "", text)
    return text.strip()


def _first_url(value: str) -> str:
    match = re.search(r"https?://[^\s*>]+", value or "")
    return match.group(0).rstrip(").,]") if match else _clean_field(value)


def _as_idea(number: str, title: str, summary: str, body: str, supporting_lines: list[str], source_url: str) -> dict[str, Any] | None:
    body = format_post_body(_clean_field(body or ""))
    if not body:
        return None
    source_url = _first_url(source_url or "")
    return {
        "id": f"idea_{number}",
        "title": format_idea_title(re.sub(r"\s+", " ", _clean_field(title) or f"Idea {number}").strip()),
        "window": "",
        "status": "ready",
        "body": body,
        "skip_reason": "",
        "source_notes": source_url or "FMP_DATA",
        "kind": "x_idea",
        "summary": format_idea_title(re.sub(r"\s+", " ", _clean_field(summary or "")).strip()),
        "supporting_data": [format_post_body(_clean_field(row)) for row in supporting_lines if _clean_field(row)],
        "source_url": source_url,
        "tickers": sorted(set(CASHTAG_RE.findall(body))),
    }


def _parse_json_ideas(text: str, limit: int = 3) -> list[dict[str, Any]]:
    blob = text.strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json|markdown)?", "", blob).removesuffix("```").strip()
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end <= start:
        start = blob.find("[")
        end = blob.rfind("]")
        if start < 0 or end <= start:
            return []
    try:
        parsed = json.loads(blob[start : end + 1])
    except json.JSONDecodeError:
        return []
    rows = parsed.get("ideas") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        return []
    ideas: list[dict[str, Any]] = []
    for i, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, dict):
            continue
        supporting = row.get("supporting_data") or row.get("Supporting Data") or []
        if isinstance(supporting, str):
            supporting_lines = [ln.strip(" -*") for ln in supporting.splitlines() if ln.strip()]
        else:
            supporting_lines = [str(x) for x in supporting]
        idea = _as_idea(
            str(row.get("n") or i),
            str(row.get("title") or row.get("Title") or ""),
            str(row.get("summary") or row.get("short_summary") or row.get("Short Summary") or ""),
            str(row.get("body") or row.get("post_body") or row.get("Post Body") or ""),
            supporting_lines,
            str(row.get("source_url") or row.get("Source URL") or ""),
        )
        if idea:
            ideas.append(idea)
    return ideas


def parse_ideas(raw: str, limit: int = 3) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    ideas = _parse_json_ideas(text, limit)
    if ideas:
        return ideas[:limit]
    for match in IDEA_RE.finditer(text):
        number = match.group(1)
        block = match.group(2)
        fields = {key.lower(): value.strip() for key, value in FIELD_RE.findall(block)}
        supporting = fields.get("supporting data") or ""
        supporting_lines = [
            re.sub(r"^[-*]\s*", "", line).strip()
            for line in supporting.splitlines()
            if line.strip() and line.strip() != "-"
        ]
        idea = _as_idea(
            number,
            fields.get("title") or "",
            fields.get("short summary") or "",
            fields.get("post body") or "",
            supporting_lines,
            fields.get("source url") or "",
        )
        if idea:
            ideas.append(idea)
        if len(ideas) >= limit:
            break
    return ideas


def _openai(api_key: str, prompt: str, *, max_tokens: int = 4000) -> str:
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _anthropic(api_key: str, prompt: str, *, max_tokens: int = 8000) -> str:
    model = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20250929"
    body = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["content"][0]["text"]


def _cursor(api_key: str, prompt: str) -> str:
    try:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
    except ImportError as exc:
        raise RuntimeError("cursor-sdk is not installed. Run: pip install cursor-sdk") from exc

    model = os.environ.get("CURSOR_MODEL") or "composer-2.5"
    try:
        result = Agent.prompt(
            prompt,
            AgentOptions(
                api_key=api_key,
                model=model,
                tools=[],
                local=LocalAgentOptions(cwd=str(ROOT)),
            ),
        )
    except CursorAgentError as exc:
        raise RuntimeError(f"Cursor agent failed to start: {exc}") from exc
    if result.status != "finished":
        raise RuntimeError(f"Cursor agent {result.status}: {(result.result or result.id or '')}")
    text = (result.result or "").strip()
    if not text:
        raise RuntimeError("Cursor agent returned empty text.")
    return text


def generate_x_ideas(brief: dict[str, Any], mode: str, session: date) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return (slots, error). slots is None if LLM is off or the call failed."""
    cursor, anthropic, openai = _llm_keys()
    if not cursor and not anthropic and not openai:
        return None, None
    if mode not in MODE_TO_STREAM:
        return None, f"Unknown mode: {mode}"

    stream = MODE_TO_STREAM[mode]
    window = STREAM_WINDOWS[stream]
    want = expected_idea_count(mode)
    try:
        prompt = fill_prompt(stream, slim_fmp_data(brief), previous_posts(session), mode=mode)
    except (OSError, ValueError, KeyError) as exc:
        return None, str(exc)
    max_tokens = 12000 if mode == "close" else 8000
    try:
        if cursor:
            raw = _cursor(cursor, prompt)
        elif anthropic:
            raw = _anthropic(anthropic, prompt, max_tokens=max_tokens)
        else:
            raw = _openai(openai, prompt, max_tokens=min(max_tokens, 8000))
        RAW_DUMP.parent.mkdir(parents=True, exist_ok=True)
        RAW_DUMP.write_text(raw or "", encoding="utf-8")
        ideas = parse_ideas(raw, want)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return None, f"HTTP {exc.code}: {detail}"
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        return None, str(exc)

    if not ideas:
        preview = (raw or "").strip().replace("\n", " ")[:240]
        return None, f"LLM returned no parseable Idea blocks: {preview}"

    for idea in ideas:
        idea["window"] = window
    if mode == "close" and len(ideas) >= 4:
        ideas[3]["kind"] = "x_recap"
    while len(ideas) < want:
        n = len(ideas) + 1
        ideas.append(
            {
                "id": f"idea_{n}",
                "title": f"Idea {n}",
                "window": window,
                "status": "empty",
                "body": "",
                "skip_reason": f"model returned fewer than {want} posts",
                "source_notes": "",
                "kind": "x_recap" if mode == "close" and n == 4 else "x_idea",
                "summary": "",
                "supporting_data": [],
                "source_url": "",
                "tickers": [],
            }
        )
    return ideas[:want], None
