"""Single-instance lock для демона: не даём двум прогонам/демонам работать одновременно.

Лок-файл хранит PID и время последнего heartbeat. Лок считается протухшим, если процесс
мёртв (определяется на POSIX) ИЛИ heartbeat старше stale_after (кроссплатформенный сигнал).
Демон обновляет heartbeat на каждом тике; вторая копия при живом свежем локе не стартует.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("analyzer.lock")


def _pid_alive(pid: int) -> bool | None:
    """True/False на POSIX; None — определить нельзя (напр. Windows) → полагаемся на возраст heartbeat."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, AttributeError):
        return None


class LockHeld(RuntimeError):
    """Лок удерживается другим живым процессом."""


class SingleInstanceLock:
    def __init__(self, path: Path, stale_after_s: float = 300.0):
        self.path = Path(path)
        self.stale_after_s = stale_after_s
        self._held = False

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _write(self, ts: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "ts": ts}), encoding="utf-8")

    def _is_stale(self, info: dict, now: float) -> bool:
        alive = _pid_alive(int(info.get("pid", -1)))
        if alive is False:
            return True                          # процесс мёртв (POSIX) — точно протух
        if now - float(info.get("ts", 0)) > self.stale_after_s:
            return True                          # heartbeat давно не обновлялся
        return False                             # жив/недавний — занят

    def acquire(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        info = self._read()
        if (info is not None and int(info.get("pid", -1)) != os.getpid()
                and not self._is_stale(info, now)):
            return False
        self._write(now)
        self._held = True
        return True

    def heartbeat(self, now: float | None = None) -> None:
        if self._held:
            self._write(time.time() if now is None else now)

    def release(self) -> None:
        if not self._held:
            return
        try:
            info = self._read()
            if info and int(info.get("pid", -1)) == os.getpid() and self.path.exists():
                self.path.unlink()              # снимаем только свой лок
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось снять лок %s: %s", self.path, e)
        self._held = False

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise LockHeld(f"Лок уже удерживается другим процессом: {self.path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
