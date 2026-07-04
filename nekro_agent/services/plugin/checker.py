from __future__ import annotations

import ast
import importlib
import importlib.machinery
import importlib.util
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


# ============================================================================
# 静态检查（static 级别）
#
# 安全约束：静态检查绝不执行候选插件代码——不暂存到插件目录、不把候选根目录
# 加入 sys.path、不 import 候选模块。导入验证只允许解析/导入运行环境中已存在
# 的可信模块（仓库源码、标准库、site-packages），数据目录下的模块一律跳过。
# ============================================================================

_STATIC_IMPORT_GUARD_EXC_NAMES = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}

# 这些挂载器要求被装饰函数是 async（与运行时 _validate_async_contracts 对齐）
_ASYNC_REQUIRED_MOUNT_DECORATORS = {
    "mount_init_method",
    "mount_cleanup_method",
    "mount_sandbox_method",
    "mount_prompt_inject_method",
    "mount_on_channel_reset",
    "mount_on_user_message",
    "mount_on_system_message",
    "mount_webhook_method",
    "mount_async_task",
}


def _static_entry_file(layout: CandidateLayout) -> Path:
    if layout.mode == "file":
        return layout.source_path
    return layout.root_path / "__init__.py"


def _iter_candidate_python_files(layout: CandidateLayout) -> list[Path]:
    if layout.mode == "file":
        return [layout.source_path]
    files = sorted(path for path in layout.root_path.rglob("*.py") if "__pycache__" not in path.parts)
    if layout.source_path.is_file() and layout.source_path not in files:
        files.append(layout.source_path)
    return files


def _handles_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    for handler in handlers:
        if handler.type is None:
            return True
        type_nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for node in type_nodes:
            if isinstance(node, ast.Name) and node.id in _STATIC_IMPORT_GUARD_EXC_NAMES:
                return True
            if isinstance(node, ast.Attribute) and node.attr in _STATIC_IMPORT_GUARD_EXC_NAMES:
                return True
    return False


def _iter_module_level_imports(tree: ast.Module) -> list[tuple[ast.Import | ast.ImportFrom, bool]]:
    """收集模块导入时会执行的 import 语句；bool 表示是否被 try/except ImportError 保护。

    函数体内的 import 是惰性执行，不影响插件加载，因此跳过。
    """
    results: list[tuple[ast.Import | ast.ImportFrom, bool]] = []

    def visit_stmts(stmts: list[ast.stmt], guarded: bool) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                results.append((stmt, guarded))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            elif isinstance(stmt, (ast.Try, ast.TryStar)):
                visit_stmts(stmt.body, guarded or _handles_import_error(stmt.handlers))
                for handler in stmt.handlers:
                    visit_stmts(handler.body, guarded)
                visit_stmts(stmt.orelse, guarded)
                visit_stmts(stmt.finalbody, guarded)
            else:
                for _, value in ast.iter_fields(stmt):
                    if not isinstance(value, list):
                        continue
                    nested_stmts = [item for item in value if isinstance(item, ast.stmt)]
                    if nested_stmts:
                        visit_stmts(nested_stmts, guarded)
                    for item in value:
                        if isinstance(item, ast.match_case):
                            visit_stmts(item.body, guarded)

    visit_stmts(tree.body, False)
    return results


def _is_trusted_import_origin(spec: importlib.machinery.ModuleSpec) -> bool:
    """数据目录（真实插件目录所在处）下的模块不可信，禁止在静态检查中加载。"""
    origin = getattr(spec, "origin", None)
    if origin in (None, "built-in", "frozen"):
        return True
    try:
        origin_path = Path(str(origin)).resolve()
    except Exception:
        return False
    repo_root = Path(__file__).resolve().parents[3]
    untrusted_roots = [Path(OsEnv.DATA_DIR).resolve(), repo_root / "data"]
    return all(not origin_path.is_relative_to(root) for root in untrusted_roots)


def _resolve_module_spec_safely(module_name: str) -> Literal["found", "missing", "untrusted", "error"]:
    """逐级 find_spec：任何一级不可信就停止，确保不会执行不可信父包的模块代码。"""
    parts = module_name.split(".")
    for depth in range(1, len(parts) + 1):
        prefix = ".".join(parts[:depth])
        try:
            spec = importlib.util.find_spec(prefix)
        except (ImportError, ValueError, AttributeError):
            return "missing"
        except Exception:
            return "error"
        if spec is None:
            return "missing"
        if not _is_trusted_import_origin(spec):
            return "untrusted"
    return "found"


def _check_absolute_import(module_name: str, symbol: str | None) -> tuple[bool | None, str]:
    """返回 (ok, message)。ok=None 表示无法静态确认（降级为警告）。"""
    if not module_name:
        return None, "无法解析导入的模块名"
    status = _resolve_module_spec_safely(module_name)
    if status == "missing":
        return False, f"导入的模块不存在: `{module_name}`"
    if status == "error":
        return None, f"无法静态确认模块 `{module_name}`（解析时出错）"
    if status == "untrusted":
        return None, f"模块 `{module_name}` 位于数据目录，静态检查跳过导入验证"
    if symbol is None or symbol == "*":
        return True, ""

    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        return None, f"无法静态确认 `{module_name}`（导入时出错: {type(e).__name__}）"
    if hasattr(module, symbol):
        return True, ""
    sub_status = _resolve_module_spec_safely(f"{module_name}.{symbol}")
    if sub_status == "found":
        return True, ""
    if sub_status in ("untrusted", "error"):
        return None, f"无法静态确认 `{module_name}.{symbol}`"
    return False, f"模块 `{module_name}` 中不存在 `{symbol}`"


def _check_relative_import(
    node: ast.ImportFrom,
    file_path: Path,
    layout: CandidateLayout,
) -> list[tuple[bool | None, str]]:
    if layout.mode == "file":
        return [(False, "单文件插件不支持相对导入；请改为单文件实现或提供完整插件包")]

    base_dir = file_path.parent
    for _ in range(node.level - 1):
        base_dir = base_dir.parent
    try:
        base_dir.resolve().relative_to(layout.root_path.resolve())
    except ValueError:
        return [(False, f"相对导入越出插件包范围（level={node.level}）")]

    results: list[tuple[bool | None, str]] = []
    if node.module:
        target = base_dir / Path(*node.module.split("."))
        if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
            results.append((False, f"相对导入的模块不存在: `{'.' * node.level}{node.module}`"))
        return results

    for alias in node.names:
        if alias.name == "*":
            continue
        target = base_dir / alias.name
        if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
            results.append((None, f"无法静态确认 `from {'.' * node.level} import {alias.name}`（可能定义于 __init__.py）"))
    return results


def _check_static_imports(
    parsed_files: list[tuple[Path, ast.Module]],
    layout: CandidateLayout,
) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    warnings: list[str] = []
    checked_count = 0

    for file_path, tree in parsed_files:
        for node, guarded in _iter_module_level_imports(tree):
            location = f"{file_path.name}:{node.lineno}"
            issues: list[tuple[bool | None, str]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    checked_count += 1
                    ok, message = _check_absolute_import(alias.name, None)
                    if ok is not True:
                        issues.append((ok, message))
            elif node.level > 0:
                checked_count += 1
                issues.extend(_check_relative_import(node, file_path, layout))
            else:
                for alias in node.names:
                    checked_count += 1
                    ok, message = _check_absolute_import(node.module or "", alias.name)
                    if ok is not True:
                        issues.append((ok, message))

            for ok, message in issues:
                text = f"{location} {message}"
                if ok is False and guarded:
                    warnings.append(f"{text}（已被 try/except 保护，运行时不会中断加载）")
                elif ok is False:
                    errors.append(text)
                else:
                    warnings.append(text)
    return errors, warnings, checked_count


def _decorator_mount_name(decorator: ast.expr) -> str:
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _check_static_async_contracts(parsed_files: list[tuple[Path, ast.Module]]) -> list[str]:
    issues: list[str] = []

    def visit_stmts(file_name: str, stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(stmt, ast.FunctionDef):
                    for decorator in stmt.decorator_list:
                        mount_name = _decorator_mount_name(decorator)
                        if mount_name in _ASYNC_REQUIRED_MOUNT_DECORATORS:
                            issues.append(
                                f"{file_name}:{stmt.lineno} `{stmt.name}` 被 `{mount_name}` 挂载，必须定义为 async 函数"
                            )
                            break
                continue
            for _, value in ast.iter_fields(stmt):
                if not isinstance(value, list):
                    continue
                nested_stmts = [item for item in value if isinstance(item, ast.stmt)]
                if nested_stmts:
                    visit_stmts(file_name, nested_stmts)

    for file_path, tree in parsed_files:
        visit_stmts(file_path.name, tree.body)
    return issues


def _check_static_plugin_instance(tree: ast.Module) -> tuple[bool, str]:
    found: list[str] = []

    def visit_stmts(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
                    continue
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if any(isinstance(target, ast.Name) and target.id == "plugin" for target in targets):
                    value = stmt.value
                    if isinstance(value, ast.Call):
                        func = value.func
                        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                        if func_name == "NekroPlugin":
                            found.append("已找到 `plugin = NekroPlugin(...)` 定义")
                            continue
                    found.append("已找到模块级 `plugin` 赋值")
                continue
            if isinstance(stmt, ast.ImportFrom):
                if any((alias.asname or alias.name) == "plugin" for alias in stmt.names):
                    found.append("已找到 `plugin` 导入（通常来自包内子模块）")
                continue
            for _, value in ast.iter_fields(stmt):
                if not isinstance(value, list):
                    continue
                nested_stmts = [item for item in value if isinstance(item, ast.stmt)]
                if nested_stmts:
                    visit_stmts(nested_stmts)

    visit_stmts(tree.body)
    if not found:
        return False, ""
    found.sort(key=lambda text: 0 if "NekroPlugin" in text else 1)
    return True, found[0]


def _run_static_check(layout: CandidateLayout, report: PluginCheckReport) -> None:
    parsed_files: list[tuple[Path, ast.Module]] = []
    syntax_errors: list[str] = []
    for file_path in _iter_candidate_python_files(layout):
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            syntax_errors.append(f"{file_path.name}: 读取失败（{e}）")
            continue
        try:
            parsed_files.append((file_path, ast.parse(source, filename=str(file_path))))
        except SyntaxError as e:
            syntax_errors.append(f"{file_path.name}:{e.lineno}: {e.msg}")

    if syntax_errors:
        _add_check(report, "static_syntax", "语法检查", False, error="；".join(syntax_errors[:5]))
        return
    _add_check(report, "static_syntax", "语法检查", True, detail=f"已解析 {len(parsed_files)} 个 Python 文件")

    import_errors, import_warnings, checked_count = _check_static_imports(parsed_files, layout)
    report.warnings.extend(import_warnings[:10])
    if import_errors:
        _add_check(report, "static_imports", "导入可用性检查", False, error="；".join(import_errors[:5]))
    else:
        _add_check(report, "static_imports", "导入可用性检查", True, detail=f"已检查 {checked_count} 条模块级导入")

    async_issues = _check_static_async_contracts(parsed_files)
    if async_issues:
        _add_check(report, "static_async_contracts", "静态 async 约束检查", False, error="；".join(async_issues[:5]))
    else:
        _add_check(report, "static_async_contracts", "静态 async 约束检查", True, detail="挂载函数均满足 async 约束")

    entry_file = _static_entry_file(layout).resolve()
    entry_tree = next((tree for path, tree in parsed_files if path.resolve() == entry_file), None)
    if entry_tree is None:
        _add_check(report, "static_plugin_instance", "插件实例定义检查", False, error=f"未找到插件入口文件: {entry_file.name}")
        return
    instance_ok, instance_detail = _check_static_plugin_instance(entry_tree)
    if instance_ok:
        _add_check(report, "static_plugin_instance", "插件实例定义检查", True, detail=instance_detail)
    else:
        _add_check(
            report,
            "static_plugin_instance",
            "插件实例定义检查",
            False,
            error="插件入口缺少模块级 `plugin` 实例（需要 `plugin = NekroPlugin(...)`）",
        )


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

    if level == "static":
        _run_static_check(layout, report)
        report.ok = bool(report.checks) and all(item.ok for item in report.checks)
        return report

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
