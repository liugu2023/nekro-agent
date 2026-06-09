from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = ROOT / "run_nekro_cli.py"


def test_plugin_check_cli_success(tmp_path: Path):
    plugin_file = tmp_path / "demo_plugin.py"
    plugin_file.write_text(
        "\n".join(
            [
                'from nekro_agent.api.plugin import NekroPlugin',
                '',
                'plugin = NekroPlugin(',
                '    name="DemoPlugin",',
                '    module_name="demo_plugin",',
                '    description="demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ')',
                '',
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(plugin_file), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["plugin"]["module_name"] == "demo_plugin"


def test_plugin_check_cli_writes_report_file(tmp_path: Path):
    plugin_file = tmp_path / "report_demo.py"
    report_file = tmp_path / "reports" / "plugin-check.json"
    plugin_file.write_text(
        "\n".join(
            [
                "from nekro_agent.api.plugin import NekroPlugin",
                "",
                "plugin = NekroPlugin(",
                '    name="ReportDemo",',
                '    module_name="report_demo",',
                '    description="report demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ")",
            ],
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "plugin",
            "check",
            str(plugin_file),
            "--report-file",
            str(report_file),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "插件检查结果: 通过" in completed.stdout
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["plugin"]["module_name"] == "report_demo"


def test_plugin_check_cli_writes_report_file_for_missing_path(tmp_path: Path):
    plugin_file = tmp_path / "missing.py"
    report_file = tmp_path / "reports" / "missing-plugin-check.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "plugin",
            "check",
            str(plugin_file),
            "--report-file",
            str(report_file),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "插件检查结果: 失败" in completed.stdout
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["candidate_path"] == str(plugin_file.resolve())
    assert any("插件路径不存在" in error for error in report["errors"])


def test_plugin_check_cli_failure(tmp_path: Path):
    plugin_file = tmp_path / "broken_plugin.py"
    plugin_file.write_text("plugin = 123\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(plugin_file), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["ok"] is False
    assert any(check["id"] == "plugin_load" and not check["ok"] for check in report["checks"])


def test_plugin_check_cli_package_success(tmp_path: Path):
    package_dir = tmp_path / "package_demo"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (package_dir / "plugin.py").write_text(
        "\n".join(
            [
                "from nekro_agent.api.plugin import NekroPlugin",
                "",
                "plugin = NekroPlugin(",
                '    name="PackageDemo",',
                '    module_name="package_demo",',
                '    description="package demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(package_dir), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["stage_mode"] == "package"
    assert report["plugin"]["module_name"] == "package_demo"


def test_plugin_check_cli_disabled_file_success(tmp_path: Path):
    plugin_file = tmp_path / "disabled_demo.py.disabled"
    plugin_file.write_text(
        "\n".join(
            [
                "from nekro_agent.api.plugin import NekroPlugin",
                "",
                "plugin = NekroPlugin(",
                '    name="DisabledDemo",',
                '    module_name="disabled_demo",',
                '    description="disabled demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(plugin_file), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["plugin"]["module_name"] == "disabled_demo"
    assert any(".py 文件名暂存" in warning for warning in report["warnings"])


def test_plugin_check_cli_strict_reports_async_contract_issue(tmp_path: Path):
    plugin_file = tmp_path / "sync_method_plugin.py"
    plugin_file.write_text(
        "\n".join(
            [
                "from nekro_agent.api.plugin import NekroPlugin, SandboxMethodType",
                "",
                "plugin = NekroPlugin(",
                '    name="SyncMethodPlugin",',
                '    module_name="sync_method_plugin",',
                '    description="sync method demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ")",
                "",
                '@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "同步方法")',
                "def sync_method(_ctx):",
                '    return "ok"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(plugin_file), "--level", "strict", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["ok"] is False
    assert any(check["id"] == "async_contracts" and not check["ok"] for check in report["checks"])


def test_plugin_check_cli_strict_reports_duplicate_method_names(tmp_path: Path):
    plugin_file = tmp_path / "duplicate_method_plugin.py"
    plugin_file.write_text(
        "\n".join(
            [
                "from nekro_agent.api.plugin import NekroPlugin, SandboxMethodType",
                "from nekro_agent.api.schemas import AgentCtx",
                "",
                "plugin = NekroPlugin(",
                '    name="DuplicateMethodPlugin",',
                '    module_name="duplicate_method_plugin",',
                '    description="duplicate method demo",',
                '    version="0.1.0",',
                '    author="Tester",',
                '    url="https://example.com",',
                ")",
                "",
                '@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "重复标题")',
                "async def first_method(_ctx: AgentCtx) -> str:",
                '    return "first"',
                "",
                '@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "重复标题")',
                "async def second_method(_ctx: AgentCtx) -> str:",
                '    return "second"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "plugin", "check", str(plugin_file), "--level", "strict", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["ok"] is False
    assert any(check["id"] == "duplicate_method_names" and not check["ok"] for check in report["checks"])
