from pydantic import BaseModel

from nekro_agent.core.args import Args
from nekro_agent.core.os_env import OsEnv

try:
    from nonebot.plugin import PluginMetadata
except ModuleNotFoundError:
    if not OsEnv.CLI_MODE:
        raise

    class PluginMetadata:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


class _Config(BaseModel):
    pass


__plugin_meta__ = PluginMetadata(
    name="nekro-agent",
    description="集代码执行/高度可扩展性为一体的聊天机器人，应用了容器化技术快速构建沙盒环境",
    usage="",
    type="application",
    homepage="https://github.com/KroMiose/nekro-agent",
    supported_adapters={"~onebot.v11"},
    config=_Config,
)

global_config = None

if not OsEnv.CLI_MODE:
    from nekro_agent.core.logger import logger
    from nekro_agent.runtime_bootstrap import bootstrap_nonebot_plugin

    global_config = bootstrap_nonebot_plugin()

    if Args.LOAD_TEST:
        logger.success("Plugin load tested successfully")
        raise SystemExit(0)
