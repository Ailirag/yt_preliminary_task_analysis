"""Разрешение записей белого списка авторов в идентификаторы трекера.

Запись в `bugs.trigger_authors` может быть задана как e-mail (предпочтительно),
uid или отображаемое имя. E-mail разрешается в uid (он совпадает с
`changelog.updatedBy.id`) через справочник пользователей трекера с файловым кешем
— «таблицей соответствия». Кеш содержит корпоративные данные (email/uid коллег),
поэтому лежит в work/ (в .gitignore), а не в конфиге.

ВАЖНО: в организации у одного человека может быть НЕСКОЛЬКО активных аккаунтов на
один e-mail (напр. основной и второй). Поэтому email резолвится в СПИСОК активных
uid, а совпадение автора тега засчитывается по ЛЮБОМУ из них — это тот же человек.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("analyzer.users")


def active_email_uids(users: list[dict]) -> dict[str, list[str]]:
    """email (в нижнем регистре) -> список uid активных (dismissed=False) аккаунтов."""
    idx: dict[str, list[str]] = {}
    for u in users:
        if u.get("dismissed"):
            continue
        email = (u.get("email") or "").strip().lower()
        uid = str(u.get("uid") or u.get("trackerUid") or "").strip()
        if not email or not uid:
            continue
        lst = idx.setdefault(email, [])
        if uid not in lst:
            lst.append(uid)
    return idx


class UserMap:
    """Кеш email->[uid] («таблица соответствия») с ленивым дозаполнением из трекера."""

    def __init__(self, cache_path: Path):
        self.cache_path = Path(cache_path)
        self._idx: dict[str, list[str]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.cache_path.exists():
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                # совместимость: старый формат email->uid (строка) -> email->[uid]
                self._idx = {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось прочитать кеш пользователей %s: %s", self.cache_path, e)
            self._idx = {}
        self._loaded = True

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._idx, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сохранить кеш пользователей %s: %s", self.cache_path, e)

    def resolve(self, emails: list[str], fetch_users: Callable[[], list[dict]]) -> dict[str, list[str]]:
        """email(lower)->[uid] для заданных email. Недостающие дозагружает, обновив кеш
        полным справочником из fetch_users(). Неразрешённые (не найдены/уволены) — в лог."""
        self._load()
        want = {e.strip().lower() for e in emails if e and "@" in e}
        if not want:
            return {}
        missing = want - set(self._idx)
        if missing:
            log.info("Обновляю таблицу соответствия пользователей (не хватает %d email)", len(missing))
            self._idx = active_email_uids(fetch_users())
            self._save()
        resolved = {e: self._idx[e] for e in want if e in self._idx}
        unresolved = want - set(resolved)
        if unresolved:
            log.warning("Не найдены активные пользователи для email: %s", ", ".join(sorted(unresolved)))
        return resolved
