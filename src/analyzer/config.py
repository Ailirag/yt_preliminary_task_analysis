"""Загрузка и валидация конфигов analyzer.yaml / providers.yaml (pydantic)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


# ---------- analyzer.yaml ----------

class SubtaskCfg(BaseModel):
    type: str = "task"
    summary_prefix: str
    unique_prefix: str


class ComplexityTags(BaseModel):
    simple: str
    complex: str


class BugsCfg(BaseModel):
    selection: Literal["no-done-tag", "trigger-tag"] = "no-done-tag"
    trigger_tag: str
    done_tag: str
    complexity_tags: ComplexityTags
    subtask: SubtaskCfg


class FtCfg(BaseModel):
    trigger_tag: str
    done_tag: str
    require_doc_link: bool = True
    complexity_tags: ComplexityTags
    subtask: SubtaskCfg


class LimitsCfg(BaseModel):
    max_issues_per_run: int = 5
    max_images_per_issue: int = 8
    max_image_mb: int = 15
    max_wiki_pages_per_issue: int = 5
    max_tool_steps: int = 15
    max_comment_chars: int = 30000
    max_wiki_chars: int = 40000
    max_tool_result_chars: int = 20000
    max_vision_calls_per_issue: int = 20   # потолок обращений к vision на один анализ (осн.+связанные)
    throttle_between_issues_s: int = 10
    max_consecutive_errors: int = 3


class OnecCfg(BaseModel):
    enabled: bool = True
    vecgraph_dir: str = "vendor/onec-vecgraph"
    dump_path: str = ""
    command: str = "uv"
    args: list[str] = Field(default_factory=lambda: ["run", "--directory", "{vecgraph_dir}", "onec-lite"])
    env: dict[str, str] = Field(default_factory=dict)
    tool_whitelist: list[str] = Field(default_factory=list)
    start_timeout_s: int = 120
    call_timeout_s: int = 120

    def resolved_args(self, project_root: Path) -> list[str]:
        vec = str((project_root / self.vecgraph_dir).resolve())
        args = [a.format(vecgraph_dir=vec, dump_path=self.dump_path) for a in self.args]
        # если dump_path не задан — выбрасываем пустую пару "--root ''"
        cleaned: list[str] = []
        skip_next = False
        for i, a in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if a == "--root" and (i + 1 >= len(args) or not args[i + 1].strip()):
                skip_next = True
                continue
            cleaned.append(a)
        return cleaned


class WikiCfg(BaseModel):
    api: str = "https://api.wiki.yandex.net"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["wiki.yandex.ru"])
    doc_field: str = "documentationLink"


class TrackerCfg(BaseModel):
    base_url: str = "https://api.tracker.yandex.net"
    token_env: str = "YATRACKER_TOKEN_GT"
    org_id_env: str = "YATRACKER_ORGID_GT"
    org_header: str = "X-Org-ID"


class ReportCfg(BaseModel):
    language: str = "ru"
    disclaimer: str


class PathsCfg(BaseModel):
    work_dir: str = "work"
    journal_dir: str = "journal"


class NavigationCfg(BaseModel):
    """Read-only инструменты, которыми аналитик сам ходит по трекеру/вики в агентском цикле."""
    enabled: bool = True
    tools: list[str] = Field(default_factory=lambda: ["get_issue", "search_issues", "get_wiki"])
    allowed_queues: list[str] = Field(default_factory=list)  # пусто -> [queue]; очереди, разрешённые агенту
    max_issue_chars: int = 6000        # усечение описания/комментариев связанной задачи
    max_search_results: int = 10


class AnalyzerCfg(BaseModel):
    queue: str
    component_name: str
    mode: Literal["dry-run", "live"] = "dry-run"
    bugs: BugsCfg
    ft: FtCfg
    limits: LimitsCfg
    onec: OnecCfg
    wiki: WikiCfg
    tracker: TrackerCfg
    report: ReportCfg
    paths: PathsCfg
    navigation: NavigationCfg = Field(default_factory=NavigationCfg)


# ---------- providers.yaml ----------

class ModelCaps(BaseModel):
    tools: bool = True
    vision: bool = False
    force_first_tool: bool = False   # tool_choice=required на первом ходу (для моделей, что ленятся звать tools)
    price_in: float | None = None      # ₽ за 1000 входящих токенов (вкл. НДС); None — цена неизвестна
    price_out: float | None = None     # ₽ за 1000 исходящих токенов (вкл. НДС)
    price_cached: float | None = None  # ₽ за 1000 кешированных входящих токенов (дешевле price_in)
    price_tools: float | None = None   # ₽ за 1000 токенов инструментов (Responses отдаёт tool_tokens)


class ProviderCfg(BaseModel):
    kind: Literal["openai-compat", "anthropic", "openai-responses"]
    base_url: str | None = None
    api_key_env: str
    model_uri_template: str | None = None
    folder_id_env: str | None = None
    models: dict[str, ModelCaps]

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class LLMLimits(BaseModel):
    max_output_tokens: int = 8000
    request_timeout_s: int = 300
    retries: int = 3


class RolesCfg(BaseModel):
    analyst: str
    vision: str = ""


class ProfileCfg(BaseModel):
    """Именованный сценарий: какие модели на роли analyst/vision."""
    analyst: str
    vision: str = ""


class ProvidersCfg(BaseModel):
    roles: RolesCfg
    profiles: dict[str, ProfileCfg] = Field(default_factory=dict)
    default_profile: str | None = None
    limits: LLMLimits = Field(default_factory=LLMLimits)
    providers: dict[str, ProviderCfg]

    def effective_roles(self, profile_name: str | None = None) -> tuple[str, str]:
        """Спеки (analyst, vision) с учётом профиля: аргумент > default_profile > roles."""
        name = profile_name or self.default_profile
        if name:
            p = self.profiles.get(name)
            if p is None:
                raise ValueError(
                    f"Профиль {name!r} не найден в providers.yaml (доступны: {sorted(self.profiles)})"
                )
            return p.analyst, p.vision
        return self.roles.analyst, self.roles.vision

    def resolve(self, role_spec: str) -> tuple[str, ProviderCfg, str, ModelCaps]:
        """'провайдер/модель' -> (имя провайдера, конфиг, имя модели, capabilities)."""
        if "/" not in role_spec:
            raise ValueError(f"Роль должна быть в формате 'провайдер/модель': {role_spec!r}")
        pname, model = role_spec.split("/", 1)
        if pname not in self.providers:
            raise ValueError(f"Провайдер {pname!r} не описан в providers.yaml")
        pcfg = self.providers[pname]
        caps = pcfg.models.get(model)
        if caps is None:
            # неизвестная модель — допускаем, но с дефолтными caps (tools=True, vision=False)
            caps = ModelCaps()
        return pname, pcfg, model, caps


# ---------- загрузка ----------

def project_root() -> Path:
    # src/analyzer/config.py -> корень проекта
    return Path(__file__).resolve().parents[2]


def load_configs(config_dir: str | Path | None = None) -> tuple[AnalyzerCfg, ProvidersCfg]:
    root = project_root()
    cdir = Path(config_dir) if config_dir else root / "config"
    with open(cdir / "analyzer.yaml", encoding="utf-8") as f:
        acfg = AnalyzerCfg.model_validate(yaml.safe_load(f))
    with open(cdir / "providers.yaml", encoding="utf-8") as f:
        pcfg = ProvidersCfg.model_validate(yaml.safe_load(f))
    return acfg, pcfg
