from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="na", description="Nekro Agent CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plugin_parser = subparsers.add_parser("plugin", help="插件相关命令")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command", required=True)

    plugin_check = plugin_subparsers.add_parser("check", help="在隔离环境中自检插件")
    plugin_check.add_argument("path", help="待检查的插件文件或包目录")
    plugin_check.add_argument("--level", choices=("load", "smoke", "strict"), default="smoke")
    plugin_check.add_argument("--timeout", type=int, default=30, help="子进程检查超时时间（秒）")
    plugin_check.add_argument("--json", action="store_true", help="输出 JSON 结果")
    plugin_check.add_argument("--keep-temp", action="store_true", help="保留本次检查的临时数据目录")

    worker_parser = subparsers.add_parser("__plugin-check-worker")
    worker_parser.add_argument("path")
    worker_parser.add_argument("--level", choices=("load", "smoke", "strict"), default="smoke")
    worker_parser.add_argument("--report-file", required=True)

    return parser


def _build_fallback_report(candidate_path: str, level: str, error_message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "level": level,
        "candidate_path": candidate_path,
        "runtime_data_dir": "",
        "staged_path": "",
        "staged_entry_path": "",
        "stage_mode": "file",
        "plugin": None,
        "checks": [
            {
                "id": "worker_error",
                "title": "执行插件检查 CLI",
                "ok": False,
                "detail": "插件检查子进程未能产出有效报告",
                "error": error_message,
            }
        ],
        "warnings": [],
        "errors": [error_message],
        "load_failures": [],
    }


def _load_report(report_file: Path, candidate_path: str, level: str, fallback_error: str) -> dict[str, Any]:
    if report_file.exists():
        try:
            return json.loads(report_file.read_text(encoding="utf-8"))
        except Exception as e:
            return _build_fallback_report(candidate_path, level, f"读取报告文件失败: {e}")
    return _build_fallback_report(candidate_path, level, fallback_error)


def _format_report(report: dict[str, Any], *, temp_root: str | None = None) -> str:
    lines = [f"插件检查结果: {'通过' if report.get('ok') else '失败'}"]
    plugin = report.get("plugin") or {}
    if plugin:
        lines.append(f"插件: {plugin.get('key', '')} ({plugin.get('version', '')})")
    lines.append(f"检查级别: {report.get('level', 'smoke')}")
    lines.append(f"候选路径: {report.get('candidate_path', '')}")
    for check in report.get("checks", []):
        status = "PASS" if check.get("ok") else "FAIL"
        detail = check.get("detail") or check.get("error") or ""
        lines.append(f"[{status}] {check.get('title', check.get('id', 'check'))}: {detail}".rstrip())
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("警告:")
        lines.extend(f"- {warning}" for warning in warnings)
    errors = report.get("errors") or []
    if errors:
        lines.append("错误:")
        lines.extend(f"- {error}" for error in errors)
    if temp_root:
        lines.append(f"临时目录: {temp_root}")
    return "\n".join(lines)


def _run_plugin_check(args: argparse.Namespace) -> int:
    candidate_path = str(Path(args.path).expanduser().resolve())
    temp_root: str | None = None
    cleanup_temp = not args.keep_temp
    report: dict[str, Any]

    if not Path(candidate_path).exists():
        report = _build_fallback_report(candidate_path, args.level, f"插件路径不存在: {candidate_path}")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(_format_report(report))
        return 1

    try:
        temp_root = tempfile.mkdtemp(prefix="nekro-plugin-check-")
        report_file = Path(temp_root) / "plugin_check_report.json"
        env = os.environ.copy()
        env["NEKRO_CLI_MODE"] = "true"
        env["NEKRO_DATA_DIR"] = str(Path(temp_root) / "data")
        env["NEKRO_AUTO_DB_MIGRATE"] = "true"

        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "__plugin-check-worker",
            candidate_path,
            "--level",
            args.level,
            "--report-file",
            str(report_file),
        ]
        completed = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
        fallback_error = (completed.stderr or completed.stdout or f"子进程退出码 {completed.returncode}").strip()
        report = _load_report(report_file, candidate_path, args.level, fallback_error)
    except subprocess.TimeoutExpired:
        report = _build_fallback_report(candidate_path, args.level, f"插件检查超时（>{args.timeout}s）")
        report["checks"][0]["id"] = "worker_timeout"
        report["checks"][0]["title"] = "执行插件检查 CLI"
        report["checks"][0]["detail"] = "插件检查子进程超时"
    finally:
        if cleanup_temp and temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    if args.keep_temp and temp_root:
        report["warnings"] = [*(report.get("warnings") or []), f"已保留临时目录: {temp_root}"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_report(report, temp_root=temp_root if args.keep_temp else None))
    return 0 if report.get("ok") else 1


def _run_plugin_check_worker(args: argparse.Namespace) -> int:
    os.environ.setdefault("NEKRO_CLI_MODE", "true")
    os.chdir(REPO_ROOT)

    from nekro_agent.services.plugin.checker import run_plugin_check

    report = asyncio.run(run_plugin_check(args.path, level=args.level))
    Path(args.report_file).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return 0 if report.ok else 1


def main() -> int:
    os.chdir(REPO_ROOT)
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "plugin" and args.plugin_command == "check":
        return _run_plugin_check(args)
    if args.command == "__plugin-check-worker":
        return _run_plugin_check_worker(args)
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
