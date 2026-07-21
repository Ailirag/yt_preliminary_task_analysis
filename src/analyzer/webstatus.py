"""Лёгкая read-only веб-страница статуса анализатора (только stdlib, без зависимостей).

Работает фоновым потоком внутри демона (`analyzer watch`); отдаёт HTML с автообновлением и
`/status.json`. Данные даёт демон через колбэк snapshot_fn() -> dict (живое состояние + файлы).
Сервер только ЧИТАЕТ; никаких действий. В контейнере слушает 0.0.0.0, наружу порт публикуется
compose на 127.0.0.1 (+ SSH-туннель) — сетевого доступа извне нет.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("analyzer.web")


@dataclass
class DaemonStats:
    """Живые метрики цикла демона (обновляются главным потоком, читаются веб-потоком)."""
    start_ts: float
    tick: int = 0
    last_tick_ts: float = 0.0
    backoff_s: int = 0
    last_error: str = ""


# ---------- форматирование ----------

def _age(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 90:
        return f"{s}с"
    if s < 5400:
        return f"{s // 60}м {s % 60:02d}с"
    return f"{s // 3600}ч {(s % 3600) // 60:02d}м"


def _esc(v) -> str:
    return html.escape("—" if v is None else str(v))


def _num(v, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 2)
    return f"{v}{suffix}"


# ---------- рендер ----------

def render_json(snap: dict) -> bytes:
    return json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")


def _kv_rows(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)


def render_html(snap: dict, refresh_s: int = 10) -> bytes:
    d = snap.get("daemon", {})
    w = snap.get("watch", {})
    b = snap.get("budget", {})
    lim = snap.get("limits", {})
    today = snap.get("today", {})
    onec = snap.get("onec", {})

    err = d.get("last_error") or ""
    backoff = d.get("backoff_s") or 0
    daemon_rows = _kv_rows([
        ("Состояние", f'<span class="ok">работает</span>' if not err
         else f'<span class="warn">ошибка тика</span>'),
        ("Аптайм", _age(d.get("uptime_s"))),
        ("Тиков", _num(d.get("tick"))),
        ("Последний тик", _age(d.get("last_tick_s_ago")) + " назад" if d.get("last_tick_s_ago") is not None else "—"),
        ("Backoff", f'<span class="warn">{backoff}с</span>' if backoff else "—"),
        ("Последняя ошибка", f'<span class="warn">{_esc(err)}</span>' if err else "—"),
    ])
    watch_rows = _kv_rows([
        ("Режим", _esc(snap.get("mode"))),
        ("Профиль", _esc(snap.get("profile"))),
        ("Воркфлоу/отбор", f"{_esc(w.get('workflow'))} / {_esc(w.get('selection'))}"),
        ("Интервал", _num(w.get("interval_s"), "с")),
        ("Параллельно", _num(w.get("concurrency"))),
        ("Окно работы", _esc(w.get("work_hours") or "24/7")),
    ])

    # бюджет — с полоской
    spent, budget, rem = b.get("spent"), b.get("budget"), b.get("remaining")
    ccy = b.get("currency", "")
    if budget:
        pct = min(100, round(100 * float(spent or 0) / float(budget)))
        bar = (f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'
               f'<div class="muted">{_num(spent)} / {_num(budget)} {_esc(ccy)} · осталось {_num(rem)} {_esc(ccy)} ({pct}%)</div>')
    else:
        bar = f'<div class="muted">потрачено {_num(spent)} {_esc(ccy)} (дневной лимит не задан)</div>'

    # лимиты: авторы
    authors = lim.get("authors") or []
    if authors:
        pa = lim.get("per_author_limit") or 0
        arows = "".join(
            f'<tr><td>{_esc(a.get("uid"))}</td><td>{_num(a.get("count"))}'
            + (f" / {pa}" if pa else "") + "</td></tr>" for a in authors)
        authors_tbl = f'<table class="grid"><tr><th>Автор (uid)</th><th>Разборов сегодня</th></tr>{arows}</table>'
    else:
        authors_tbl = '<div class="muted">сегодня разборов по авторам ещё нет</div>'
    rl = lim.get("rate_limited_today") or 0
    dfr = lim.get("deferred_count")
    lim_rows = _kv_rows([
        ("Лимит на автора/сутки", _num(lim.get("per_author_limit")) if lim.get("per_author_limit") else "—"),
        ("Отложено по лимиту", _num(dfr) if dfr is not None else "н/д"),
        ("Упёрлось в 429 сегодня", f'<span class="warn">{rl}</span>' if rl else "0"),
    ])

    # в работе
    ip = snap.get("in_progress") or []
    conc = w.get("concurrency") or "?"
    if ip:
        iprows = "".join(f'<tr><td>{_esc(t.get("key"))}</td><td>{_esc(t.get("workflow"))}</td>'
                         f'<td>{_age(t.get("age_s"))}</td></tr>' for t in ip)
        ip_html = (f'<div class="muted">занято слотов {len(ip)}/{conc}</div>'
                   f'<table class="grid"><tr><th>Задача</th><th>Воркфлоу</th><th>В работе</th></tr>{iprows}</table>')
    else:
        ip_html = f'<div class="muted">сейчас разборов нет (слотов {conc})</div>'

    # сегодня
    acts = today.get("actions") or {}
    trust = today.get("trust") or {}
    cost_ccy = today.get("cost_by_ccy") or {}
    today_rows = _kv_rows([
        ("Прогонов", _num(today.get("runs"))),
        ("Действия", ", ".join(f"{_esc(k)}={v}" for k, v in sorted(acts.items())) or "—"),
        ("Доверие", ", ".join(f"{_esc(k)}={v}" for k, v in trust.items()) or "—"),
        ("Средняя уверенность", _num(today.get("avg_confidence"))),
        ("Стоимость", ", ".join(f"{_num(v)} {_esc(k)}" for k, v in cost_ccy.items()) or "—"),
        ("Средн. стоимость/разбор", _num(today.get("avg_cost"))),
        ("Средн. время/разбор", _age(today.get("avg_duration_s"))),
        ("Пропускная способность", _num(today.get("throughput_per_h"), " задач/ч") if today.get("throughput_per_h") is not None else "—"),
    ])

    # onec-lite
    wss = onec.get("workspaces") or []
    if wss:
        wsrows = "".join(f'<tr><td>{_esc(s.get("name"))}</td><td>{_esc(s.get("workspace"))}</td>'
                         f'<td>{_esc(s.get("revision"))}</td></tr>' for s in wss)
        ws_tbl = f'<table class="grid"><tr><th>Система</th><th>Воркспейс</th><th>Ревизия зеркала</th></tr>{wsrows}</table>'
    else:
        ws_tbl = ""
    onec_rows = _kv_rows([
        ("Подключён", f'<span class="ok">да</span>' if onec.get("available")
         else f'<span class="warn">нет</span>'),
        ("Инструментов", _num(onec.get("tools"))),
    ])

    # последние прогоны
    recent = snap.get("recent") or []
    if recent:
        rrows = "".join(
            f'<tr><td>{_esc(r.get("time"))}</td><td>{_esc(r.get("issue"))}</td>'
            f'<td>{_esc(r.get("action"))}</td><td>{_esc(r.get("trust") or "—")}</td>'
            f'<td>{_num(r.get("cost"))} {_esc(r.get("currency") or "")}</td>'
            f'<td>{_esc(r.get("subtask") or "—")}</td></tr>' for r in recent)
        recent_tbl = (f'<table class="grid"><tr><th>Время</th><th>Задача</th><th>Действие</th>'
                      f'<th>Доверие</th><th>Стоимость</th><th>Подзадача</th></tr>{rrows}</table>')
    else:
        recent_tbl = '<div class="muted">сегодня прогонов ещё не было</div>'

    pending = snap.get("queue_pending")
    pending_html = (f'<span class="big">{pending}</span> задач ждут анализа'
                    if pending is not None else '<span class="muted">н/д (нет связи с трекером)</span>')

    doc = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh_s}">
<title>Анализатор — статус</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 18px; background:#0f1115; color:#e6e6e6; }}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing:.04em; color:#8aa; margin: 0 0 8px; }}
.top {{ color:#9aa; margin-bottom: 16px; font-size: 12px; }}
.grid-cols {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(320px,1fr)); gap:14px; }}
.card {{ background:#171a21; border:1px solid #262b36; border-radius:10px; padding:14px 16px; overflow-x:auto; }}
table {{ border-collapse: collapse; width:100%; }}
table.kv th {{ text-align:left; color:#9aa; font-weight:500; padding:3px 12px 3px 0; white-space:nowrap; vertical-align:top; }}
table.kv td {{ padding:3px 0; }}
table.grid th {{ text-align:left; color:#8aa; font-weight:600; border-bottom:1px solid #2a3040; padding:5px 10px 5px 0; }}
table.grid td {{ padding:4px 10px 4px 0; border-bottom:1px solid #20242e; }}
.ok {{ color:#5fd28a; }} .warn {{ color:#ff8a7a; }}
.muted {{ color:#8a91a0; font-size:12px; }}
.big {{ font-size:22px; font-weight:700; }}
.bar {{ background:#262b36; border-radius:6px; height:10px; overflow:hidden; margin-bottom:6px; }}
.fill {{ background:linear-gradient(90deg,#4a9,#5fd28a); height:100%; }}
</style></head><body>
<h1>ИИ-анализатор — статус</h1>
<div class="top">{_esc(snap.get('now'))} · обновление каждые {refresh_s}с · <a href="/status.json" style="color:#6ac">/status.json</a></div>
<div class="grid-cols">
  <div class="card"><h2>Демон</h2><table class="kv">{daemon_rows}</table></div>
  <div class="card"><h2>Конфигурация</h2><table class="kv">{watch_rows}</table></div>
  <div class="card"><h2>Очередь</h2><p>{pending_html}</p><h2 style="margin-top:12px">Бюджет</h2>{bar}</div>
  <div class="card"><h2>Лимиты</h2><table class="kv">{lim_rows}</table><div style="margin-top:8px">{authors_tbl}</div></div>
  <div class="card"><h2>В работе</h2>{ip_html}</div>
  <div class="card"><h2>Сегодня</h2><table class="kv">{today_rows}</table></div>
  <div class="card"><h2>Код (onec-lite)</h2><table class="kv">{onec_rows}</table><div style="margin-top:8px">{ws_tbl}</div></div>
  <div class="card" style="grid-column:1/-1"><h2>Последние прогоны</h2>{recent_tbl}</div>
</div>
</body></html>"""
    return doc.encode("utf-8")


# ---------- сервер ----------

class _Handler(BaseHTTPRequestHandler):
    server_version = "analyzer-status/1.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            snap = self.server.snapshot_fn()  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("web: снимок статуса не собрался: %s", e)
            self._send(500, b"status snapshot error", "text/plain; charset=utf-8")
            return
        try:
            if path in ("/status.json", "/status"):
                self._send(200, render_json(snap), "application/json; charset=utf-8")
            elif path == "/":
                self._send(200, render_html(snap, self.server.refresh_s),  # type: ignore[attr-defined]
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("web: рендер не удался: %s", e)
            self._send(500, b"render error", "text/plain; charset=utf-8")

    def log_message(self, *args) -> None:  # глушим стандартный access-лог в stderr
        pass


class StatusServer:
    """Фоновый HTTP-сервер статуса. start() неблокирующий; stop() корректно гасит."""

    def __init__(self, host: str, port: int, snapshot_fn, refresh_s: int = 10):
        self.host = host
        self.port = port
        self.snapshot_fn = snapshot_fn
        self.refresh_s = refresh_s
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            log.warning("web: не удалось занять %s:%s (%s) — страница статуса недоступна",
                        self.host, self.port, e)
            return False
        httpd.snapshot_fn = self.snapshot_fn      # type: ignore[attr-defined]
        httpd.refresh_s = self.refresh_s          # type: ignore[attr-defined]
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="status-web", daemon=True)
        self._thread.start()
        log.info("web: страница статуса на http://%s:%s (внутри контейнера; наружу — публикация порта + SSH-туннель)",
                 self.host, self.port)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
