"""Провайдеро-независимые типы сообщений/инструментов и интерфейс Provider."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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

    @abstractmethod
    def chat(self, messages: list[Msg], tools: list[ToolSpec] | None = None,
             tool_choice: str | None = None) -> LLMResponse:
        ...

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
