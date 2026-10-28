"""Chat models via LangChain: Groq first, Gemini as a fallback, plus a
cached structured-output helper.

Per-minute rate limits are waited out on the same provider. A daily limit
(or any wait too long to sit through) puts that provider aside until its
quota resets, and the next configured provider takes over.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    name: str  # "groq" | "gemini"
    label: str
    model: str
    make: Callable[[float], object]


def _groq(model: str, key: str) -> Provider:
    def make(temperature: float):
        from langchain_groq import ChatGroq

        # structured() handles rate limits; SDK retries would double-spend quota.
        return ChatGroq(model=model, api_key=key, temperature=temperature, max_retries=0, timeout=60)

    return Provider("groq", "Groq", model, make)


def _gemini(model: str, key: str) -> Provider:
    def make(temperature: float):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, api_key=key, temperature=temperature, max_retries=0, timeout=60)

    return Provider("gemini", "Gemini", model, make)


def providers() -> list[Provider]:
    """Configured providers in fallback order."""
    config.reload_env()
    out = []
    if config.groq_api_key():
        out.append(_groq(config.groq_model(), config.groq_api_key()))
    if config.gemini_api_key():
        out.append(_gemini(config.gemini_model(), config.gemini_api_key()))
    return out


# -- rate limits ------------------------------------------------------------

_DURATION = re.compile(r"(?:try again|retry) in\s+((?:[\d.]+(?:h|ms|m|s)\s*)+)", re.I)
_RETRY_DELAY = re.compile(r"retry_?delay\W+(?:seconds\W+)?([\d.]+)s?", re.I)
_DAILY = re.compile(r"per day|\bTPD\b|\bRPD\b|PerDay|quota", re.I)
_RATE_LIMITED = re.compile(r"rate.?limit|\b429\b|RESOURCE_EXHAUSTED|quota", re.I)

_paused: dict[str, float] = {}  # provider name -> unix time its quota comes back
_paused_lock = threading.Lock()

MAX_INLINE_WAIT = 30  # seconds we're willing to block a trace for


def _seconds(text: str) -> float:
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)(h|ms|m|s)", text):
        total += float(value) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total


def rate_limit(exc: Exception) -> tuple[float, bool] | None:
    """(seconds to wait, is a daily limit) if `exc` is a rate-limit error."""
    text = str(exc)
    if not _RATE_LIMITED.search(text):
        return None
    m = _DURATION.search(text)
    wait = _seconds(m.group(1)) if m else None
    if wait is None and (m := _RETRY_DELAY.search(text)):
        wait = float(m.group(1))
    daily = bool(_DAILY.search(text))
    if wait is None:
        wait = 3600.0 if daily else 5.0
    return wait + 0.5, daily


def paused_until(name: str) -> float | None:
    with _paused_lock:
        until = _paused.get(name)
        if until and until <= time.time():
            del _paused[name]
            return None
        return until


def _pause(name: str, seconds: float) -> None:
    with _paused_lock:
        _paused[name] = time.time() + seconds


def status() -> list[dict]:
    """Providers for the status bar, with when a paused one comes back."""
    return [
        {"name": p.name, "label": p.label, "model": p.model, "paused_until": paused_until(p.name)}
        for p in providers()
    ]


# -- cache ------------------------------------------------------------------

_CACHE_DIR = config.CACHE_DIR / "llm"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(model: str, schema: type[BaseModel], messages: list[BaseMessage], temperature: float) -> str:
    payload = json.dumps(
        {
            "model": model,
            "schema": schema.model_json_schema(),
            "temperature": temperature,
            "messages": [[m.type, m.content] for m in messages],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cached(path, schema: type[T]) -> T | None:
    if not path.exists():
        return None
    try:
        return schema.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        path.unlink(missing_ok=True)
        return None


# -- entry point ------------------------------------------------------------


def structured(
    schema: type[T], messages: list[BaseMessage], temperature: float = 0.0, usage: list[str] | None = None
) -> T:
    """Get `schema` from the first available provider.

    An identical earlier answer from any provider is reused without a call.
    Appends "cached", "groq" or "gemini" to `usage` so callers can report
    which quota was spent.
    """
    note = usage.append if usage is not None else (lambda _x: None)
    chain = providers()
    if not chain:
        raise LLMUnavailable("No model configured. Add GROQ_API_KEY or GEMINI_API_KEY to .env.")

    for p in chain:
        hit = _cached(_CACHE_DIR / f"{_cache_key(p.model, schema, messages, temperature)}.json", schema)
        if hit is not None:
            note("cached")
            return hit

    limits: list[str] = []
    for p in chain:
        until = paused_until(p.name)
        if until:
            limits.append(f"{p.label} limit reached, back in {_fmt(until - time.time())}")
            continue
        for attempt in range(4):
            try:
                result = _structured(p, schema, messages, temperature)
            except Exception as exc:
                rl = rate_limit(exc)
                if rl is None:
                    raise
                wait, daily = rl
                if daily or wait > MAX_INLINE_WAIT or attempt == 3:
                    _pause(p.name, wait)
                    limits.append(f"{p.label} limit reached, back in {_fmt(wait)}")
                    break  # next provider
                time.sleep(wait)
                continue
            note(p.name)
            path = _CACHE_DIR / f"{_cache_key(p.model, schema, messages, temperature)}.json"
            path.write_text(result.model_dump_json(), encoding="utf-8")
            return result

    hint = "" if len(chain) > 1 else " Add GEMINI_API_KEY to .env to fall back to Gemini."
    raise LLMUnavailable("; ".join(limits) + "." + hint)


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Gemini can return content parts
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def _structured(p: Provider, schema: type[T], messages: list[BaseMessage], temperature: float) -> T:
    """Ask for `schema` via tool calling; fall back to parsing raw JSON."""
    llm = p.make(temperature)
    try:
        result = llm.with_structured_output(schema).invoke(messages)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except Exception as exc:
        if rate_limit(exc) is not None:
            raise  # let structured() decide; the JSON fallback would hit the same limit

    hint = (
        "Respond with a single JSON object matching this JSON schema, and nothing else:\n"
        + json.dumps(schema.model_json_schema())
    )
    raw = _text(llm.invoke([*messages, ("user", hint)]).content)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("Model did not return JSON.")
    try:
        return schema.model_validate_json(match.group(0))
    except ValidationError as exc:
        raise ValueError(f"Model returned malformed output: {exc.errors()[:2]}") from exc
