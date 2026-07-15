"""Модель результата анализа (общая для workflow bugs и ft) + нормализация JSON от LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cause: str = ""
    confidence: str = "средняя"
    basis: str = ""


class AffectedObject(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object: str = ""
    module: str = ""
    procedure: str = ""
    role: str = ""


class FtAspect(BaseModel):
    model_config = ConfigDict(extra="ignore")
    aspect: str = ""
    status: str = ""
    comment: str = ""


class MappingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    requirement: str = ""
    objects: str = ""
    notes: str = ""


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    reproduction: str = ""
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    affected_objects: list[AffectedObject] = Field(default_factory=list)
    complexity: Literal["simple", "complex"] = "complex"
    complexity_reason: str = ""
    draft_solution: str = ""
    missing_info: list[str] = Field(default_factory=list)
    code_refs: list[str] = Field(default_factory=list)
    notes: str = ""
    # поля workflow ft
    ft_completeness: list[FtAspect] = Field(default_factory=list)
    mapping: list[MappingItem] = Field(default_factory=list)
    implementation_plan: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def _coerce_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def normalize_analysis(data: dict) -> AnalysisResult:
    """Приводит JSON от LLM к модели, прощая типовые отклонения формата."""
    d = dict(data or {})
    # complexity: терпим русские варианты
    comp = str(d.get("complexity", "")).strip().lower()
    if comp in ("simple", "простая", "простой"):
        d["complexity"] = "simple"
    else:
        d["complexity"] = "complex"
    # списки строк
    for key in ("missing_info", "code_refs", "implementation_plan", "risks"):
        d[key] = _coerce_str_list(d.get(key))
    # списки объектов: строки -> объекты
    hyps = d.get("hypotheses") or []
    d["hypotheses"] = [h if isinstance(h, dict) else {"cause": str(h)} for h in hyps if h]
    objs = d.get("affected_objects") or []
    d["affected_objects"] = [o if isinstance(o, dict) else {"object": str(o)} for o in objs if o]
    fts = d.get("ft_completeness") or []
    d["ft_completeness"] = [a if isinstance(a, dict) else {"aspect": str(a)} for a in fts if a]
    maps = d.get("mapping") or []
    d["mapping"] = [m if isinstance(m, dict) else {"requirement": str(m)} for m in maps if m]
    # скалярные строки
    for key in ("summary", "reproduction", "complexity_reason", "draft_solution", "notes"):
        v = d.get(key)
        if isinstance(v, (list, dict)):
            d[key] = str(v)
        elif v is None:
            d[key] = ""
    return AnalysisResult.model_validate(d)
