from pathlib import Path

from nekro_agent.core.os_env import OsEnv

PLUGIN_DEV_DIR = Path(OsEnv.DATA_DIR) / "plugin_dev"
PLUGIN_DEV_HISTORY_DIR = PLUGIN_DEV_DIR / "history"
PLUGIN_DEV_PROPOSAL_DIR = PLUGIN_DEV_DIR / "proposals"
PLUGIN_DEV_TASK_DIR = PLUGIN_DEV_DIR / "tasks"
PLUGIN_DEV_VERSION_PATH = PLUGIN_DEV_DIR / "version.json"
PLUGIN_DEV_SANDBOX_STATE_PATH = PLUGIN_DEV_DIR / "sandbox_state.json"
PLUGIN_DEV_WORKSPACE_DIR = PLUGIN_DEV_DIR / "sandbox_workspace"
PLUGIN_DEV_SOURCE_CACHE_DIR = PLUGIN_DEV_DIR / "source_cache"
PLUGIN_DEV_NEKRO_SOURCE_DIR = PLUGIN_DEV_SOURCE_CACHE_DIR / "nekro-agent"
