"""Провайдер для OpenAI-совместимых API: OpenAI, z.ai (GLM), Yandex AI Studio и др."""

from __future__ import annotations

import base64
import json
import logging
import os

from openai import OpenAI

from .base import ImagePart, LLMResponse, Msg, Provider, ToolCall, ToolSpec

log = logging.getLogger("analyzer.llm")


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        supports_vision: bool,
        supports_tools: bool,
        max_output_tokens: int = 8000,
        timeout_s: int = 300,
        retries: int = 3,
        model_uri_template: str | None = None,
        folder_id_env: str | None = None,
    ):
        self.name = name
        self.supports_vision = supports_vision
        self.supports_tools = supports_tools
        self.max_output_tokens = max_output_tokens
        self.model = model
        # Yandex: модель адресуется URI вида gpt://<folder_id>/<model>
        self._wire_model = model
        if model_uri_template:
            folder_id = os.environ.get(folder_id_env or "", "")
            if not folder_id:
                raise RuntimeError(
                    f"Провайдер {name}: требуется переменная окружения {folder_id_env} (folder_id)"
                )
            self._wire_model = model_uri_template.format(folder_id=folder_id, model=model)
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=retries,
        )

    # ---------- трансляция сообщений ----------

    def _to_openai_messages(self, messages: list[Msg]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                out.append({"role": "system", "content": m.text()})
            elif m.role == "user":
                parts = []
                for p in m.content:
                    if isinstance(p, ImagePart):
                        b64 = base64.b64encode(p.data).decode()
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{p.mime};base64,{b64}"},
                        })
                    elif p:
                        parts.append({"type": "text", "text": p})
                # если только текст — передаём строкой (совместимее)
                if all(pt.get("type") == "text" for pt in parts):
                    out.append({"role": "user", "content": "\n".join(pt["text"] for pt in parts)})
                else:
                    out.append({"role": "user", "content": parts})
            elif m.role == "assistant":
                msg: dict = {"role": "assistant", "content": m.text() or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)},
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            elif m.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.text(),
                })
        return out

    @staticmethod
    def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    # ---------- вызов ----------

    def chat(self, messages: list[Msg], tools: list[ToolSpec] | None = None) -> LLMResponse:
        kwargs: dict = {
            "model": self._wire_model,
            "messages": self._to_openai_messages(messages),
            "max_tokens": self.max_output_tokens,
        }
        if tools and self.supports_tools:
            kwargs["tools"] = self._to_openai_tools(tools)
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = choice.message.content or ""
        tool_calls: list[ToolCall] = []
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {"value": args}
            except (json.JSONDecodeError, ValueError):
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        usage = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            }
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=choice.finish_reason,
        )
