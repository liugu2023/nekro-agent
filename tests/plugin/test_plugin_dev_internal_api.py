from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _fake_sandbox_runtime(*, tools: list[str], model_label: str = "claude-test") -> SimpleNamespace:
    return SimpleNamespace(
        container_name="nekro-plugin-dev-test",
        container_id="abc123",
        api_endpoint="http://127.0.0.1:12345",
        healthy=True,
        preset_id=1,
        preset_name="test-preset",
        model_type="manual",
        model_label=model_label,
        tools=tools,
    )


def test_plugin_dev_internal_gateway_creates_proposal_without_writing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NEKRO_DATA_DIR", str(tmp_path / "data"))

    from nekro_agent.routers.plugin_dev import internal_router
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
    from nekro_agent.services.plugin_dev.tasks import get_latest_pending_proposal_for_task

    plugin_root = tmp_path / "plugins"
    proposal_root = tmp_path / "proposals"
    task_root = tmp_path / "tasks"
    plugin_root.mkdir()
    task_root.mkdir()
    plugin_file = plugin_root / "demo.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")

    monkeypatch.setattr(
        "nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR",
        str(plugin_root),
    )
    monkeypatch.setattr(
        "nekro_agent.services.plugin_dev.tasks.PLUGIN_DEV_PROPOSAL_DIR",
        proposal_root,
    )
    monkeypatch.setattr("nekro_agent.services.plugin_dev.tasks.PLUGIN_DEV_TASK_DIR", task_root)
    _write_plugin_dev_task_file(task_root, "test-task", "running_cc")
    (task_root / "test-task-pkg.json").write_text(
        json.dumps(
            {
                "task_id": "test-task-pkg",
                "file_path": "mypkg/plugin.py",
                "status": "running_cc",
                "summary": "",
                "logs": [],
                "proposal_id": None,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(PluginDevSandboxService, "get_internal_api_token", staticmethod(lambda: "secret-token"))

    checked_levels: list[str] = []

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "static", extra_files: dict | None = None
    ):
        checked_levels.append(level)
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr("nekro_agent.routers.plugin_dev.run_plugin_self_check", fake_run_plugin_self_check)

    app = FastAPI()
    app.include_router(internal_router)
    client = TestClient(app)
    headers = {"X-Internal-API-Token": "secret-token"}

    files_response = client.get("/internal/plugin-dev/files", headers=headers)
    assert files_response.status_code == 200
    assert files_response.json() == ["demo.py"]

    file_response = client.get("/internal/plugin-dev/file", params={"path": "demo.py"}, headers=headers)
    assert file_response.status_code == 200
    assert file_response.json()["content"] == "plugin = None\n"

    proposal_response = client.post(
        "/internal/plugin-dev/proposals",
        headers=headers,
        json={
            "file_path": "demo.py",
            "content": "plugin = 'updated'\n",
            "task_id": "test-task",
            "summary": "测试内部提案",
        },
    )
    assert proposal_response.status_code == 200
    proposal = proposal_response.json()
    assert proposal["status"] == "pending"
    assert proposal["task_id"] == "test-task"
    assert proposal["file_path"] == "demo.py"
    assert "plugin = 'updated'" in proposal["result_code"]
    from nekro_agent.services.plugin_dev.host_file_gateway import sha256_text

    assert proposal["before_sha256"] == sha256_text("plugin = None\n")
    latest_proposal = get_latest_pending_proposal_for_task("test-task")
    assert latest_proposal is not None
    assert latest_proposal.proposal_id == proposal["proposal_id"]
    assert plugin_file.read_text(encoding="utf-8") == "plugin = None\n"

    check_response = client.post(
        "/internal/plugin-dev/check",
        headers=headers,
        json={
            "file_path": "demo.py",
            "content": "plugin = 'checked'\n",
            "task_id": "test-task",
            "level": "smoke",
        },
    )
    assert check_response.status_code == 200
    check_payload = check_response.json()
    assert check_payload["ok"] is True
    # 内部网关自检必须被钳制为 static 级别，绝不执行沙盒提交的候选代码
    assert checked_levels == ["static"]
    assert any("static" in warning for warning in check_payload["warnings"])

    # 包形式插件：多文件提案
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    multi_response = client.post(
        "/internal/plugin-dev/proposals",
        headers=headers,
        json={
            "file_path": "mypkg/plugin.py",
            "content": "plugin = 'pkg-updated'\n",
            "task_id": "test-task-pkg",
            "summary": "包多文件提案",
            "files": [
                {"file_path": "mypkg/plugin.py", "content": "plugin = 'pkg-updated'\n"},
                {"file_path": "mypkg/__init__.py", "content": "from .plugin import plugin\n"},
                {"file_path": "mypkg/utils.py", "content": "VALUE = 1\n"},
            ],
        },
    )
    assert multi_response.status_code == 200
    multi_proposal = multi_response.json()
    assert multi_proposal["file_path"] == "mypkg/plugin.py"
    assert {item["file_path"] for item in multi_proposal["files"]} == {
        "mypkg/plugin.py",
        "mypkg/__init__.py",
        "mypkg/utils.py",
    }
    assert "b/mypkg/utils.py" in multi_proposal["diff"]
    assert not (pkg_dir / "utils.py").exists()

    # 任务已终态后不允许再通过网关注入新提案
    from nekro_agent.schemas.errors import ValidationError

    # 多文件提案不允许跨插件根目录
    with pytest.raises(ValidationError):
        client.post(
            "/internal/plugin-dev/proposals",
            headers=headers,
            json={
                "file_path": "mypkg/plugin.py",
                "content": "plugin = 'x'\n",
                "task_id": "test-task-pkg",
                "summary": "跨根提案",
                "files": [
                    {"file_path": "mypkg/plugin.py", "content": "plugin = 'x'\n"},
                    {"file_path": "otherpkg/evil.py", "content": "VALUE = 1\n"},
                ],
            },
        )

    # 单文件插件不允许附带其他文件
    with pytest.raises(ValidationError):
        client.post(
            "/internal/plugin-dev/proposals",
            headers=headers,
            json={
                "file_path": "demo.py",
                "content": "plugin = 'x'\n",
                "task_id": "test-task",
                "summary": "单文件夹带",
                "files": [
                    {"file_path": "demo.py", "content": "plugin = 'x'\n"},
                    {"file_path": "sneaky.py", "content": "VALUE = 1\n"},
                ],
            },
        )

    (task_root / "finished-task.json").write_text(
        json.dumps({"task_id": "finished-task", "file_path": "demo.py", "status": "waiting_apply"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        client.post(
            "/internal/plugin-dev/proposals",
            headers=headers,
            json={
                "file_path": "demo.py",
                "content": "plugin = 'late'\n",
                "task_id": "finished-task",
                "summary": "迟到提案",
            },
        )


def test_plugin_dev_reference_source_uses_runtime_snapshot(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import sandbox

    source_dir = tmp_path / "source-cache" / "nekro-agent"
    captured: dict[str, object] = {}

    def fake_update_source_lock_info(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(sandbox, "PLUGIN_DEV_NEKRO_SOURCE_DIR", source_dir)
    monkeypatch.setattr(sandbox, "update_source_lock_info", fake_update_source_lock_info)
    source_dir.mkdir(parents=True)
    old_inode = source_dir.stat().st_ino
    (source_dir / "stale.txt").write_text("old", encoding="utf-8")

    result_dir, message = sandbox.PluginDevSandboxService._prepare_runtime_source_snapshot()

    assert result_dir == source_dir
    assert "本地运行环境源码快照" in message
    assert source_dir.stat().st_ino == old_inode
    assert not (source_dir / "stale.txt").exists()
    assert (source_dir / "nekro_agent").is_dir()
    assert (source_dir / "run_nekro_cli.py").is_file()
    assert not (source_dir / ".git").exists()
    assert captured["source_origin"] == "runtime_snapshot"
    assert captured["source_path"] == str(source_dir.resolve())
    assert isinstance(captured["source_dirty"], bool)


def test_plugin_dev_task_workspace_is_writable_by_sandbox_user(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import sandbox

    plugin_root = tmp_path / "plugins"
    workspace_dir = tmp_path / "workspace"
    plugin_root.mkdir()
    (plugin_root / "demo.py").write_text("plugin = None\n", encoding="utf-8")

    monkeypatch.setattr(sandbox, "PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(
        "nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR",
        str(plugin_root),
    )

    container_path = sandbox.PluginDevSandboxService.prepare_task_workspace("demo.py", "plugin = 'candidate'\n")
    staged_path = workspace_dir / "default" / "current" / "demo.py"

    assert container_path == "/workspace/default/current/demo.py"
    assert staged_path.read_text(encoding="utf-8") == "plugin = 'candidate'\n"
    assert stat.S_IMODE((workspace_dir / "default" / "current").stat().st_mode) == 0o777
    assert stat.S_IMODE(staged_path.stat().st_mode) == 0o666
    command = sandbox.PluginDevSandboxService.build_self_check_command(container_path, "demo.py", "task-1")
    assert command == "python /workspace/default/plugin_dev_check.py /workspace/default/current/demo.py demo.py task-1 static"


@pytest.mark.asyncio
async def test_plugin_dev_task_retries_cc_after_self_check_failure(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    candidate_host_path = workspace_dir / "default" / "current" / "demo.py"
    task_id = "plugin-dev-retry-test"
    task_dir.mkdir()
    proposal_dir.mkdir()
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": "demo.py",
                "status": "pending",
                "summary": "修复插件",
                "logs": [],
                "proposal_id": None,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prompts: list[str] = []
    checked_codes: list[str] = []
    checked_levels: list[str] = []
    self_check_command = (
        "python /workspace/default/plugin_dev_check.py /workspace/default/current/demo.py demo.py plugin-dev-retry-test static"
    )

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        candidate_host_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_host_path.write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/demo.py"

    async def fake_stream_generate(prompt: str):
        prompts.append(prompt)
        yield {"type": "tool_call", "name": "Read", "tool_use_id": "tool-read", "input": {"file_path": "/workspace/default/current/demo.py"}}
        yield {"type": "tool_result", "tool_use_id": "tool-read"}
        yield {"type": "tool_call", "name": "Write", "tool_use_id": "tool-write", "arguments": {"path": "/workspace/default/current/demo.py"}}
        yield {"type": "tool_result", "tool_use_id": "tool-write"}
        yield {"type": "tool_call", "name": "Edit", "tool_use_id": "tool-1", "input": {"file_path": "/workspace/default/current/demo.py"}}
        yield {"type": "tool_result", "tool_use_id": "tool-1"}
        if len(prompts) == 1:
            candidate_host_path.write_text("plugin = 'broken\n", encoding="utf-8")
        else:
            candidate_host_path.write_text("plugin = 'fixed'\n", encoding="utf-8")
        yield {
            "type": "tool_call",
            "name": "Bash",
            "tool_use_id": "tool-bash",
            "input": {
                "command": self_check_command,
                "description": "执行插件自检",
                "cwd": "/workspace/default",
            },
        }
        yield {"type": "tool_result", "tool_use_id": "tool-bash", "content": '{"ok": true}', "is_error": False}
        yield "已写入第一轮候选" if len(prompts) == 1 else "已写入第二轮候选"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Write", "Edit", "Bash"])

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "static", extra_files: dict | None = None
    ):
        checked_codes.append(code)
        checked_levels.append(level)
        if len(checked_codes) == 1:
            return PluginCheckReport(
                candidate_path=file_path,
                checks=[
                    PluginCheckItem(
                        id="plugin_load",
                        title="加载插件",
                        ok=False,
                        error="unterminated string literal (plugin.py, line 1)",
                    )
                ],
            )
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(
        PluginDevSandboxService,
        "inspect_runtime",
        staticmethod(fake_inspect_runtime),
    )
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))
    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)

    body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="修复插件",
        current_code="plugin = None\n",
        base_code="plugin = None\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "修复插件")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "waiting_apply"
    assert task_data["result_code"].strip() == "plugin = 'fixed'"
    assert len(prompts) == 2
    assert "unterminated string literal" in prompts[1]
    # 迭代期宿主机复核必须是静态检查，绝不执行候选代码
    assert checked_levels == ["static", "static"]
    assert any('"name":"Edit"' in log for log in task_data["logs"])
    assert any("工具结果：" in log and '"name":"Edit"' in log for log in task_data["logs"])
    assert any('"name":"Read"' in log and '"/workspace/default/current/demo.py"' in log for log in task_data["logs"])
    assert any('"name":"Write"' in log and '"/workspace/default/current/demo.py"' in log for log in task_data["logs"])
    assert any('"name":"Bash"' in log and "plugin_dev_check.py" in log for log in task_data["logs"])
    assert any("工具结果：" in log and '"name":"Bash"' in log for log in task_data["logs"])
    assert any("检测到 CC 已修改工作副本" in log for log in task_data["logs"])
    assert any("宿主机复核未通过，已将失败报告交回 CC 自动修复" in log for log in task_data["logs"])


@pytest.mark.asyncio
async def test_plugin_dev_task_does_not_self_check_unchanged_default_code(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    candidate_host_path = workspace_dir / "default" / "current" / "demo.py"
    task_id = "plugin-dev-unchanged-default-test"
    task_dir.mkdir()
    proposal_dir.mkdir()
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": "demo.py",
                "status": "pending",
                "summary": "生成插件",
                "logs": [],
                "proposal_id": None,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    checked_codes: list[str] = []

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        candidate_host_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_host_path.write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/demo.py"

    async def fake_stream_generate(_prompt: str):
        yield {"type": "tool_call", "name": "Read"}
        yield "```python\nplugin = 'default-broken\n```"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Write", "Edit", "Bash"])

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "smoke", extra_files: dict | None = None
    ):
        checked_codes.append(code)
        raise AssertionError(f"不应该自检未落地的默认/文本候选: {file_path} {level}")

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(
        PluginDevSandboxService,
        "inspect_runtime",
        staticmethod(fake_inspect_runtime),
    )
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))
    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)

    body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="生成插件",
        current_code="plugin = 'default-broken\n",
        base_code="plugin = 'default-broken\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "生成插件")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "failed"
    assert not checked_codes
    assert "没有检测到 CC 沙盒提交新的候选代码" in task_data["error"]
    assert any("未对默认/当前代码执行自检" in log for log in task_data["logs"])


@pytest.mark.asyncio
async def test_plugin_dev_task_fails_fast_when_sandbox_write_tools_missing(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    candidate_host_path = workspace_dir / "default" / "current" / "demo.py"
    task_id = "plugin-dev-tools-missing-test"
    task_dir.mkdir()
    proposal_dir.mkdir()
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": "demo.py",
                "status": "pending",
                "summary": "生成插件",
                "logs": [],
                "proposal_id": None,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stream_called = False

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        candidate_host_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_host_path.write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/demo.py"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Bash"])

    async def fake_stream_generate(_prompt: str):
        nonlocal stream_called
        stream_called = True
        yield "不应该执行到这里"

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(PluginDevSandboxService, "inspect_runtime", staticmethod(fake_inspect_runtime))
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))

    body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="生成插件",
        current_code="plugin = None\n",
        base_code="plugin = None\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "生成插件")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "failed"
    assert "缺少 Write/Edit/MultiEdit 工具" in task_data["error"]
    assert stream_called is False


@pytest.mark.asyncio
async def test_plugin_dev_task_fails_fast_on_cc_model_error(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    candidate_host_path = workspace_dir / "default" / "current" / "demo.py"
    task_id = "plugin-dev-model-error-test"
    task_dir.mkdir()
    proposal_dir.mkdir()
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": "demo.py",
                "status": "pending",
                "summary": "生成插件",
                "logs": [],
                "proposal_id": None,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    checked_codes: list[str] = []

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        candidate_host_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_host_path.write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/demo.py"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Write", "Edit", "Bash"], model_label="gpt-5.5")

    async def fake_stream_generate(_prompt: str):
        yield "There's an issue with the selected model (gpt-5.5). It may not exist or you may not have access to it. Run --model to pick a different model."

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "smoke", extra_files: dict | None = None
    ):
        checked_codes.append(code)
        raise AssertionError(f"模型错误不应该进入自检: {file_path} {level}")

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(PluginDevSandboxService, "inspect_runtime", staticmethod(fake_inspect_runtime))
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))
    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)

    body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="生成插件",
        current_code="plugin = None\n",
        base_code="plugin = None\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "生成插件")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "failed"
    assert "CC 模型配置不可用" in task_data["error"]
    assert "gpt-5.5" in task_data["error"]
    assert not checked_codes
    assert any("CC 沙盒已启动" in log for log in task_data["logs"])
    assert any("CC 模型组" in log and "gpt-5.5" in log for log in task_data["logs"])


@pytest.mark.asyncio
async def test_plugin_dev_apply_proposal_rejects_concurrent_modification(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.host_file_gateway import sha256_text

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    plugin_root.mkdir()
    proposal_dir.mkdir()
    task_dir.mkdir()
    plugin_file = plugin_root / "demo.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")

    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    checked_levels: list[str] = []

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "smoke", extra_files: dict | None = None
    ):
        checked_levels.append(level)
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)
    monkeypatch.setattr(tasks, "record_version", lambda **_kwargs: "version-test")

    proposal = tasks.create_proposal(
        task_id="apply-test",
        file_path="demo.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="更新插件",
    )
    _write_plugin_dev_task_file(
        task_dir,
        "apply-test",
        "waiting_apply",
        proposal_id=proposal.proposal_id,
    )
    assert proposal.before_sha256 == sha256_text("plugin = None\n")

    plugin_file.write_text("plugin = 'changed-by-user'\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        await tasks.apply_proposal(proposal.proposal_id)
    assert plugin_file.read_text(encoding="utf-8") == "plugin = 'changed-by-user'\n"
    assert tasks.get_proposal(proposal.proposal_id).status == "pending"

    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    version_id = await tasks.apply_proposal(proposal.proposal_id)
    assert version_id == "version-test"
    assert plugin_file.read_text(encoding="utf-8") == "plugin = 'updated'\n"
    assert tasks.get_proposal(proposal.proposal_id).status == "applied"
    # 应用提案时执行的是完整加载检查
    assert checked_levels == ["smoke"]


def _write_plugin_dev_task_file(
    task_dir: Path,
    task_id: str,
    status: str,
    *,
    file_path: str = "demo.py",
    proposal_id: str | None = None,
) -> None:
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": file_path,
                "status": status,
                "summary": "",
                "logs": [],
                "proposal_id": proposal_id,
                "diff": "",
                "result_code": "",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_plugin_dev_cancel_queued_task_keeps_active_sandbox_run(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_dev import PluginDevVersionInfo
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    task_dir.mkdir()
    proposal_dir.mkdir()
    _write_plugin_dev_task_file(task_dir, "queued-task", "pending")
    _write_plugin_dev_task_file(task_dir, "active-task", "running_cc")

    cancel_calls: list[str] = []

    async def fake_cancel_current_task() -> bool:
        cancel_calls.append("called")
        return True

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "_ACTIVE_TASK_ID", "active-task")
    monkeypatch.setattr(
        tasks,
        "get_version_info",
        lambda: PluginDevVersionInfo(updated_at="2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(PluginDevSandboxService, "cancel_current_task", staticmethod(fake_cancel_current_task))

    stale_proposal = tasks.create_proposal(
        task_id="queued-task",
        file_path="demo.py",
        before="plugin = None\n",
        after="plugin = 'queued'\n",
        summary="排队任务的提案",
    )

    cancelled_queued = await tasks.cancel_task("queued-task")
    assert cancelled_queued.status == "cancelled"
    # 取消排队中的任务不允许中断沙盒里正在运行的活动任务
    assert cancel_calls == []
    # 取消任务时应丢弃其遗留的 pending 提案
    assert tasks.get_proposal(stale_proposal.proposal_id).status == "discarded"

    cancelled_active = await tasks.cancel_task("active-task")
    assert cancelled_active.status == "cancelled"
    assert cancel_calls == ["called"]


def test_plugin_dev_recover_stale_tasks_marks_them_failed(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import tasks

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    task_dir.mkdir()
    proposal_dir.mkdir()
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    stale_proposal = tasks.create_proposal(
        task_id="stale-running",
        file_path="demo.py",
        before="plugin = None\n",
        after="plugin = 'stale'\n",
        summary="重启前遗留提案",
    )
    _write_plugin_dev_task_file(
        task_dir,
        "stale-running",
        "running_cc",
        proposal_id=stale_proposal.proposal_id,
    )
    _write_plugin_dev_task_file(task_dir, "stale-pending", "pending")
    _write_plugin_dev_task_file(task_dir, "done-task", "applied")

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    recovered = tasks.recover_stale_plugin_dev_tasks()

    assert recovered == 2
    for task_id in ("stale-running", "stale-pending"):
        data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
        assert data["status"] == "failed"
        assert "服务重启" in data["error"]
    done_data = json.loads((task_dir / "done-task.json").read_text(encoding="utf-8"))
    assert done_data["status"] == "applied"
    assert tasks.get_proposal(stale_proposal.proposal_id).status == "discarded"


@pytest.mark.asyncio
async def test_plugin_dev_create_task_validates_size_and_queue(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks

    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    (plugin_root / "demo.py").write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))

    oversized_body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="生成插件",
        current_code="x = 1\n" + "#" * (512 * 1024 + 1),
        base_code="",
        dirty=False,
    )
    with pytest.raises(ValidationError):
        await tasks.create_task(oversized_body)

    monkeypatch.setattr(tasks, "get_task_runtime_snapshot", lambda: ("active-task", 3))
    queued_body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="生成插件",
        current_code="plugin = None\n",
        base_code="",
        dirty=False,
    )
    with pytest.raises(ValidationError):
        await tasks.create_task(queued_body)


def test_plugin_dev_cleanup_artifacts_removes_stale_files(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import tasks

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    task_dir.mkdir()
    proposal_dir.mkdir()

    _write_plugin_dev_task_file(task_dir, "old-failed", "failed")
    _write_plugin_dev_task_file(task_dir, "old-waiting", "waiting_apply")
    _write_plugin_dev_task_file(task_dir, "fresh-failed", "failed")
    task_stale_ts = time.time() - 40 * 86400
    os.utime(task_dir / "old-failed.json", (task_stale_ts, task_stale_ts))
    os.utime(task_dir / "old-waiting.json", (task_stale_ts, task_stale_ts))

    def write_proposal_file(name: str, status: str) -> Path:
        path = proposal_dir / f"proposal-{name}.json"
        path.write_text(
            json.dumps({"proposal_id": f"proposal-{name}", "task_id": "t", "status": status}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    old_discarded = write_proposal_file("old-discarded", "discarded")
    old_pending = write_proposal_file("old-pending", "pending")
    fresh_applied = write_proposal_file("fresh-applied", "applied")
    proposal_stale_ts = time.time() - 10 * 86400
    os.utime(old_discarded, (proposal_stale_ts, proposal_stale_ts))
    os.utime(old_pending, (proposal_stale_ts, proposal_stale_ts))

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)

    removed_tasks, removed_proposals = tasks.cleanup_plugin_dev_artifacts()

    assert removed_tasks == 1
    assert removed_proposals == 1
    assert not (task_dir / "old-failed.json").exists()
    # waiting_apply 与未超期的终态任务保留
    assert (task_dir / "old-waiting.json").exists()
    assert (task_dir / "fresh-failed.json").exists()
    assert not old_discarded.exists()
    # pending 提案与未超期的已处理提案保留
    assert old_pending.exists()
    assert fresh_applied.exists()


@pytest.mark.asyncio
async def test_plugin_dev_package_task_produces_multi_file_proposal(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    plugin_root = tmp_path / "plugins"
    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    current_root = workspace_dir / "default" / "current"
    task_id = "plugin-dev-package-test"
    plugin_root.mkdir()
    task_dir.mkdir()
    proposal_dir.mkdir()
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    _write_plugin_dev_task_file(task_dir, task_id, "pending")

    checked_extra_files: list[dict] = []
    self_check_command = (
        f"python /workspace/default/plugin_dev_check.py /workspace/default/current/mypkg mypkg/plugin.py {task_id} static"
    )

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        # 模拟真实 staging：拷贝真实包 + 覆盖主文件
        pkg_stage = current_root / "mypkg"
        pkg_stage.mkdir(parents=True, exist_ok=True)
        (pkg_stage / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
        (pkg_stage / "plugin.py").write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/mypkg/plugin.py"

    async def fake_stream_generate(_prompt: str):
        yield {"type": "tool_call", "name": "Write", "tool_use_id": "tool-write", "input": {"file_path": "/workspace/default/current/mypkg/plugin.py"}}
        yield {"type": "tool_result", "tool_use_id": "tool-write"}
        (current_root / "mypkg" / "plugin.py").write_text("plugin = 'pkg-fixed'\n", encoding="utf-8")
        (current_root / "mypkg" / "utils.py").write_text("VALUE = 1\n", encoding="utf-8")
        yield {
            "type": "tool_call",
            "name": "Bash",
            "tool_use_id": "tool-bash",
            "input": {"command": self_check_command, "description": "执行插件自检", "cwd": "/workspace/default"},
        }
        yield {"type": "tool_result", "tool_use_id": "tool-bash", "content": '{"ok": true}', "is_error": False}
        yield "已写入包候选"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Write", "Edit", "Bash"])

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "static", extra_files: dict | None = None
    ):
        checked_extra_files.append(dict(extra_files or {}))
        assert level == "static"
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(PluginDevSandboxService, "inspect_runtime", staticmethod(fake_inspect_runtime))
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))
    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)

    body = PluginDevGenerateRequest(
        file_path="mypkg/plugin.py",
        prompt="给包插件加工具模块",
        current_code="plugin = None\n",
        base_code="plugin = None\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "给包插件加工具模块")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "waiting_apply"
    assert task_data["result_code"].strip() == "plugin = 'pkg-fixed'"
    assert any("工作副本包目录 mypkg/" in log for log in task_data["logs"])
    # 宿主复核收到包内其他文件
    assert len(checked_extra_files) == 1
    assert set(checked_extra_files[0]) == {"mypkg/__init__.py", "mypkg/utils.py"}

    proposal = tasks.get_proposal(task_data["proposal_id"])
    assert {item.file_path for item in proposal.files} == {
        "mypkg/plugin.py",
        "mypkg/__init__.py",
        "mypkg/utils.py",
    }
    assert "b/mypkg/utils.py" in proposal.diff
    # 真实插件目录未被写入
    assert (pkg_dir / "plugin.py").read_text(encoding="utf-8") == "plugin = None\n"
    assert not (pkg_dir / "utils.py").exists()


@pytest.mark.asyncio
async def test_plugin_dev_single_file_task_ignores_out_of_scope_files(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest
    from nekro_agent.services.plugin_dev import tasks
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    task_dir = tmp_path / "tasks"
    proposal_dir = tmp_path / "proposals"
    workspace_dir = tmp_path / "workspace"
    current_root = workspace_dir / "default" / "current"
    task_id = "plugin-dev-out-of-scope-test"
    task_dir.mkdir()
    proposal_dir.mkdir()
    _write_plugin_dev_task_file(task_dir, task_id, "pending")

    def fake_prepare_task_workspace(_file_path: str, current_code: str) -> str:
        current_root.mkdir(parents=True, exist_ok=True)
        (current_root / "demo.py").write_text(current_code, encoding="utf-8")
        return "/workspace/default/current/demo.py"

    async def fake_stream_generate(_prompt: str):
        yield {"type": "tool_call", "name": "Write", "tool_use_id": "tool-write", "input": {"file_path": "/workspace/default/current/demo.py"}}
        yield {"type": "tool_result", "tool_use_id": "tool-write"}
        (current_root / "demo.py").write_text("plugin = 'fixed'\n", encoding="utf-8")
        (current_root / "sneaky.py").write_text("VALUE = 1\n", encoding="utf-8")
        yield {
            "type": "tool_call",
            "name": "Bash",
            "tool_use_id": "tool-bash",
            "input": {
                "command": f"python /workspace/default/plugin_dev_check.py /workspace/default/current/demo.py demo.py {task_id} static",
                "description": "执行插件自检",
            },
        }
        yield {"type": "tool_result", "tool_use_id": "tool-bash", "content": '{"ok": true}', "is_error": False}
        yield "完成"

    async def fake_inspect_runtime(refresh_tools: bool = False):
        assert refresh_tools is True
        return _fake_sandbox_runtime(tools=["Read", "Write", "Edit", "Bash"])

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "static", extra_files: dict | None = None
    ):
        assert not extra_files, "单文件任务不应携带额外文件"
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr("nekro_agent.services.plugin_dev.sandbox.PLUGIN_DEV_WORKSPACE_DIR", workspace_dir)
    monkeypatch.setattr(PluginDevSandboxService, "prepare_task_workspace", staticmethod(fake_prepare_task_workspace))
    monkeypatch.setattr(PluginDevSandboxService, "inspect_runtime", staticmethod(fake_inspect_runtime))
    monkeypatch.setattr(PluginDevSandboxService, "stream_generate", staticmethod(fake_stream_generate))
    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)

    body = PluginDevGenerateRequest(
        file_path="demo.py",
        prompt="修复插件",
        current_code="plugin = None\n",
        base_code="plugin = None\n",
        dirty=False,
    )

    await tasks._execute_task(task_id, body, "修复插件")

    task_data = json.loads((task_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert task_data["status"] == "waiting_apply"
    assert any("已忽略插件范围外的工作副本文件变更：sneaky.py" in log for log in task_data["logs"])
    proposal = tasks.get_proposal(task_data["proposal_id"])
    assert [item.file_path for item in proposal.files] == ["demo.py"]


@pytest.mark.asyncio
async def test_plugin_dev_apply_multi_file_proposal_atomically(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    plugin_root.mkdir()
    proposal_dir.mkdir()
    task_dir.mkdir()
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")

    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    recorded_versions: list[str] = []
    checked_extra_files: list[dict] = []

    async def fake_run_plugin_self_check(
        file_path: str, code: str, level: str = "smoke", extra_files: dict | None = None
    ):
        assert level == "smoke"
        checked_extra_files.append(dict(extra_files or {}))
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    def fake_record_version(**kwargs):
        recorded_versions.append(str(kwargs.get("file_path")))
        return f"version-{len(recorded_versions)}"

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_run_plugin_self_check)
    monkeypatch.setattr(tasks, "record_version", fake_record_version)

    proposal = tasks.create_proposal(
        task_id="apply-multi-test",
        file_path="mypkg/plugin.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="包更新",
        extra_files={
            "mypkg/utils.py": ("", "VALUE = 1\n"),
        },
    )

    _write_plugin_dev_task_file(
        task_dir,
        "apply-multi-test",
        "waiting_apply",
        file_path="mypkg/plugin.py",
        proposal_id=proposal.proposal_id,
    )

    # 提案创建后其中一个文件被外部修改 → 全部拒绝、任何文件都不写入
    (pkg_dir / "utils.py").write_text("VALUE = 999\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        await tasks.apply_proposal(proposal.proposal_id)
    assert (pkg_dir / "plugin.py").read_text(encoding="utf-8") == "plugin = None\n"
    assert recorded_versions == []

    # 恢复后应用成功：全部文件写入，逐文件记录版本
    (pkg_dir / "utils.py").unlink()
    version_id = await tasks.apply_proposal(proposal.proposal_id)
    assert version_id == "version-1"
    assert (pkg_dir / "plugin.py").read_text(encoding="utf-8") == "plugin = 'updated'\n"
    assert (pkg_dir / "utils.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert set(recorded_versions) == {"mypkg/plugin.py", "mypkg/utils.py"}
    assert checked_extra_files == [{"mypkg/utils.py": "VALUE = 1\n"}]


def test_plugin_dev_stage_candidate_supports_package_with_extra_files(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev.self_check import stage_plugin_candidate

    plugin_root = tmp_path / "plugins"
    stage_root = tmp_path / "stage"
    plugin_root.mkdir()
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    (pkg_dir / "legacy.py").write_text("OLD = 1\n", encoding="utf-8")

    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))

    entry = stage_plugin_candidate(
        "mypkg/plugin.py",
        "plugin = 'candidate'\n",
        stage_root,
        extra_files={"mypkg/utils.py": "VALUE = 1\n"},
    )

    # 包任务的检查入口是顶层包目录
    assert entry == stage_root / "mypkg"
    assert (stage_root / "mypkg" / "plugin.py").read_text(encoding="utf-8") == "plugin = 'candidate'\n"
    assert (stage_root / "mypkg" / "utils.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    # 真实包中的其他文件被一并拷入，保证完整包上下文
    assert (stage_root / "mypkg" / "__init__.py").exists()
    assert (stage_root / "mypkg" / "legacy.py").exists()

    single_entry = stage_plugin_candidate("demo.py", "plugin = None\n", stage_root)
    assert single_entry == stage_root / "demo.py"


@pytest.mark.asyncio
async def test_plugin_dev_sandbox_start_is_serialized(monkeypatch):
    from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService

    active_calls = 0
    max_active_calls = 0

    async def fake_start_unlocked():
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.01)
        active_calls -= 1
        return SimpleNamespace(status="active")

    monkeypatch.setattr(PluginDevSandboxService, "_start_unlocked", fake_start_unlocked)

    await asyncio.gather(PluginDevSandboxService.start(), PluginDevSandboxService.start())

    assert max_active_calls == 1


def test_plugin_dev_rollback_rejects_version_from_other_file(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import NotFoundError
    from nekro_agent.services.plugin_dev import versioning
    from nekro_agent.services.plugin_dev.host_file_gateway import safe_file_slug

    history_root = tmp_path / "history"
    current_file = "current.py"
    other_file = "other.py"
    foreign_version_id = "20260101-000000-000000"

    current_dir = history_root / safe_file_slug(current_file)
    other_dir = history_root / safe_file_slug(other_file)
    current_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (current_dir / "manifest.json").write_text(
        json.dumps({"file_path": current_file, "current_version_id": None, "versions": []}),
        encoding="utf-8",
    )
    (other_dir / f"{foreign_version_id}-before.py").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(versioning, "PLUGIN_DEV_HISTORY_DIR", history_root)
    crafted_version_id = f"../{safe_file_slug(other_file)}/{foreign_version_id}"

    with pytest.raises(NotFoundError):
        versioning.rollback(current_file, crafted_version_id, "before")


@pytest.mark.asyncio
async def test_plugin_dev_apply_package_file_deletion(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    plugin_root.mkdir()
    task_dir.mkdir()
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    (pkg_dir / "legacy.py").write_text("OLD = 1\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    checked_deleted_files: list[set[str]] = []

    async def fake_check(file_path: str, code: str, **kwargs):
        checked_deleted_files.append(set(kwargs.get("deleted_files") or set()))
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_check)
    monkeypatch.setattr(tasks, "record_version", lambda **kwargs: f"version-{kwargs['file_path']}")

    proposal = tasks.create_proposal(
        task_id="delete-package-file",
        file_path="mypkg/plugin.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="删除旧模块",
        deleted_files={"mypkg/legacy.py"},
    )

    _write_plugin_dev_task_file(
        task_dir,
        "delete-package-file",
        "waiting_apply",
        file_path="mypkg/plugin.py",
        proposal_id=proposal.proposal_id,
    )
    assert any(item.file_path == "mypkg/legacy.py" and item.action == "delete" for item in proposal.files)
    await tasks.apply_proposal(proposal.proposal_id)

    assert not (pkg_dir / "legacy.py").exists()
    assert checked_deleted_files == [{"mypkg/legacy.py"}]


@pytest.mark.asyncio
async def test_plugin_dev_apply_record_failure_restores_files_and_history(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    plugin_root.mkdir()
    task_dir.mkdir()
    pkg_dir = plugin_root / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    (pkg_dir / "utils.py").write_text("VALUE = 0\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    async def fake_check(file_path: str, code: str, **_kwargs):
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    record_calls = 0
    removed_records: list[tuple[str, str]] = []

    def flaky_record_version(**_kwargs):
        nonlocal record_calls
        record_calls += 1
        if record_calls == 2:
            raise OSError("history write failed")
        return "version-first"

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_check)
    monkeypatch.setattr(tasks, "record_version", flaky_record_version)
    monkeypatch.setattr(
        tasks,
        "remove_version_record",
        lambda file_path, version_id: removed_records.append((file_path, version_id)),
    )

    proposal = tasks.create_proposal(
        task_id="record-failure",
        file_path="mypkg/plugin.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="原子应用",
        extra_files={"mypkg/utils.py": ("VALUE = 0\n", "VALUE = 1\n")},
    )

    _write_plugin_dev_task_file(
        task_dir,
        "record-failure",
        "waiting_apply",
        file_path="mypkg/plugin.py",
        proposal_id=proposal.proposal_id,
    )
    with pytest.raises(OSError, match="history write failed"):
        await tasks.apply_proposal(proposal.proposal_id)

    assert (pkg_dir / "plugin.py").read_text(encoding="utf-8") == "plugin = None\n"
    assert (pkg_dir / "utils.py").read_text(encoding="utf-8") == "VALUE = 0\n"
    assert removed_records == [("mypkg/plugin.py", "version-first")]
    assert tasks.get_proposal(proposal.proposal_id).status == "pending"


def test_plugin_dev_rejects_internal_path_aliases(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.services.plugin_dev.host_file_gateway import normalize_plugin_file_path

    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))

    for file_path in ("pkg/../victim.py", "pkg/./helper.py", "pkg//helper.py", "pkg\\helper.py"):
        with pytest.raises(ValidationError):
            normalize_plugin_file_path(file_path)


@pytest.mark.asyncio
async def test_plugin_dev_apply_rechecks_after_smoke(tmp_path: Path, monkeypatch):
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    plugin_root.mkdir()
    task_dir.mkdir()
    plugin_file = plugin_root / "demo.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)

    async def fake_check(file_path: str, code: str, **_kwargs):
        plugin_file.write_text("plugin = 'external'\n", encoding="utf-8")
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_check)
    proposal = tasks.create_proposal(
        task_id="toctou-test",
        file_path="demo.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="二次校验",
    )
    _write_plugin_dev_task_file(
        task_dir,
        "toctou-test",
        "waiting_apply",
        proposal_id=proposal.proposal_id,
    )

    with pytest.raises(ValidationError, match="复核期间已被修改"):
        await tasks.apply_proposal(proposal.proposal_id)
    assert plugin_file.read_text(encoding="utf-8") == "plugin = 'external'\n"


def test_plugin_dev_rollback_restores_file_existence(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import versioning

    plugin_root = tmp_path / "plugins"
    history_root = tmp_path / "history"
    plugin_root.mkdir()
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(versioning, "PLUGIN_DEV_HISTORY_DIR", history_root)

    new_file = plugin_root / "new_file.py"
    new_file.write_text("VALUE = 1\n", encoding="utf-8")
    created_version = versioning.record_version(
        file_path="new_file.py",
        task_id="create",
        action="apply",
        before_content="",
        after_content="VALUE = 1\n",
        before_exists=False,
        after_exists=True,
        summary="新建文件",
    )
    versioning.rollback("new_file.py", created_version, "before")
    assert not new_file.exists()

    deleted_version = versioning.record_version(
        file_path="old_file.py",
        task_id="delete",
        action="apply",
        before_content="OLD = 1\n",
        after_content="",
        before_exists=True,
        after_exists=False,
        summary="删除文件",
    )
    versioning.rollback("old_file.py", deleted_version, "before")
    assert (plugin_root / "old_file.py").read_text(encoding="utf-8") == "OLD = 1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_target", ["proposal", "task"])
async def test_plugin_dev_apply_metadata_failure_restores_transaction(
    tmp_path: Path,
    monkeypatch,
    failure_target: str,
):
    from nekro_agent.schemas.plugin_check import PluginCheckItem, PluginCheckReport
    from nekro_agent.services.plugin_dev import tasks, versioning

    plugin_root = tmp_path / "plugins"
    proposal_dir = tmp_path / "proposals"
    task_dir = tmp_path / "tasks"
    history_dir = tmp_path / "history"
    plugin_root.mkdir()
    task_dir.mkdir()
    plugin_file = plugin_root / "demo.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))
    monkeypatch.setattr(tasks, "PLUGIN_DEV_PROPOSAL_DIR", proposal_dir)
    monkeypatch.setattr(tasks, "PLUGIN_DEV_TASK_DIR", task_dir)
    monkeypatch.setattr(versioning, "PLUGIN_DEV_HISTORY_DIR", history_dir)
    monkeypatch.setattr(versioning, "PLUGIN_DEV_VERSION_PATH", tmp_path / "version.json")

    async def fake_check(file_path: str, code: str, **_kwargs):
        return PluginCheckReport(
            ok=True,
            candidate_path=file_path,
            checks=[PluginCheckItem(id="plugin_load", title="加载插件", ok=True)],
        )

    monkeypatch.setattr(tasks, "run_plugin_self_check", fake_check)
    proposal = tasks.create_proposal(
        task_id="metadata-failure",
        file_path="demo.py",
        before="plugin = None\n",
        after="plugin = 'updated'\n",
        summary="元数据失败补偿",
    )
    _write_plugin_dev_task_file(
        task_dir,
        "metadata-failure",
        "waiting_apply",
        proposal_id=proposal.proposal_id,
    )
    proposal_path = proposal_dir / f"{proposal.proposal_id}.json"
    task_path = task_dir / "metadata-failure.json"
    original_proposal = proposal_path.read_bytes()
    original_task = task_path.read_bytes()
    original_write_json = tasks._write_json
    failed = False

    def fail_one_metadata_write(path: Path, data: dict) -> None:
        nonlocal failed
        target_path = proposal_path if failure_target == "proposal" else task_path
        if not failed and path == target_path:
            failed = True
            raise OSError(f"{failure_target} metadata write failed")
        original_write_json(path, data)

    monkeypatch.setattr(tasks, "_write_json", fail_one_metadata_write)

    with pytest.raises(OSError, match="metadata write failed"):
        await tasks.apply_proposal(proposal.proposal_id)

    assert plugin_file.read_text(encoding="utf-8") == "plugin = None\n"
    assert proposal_path.read_bytes() == original_proposal
    assert task_path.read_bytes() == original_task
    assert versioning.get_history("demo.py").versions == []
    assert not list(history_dir.rglob("*-before.py"))
    assert not list(history_dir.rglob("*-after.py"))


def test_plugin_dev_history_pruning_can_be_deferred(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin_dev import versioning

    history_root = tmp_path / "history"
    monkeypatch.setattr(versioning, "PLUGIN_DEV_HISTORY_DIR", history_root)
    monkeypatch.setattr(versioning, "PLUGIN_DEV_VERSION_PATH", tmp_path / "version.json")
    version_ids = iter(f"version-{index:02d}" for index in range(51))
    monkeypatch.setattr(versioning, "version_id_now", lambda: next(version_ids))

    for index in range(51):
        versioning.record_version(
            file_path="demo.py",
            task_id=f"task-{index}",
            action="apply",
            before_content=str(index),
            after_content=str(index + 1),
            summary="延迟裁剪",
            prune=False,
        )

    assert len(versioning.get_history("demo.py").versions) == 51
    first_snapshot = history_root / versioning.safe_file_slug("demo.py") / "version-00-before.py"
    assert first_snapshot.exists()
    versioning.prune_version_history("demo.py")
    assert len(versioning.get_history("demo.py").versions) == 50
    assert not first_snapshot.exists()


@pytest.mark.asyncio
async def test_plugin_dev_sandbox_stop_failure_preserves_runtime_state_and_token(monkeypatch):
    from nekro_agent.services.plugin_dev import sandbox

    state = sandbox.PluginDevSandboxState(
        status="active",
        container_name="plugin-dev-test",
        sandbox_api_token="active-token",
    )

    class FailingContainer:
        async def stop(self, *, t: int) -> None:
            raise OSError(f"stop failed after {t}s")

    class FakeContainers:
        async def get(self, _name: str) -> FailingContainer:
            return FailingContainer()

    class FakeDocker:
        containers = FakeContainers()

        async def close(self) -> None:
            return None

    async def container_running(_name: str | None) -> bool:
        return True

    monkeypatch.setattr(sandbox.PluginDevSandboxService, "_ensure_state", staticmethod(lambda: state))
    monkeypatch.setattr(sandbox.PluginDevSandboxService, "_save_state", staticmethod(lambda value: value))
    monkeypatch.setattr(sandbox.PluginDevSandboxService, "_container_running", staticmethod(container_running))
    monkeypatch.setattr(sandbox.aiodocker, "Docker", FakeDocker)

    result = await sandbox.PluginDevSandboxService._stop_unlocked()
    assert result.status == "active"
    assert result.sandbox_api_token == "active-token"
    assert result.last_error and "停止容器失败" in result.last_error


@pytest.mark.asyncio
async def test_plugin_dev_history_routes_reject_path_aliases(tmp_path: Path, monkeypatch):
    from nekro_agent.routers import plugin_dev
    from nekro_agent.schemas.errors import ValidationError

    plugin_root = tmp_path / "plugins"
    (plugin_root / "pkg").mkdir(parents=True)
    (plugin_root / "pkg" / "demo.py").write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr("nekro_agent.services.plugin_dev.host_file_gateway.WORKDIR_PLUGIN_DIR", str(plugin_root))

    with pytest.raises(ValidationError):
        await plugin_dev.get_plugin_dev_history.__wrapped__(
            file_path="pkg/sub/../demo.py",
            _current_user=None,
        )
    with pytest.raises(ValidationError):
        await plugin_dev.rollback_plugin_dev_file.__wrapped__(
            body=plugin_dev.PluginDevRollbackRequest(version_id="version", target="before"),
            file_path="pkg/sub/../demo.py",
            _current_user=None,
        )
