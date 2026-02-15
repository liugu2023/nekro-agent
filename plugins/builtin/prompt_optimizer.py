"""
# Prompt 优化器插件 (Prompt Optimizer) v2.0

通过精简插件方法文档来大幅缩减输入 Token 消耗，同时保留完整功能。

## 工作原理

启用此插件后：
- **白名单内的插件**：保持完整的方法文档（包含详细参数说明、示例等）
- **白名单外的插件**：仅显示方法签名和简短描述
- AI 可通过多种方法按需查询详细文档

## 核心功能

1. **query_method_doc** - 查询单个方法的详细文档
2. **query_methods_batch** - 批量查询多个方法文档
3. **query_plugin_methods** - 查询插件的所有方法
4. **search_methods** - 根据关键词搜索相关方法
5. **get_capability_overview** - 获取当前所有能力的结构化概览

## 预期效果

- 初始 Prompt 缩减 50-70%
- 简单对话无需额外调用
- 复杂任务按需查询，平均增加 1-2 次调用

## 配置说明

- **WHITELIST_PLUGINS**: 白名单插件列表，使用插件 key（格式：作者.模块名）
- **ENABLE_USAGE_STATS**: 启用使用统计功能
- **AUTO_SUGGEST_THRESHOLD**: 方法被调用多少次后建议加入白名单

## 注意事项

- 禁用此插件后，所有插件将恢复完整文档模式
- 建议将使用频率最高的插件加入白名单
"""

import inspect
import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from pydantic import Field

from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethod

# 文档查询标记，带此标记的 AGENT 返回在迭代周期内保留不清理
DOC_QUERY_MARKER = "[DOC_QUERY]"

plugin = NekroPlugin(
    name="Prompt优化器",
    module_name="prompt_optimizer",
    description="精简插件文档以减少Token消耗，支持按需查询、关键词搜索、批量查询等功能",
    version="2.0.0",
    author="KroMiose",
    url="https://github.com/KroMiose/nekro-agent",
)


@plugin.mount_config()
class PromptOptimizerConfig(ConfigBase):
    """Prompt优化器配置"""

    WHITELIST_PLUGINS: List[str] = Field(
        default=["KroMiose.basic"],
        title="白名单插件列表",
        description="白名单内的插件保持完整方法文档，使用插件key格式（作者.模块名）",
    )

    ENABLE_USAGE_STATS: bool = Field(
        default=True,
        title="启用使用统计",
        description="记录方法调用次数，用于优化白名单建议",
    )

    AUTO_SUGGEST_THRESHOLD: int = Field(
        default=10,
        title="自动建议阈值",
        description="方法被查询多少次后建议加入白名单",
    )


config: PromptOptimizerConfig = plugin.get_config(PromptOptimizerConfig)

# 使用统计：记录方法查询次数
_usage_stats: Dict[str, int] = defaultdict(int)

# 关键词到方法的映射缓存
_keyword_cache: Dict[str, List[Tuple[str, str, float]]] = {}
_cache_valid = False


def _invalidate_cache():
    """使缓存失效"""
    global _cache_valid
    _cache_valid = False


def _build_keyword_index() -> Dict[str, List[Tuple[str, str, float]]]:
    """构建关键词索引

    Returns:
        Dict[str, List[Tuple[method_name, plugin_name, relevance]]]: 关键词到方法的映射
    """
    global _keyword_cache, _cache_valid

    if _cache_valid:
        return _keyword_cache

    index: Dict[str, List[Tuple[str, str, float]]] = defaultdict(list)

    for p in plugin_collector.get_all_active_plugins():
        for method in p.sandbox_methods:
            # 从方法名提取关键词
            method_name = method.func.__name__
            name_words = re.findall(r"[a-z]+", method_name.lower())

            # 从描述提取关键词
            desc = method.description or ""
            doc = method.func.__doc__ or ""
            full_text = f"{desc} {doc}".lower()

            # 提取中文关键词
            chinese_words = re.findall(r"[\u4e00-\u9fff]+", full_text)

            # 提取英文关键词
            english_words = re.findall(r"[a-z]{3,}", full_text)

            # 建立索引
            all_keywords = set(name_words + chinese_words + english_words)
            for keyword in all_keywords:
                if len(keyword) >= 2:  # 过滤太短的词
                    # 根据关键词位置计算相关度
                    relevance = 1.0
                    if keyword in method_name.lower():
                        relevance = 2.0  # 方法名中的词相关度更高
                    if keyword in (method.name or "").lower():
                        relevance = 1.8

                    index[keyword].append((method_name, p.name, relevance))

    _keyword_cache = index
    _cache_valid = True
    return index


def _record_usage(method_name: str):
    """记录方法使用"""
    if config.ENABLE_USAGE_STATS:
        _usage_stats[method_name] += 1


def _find_method(method_name: str) -> Optional[Tuple[NekroPlugin, SandboxMethod]]:
    """查找指定名称的方法"""
    for p in plugin_collector.get_all_active_plugins():
        for method in p.sandbox_methods:
            if method.func.__name__ == method_name:
                return (p, method)
    return None


def _find_plugin(plugin_identifier: str) -> Optional[NekroPlugin]:
    """查找指定的插件"""
    for p in plugin_collector.get_all_active_plugins():
        if p.key == plugin_identifier or p.module_name == plugin_identifier or p.name == plugin_identifier:
            return p
    return None


def _get_method_signature(func) -> str:
    """获取方法签名"""
    try:
        sig = inspect.signature(func)
        params = []
        for name, param in sig.parameters.items():
            if name == "_ctx":
                continue
            if param.annotation != inspect.Parameter.empty:
                type_name = getattr(param.annotation, "__name__", str(param.annotation))
                if param.default != inspect.Parameter.empty:
                    params.append(f"{name}: {type_name} = ...")
                else:
                    params.append(f"{name}: {type_name}")
            else:
                params.append(name)
        return f"({', '.join(params)})"
    except Exception:
        return "()"


def _format_method_doc(p: NekroPlugin, method: SandboxMethod, compact: bool = False) -> str:
    """格式化方法文档"""
    agent_tag = ""
    if method.method_type in [SandboxMethodType.AGENT, SandboxMethodType.MULTIMODAL_AGENT]:
        agent_tag = " **[AGENT METHOD - STOP AFTER CALL]**"

    if compact:
        sig = _get_method_signature(method.func)
        desc = method.description or (method.func.__doc__ or "").split("\n")[0].strip()
        return f"- `{method.func.__name__}{sig}`{agent_tag}: {desc}"

    doc = method.func.__doc__ or "No documentation available."

    return f"""## {method.func.__name__}{agent_tag}

**Plugin**: {p.name} ({p.key})
**Type**: {method.method_type.value}
**Description**: {method.description or 'N/A'}

### Documentation:
```
{doc.strip()}
```
"""


def _format_method_brief(p: NekroPlugin, method: SandboxMethod) -> str:
    """格式化方法简要信息"""
    sig = _get_method_signature(method.func)
    agent_tag = " [AGENT]" if method.method_type in [SandboxMethodType.AGENT, SandboxMethodType.MULTIMODAL_AGENT] else ""
    desc = method.description or (method.func.__doc__ or "").split("\n")[0].strip()
    return f"`{method.func.__name__}{sig}`{agent_tag}: {desc}"


# ============== 沙盒方法 ==============


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="查询方法文档",
    description="查询某个方法的完整文档，包含参数说明和使用示例",
)
async def query_method_doc(_ctx: AgentCtx, method_name: str) -> str:
    """查询方法的详细文档

    当你需要了解某个方法的具体用法、参数说明或示例时调用此方法。

    Args:
        method_name (str): 要查询的方法名称，如 "send_msg_text"

    Returns:
        str: 该方法的完整文档

    Example:
        query_method_doc("send_msg_text")
    """
    _record_usage(method_name)

    result = _find_method(method_name)
    if result is None:
        # 尝试模糊匹配
        suggestions = []
        for p in plugin_collector.get_all_active_plugins():
            for m in p.sandbox_methods:
                if method_name.lower() in m.func.__name__.lower():
                    suggestions.append(f"- {m.func.__name__} ({p.name})")

        if suggestions:
            return f"""{DOC_QUERY_MARKER}Method '{method_name}' not found.

Did you mean:
{chr(10).join(suggestions[:5])}

Use `search_methods(keyword)` to search by functionality."""

        return f"""{DOC_QUERY_MARKER}Method '{method_name}' not found.

Use `search_methods(keyword)` to find methods by functionality.
Use `get_capability_overview()` to see all available capabilities."""

    p, method = result
    return f"{DOC_QUERY_MARKER}{_format_method_doc(p, method)}"


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="批量查询方法文档",
    description="一次性查询多个方法的文档",
)
async def query_methods_batch(_ctx: AgentCtx, method_names: str) -> str:
    """批量查询多个方法的文档

    当你需要同时了解多个方法时使用，减少调用次数。

    Args:
        method_names (str): 逗号分隔的方法名列表，如 "send_msg_text,send_msg_file,get_user_info"

    Returns:
        str: 所有方法的完整文档

    Example:
        query_methods_batch("send_msg_text,send_msg_file")
    """
    names = [n.strip() for n in method_names.split(",") if n.strip()]

    if not names:
        return f"{DOC_QUERY_MARKER}Error: No method names provided."

    if len(names) > 10:
        return f"{DOC_QUERY_MARKER}Error: Too many methods requested. Maximum is 10."

    docs = []
    not_found = []

    for name in names:
        _record_usage(name)
        result = _find_method(name)
        if result:
            p, method = result
            docs.append(_format_method_doc(p, method))
        else:
            not_found.append(name)

    output = f"# Method Documentation ({len(docs)} found)\n\n"
    output += "\n---\n".join(docs)

    if not_found:
        output += f"\n\n---\n\n**Not found**: {', '.join(not_found)}"

    return f"{DOC_QUERY_MARKER}{output}"


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="搜索方法",
    description="根据关键词搜索相关方法，支持中英文",
)
async def search_methods(_ctx: AgentCtx, keyword: str) -> str:
    """根据关键词搜索相关方法

    当你知道想要的功能但不确定方法名时使用。

    Args:
        keyword (str): 搜索关键词，如 "消息"、"图片"、"search"、"file"

    Returns:
        str: 匹配的方法列表及简要说明

    Example:
        search_methods("消息")
        search_methods("image")
    """
    keyword = keyword.lower().strip()
    if len(keyword) < 2:
        return f"{DOC_QUERY_MARKER}Error: Keyword too short. Use at least 2 characters."

    index = _build_keyword_index()
    matches: Dict[str, Tuple[str, float]] = {}  # method_name -> (plugin_name, max_relevance)

    # 精确匹配
    if keyword in index:
        for method_name, plugin_name, relevance in index[keyword]:
            if method_name not in matches or matches[method_name][1] < relevance:
                matches[method_name] = (plugin_name, relevance)

    # 部分匹配
    for kw, methods in index.items():
        if keyword in kw or kw in keyword:
            for method_name, plugin_name, relevance in methods:
                adjusted_relevance = relevance * 0.7  # 部分匹配降低相关度
                if method_name not in matches or matches[method_name][1] < adjusted_relevance:
                    matches[method_name] = (plugin_name, adjusted_relevance)

    if not matches:
        return f"""{DOC_QUERY_MARKER}No methods found for keyword '{keyword}'.

Try:
- Using different keywords (Chinese or English)
- Use `get_capability_overview()` to see all capabilities
- Use `query_plugin_methods(plugin_name)` to explore a specific plugin"""

    # 按相关度排序
    sorted_matches = sorted(matches.items(), key=lambda x: x[1][1], reverse=True)[:15]

    lines = [f"# Search Results for '{keyword}'\n"]
    lines.append(f"Found {len(sorted_matches)} matching methods:\n")

    for method_name, (plugin_name, _) in sorted_matches:
        result = _find_method(method_name)
        if result:
            p, method = result
            lines.append(f"- {_format_method_brief(p, method)}")

    lines.append("\n\nUse `query_method_doc(method_name)` for detailed documentation.")

    return f"{DOC_QUERY_MARKER}" + "\n".join(lines)


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="查询插件方法",
    description="查询某个插件提供的所有方法",
)
async def query_plugin_methods(_ctx: AgentCtx, plugin_identifier: str) -> str:
    """查询某个插件的所有方法详细文档

    Args:
        plugin_identifier (str): 插件标识（可以是插件key、模块名或插件名称）

    Returns:
        str: 该插件所有方法的完整文档

    Example:
        query_plugin_methods("basic")
        query_plugin_methods("KroMiose.basic")
    """
    p = _find_plugin(plugin_identifier)
    if p is None:
        all_plugins = [f"- {p.name} ({p.module_name})" for p in plugin_collector.get_all_active_plugins()]

        return f"""{DOC_QUERY_MARKER}Plugin '{plugin_identifier}' not found.

Available plugins:
{chr(10).join(all_plugins)}"""

    if not p.sandbox_methods:
        return f"{DOC_QUERY_MARKER}Plugin '{p.name}' has no sandbox methods."

    for method in p.sandbox_methods:
        _record_usage(method.func.__name__)

    docs = [f"# {p.name}\n\n**Key**: {p.key}\n**Description**: {p.description}\n**Version**: {p.version}\n"]

    for method in p.sandbox_methods:
        docs.append(_format_method_doc(p, method))

    return f"{DOC_QUERY_MARKER}" + "\n---\n".join(docs)


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="能力概览",
    description="获取当前所有可用能力的结构化概览",
)
async def get_capability_overview(_ctx: AgentCtx) -> str:
    """获取当前所有可用能力的结构化概览

    当你需要了解系统整体能力或寻找合适的方法时使用。

    Returns:
        str: 按分类组织的能力概览

    Example:
        get_capability_overview()
    """
    # 按功能分类组织方法
    categories: Dict[str, List[Tuple[NekroPlugin, SandboxMethod]]] = defaultdict(list)

    # 简单的关键词分类
    category_keywords = {
        "消息/通信": ["msg", "message", "send", "消息", "发送", "通信"],
        "文件/媒体": ["file", "image", "audio", "video", "图片", "文件", "媒体", "图像"],
        "搜索/查询": ["search", "query", "find", "get", "搜索", "查询", "获取"],
        "工具/计算": ["calc", "compute", "tool", "util", "计算", "工具"],
        "系统/管理": ["system", "admin", "config", "manage", "系统", "管理", "配置"],
        "AI/智能": ["ai", "llm", "gpt", "智能", "模型"],
    }

    for p in plugin_collector.get_all_active_plugins():
        for method in p.sandbox_methods:
            method_text = f"{method.func.__name__} {method.description or ''} {method.func.__doc__ or ''}".lower()

            categorized = False
            for category, keywords in category_keywords.items():
                if any(kw in method_text for kw in keywords):
                    categories[category].append((p, method))
                    categorized = True
                    break

            if not categorized:
                categories["其他"].append((p, method))

    lines = ["# Capability Overview\n"]
    lines.append("All available methods organized by category:\n")

    total_methods = 0
    for category, methods in sorted(categories.items()):
        if not methods:
            continue

        lines.append(f"\n## {category} ({len(methods)} methods)\n")
        for p, method in methods:
            lines.append(f"  {_format_method_brief(p, method)}")
            total_methods += 1

    lines.append(
        f"\n---\n**Total**: {total_methods} methods from {len(list(plugin_collector.get_all_active_plugins()))} plugins",
    )
    lines.append("\nUse `query_method_doc(name)` for details, `search_methods(keyword)` to search.")

    return f"{DOC_QUERY_MARKER}" + "\n".join(lines)


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="获取使用统计",
    description="获取方法查询频率统计，用于优化白名单",
)
async def get_usage_stats(_ctx: AgentCtx) -> str:
    """获取方法查询频率统计

    Returns:
        str: 使用频率统计和白名单建议

    Example:
        get_usage_stats()
    """
    if not config.ENABLE_USAGE_STATS:
        return "Usage statistics is disabled."

    if not _usage_stats:
        return "No usage data collected yet."

    sorted_stats = sorted(_usage_stats.items(), key=lambda x: x[1], reverse=True)

    lines = ["# Method Usage Statistics\n"]
    lines.append("| Method | Query Count | Suggested Action |")
    lines.append("|--------|-------------|------------------|")

    suggestions = []
    for method_name, count in sorted_stats[:20]:
        result = _find_method(method_name)
        if result:
            p, _ = result
            action = ""
            if count >= config.AUTO_SUGGEST_THRESHOLD:
                if p.key not in config.WHITELIST_PLUGINS and p.module_name not in config.WHITELIST_PLUGINS:
                    action = f"Consider adding `{p.key}` to whitelist"
                    suggestions.append(p.key)
            lines.append(f"| {method_name} | {count} | {action} |")

    if suggestions:
        lines.append("\n\n## Whitelist Suggestions\n")
        lines.append("Based on usage, consider adding these plugins to whitelist:")
        for s in set(suggestions):
            lines.append(f"- `{s}`")

    return "\n".join(lines)


# ============== 注入提示 ==============


@plugin.mount_prompt_inject_method(name="prompt_optimizer_inject")
async def inject_prompt(_ctx: AgentCtx) -> str:
    """注入提示词"""
    # 统计当前精简了多少方法
    total_methods = 0
    compact_methods = 0
    whitelist_set = set(config.WHITELIST_PLUGINS)

    for p in plugin_collector.get_all_active_plugins():
        for _ in p.sandbox_methods:
            total_methods += 1
            if p.key not in whitelist_set and p.module_name not in whitelist_set:
                compact_methods += 1

    return f"""## Prompt Optimizer Active

**Status**: {compact_methods}/{total_methods} methods using compact docs (saving ~{compact_methods * 150} tokens)

**Quick Reference**:
| Need | Command |
|------|---------|
| Method details | `query_method_doc("method_name")` |
| Multiple methods | `query_methods_batch("m1,m2,m3")` |
| Find by function | `search_methods("keyword")` |
| Plugin methods | `query_plugin_methods("plugin")` |
| All capabilities | `get_capability_overview()` |

Tip: Methods marked with [AGENT] will pause execution and return results to you.
"""


# ============== 清理方法 ==============


@plugin.mount_cleanup_method()
async def cleanup():
    """清理插件资源"""
    global _usage_stats, _cache_valid
    # 可选：持久化使用统计
    _usage_stats.clear()
    _cache_valid = False
