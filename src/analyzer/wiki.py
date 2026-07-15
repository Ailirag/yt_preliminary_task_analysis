"""Клиент Yandex Wiki (тот же OAuth-токен и X-Org-ID, что и у трекера)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger("analyzer.wiki")

_WIKI_URL_RE = re.compile(r"https?://(?:[\w.-]+\.)?wiki\.yandex\.ru/[^\s)\]>\"'|»]+", re.IGNORECASE)


def extract_wiki_urls(text: str, allowed_hosts: list[str]) -> list[str]:
    """Достаёт ссылки на вики из текста (описание/комментарии), фильтруя по allowed_hosts."""
    urls: list[str] = []
    for m in _WIKI_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:")
        host = urlparse(url).netloc.lower()
        if any(host == h or host.endswith("." + h) for h in allowed_hosts):
            if url not in urls:
                urls.append(url)
    return urls


class WikiClient:
    def __init__(self, api_base: str, token: str, org_id: str, org_header: str):
        self.api_base = api_base.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"OAuth {token}", org_header: org_id},
            timeout=30.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def slug_from_url(url: str) -> str:
        path = urlparse(url).path
        return path.strip("/")

    def get_page(self, url: str, max_chars: int) -> dict:
        """Возвращает {url, slug, title, content, error}. Ошибки доступа — мягкая деградация."""
        slug = self.slug_from_url(url)
        result = {"url": url, "slug": slug, "title": "", "content": "", "error": None}
        if not slug:
            result["error"] = "пустой slug"
            return result
        try:
            resp = self._client.get(
                f"{self.api_base}/v1/pages",
                params={"slug": slug, "fields": "content,title"},
            )
            if resp.status_code == 403:
                result["error"] = "нет доступа (403)"
                return result
            if resp.status_code == 404:
                result["error"] = "страница не найдена (404)"
                return result
            resp.raise_for_status()
            data = resp.json()
            # API может вернуть объект страницы либо обёртку со списком
            if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
                data = data["results"][0] if data["results"] else {}
            result["title"] = str(data.get("title", ""))
            content = data.get("content") or ""
            if isinstance(content, dict):
                content = content.get("body") or content.get("raw") or str(content)
            content = str(content)
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n[... усечено, всего {len(content)} символов]"
            result["content"] = content
        except httpx.HTTPError as e:
            result["error"] = f"ошибка запроса: {e}"
        return result
