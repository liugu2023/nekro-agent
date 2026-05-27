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
_PLUGIN_CHECK_IGNORE_PATTERNS = ("__pycache__", "*.pyc", "*.pyo")


def normalize_check_relative_path(file_path: str) -> Path:
    relative_path = Path(file_path)
    if relative_path.name.endswith(".py.disabled"):
        return relative_path.with_name(relative_path.name[: -len(".disabled")])
    return relative_path


def stage_plugin_candidate(file_path: str, code: str, stage_root: Path) -> Path:
    source_path = resolve_plugin_file(file_path)
    relative_path = normalize_check_relative_path(file_path)
    stage_root.mkdir(parents=True, exist_ok=True)

    package_root = source_path.parent if (source_path.parent / "__init__.py").exists() else None
    if package_root is not None:
        target_dir = stage_root / relative_path.parent
        if target_dir.exists():
            if target_dir.is_dir():
                shutil.rmtree(target_dir)
            else:
                target_dir.unlink()
        if package_root.exists():
            shutil.copytree(package_root, target_dir, ignore=shutil.ignore_patterns(*_PLUGIN_CHECK_IGNORE_PATTERNS))
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = target_dir / relative_path.name
    else:
        candidate_path = stage_root / relative_path
        candidate_path.parent.mkdir(parents=True, exist_ok=True)

    candidate_path.write_text(code, encoding="utf-8")
    return candidate_path


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
    level: str = "smoke",
    timeout_seconds: int = _PLUGIN_CHECK_TIMEOUT_SECONDS,
) -> PluginCheckReport:
    repo_root = Path(__file__).resolve().parents[3]
    cli_script_path = repo_root / "run_nekro_cli.py"
    if not cli_script_path.exists():
        raise OperationFailedError(operation="执行插件自检", detail=f"未找到 CLI 入口: {cli_script_path}")

    PLUGIN_DEV_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plugin-dev-self-check-", dir=PLUGIN_DEV_DIR) as temp_root_str:
        temp_root = Path(temp_root_str)
        candidate_root = temp_root / "candidate"
        candidate_path = stage_plugin_candidate(file_path, code, candidate_root)
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
