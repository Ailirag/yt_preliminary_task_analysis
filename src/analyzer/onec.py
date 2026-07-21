"""MCP-клиент к onec-vecgraph lite (stdio) — инструменты анализа кода 1С для LLM.

MCP SDK асинхронный, а пайплайн синхронный, поэтому сессия живёт в фоновом
потоке с собственным event loop; вызовы инструментов — через
run_coroutine_threadsafe. При недоступности сервера пайплайн деградирует
(анализ без кода) — это не фатальная ошибка.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from pathlib import Path

from .config import OnecCfg
from .llm.base import ToolSpec, truncate

log = logging.getLogger("analyzer.onec")


def _passthrough_env(environ: dict) -> dict:
    """Переменные, которые НУЖНО пробросить в подпроцесс onec-lite поверх безопасного набора
    MCP-SDK (get_default_environment() вырезает всё лишнее). Без ONEC_LITE_STATE мульти-воркспейс
    onec-lite не видит ни одной рабочей копии («не сконфигурирован»). TZ — чтобы время совпадало."""
    return {k: v for k, v in environ.items() if k.startswith("ONEC_LITE") or k == "TZ"}


class OnecMCP:
    def __init__(self, cfg: OnecCfg, project_root: Path):
        self.cfg = cfg
        self.project_root = project_root
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._tools_raw: list = []
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._error: str | None = None

    # ---------- lifecycle ----------

    async def _run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        # ВАЖНО: get_default_environment() (MCP SDK) отдаёт лишь минимальный безопасный набор и
        # вырезает наши ONEC_LITE_* (в т.ч. ONEC_LITE_STATE) — из-за чего мульти-воркспейс onec-lite
        # стартовал без единой рабочей копии. Пробрасываем их явно; cfg.env перекрывает при необходимости.
        env = {**get_default_environment(), **_passthrough_env(os.environ), **self.cfg.env}
        params = StdioServerParameters(
            command=self.cfg.command,
            args=self.cfg.resolved_args(self.project_root),
            env=env,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._session = session
                    self._tools_raw = listed.tools
                    self._ready.set()
                    await self._shutdown.wait()
        except Exception as e:  # noqa: BLE001 — стартовые сбои деградируют мягко
            self._error = f"{type(e).__name__}: {e}"
            self._ready.set()

    def start(self) -> bool:
        """Запускает MCP-сервер; True = инструменты доступны."""
        if not self.cfg.enabled:
            self._error = "onec.enabled: false"
            return False
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout=self.cfg.start_timeout_s)
        if not ok:
            self._error = f"onec-lite не поднялся за {self.cfg.start_timeout_s}с"
            log.warning(self._error)
            return False
        if self._error:
            log.warning("onec-lite недоступен: %s", self._error)
            return False
        log.info("onec-lite подключен, инструментов: %d", len(self._tools_raw))
        return True

    def stop(self) -> None:
        if self._loop and self._shutdown and not self._error:
            try:
                self._loop.call_soon_threadsafe(self._shutdown.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def available(self) -> bool:
        return self._session is not None and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    # ---------- инструменты ----------

    def tool_specs(self) -> list[ToolSpec]:
        """Инструменты для LLM с учётом whitelist (пустой = все)."""
        wl = {w.lower() for w in self.cfg.tool_whitelist}
        specs: list[ToolSpec] = []
        for t in self._tools_raw:
            if wl and t.name.lower() not in wl:
                continue
            specs.append(ToolSpec(
                name=t.name,
                description=(t.description or "")[:1024],
                schema=t.inputSchema or {"type": "object", "properties": {}},
            ))
        return specs

    def all_tool_names(self) -> list[str]:
        return [t.name for t in self._tools_raw]

    def accepts_workspace(self, name: str) -> bool:
        """True, если инструмент объявляет параметр workspace (его можно адресовать воркспейсу).
        Инструменты без workspace (напр. list_workspaces, справка платформы) не трогаем."""
        for t in self._tools_raw:
            if t.name == name:
                props = (t.inputSchema or {}).get("properties") or {}
                return "workspace" in props
        return False

    def call(self, name: str, args: dict, max_chars: int = 20000) -> str:
        """Синхронный вызов инструмента; ошибки возвращаются текстом (модель их видит)."""
        if not self.available:
            return f"[инструмент недоступен: {self._error}]"
        assert self._loop is not None and self._session is not None
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        try:
            result = fut.result(timeout=self.cfg.call_timeout_s)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            return f"[таймаут вызова {name} ({self.cfg.call_timeout_s}с)]"
        except Exception as e:  # noqa: BLE001
            return f"[ошибка вызова {name}: {type(e).__name__}: {e}]"
        parts: list[str] = []
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(f"[не-текстовый контент: {getattr(item, 'type', '?')}]")
        text = "\n".join(parts) if parts else "[пустой результат]"
        if getattr(result, "isError", False):
            text = f"[инструмент вернул ошибку]\n{text}"
        return truncate(text, max_chars, note="результат инструмента усечён")
