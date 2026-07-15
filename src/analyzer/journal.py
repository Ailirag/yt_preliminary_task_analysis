"""Журналы прогонов: runs.jsonl (по строке на задачу) и writes.jsonl (аудит записей в трекер)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("analyzer")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Journal:
    def __init__(self, journal_dir: Path, run_id: str):
        self.dir = journal_dir
        self.run_id = run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "dry-run").mkdir(exist_ok=True)

    def _append(self, filename: str, obj: dict) -> None:
        obj = {"ts": now_iso(), "run_id": self.run_id, **obj}
        with open(self.dir / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def run_event(self, **kwargs) -> None:
        self._append("runs.jsonl", kwargs)

    def write_event(self, **kwargs) -> None:
        """Аудит каждой live-записи в трекер."""
        self._append("writes.jsonl", kwargs)

    def dry_run_report(self, issue_key: str, markdown: str) -> Path:
        path = self.dir / "dry-run" / f"{issue_key}.md"
        path.write_text(markdown, encoding="utf-8")
        return path


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # приглушаем болтливые библиотеки
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
