"""Configuration loading for VPA.

Runtime config is TOML so project defaults can live on disk instead of being
repeated in long shell commands.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("vpa.toml")


@dataclass(frozen=True)
class LLMSettings:
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str = "VPA_API_KEY"
    max_completion_tokens: int = 1000000


@dataclass(frozen=True)
class VPASettings:
    upstream_repo: Path | None = None
    local_repo: Path | None = None
    target_isa_path: Path = Path("src/dynarec/sw64_core3")
    reference_isa_path: Path = Path("src/dynarec/rv64")
    fallback_reference_isa_paths: list[Path] = field(default_factory=list)
    build_command: str | None = None
    smoke_commands: list[str] = field(default_factory=list)
    verify_command: str | None = None
    merge_source: str = "upstream/main"
    ledger_path: Path | None = None
    report_path: Path | None = None
    risk_preference: str = "balanced"
    llm: LLMSettings = field(default_factory=LLMSettings)


def load_settings(path: Path | None) -> VPASettings:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return VPASettings()
    with config_path.open("rb") as file:
        data = tomllib.load(file)
    return settings_from_dict(data)


def settings_from_dict(data: dict[str, Any]) -> VPASettings:
    promotion = _table(data, "promotion")
    isa = _table(data, "isa")
    validation = _table(data, "validation")
    output = _table(data, "output")
    gate = _table(data, "gate")
    llm = _table(data, "llm")

    return VPASettings(
        upstream_repo=_optional_path(promotion.get("upstream_repo")),
        local_repo=_optional_path(promotion.get("local_repo")),
        merge_source=str(promotion.get("merge_source", "upstream/main")),
        target_isa_path=_path(isa.get("target_isa_path"), "src/dynarec/sw64_core3"),
        reference_isa_path=_path(isa.get("reference_isa_path"), "src/dynarec/rv64"),
        fallback_reference_isa_paths=[
            Path(path) for path in _string_list(isa.get("fallback_reference_isa_paths"))
        ],
        build_command=_optional_string(validation.get("build_command")),
        smoke_commands=_string_list(validation.get("smoke_commands")),
        verify_command=_optional_string(validation.get("verify_command")),
        ledger_path=_optional_path(output.get("ledger_path")),
        report_path=_optional_path(output.get("report_path")),
        risk_preference=str(gate.get("risk_preference", "balanced")),
        llm=LLMSettings(
            model=_optional_string(llm.get("model")),
            base_url=_optional_string(llm.get("base_url")),
            api_key=_optional_string(llm.get("api_key")),
            api_key_env=str(llm.get("api_key_env", "VPA_API_KEY")),
            max_completion_tokens=int(llm.get("max_completion_tokens", 1000000)),
        ),
    )


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Config section [{key}] must be a table")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(str(value))


def _path(value: Any, fallback: str) -> Path:
    if value is None:
        return Path(fallback)
    return Path(str(value))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("Config value must be a list of strings")
