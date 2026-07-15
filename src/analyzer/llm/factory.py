"""Фабрика провайдеров: строит Provider по спецификации роли 'провайдер/модель'."""

from __future__ import annotations

from ..config import ProvidersCfg
from .anthropic_provider import AnthropicProvider
from .base import Provider
from .openai_compat import OpenAICompatProvider


def build_provider(pcfgs: ProvidersCfg, role_spec: str) -> Provider:
    pname, pcfg, model, caps = pcfgs.resolve(role_spec)
    api_key = pcfg.api_key()
    if not api_key:
        raise RuntimeError(
            f"Провайдер {pname!r}: не задана переменная окружения {pcfg.api_key_env}"
        )
    limits = pcfgs.limits
    if pcfg.kind == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=model,
            supports_vision=caps.vision,
            supports_tools=caps.tools,
            max_output_tokens=limits.max_output_tokens,
            timeout_s=limits.request_timeout_s,
            retries=limits.retries,
        )
    if pcfg.kind == "openai-compat":
        if not pcfg.base_url:
            raise RuntimeError(f"Провайдер {pname!r}: не задан base_url")
        return OpenAICompatProvider(
            name=pname,
            base_url=pcfg.base_url,
            api_key=api_key,
            model=model,
            supports_vision=caps.vision,
            supports_tools=caps.tools,
            max_output_tokens=limits.max_output_tokens,
            timeout_s=limits.request_timeout_s,
            retries=limits.retries,
            model_uri_template=pcfg.model_uri_template,
            folder_id_env=pcfg.folder_id_env,
        )
    raise RuntimeError(f"Неизвестный kind провайдера: {pcfg.kind}")
