from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import OperationFailedError, ValidationError
from nekro_agent.schemas.plugin_check import PluginCheckReport
from nekro_agent.services.plugin_dev.host_file_gateway import resolve_plugin_file
from nekro_agent.services.plugin_dev.paths import PLUGIN_DEV_DIR

logger = get_sub_logger("plugin_dev_self_check")

_PLUGIN_CHECK_TIMEOUT_SECONDS = 90
_PLUGIN_STATIC_CHECK_TIMEOUT_SECONDS = 60
_PLUGIN_CHECK_IGNORE_PATTERNS = ("__pycache__", "*.pyc", "*.pyo")


def normalize_check_relative_path(file_path: str) -> Path:
    relative_path = Path(file_path)
    if relative_path.name.endswith(".py.disabled"):
        return relative_path.with_name(relative_path.name[: -len(".disabled")])
    return relative_path


def _write_staged_file(stage_root: Path, file_path: str, content: str) -> Path:
    relative_path = normalize_check_relative_path(file_path)
    target = stage_root / relative_path
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(stage_root.resolve())
    except ValueError as e:
        raise ValidationError(reason=f"候选文件路径越界: {file_path}") from e
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def stage_plugin_candidate(
    file_path: str,
    code: str,
    stage_root: Path,
    extra_files: dict[str, str] | None = None,
    deleted_files: set[str] | None = None,
) -> Path:
    """把候选文件集写入暂存目录，返回检查入口路径。

    - 顶层单文件插件：入口为该文件。
    - 包形式插件（file_path 含目录）：先拷贝真实插件目录的顶层包（若存在）提供
      完整包上下文，再覆盖写入候选文件集；入口为暂存区的顶层包目录。
    """
    resolved_source = resolve_plugin_file(file_path)
    relative_path = normalize_check_relative_path(file_path)
    stage_root.mkdir(parents=True, exist_ok=True)

    top_dir = relative_path.parts[0] if len(relative_path.parts) > 1 else None
    if top_dir is None:
        candidate_entry = _write_staged_file(stage_root, file_path, code)
        for extra_path, extra_content in (extra_files or {}).items():
            _write_staged_file(stage_root, extra_path, extra_content)
        return candidate_entry

    target_top = stage_root / top_dir
    if target_top.exists():
        if target_top.is_dir():
            shutil.rmtree(target_top)
        else:
            target_top.unlink()
    real_top = resolved_source.parents[len(relative_path.parts) - 2]
    if real_top.is_dir():
        shutil.copytree(real_top, target_top, ignore=shutil.ignore_patterns(*_PLUGIN_CHECK_IGNORE_PATTERNS))
    else:
        target_top.mkdir(parents=True, exist_ok=True)

    _write_staged_file(stage_root, file_path, code)
    for extra_path, extra_content in (extra_files or {}).items():
        _write_staged_file(stage_root, extra_path, extra_content)
    for deleted_path in deleted_files or set():
        target = stage_root / normalize_check_relative_path(deleted_path)
        try:
            target.resolve().relative_to(stage_root.resolve())
        except ValueError as e:
            raise ValidationError(reason=f"候选删除路径越界: {deleted_path}") from e
        target.unlink(missing_ok=True)
    return target_top


def summarize_plugin_check(report: PluginCheckReport) -> str:
    for check in report.checks:
        if not check.ok:
            return check.error or check.detail or check.title
    if report.errors:
        return report.errors[0]
    return "插件自检未通过"


async def run_plugin_self_check(
    file_path: str,
    code: str,
    *,
    extra_files: dict[str, str] | None = None,
    deleted_files: set[str] | None = None,
    level: str = "smoke",
    timeout_seconds: int | None = None,
) -> PluginCheckReport:
    if timeout_seconds is None:
        timeout_seconds = _PLUGIN_STATIC_CHECK_TIMEOUT_SECONDS if level == "static" else _PLUGIN_CHECK_TIMEOUT_SECONDS
    repo_root = Path(__file__).resolve().parents[3]
    cli_script_path = repo_root / "run_nekro_cli.py"
    if not cli_script_path.exists():
        raise OperationFailedError(operation="执行插件自检", detail=f"未找到 CLI 入口: {cli_script_path}")

    PLUGIN_DEV_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plugin-dev-self-check-", dir=PLUGIN_DEV_DIR) as temp_root_str:
        temp_root = Path(temp_root_str)
        candidate_root = temp_root / "candidate"
        candidate_path = stage_plugin_candidate(
            file_path,
            code,
            candidate_root,
            extra_files=extra_files,
            deleted_files=deleted_files,
        )
        report_file = temp_root / "plugin_check_report.json"
        runtime_data_dir = temp_root / "runtime_data"

        env = os.environ.copy()
        env["NEKRO_CLI_MODE"] = "true"
        env["NEKRO_DATA_DIR"] = str(runtime_data_dir)
        env["NEKRO_AUTO_DB_MIGRATE"] = "true"

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(cli_script_path),
            "__plugin-check-worker",
            str(candidate_path),
            "--level",
            level,
            "--report-file",
            str(report_file),
            cwd=str(repo_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError as e:
            process.kill()
            await process.communicate()
            raise ValidationError(reason=f"插件自检超时（>{timeout_seconds}s）") from e

        if report_file.exists():
            try:
                report_data = json.loads(report_file.read_text(encoding="utf-8"))
                return PluginCheckReport.model_validate(report_data)
            except Exception as e:
                raise OperationFailedError(operation="读取插件自检报告", detail=str(e)) from e

        stderr_text = stderr.decode("utf-8", errors="ignore").strip()
        stdout_text = stdout.decode("utf-8", errors="ignore").strip()
        detail = stderr_text or stdout_text or f"插件自检子进程退出码: {process.returncode}"
        raise OperationFailedError(operation="执行插件自检", detail=detail)
