from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from nekro_agent.services.plugin_dev.paths import PLUGIN_DEV_DIR

PLUGIN_DEV_CONFIG_PATH = PLUGIN_DEV_DIR / "config.json"


class PluginDevConfig(BaseModel):
    cc_model_preset_id: int | None = None
    source_enabled: bool = True
    source_repo_url: str = "https://github.com/KroMiose/nekro-agent.git"
    source_ref: str = "main"
    source_update_timeout: int = 120


def _read_json() -> dict[str, Any]:
    if not PLUGIN_DEV_CONFIG_PATH.exists():
        return {}
    data = json.loads(PLUGIN_DEV_CONFIG_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def get_plugin_dev_config() -> PluginDevConfig:
    return PluginDevConfig.model_validate(_read_json())


def update_plugin_dev_config(*, cc_model_preset_id: int | None) -> PluginDevConfig:
    config = get_plugin_dev_config().model_copy(update={"cc_model_preset_id": cc_model_preset_id})
    PLUGIN_DEV_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_DEV_CONFIG_PATH.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config
