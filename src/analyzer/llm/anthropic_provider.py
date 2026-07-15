"""Провайдер Claude через официальный SDK anthropic (native tool use + vision)."""

from __future__ import annotations

import base64
import logging

import anthropic

from .base import ImagePart, LLMResponse, Msg, Provider, ToolCall, ToolSpec

log = logging.getLogger("analyzer.llm")


class AnthropicProvider(Provider):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-8",
        supports_vision: bool = True,
        supports_tools: bool = True,
        max_output_tokens: int = 8000,
        timeout_s: int = 300,
        retries: int = 3,
    ):
        self.name = "claude"
        self.model = model
        self.supports_vision = supports_vision
        self.supports_tools = supports_tools
        self.max_output_tokens = max_output_tokens
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout_s), max_retries=retries)

    # ---------- трансляция ----------

    @staticmethod
    def _content_blocks(parts) -> list[dict]:
        blocks: list[dict] = []
        for p in parts:
            if isinstance(p, ImagePart):
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": p.mime,
                        "data": base64.b64encode(p.data).decode(),
                    },
                })
            elif p:
                blocks.append({"type": "text", "text": p})
        return blocks

    def _translate(self, messages: list[Msg]) -> tuple[str, list[dict]]:
        system_text = "\n\n".join(m.text() for m in messages if m.role == "system")
        out: list[dict] = []
        pending_tool_results: list[dict] = []

        def flush_tools():
            nonlocal pending_tool_results
            if pending_tool_results:
                out.append({"role": "user", "content": pending_tool_results})
                pending_tool_results = []

        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.text(),
                })
                continue
            flush_tools()
            if m.role == "user":
                out.append({"role": "user", "content": self._content_blocks(m.content)})
            elif m.role == "assistant":
                blocks = self._content_blocks(m.content)
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                out.append({"role": "assistant", "content": blocks})
        flush_tools()
        return system_text, out

    # ---------- вызов ----------

    def chat(self, messages: list[Msg], tools: list[ToolSpec] | None = None) -> LLMResponse:
        system_text, api_messages = self._translate(messages)
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": api_messages,
            "thinking": {"type": "adaptive"},
        }
        if system_text:
            kwargs["system"] = system_text
        if tools and self.supports_tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description,
                 "input_schema": t.schema or {"type": "object", "properties": {}}}
                for t in tools
            ]
        resp = self._client.messages.create(**kwargs)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {"value": block.input}
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=args))
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=resp.stop_reason,
        )
