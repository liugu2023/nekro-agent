from typing import List, Optional, Set

from nekro_agent.core import logger
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin.base import NekroPlugin

from .base import PromptTemplate, env, register_template


@register_template("plugin.j2", "plugin_prompt")
class PluginPrompt(PromptTemplate):
    plugin_name: str
    plugin_injected_prompt: str
    plugin_method_prompt: str


def _get_prompt_optimizer_config() -> Optional[tuple]:
    """获取 prompt_optimizer 插件配置

    Returns:
        Optional[tuple]: (is_enabled, whitelist_set) 如果插件存在且启用
    """
    from nekro_agent.services.plugin.collector import plugin_collector

    # 查找 prompt_optimizer 插件
    for plugin in plugin_collector.get_all_plugins():
        if plugin.module_name == "prompt_optimizer":
            if not plugin.is_enabled:
                return None
            # 获取插件配置中的白名单
            try:
                config = plugin.get_config()
                whitelist = getattr(config, "WHITELIST_PLUGINS", [])
                return (True, set(whitelist))
            except Exception as e:
                logger.warning(f"获取 prompt_optimizer 配置失败: {e}")
                return None
    return None


def _is_plugin_in_whitelist(plugin: NekroPlugin, whitelist: Set[str]) -> bool:
    """检查插件是否在白名单中

    支持两种匹配方式：
    1. 完整 key 匹配（如 KroMiose.emotion）
    2. module_name 匹配（如 emotion）

    Args:
        plugin: 插件实例
        whitelist: 白名单集合

    Returns:
        bool: 是否在白名单中
    """
    return plugin.key in whitelist or plugin.module_name in whitelist


async def _render_plugin_prompt(plugin: NekroPlugin, ctx: AgentCtx, compact: bool = False) -> str:
    """渲染单个插件的提示词

    Args:
        plugin: 插件实例
        ctx: Agent 上下文
        compact: 是否使用精简模式
    """
    return PluginPrompt(
        plugin_name=plugin.name,
        plugin_injected_prompt=await plugin.render_inject_prompt(ctx),
        plugin_method_prompt=await plugin.render_sandbox_methods_prompt(ctx, compact=compact),
    ).render(env)


async def render_plugins_prompt(plugins: List[NekroPlugin], ctx: AgentCtx) -> str:
    """渲染所有插件的提示词

    如果 prompt_optimizer 插件启用：
    - 白名单内的插件使用完整模式
    - 白名单外的插件使用精简模式

    如果 prompt_optimizer 插件未启用或不存在：
    - 所有插件使用完整模式（原有逻辑）
    """
    # 检查 prompt_optimizer 插件状态
    optimizer_config = _get_prompt_optimizer_config()

    prompts = []
    for plugin in plugins:
        # 检查适配器兼容性
        if len(plugin.support_adapter) > 0 and ctx.adapter_key not in plugin.support_adapter:
            continue

        # 决定是否使用 compact 模式
        use_compact = False
        if optimizer_config is not None:
            _, whitelist = optimizer_config
            # 不在白名单中的插件使用 compact 模式
            # prompt_optimizer 自身不压缩
            if plugin.module_name != "prompt_optimizer" and not _is_plugin_in_whitelist(plugin, whitelist):
                use_compact = True

        prompt = await _render_plugin_prompt(plugin, ctx, compact=use_compact)
        prompts.append(prompt)

    return "\n\n".join(prompts)
