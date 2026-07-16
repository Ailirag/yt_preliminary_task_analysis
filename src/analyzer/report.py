"""Рендер отчёта-подзадачи из результата анализа (jinja2 c нестандартными
разделителями, чтобы YFM-разметка трекера вида {% cut %} проходила насквозь)."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .models import AnalysisResult


def _env(templates_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_report(
    templates_dir: Path,
    workflow: str,               # bugs | ft
    result: AnalysisResult,
    *,
    parent_key: str,
    date: str,
    models: str,
    dump_rev: str,
    disclaimer: str,
    sources: dict,
    verdict=None,
    stats=None,
) -> str:
    template_name = "bug-report.md" if workflow == "bugs" else "ft-report.md"
    template = _env(templates_dir).get_template(template_name)
    return template.render(
        r=result,
        parent_key=parent_key,
        date=date,
        models=models,
        dump_rev=dump_rev,
        disclaimer=disclaimer,
        sources=sources,
        verdict=verdict,
        stats=stats,
    )
