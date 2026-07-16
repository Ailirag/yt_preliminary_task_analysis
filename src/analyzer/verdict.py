"""Вердикт доверия к результату модели — детерминированный пост-скорер.

Оценивает НЕ правильность диагноза (её не проверить без человека), а ОБОСНОВАННОСТЬ:
насколько ответ опирается на реальное исследование кода, а не на догадку по симптому.
Главный сигнал (из пилота): 0 обращений к коду + плейсхолдеры вместо объектов = «не доверять»
(ровно кейс YandexGPT/Alice); много вызовов + конкретные ссылки на код = «доверять»."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import AnalysisResult

# слова-маркеры «догадки/заглушки» в описании затронутых объектов
_PLACEHOLDER_MARKERS = (
    "неизвестн", "предположительно", "предполага", "требуется анализ", "требует анализа",
    "не установлен", "не определен", "не определён", "неясн", "гипотетич",
)


@dataclass
class Verdict:
    score: int              # 0..100
    level: str              # доверять | частично | не доверять
    reasons: list[str] = field(default_factory=list)


def _has_placeholder(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _PLACEHOLDER_MARKERS)


def score_result(result: AnalysisResult, *, tool_steps: int, code_available: bool,
                 hit_budget: bool, json_retried: bool) -> Verdict:
    score = 50
    reasons: list[str] = []

    def add(delta: int, text: str) -> None:
        nonlocal score
        score += delta
        reasons.append(f"{'+' if delta >= 0 else ''}{delta}: {text}")

    # 1. Обращения к коду 1С — ключевой сигнал обоснованности
    if code_available:
        if tool_steps == 0:
            add(-45, "ни одного обращения к коду (инструменты были доступны)")
        elif tool_steps <= 3:
            add(-10, f"мало обращений к коду ({tool_steps})")
        elif tool_steps >= 12:
            add(20, f"глубокое исследование кода ({tool_steps} вызовов)")
        else:
            add(10, f"исследование кода ({tool_steps} вызовов)")
    else:
        reasons.append("~0: инструменты кода 1С недоступны — анализ без кода, доверие ограничено")

    # 2. Ссылки на конкретный код (файл/процедура)
    n_refs = len(result.code_refs)
    if n_refs == 0:
        add(-20, "нет ссылок на конкретный код")
    elif n_refs >= 3:
        add(15, f"конкретные ссылки на код: {n_refs}")
    else:
        add(5, f"ссылки на код: {n_refs}")

    # 3. Затронутые объекты / маппинг: конкретика vs догадки
    objs = list(result.affected_objects) + list(result.mapping)
    if not objs:
        add(-15, "не указаны затронутые объекты конфигурации")
    else:
        blob = " ".join(
            f"{getattr(o, 'object', '')} {getattr(o, 'module', '')} {getattr(o, 'procedure', '')} "
            f"{getattr(o, 'role', '')} {getattr(o, 'requirement', '')} {getattr(o, 'objects', '')} "
            f"{getattr(o, 'notes', '')}"
            for o in objs
        )
        if _has_placeholder(blob):
            add(-20, "в затронутых объектах догадки/плейсхолдеры вместо конкретики")
        else:
            add(10, f"затронутые объекты указаны конкретно ({len(objs)})")

    # 4. Оверконфиденс: «простая» без единого обращения к коду
    if result.complexity == "simple" and tool_steps == 0:
        add(-10, "оценка «простая» без обращения к коду")

    # 5. Технические заминки
    if hit_budget:
        add(-5, "исчерпан бюджет шагов — анализ мог не завершиться")
    if json_retried:
        add(-5, "модель не сразу вернула валидный JSON")

    if not code_available:
        score = min(score, 55)   # без кода — не выше «частично»

    score = max(0, min(100, score))
    level = "доверять" if score >= 70 else ("частично" if score >= 40 else "не доверять")
    return Verdict(score=score, level=level, reasons=reasons)
