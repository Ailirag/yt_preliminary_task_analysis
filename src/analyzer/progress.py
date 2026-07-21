"""Список задач, находящихся в разборе ПРЯМО СЕЙЧАС — для `analyzer status`.

Демон пишет `work/current.json` при старте/финише разбора каждой задачи; `status` (отдельный
процесс) читает снимок. Потокобезопасно (threading.Lock) — готово к параллельному разбору."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class CurrentWork:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._flush_locked()          # старт демона -> сбрасываем возможные устаревшие записи

    def start(self, key: str, workflow: str, ts: float | None = None) -> None:
        with self._lock:
            self._tasks[key] = {"workflow": workflow,
                                "started_ts": ts if ts is not None else time.time()}
            self._flush_locked()

    def finish(self, key: str) -> None:
        with self._lock:
            self._tasks.pop(key, None)
            self._flush_locked()

    def _flush_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"tasks": self._tasks, "updated_ts": time.time()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
