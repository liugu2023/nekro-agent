from __future__ import annotations

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
    plugin_root.mkdir()
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
    monkeypatch.setattr(PluginDevSandboxService, "get_internal_api_token", staticmethod(lambda: "secret-token"))

    checked_levels: list[str] = []

    async def fake_run_plugin_self_check(file_path: str, code: str, level: str = "static"):
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

    # 任务已终态后不允许再通过网关注入新提案
    from nekro_agent.schemas.errors import ValidationError

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    monkeypatch.setattr("nekro_agent.services.plugin_dev.tasks.PLUGIN_DEV_TASK_DIR", task_dir)
    (task_dir / "finished-task.json").write_text(
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

    async def fake_run_plugin_self_check(file_path: str, code: str, level: str = "static"):
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

    async def fake_run_plugin_self_check(file_path: str, code: str, level: str = "smoke"):
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

    async def fake_run_plugin_self_check(file_path: str, code: str, level: str = "smoke"):
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

    async def fake_run_plugin_self_check(file_path: str, code: str, level: str = "smoke"):
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


def _write_plugin_dev_task_file(task_dir: Path, task_id: str, status: str) -> None:
    (task_dir / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "file_path": "demo.py",
                "status": status,
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
    task_dir.mkdir()
    _write_plugin_dev_task_file(task_dir, "stale-running", "running_cc")
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
