"""REST-клиент Yandex Tracker c write-guard.

Правила записи (write-guard):
  1. Существующие задачи: разрешено ТОЛЬКО добавление/удаление тегов.
  2. Создание задач: только подзадача с компонентой «ИИ анализ» и unique-ключом.
  3. Полный доступ (описание и пр.) — только к задачам, созданным в ТЕКУЩЕМ прогоне.
Любая запись выполняется только в live-режиме; в dry-run намерение пишется в журнал.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .journal import Journal

log = logging.getLogger("analyzer.tracker")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class WriteGuardError(RuntimeError):
    """Попытка недопустимой записи в трекер — прогон останавливается."""


class DryRunResult(str):
    """Маркер результата записи в dry-run режиме."""


class TrackerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        org_id: str,
        org_header: str,
        live: bool,
        journal: Journal | None = None,
        max_retries: int = 5,
    ):
        self.base_url = base_url.rstrip("/")
        self.live = live
        self.journal = journal
        self.max_retries = max_retries
        self._created_this_run: set[str] = set()  # реестр write-guard
        self._client = httpx.Client(
            headers={
                "Authorization": f"OAuth {token}",
                org_header: org_id,
            },
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    @classmethod
    def from_env(cls, cfg, live: bool, journal: Journal | None = None) -> "TrackerClient":
        token = os.environ.get(cfg.token_env)
        org_id = os.environ.get(cfg.org_id_env)
        if not token or not org_id:
            raise RuntimeError(
                f"Не заданы переменные окружения {cfg.token_env} / {cfg.org_id_env}"
            )
        return cls(cfg.base_url, token, org_id, cfg.org_header, live=live, journal=journal)

    def close(self) -> None:
        self._client.close()

    # ---------- низкий уровень ----------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        delay = 2.0
        last: httpx.Response | None = None
        for attempt in range(self.max_retries):
            resp = self._client.request(method, url, **kwargs)
            if resp.status_code not in RETRY_STATUSES:
                return resp
            last = resp
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            log.warning("HTTP %s %s -> %s, повтор через %.0fс (попытка %d/%d)",
                        method, path, resp.status_code, wait, attempt + 1, self.max_retries)
            time.sleep(wait)
            delay = min(delay * 2, 60)
        assert last is not None
        return last

    def _json(self, method: str, path: str, **kwargs) -> Any:
        resp = self._request(method, path, **kwargs)
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    # ---------- чтение ----------

    def myself(self) -> dict:
        return self._json("GET", "/v2/myself")

    def get_queue(self, queue: str) -> dict:
        return self._json("GET", f"/v2/queues/{queue}")

    def get_components(self, queue: str) -> list[dict]:
        return self._json("GET", f"/v2/queues/{queue}/components") or []

    def get_issue(self, key: str) -> dict:
        return self._json("GET", f"/v2/issues/{key}")

    def get_comments(self, key: str) -> list[dict]:
        return self._json("GET", f"/v2/issues/{key}/comments") or []

    def get_links(self, key: str) -> list[dict]:
        return self._json("GET", f"/v2/issues/{key}/links") or []

    def get_attachments(self, key: str) -> list[dict]:
        return self._json("GET", f"/v2/issues/{key}/attachments") or []

    def download_attachment(self, key: str, att: dict) -> bytes:
        url = att.get("content") or f"{self.base_url}/v2/issues/{key}/attachments/{att['id']}/{att['name']}"
        resp = self._request("GET", url)
        resp.raise_for_status()
        return resp.content

    def search(self, query: str, per_page: int = 50, max_pages: int = 4) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._json(
                "POST",
                f"/v2/issues/_search?perPage={per_page}&page={page}",
                json={"query": query},
            ) or []
            results.extend(batch)
            if len(batch) < per_page:
                break
        return results

    def count(self, query: str) -> int:
        return int(self._json("POST", "/v2/issues/_count", json={"query": query}))

    def get_changelog(self, key: str, field: str | None = None,
                      per_page: int = 100, max_pages: int = 10) -> list[dict]:
        """История изменений задачи (кто что менял from->to и когда).
        field — фильтр по полю трекера (напр. 'tags'). Cursor-пагинация по заголовку Link."""
        results: list[dict] = []
        url = f"{self.base_url}/v2/issues/{key}/changelog"
        params: dict[str, Any] | None = {"perPage": per_page}
        if field:
            params["field"] = field
        for _ in range(max_pages):
            resp = self._request("GET", url, params=params)
            resp.raise_for_status()
            batch = resp.json() if resp.content else []
            results.extend(batch)
            nxt = resp.links.get("next", {}).get("url")
            if not nxt or not batch:
                break
            url, params = nxt, None  # следующий URL уже содержит курсор
        return results

    def get_users(self, per_page: int = 1000, max_pages: int = 50) -> list[dict]:
        """Справочник пользователей (uid, email, login, display, dismissed).
        uid совпадает с changelog updatedBy.id. Пагинация по заголовку Link."""
        results: list[dict] = []
        url = f"{self.base_url}/v2/users"
        params: dict[str, Any] | None = {"perPage": per_page}
        for _ in range(max_pages):
            resp = self._request("GET", url, params=params)
            resp.raise_for_status()
            batch = resp.json() if resp.content else []
            results.extend(batch)
            nxt = resp.links.get("next", {}).get("url")
            if not nxt or not batch:
                break
            url, params = nxt, None  # следующий URL уже содержит курсор
        return results

    # ---------- write-guard ----------

    def _guard_tags_only(self, key: str, payload: dict) -> None:
        if key in self._created_this_run:
            return
        extra = set(payload.keys()) - {"tags"}
        if extra:
            raise WriteGuardError(
                f"Write-guard: попытка изменить поля {sorted(extra)} существующей задачи {key}. "
                f"На существующих задачах разрешены только теги."
            )

    def _guard_create(self, payload: dict, component_id: int | None, unique: str | None) -> None:
        if not payload.get("parent"):
            raise WriteGuardError("Write-guard: создание задачи без parent запрещено (только подзадачи).")
        if not component_id:
            raise WriteGuardError("Write-guard: создание подзадачи без компоненты «ИИ анализ» запрещено.")
        if not unique:
            raise WriteGuardError("Write-guard: создание подзадачи без unique-ключа запрещено.")

    def finish_iteration(self) -> None:
        """Полный доступ к созданным подзадачам действует только на время итерации."""
        self._created_this_run.clear()

    # ---------- запись (live-gated) ----------

    def update_tags(self, key: str, add: list[str] | None = None, remove: list[str] | None = None):
        tags: dict[str, list[str]] = {}
        if add:
            tags["add"] = add
        if remove:
            tags["remove"] = remove
        if not tags:
            return None
        payload = {"tags": tags}
        self._guard_tags_only(key, payload)
        if not self.live:
            log.info("[DRY-RUN] PATCH %s tags=%s", key, tags)
            if self.journal:
                self.journal.write_event(op="update_tags", issue=key, payload=payload, dry_run=True)
            return DryRunResult("dry-run")
        result = self._json("PATCH", f"/v2/issues/{key}", json=payload)
        if self.journal:
            self.journal.write_event(op="update_tags", issue=key, payload=payload, dry_run=False)
        log.info("Теги обновлены: %s %s", key, tags)
        return result

    def create_subtask(
        self,
        queue: str,
        parent: str,
        summary: str,
        description: str,
        issue_type: str,
        component_id: int,
        unique: str,
    ) -> str | None:
        payload = {
            "queue": queue,
            "parent": parent,
            "summary": summary,
            "description": description,
            "type": issue_type,
            "components": [component_id],
            "unique": unique,
        }
        self._guard_create(payload, component_id, unique)
        if not self.live:
            log.info("[DRY-RUN] POST /v2/issues parent=%s summary=%r unique=%s", parent, summary, unique)
            if self.journal:
                self.journal.write_event(
                    op="create_subtask", issue=parent, dry_run=True,
                    payload={k: v for k, v in payload.items() if k != "description"},
                    description_chars=len(description),
                )
            return None
        resp = self._request("POST", "/v2/issues", json=payload)
        if resp.status_code == 409:
            # unique уже существует — идемпотентность: считаем успехом
            log.info("Подзадача с unique=%s уже существует (409) — пропускаю создание", unique)
            if self.journal:
                self.journal.write_event(op="create_subtask", issue=parent, dry_run=False,
                                         status=409, unique=unique)
            return None
        resp.raise_for_status()
        created = resp.json()
        key = created["key"]
        self._created_this_run.add(key)  # полный доступ до конца итерации
        if self.journal:
            self.journal.write_event(op="create_subtask", issue=parent, dry_run=False,
                                     created_key=key, unique=unique)
        log.info("Создана подзадача %s (родитель %s)", key, parent)
        return key

    def patch_created_issue(self, key: str, fields: dict):
        """Полный PATCH — разрешён ТОЛЬКО для подзадач, созданных в текущем прогоне."""
        if key not in self._created_this_run:
            raise WriteGuardError(
                f"Write-guard: полный PATCH {key} запрещён — задача не создавалась в этом прогоне."
            )
        if not self.live:
            log.info("[DRY-RUN] PATCH %s fields=%s", key, list(fields))
            return DryRunResult("dry-run")
        result = self._json("PATCH", f"/v2/issues/{key}", json=fields)
        if self.journal:
            self.journal.write_event(op="patch_created", issue=key, fields=list(fields), dry_run=False)
        return result

    def create_component(self, name: str, queue: str):
        """Одноразовая операция этапа 0 (analyzer init-component)."""
        payload = {"name": name, "queue": queue}
        if not self.live:
            log.info("[DRY-RUN] POST /v2/components %s", payload)
            return DryRunResult("dry-run")
        result = self._json("POST", "/v2/components", json=payload)
        if self.journal:
            self.journal.write_event(op="create_component", payload=payload, dry_run=False)
        return result

    # ---------- утилиты ----------

    def find_component_id(self, queue: str, name: str) -> int | None:
        for c in self.get_components(queue):
            if (c.get("name") or "").strip().lower() == name.strip().lower():
                return int(c["id"])
        return None

    def find_existing_ai_subtask(self, parent_key: str, component_name: str, summary_prefix: str) -> str | None:
        """Идемпотентность: ищем уже созданную ИИ-подзадачу у родителя."""
        try:
            results = self.search(f'Parent: {parent_key} Components: "{component_name}"', per_page=10, max_pages=1)
        except httpx.HTTPStatusError:
            results = []
        for issue in results:
            if issue.get("summary", "").startswith(summary_prefix):
                return issue.get("key")
        if results:
            return results[0].get("key")
        return None
