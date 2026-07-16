"""Провайдер для OpenAI Responses API (client.responses.create).

Нужен для Yandex AI Studio (ai.api.cloud.yandex.net): в отличие от chat/completions,
здесь модели (в т.ч. YandexGPT/Alice) реально вызывают инструменты. История диалога
транслируется в input-элементы Responses (function_call / function_call_output)."""

from __future__ import annotations

import base64
import json
import logging
import os

from openai import OpenAI

from .base import ImagePart, LLMResponse, Msg, Provider, ToolCall, ToolSpec

log = logging.getLogger("analyzer.llm")


class OpenAIResponsesProvider(Provider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        supports_vision: bool,
        supports_tools: bool,
        force_first_tool: bool = False,
        max_output_tokens: int = 8000,
        timeout_s: int = 300,
        retries: int = 3,
        model_uri_template: str | None = None,
        folder_id_env: str | None = None,
    ):
        self.name = name
        self.supports_vision = supports_vision
        self.supports_tools = supports_tools
        self.force_first_tool = force_first_tool
        self.max_output_tokens = max_output_tokens
        self.model = model
        self._wire_model = model
        if model_uri_template:
            folder_id = os.environ.get(folder_id_env or "", "")
            if not folder_id:
                raise RuntimeError(
                    f"Провайдер {name}: требуется переменная окружения {folder_id_env} (folder_id)"
                )
            self._wire_model = model_uri_template.format(folder_id=folder_id, model=model)
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=retries)

    # ---------- трансляция истории -> input Responses ----------

    def _user_content(self, parts: list):
        text = "\n".join(p for p in parts if isinstance(p, str))
        if not any(isinstance(p, ImagePart) for p in parts):
            return text
        items: list = []
        if text:
            items.append({"type": "input_text", "text": text})
        for p in parts:
            if isinstance(p, ImagePart):
                data = base64.b64encode(p.data).decode()
                items.append({"type": "input_image", "image_url": f"data:{p.mime};base64,{data}"})
        return items

    def _to_input(self, messages: list[Msg]) -> tuple[str, list[dict]]:
        instructions: list[str] = []
        items: list[dict] = []
        for m in messages:
            if m.role == "system":
                instructions.append(m.text())
            elif m.role == "user":
                items.append({"role": "user", "content": self._user_content(m.content)})
            elif m.role == "assistant":
                txt = m.text()
                if txt:
                    items.append({"role": "assistant", "content": txt})
                for tc in m.tool_calls:
                    items.append({
                        "type": "function_call", "call_id": tc.id, "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    })
            elif m.role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": m.text(),
                })
        return "\n\n".join(p for p in instructions if p), items

    @staticmethod
    def _to_tools(tools: list[ToolSpec]) -> list[dict]:
        # Responses: function-инструменты ПЛОСКИЕ (без вложенного "function")
        return [{"type": "function", "name": t.name, "description": t.description,
                 "parameters": t.schema or {"type": "object", "properties": {}}} for t in tools]

    # ---------- вызов ----------

    def chat(self, messages: list[Msg], tools: list[ToolSpec] | None = None,
             tool_choice: str | None = None) -> LLMResponse:
        instructions, input_items = self._to_input(messages)
        kwargs: dict = {
            "model": self._wire_model,
            "input": input_items,
            "max_output_tokens": self.max_output_tokens,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools and self.supports_tools:
            kwargs["tools"] = self._to_tools(tools)
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        resp = self._client.responses.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in getattr(resp, "output", []) or []:
            itype = getattr(item, "type", "")
            if itype == "function_call":
                raw = getattr(item, "arguments", "") or "{}"
                try:
                    args = json.loads(raw)
                    if not isinstance(args, dict):
                        args = {"value": args}
                except (json.JSONDecodeError, ValueError):
                    args = {"_raw": raw}
                tool_calls.append(ToolCall(id=getattr(item, "call_id", "") or getattr(item, "id", ""),
                                           name=getattr(item, "name", ""), args=args))
            elif itype == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        text_parts.append(getattr(c, "text", "") or "")
        usage = {}
        u = getattr(resp, "usage", None)
        if u:
            det = getattr(u, "input_tokens_details", None)
            usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                     "output_tokens": getattr(u, "output_tokens", 0) or 0,
                     "cached_tokens": (getattr(det, "cached_tokens", 0) or 0) if det else 0,
                     "tool_tokens": (getattr(det, "tool_tokens", 0) or 0) if det else 0}
        return LLMResponse(text="\n".join(t for t in text_parts if t),
                           tool_calls=tool_calls, usage=usage,
                           stop_reason=getattr(resp, "status", None))
