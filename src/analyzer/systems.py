"""Генерация state-файла onec-lite (v2) из config.systems — единый источник правды.

Анализатор знает системы (name/workspace/repo|root/...); onec-lite читает СВОЙ state-файл
(`~/.onec-lite/config.json` или `ONEC_LITE_STATE`). Пишем этот JSON НАПРЯМУЮ, не импортируя
вендорный `onec_vecgraph` (у него отдельный venv). Схема совпадает с `admin.save_state` (version 2):
{"version": 2, "workspaces": {ws: {root, ext_roots, repo, branch, update_on_start}}, "active", ...}.

repo (зеркало) приоритетнее root (локальный путь): onec-lite клонирует зеркало в mirrors/<ws>.
update_on_start всегда "off" — обновляет только процесс `onec-lite sync` (единственный писатель git).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import AnalyzerCfg


def state_file_path(explicit: str | None = None) -> Path:
    """Путь к state-файлу onec-lite: аргумент > env ONEC_LITE_STATE > ~/.onec-lite/config.json
    (та же логика резолва, что в onec_vecgraph.lite.admin.state_file)."""
    if explicit and explicit.strip():
        return Path(explicit.strip())
    env = os.environ.get("ONEC_LITE_STATE", "").strip()
    return Path(env) if env else Path.home() / ".onec-lite" / "config.json"


def build_workspaces(acfg: AnalyzerCfg) -> tuple[dict[str, dict], list[str]]:
    """config.systems -> {workspace: entry} (v2) + список предупреждений (пропущенные системы)."""
    workspaces: dict[str, dict] = {}
    warnings: list[str] = []
    for s in acfg.systems:
        ws = (s.workspace or "").strip()
        if not ws:
            warnings.append(f"система «{s.name}» без workspace — пропущена")
            continue
        if ws in workspaces:
            warnings.append(f"дубликат workspace «{ws}» — пропущен повтор")
            continue
        repo = (s.repo or "").strip()
        root = (s.root or "").strip()
        if not repo and not root:
            warnings.append(f"воркспейс «{ws}» без repo и без root — пропущен (нужно одно из двух)")
            continue
        workspaces[ws] = {
            "root": "" if repo else root,   # repo приоритетнее: зеркало (root пустой, клон в mirrors/<ws>)
            "ext_roots": [],
            "repo": repo,
            "branch": (s.branch or "").strip(),
            "update_on_start": "off",       # обновляет только `onec-lite sync` — единственный писатель git
        }
    return workspaces, warnings


def write_state_file(acfg: AnalyzerCfg, explicit_path: str | None = None,
                     preserve: bool = True) -> tuple[Path, int, list[str]]:
    """Пишет v2 state-файл onec-lite из config.systems. preserve=True сохраняет platform_help/rg_path
    из существующего файла (их задаёт админка onec-lite, не мы). Возвращает (путь, число воркспейсов,
    предупреждения). active = первый воркспейс (дефолт сессии onec-lite, если не задан ONEC_LITE_WORKSPACE)."""
    path = state_file_path(explicit_path)
    workspaces, warnings = build_workspaces(acfg)
    active = next(iter(workspaces), "")
    platform_help: list = []
    rg_path = ""
    if preserve and path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                platform_help = saved.get("platform_help") or []
                rg_path = str(saved.get("rg_path") or "")
        except (OSError, ValueError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 2, "workspaces": workspaces, "active": active,
                    "platform_help": platform_help, "rg_path": rg_path},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return path, len(workspaces), warnings
