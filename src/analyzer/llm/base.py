"""Провайдеро-независимые типы сообщений/инструментов и интерфейс Provider."""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("analyzer.llm")


class RateLimitExhausted(Exception):
    """Лимит API (HTTP 429) не снялся после всех попыток ретрая — вызов LLM невозможен.
    Ловится в пайплайне и ЧЕСТНО фиксируется в создаваемой подзадаче (анализ не выполнен)."""

    def __init__(self, attempts: int, last: BaseException | None = None):
        self.attempts = attempts
        self.last = last
        super().__init__(f"лимит API (429) не снят после {attempts} попыток: {last}")


def is_rate_limit_error(e: BaseException) -> bool:
    """429 у любого SDK: по коду статуса, имени класса (RateLimitError) или тексту."""
    if getattr(e, "status_code", None) == 429 or getattr(e, "code", None) == 429:
        return True
    if "ratelimit" in type(e).__name__.lower():
        return True
    msg = str(e).lower()
    return "429" in msg or "too many requests" in msg or "rate limit" in msg


@dataclass
class ImagePart:
    data: bytes
    mime: str  # image/png, image/jpeg, ...


# часть контента: строка (текст) или картинка
Part = str | ImagePart


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Msg:
    role: str                      # system | user | assistant | tool
    content: list[Part] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)  # для assistant
    tool_call_id: str | None = None                           # для tool

    @classmethod
    def system(cls, text: str) -> "Msg":
        return cls(role="system", content=[text])

    @classmethod
    def user(cls, *parts: Part) -> "Msg":
        return cls(role="user", content=list(parts))

    @classmethod
    def assistant(cls, text: str = "", tool_calls: list[ToolCall] | None = None) -> "Msg":
        return cls(role="assistant", content=[text] if text else [], tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, tool_call_id: str, text: str) -> "Msg":
        return cls(role="tool", content=[text], tool_call_id=tool_call_id)

    def text(self) -> str:
        return "\n".join(p for p in self.content if isinstance(p, str))


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict  # JSON Schema входных параметров


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: dict
    stop_reason: str | None = None


class Provider(ABC):
    """Единый интерфейс LLM-провайдера."""

    name: str = "base"
    model: str = ""
    supports_vision: bool = False
    supports_tools: bool = True
    force_first_tool: bool = False   # принудительный tool_choice=required на первом ходу

    RATE_LIMIT_RETRIES = 5           # попыток при 429 (поверх ретраев SDK)
    RATE_LIMIT_SLEEP_S = 5           # пауза между попытками, сек

    @abstractmethod
    def _chat_once(self, messages: list[Msg], tools: list[ToolSpec] | None = None,
                   tool_choice: str | None = None) -> LLMResponse:
        """Один вызов провайдера (без ретрая). Реализуется наследниками."""
        ...

    def chat(self, messages: list[Msg], tools: list[ToolSpec] | None = None,
             tool_choice: str | None = None) -> LLMResponse:
        """Вызов LLM с ретраем при 429: RATE_LIMIT_RETRIES попыток, пауза RATE_LIMIT_SLEEP_S с.
        Если лимит так и не снят — RateLimitExhausted (наверх, где это фиксируется в подзадаче)."""
        last: BaseException | None = None
        for attempt in range(1, self.RATE_LIMIT_RETRIES + 1):
            try:
                return self._chat_once(messages, tools=tools, tool_choice=tool_choice)
            except Exception as e:  # noqa: BLE001
                if not is_rate_limit_error(e):
                    raise
                last = e
                if attempt < self.RATE_LIMIT_RETRIES:
                    log.warning("%s: лимит API (429), попытка %d/%d — пауза %dс и повтор",
                                self.label(), attempt, self.RATE_LIMIT_RETRIES, self.RATE_LIMIT_SLEEP_S)
                    time.sleep(self.RATE_LIMIT_SLEEP_S)
        raise RateLimitExhausted(self.RATE_LIMIT_RETRIES, last)

    def label(self) -> str:
        return f"{self.name}/{self.model}"


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели (голый JSON, ```json-блок или первый {...})."""
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def truncate(text: str, limit: int, note: str = "усечено") -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {note}, всего {len(text)} символов]"
