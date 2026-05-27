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
