from __future__ import annotations

import inspect
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tortoise import Tortoise

from nekro_agent.core.database import init_db
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import WORKDIR_PLUGIN_DIR, OsEnv
from nekro_agent.schemas.plugin_check import (
    PluginCheckFailure,
    PluginCheckItem,
    PluginCheckLevel,
    PluginCheckPluginInfo,
    PluginCheckReport,
)
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import PluginCollector

logger = get_sub_logger("plugin_check")


async def _ensure_plugin_check_schema() -> None:
    conn = Tortoise.get_connection("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS "plugin_data" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "plugin_key" VARCHAR(128) NOT NULL,
            "data_key" VARCHAR(128) NOT NULL,
            "data_value" TEXT NOT NULL,
            "target_chat_key" VARCHAR(64) NOT NULL,
            "target_user_id" VARCHAR(256) NOT NULL,
            "create_time" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "update_time" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS "idx_plugin_data_plugin_key" ON "plugin_data" ("plugin_key");
        CREATE INDEX IF NOT EXISTS "idx_plugin_data_data_key" ON "plugin_data" ("data_key");
        CREATE INDEX IF NOT EXISTS "idx_plugin_data_target_chat_key" ON "plugin_data" ("target_chat_key");
        CREATE INDEX IF NOT EXISTS "idx_plugin_data_target_user_id" ON "plugin_data" ("target_user_id");
        """
    )


@dataclass(slots=True)
class CandidateLayout:
    source_path: Path
    root_path: Path
    mode: Literal["file", "package"]
    expected_module_name: str
    warnings: list[str] = field(default_factory=list)


def _is_python_plugin_file(path: Path) -> bool:
    return path.suffix == ".py" or path.name.endswith(".py.disabled")


def _enabled_file_name(path: Path) -> str:
    if path.name.endswith(".py.disabled"):
        return path.name[: -len(".disabled")]
    return path.name


def _module_name_from_file(path: Path) -> str:
    return _enabled_file_name(path).removesuffix(".py")


def _add_check(
    report: PluginCheckReport,
    check_id: str,
    title: str,
    ok: bool,
    *,
    detail: str = "",
    error: str = "",
) -> None:
    report.checks.append(PluginCheckItem(id=check_id, title=title, ok=ok, detail=detail, error=error))
    if error:
        report.errors.append(error)


def _resolve_candidate_layout(candidate_path: Path) -> CandidateLayout:
    resolved = candidate_path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"插件路径不存在: {resolved}")

    warnings: list[str] = []
    if resolved.is_dir():
        if not (resolved / "__init__.py").exists():
            raise ValueError(f"目录插件必须包含 __init__.py: {resolved}")
        return CandidateLayout(
            source_path=resolved,
            root_path=resolved,
            mode="package",
            expected_module_name=resolved.name,
            warnings=warnings,
        )

    if not resolved.is_file() or not _is_python_plugin_file(resolved):
        raise ValueError(f"仅支持检查 .py / .py.disabled 文件或包含 __init__.py 的包目录: {resolved}")

    if resolved.name.endswith(".py.disabled"):
        warnings.append("检测到禁用插件文件，检查时将按启用后的 .py 文件名暂存。")

    package_init = resolved.parent / "__init__.py"
    if resolved.name == "__init__.py" and package_init.exists():
        return CandidateLayout(
            source_path=resolved,
            root_path=resolved.parent,
            mode="package",
            expected_module_name=resolved.parent.name,
            warnings=warnings,
        )

    if package_init.exists():
        warnings.append(f"检测到包结构，实际将按包入口 {package_init} 进行检查。")
        return CandidateLayout(
            source_path=resolved,
            root_path=resolved.parent,
            mode="package",
            expected_module_name=resolved.parent.name,
            warnings=warnings,
        )

    return CandidateLayout(
        source_path=resolved,
        root_path=resolved,
        mode="file",
        expected_module_name=_module_name_from_file(resolved),
        warnings=warnings,
    )


def _stage_candidate(layout: CandidateLayout) -> Path:
    workdir_root = Path(WORKDIR_PLUGIN_DIR)
    workdir_root.mkdir(parents=True, exist_ok=True)

    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    if layout.mode == "package":
        target = workdir_root / layout.root_path.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copytree(layout.root_path, target, ignore=ignore)
        return target

    target = workdir_root / _enabled_file_name(layout.root_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(layout.root_path, target)
    return target


def _ensure_plugin_import_paths(collector: PluginCollector) -> None:
    search_roots = [
        collector.builtin_plugin_dir.parent.absolute(),
        collector.workdir_plugin_dir.parent.absolute(),
        collector.packages_dir.parent.absolute(),
    ]
    for root in search_roots:
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


def _build_plugin_info(plugin: NekroPlugin) -> PluginCheckPluginInfo:
    return PluginCheckPluginInfo(
        name=plugin.name,
        module_name=plugin.module_name,
        author=plugin.author,
        version=plugin.version,
        key=plugin.key,
        enabled=plugin.is_enabled,
        is_builtin=plugin.is_builtin,
        is_package=plugin.is_package,
        sandbox_method_count=len(plugin.sandbox_methods),
        webhook_count=len(plugin.webhook_methods),
        command_count=len(plugin._commands),  # noqa: SLF001
        has_router=bool(getattr(plugin, "_router_func", None)),
    )


def _validate_async_contracts(plugin: NekroPlugin) -> list[str]:
    issues: list[str] = []
    lifecycle_methods = [
        ("init_method", plugin.init_method),
        ("cleanup_method", plugin.cleanup_method),
        ("prompt_inject_method", plugin.prompt_inject_method.func if plugin.prompt_inject_method else None),
        ("on_reset_method", plugin.on_reset_method),
        ("on_user_message_method", plugin.on_user_message_method),
        ("on_system_message_method", plugin.on_system_message_method),
    ]
    for name, func in lifecycle_methods:
        if func is not None and not inspect.iscoroutinefunction(func):
            issues.append(f"{name} 必须是 async 函数")

    for method in plugin.sandbox_methods:
        if not inspect.iscoroutinefunction(method.func):
            issues.append(f"sandbox method `{method.name}` 必须是 async 函数")

    for endpoint, method in plugin.webhook_methods.items():
        if not inspect.iscoroutinefunction(method.func):
            issues.append(f"webhook `{endpoint}` 必须是 async 函数")

    return issues


def _validate_duplicate_method_names(plugin: NekroPlugin) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for method in plugin.sandbox_methods:
        if method.name in seen:
            duplicates.append(method.name)
            continue
        seen.add(method.name)
    return duplicates


async def run_plugin_check(candidate_path: str | Path, level: PluginCheckLevel = "smoke") -> PluginCheckReport:
    layout = _resolve_candidate_layout(Path(candidate_path))
    report = PluginCheckReport(
        candidate_path=str(layout.source_path),
        level=level,
        runtime_data_dir=str(Path(OsEnv.DATA_DIR).resolve()),
        stage_mode=layout.mode,
        warnings=list(layout.warnings),
    )

    collector = PluginCollector()
    _ensure_plugin_import_paths(collector)
    cleaned_up = False
    try:
        await init_db()
        await _ensure_plugin_check_schema()
        _add_check(report, "db_ready", "准备临时数据库", True, detail="已完成数据库初始化与最小表准备")

        staged_entry = _stage_candidate(layout)
        report.staged_path = str(staged_entry)
        report.staged_entry_path = str(staged_entry)

        try:
            await collector._try_load_plugin(staged_entry, is_builtin=False, is_package=False)
        except Exception as e:
            _add_check(
                report,
                "plugin_load",
                "加载插件",
                False,
                detail="插件加载过程抛出异常",
                error=str(e),
            )
            return report
        failed_plugins = collector.get_all_failed_plugins()
        loaded_plugins = collector.get_all_plugins()

        if failed_plugins:
            report.load_failures = [
                PluginCheckFailure(
                    module_name=item.module_name,
                    file_path=item.file_path,
                    error_message=item.error_message,
                    error_type=item.error_type,
                    stack_trace=item.stack_trace,
                )
                for item in failed_plugins
            ]
            first_failure = report.load_failures[0]
            _add_check(
                report,
                "plugin_load",
                "加载插件",
                False,
                detail=f"模块 `{first_failure.module_name}` 加载失败",
                error=first_failure.error_message,
            )
            return report

        if len(loaded_plugins) != 1:
            _add_check(
                report,
                "plugin_load",
                "加载插件",
                False,
                detail=f"期望加载 1 个插件，实际加载 {len(loaded_plugins)} 个",
                error="插件加载数量异常",
            )
            return report

        plugin = loaded_plugins[0]
        report.plugin = _build_plugin_info(plugin)
        _add_check(
            report,
            "plugin_load",
            "加载插件",
            True,
            detail=f"已加载 `{plugin.key}`，模块名 `{plugin.module_name}`",
        )

        if plugin.module_name != layout.expected_module_name:
            report.warnings.append(
                f"插件 module_name 为 `{plugin.module_name}`，与当前检查入口推断的 `{layout.expected_module_name}` 不一致。"
            )

        if level in {"smoke", "strict"}:
            router_func = getattr(plugin, "_router_func", None)
            if router_func is None:
                _add_check(report, "router_build", "构建插件路由", True, detail="插件未注册自定义路由")
            else:
                router = plugin.get_plugin_router()
                if router is None:
                    _add_check(report, "router_build", "构建插件路由", False, error="插件路由构建失败")
                else:
                    _add_check(report, "router_build", "构建插件路由", True, detail=f"共生成 {len(router.routes)} 条路由")

        if level == "strict":
            async_issues = _validate_async_contracts(plugin)
            if async_issues:
                _add_check(
                    report,
                    "async_contracts",
                    "校验 async 生命周期约束",
                    False,
                    error="；".join(async_issues),
                )
            else:
                _add_check(report, "async_contracts", "校验 async 生命周期约束", True)

            duplicate_method_names = _validate_duplicate_method_names(plugin)
            if duplicate_method_names:
                _add_check(
                    report,
                    "duplicate_method_names",
                    "校验沙盒方法命名冲突",
                    False,
                    error=f"存在重复的沙盒方法标题: {', '.join(sorted(set(duplicate_method_names)))}",
                )
            else:
                _add_check(report, "duplicate_method_names", "校验沙盒方法命名冲突", True)

            if not plugin.is_enabled:
                try:
                    await plugin.enable()
                except Exception as e:
                    _add_check(report, "enable_callbacks", "触发启用回调", False, error=str(e))
                else:
                    _add_check(report, "enable_callbacks", "触发启用回调", True, detail="已执行 enable()")
            else:
                _add_check(report, "enable_callbacks", "触发启用回调", True, detail="插件在检查环境中已启用")

        await collector.cleanup_all_plugins()
        cleaned_up = True
        _add_check(report, "cleanup", "执行插件清理", True, detail="cleanup 已执行")
        report.ok = all(item.ok for item in report.checks)
        return report
    finally:
        if not cleaned_up:
            with suppress(Exception):
                await collector.cleanup_all_plugins()
        with suppress(Exception):
            await Tortoise.close_connections()
