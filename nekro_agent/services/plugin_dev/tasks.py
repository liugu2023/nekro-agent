from __future__ import annotations

import asyncio
import difflib
import json
import uuid
from pathlib import Path
from typing import Any

from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.plugin_check import PluginCheckReport
from nekro_agent.schemas.plugin_dev import PluginDevGenerateRequest, PluginDevProposalResponse, PluginDevTaskResponse
from nekro_agent.services.plugin.generator import _clean_code_format
from nekro_agent.services.plugin_dev.host_file_gateway import read_plugin_file, resolve_plugin_file, write_plugin_file
from nekro_agent.services.plugin_dev.paths import PLUGIN_DEV_PROPOSAL_DIR, PLUGIN_DEV_TASK_DIR
from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
from nekro_agent.services.plugin_dev.self_check import run_plugin_self_check, summarize_plugin_check
from nekro_agent.services.plugin_dev.versioning import get_version_info, record_version, utc_now_iso

_TASK_HANDLES: dict[str, asyncio.Task[None]] = {}


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError(reason=f"任务文件结构错误: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _task_path(task_id: str) -> Path:
    return PLUGIN_DEV_TASK_DIR / f"{task_id}.json"


def _proposal_path(proposal_id: str) -> Path:
    return PLUGIN_DEV_PROPOSAL_DIR / f"{proposal_id}.json"


def _diff(file_path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def _summary_from_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.strip().split())
    if len(normalized) <= 120:
        return normalized
    return f"{normalized[:119]}…"


def _extract_python_code(text: str) -> str:
    marker = "```"
    if marker not in text:
        return _clean_code_format(text)
    blocks = text.split(marker)
    candidates: list[str] = []
    for index in range(1, len(blocks), 2):
        block = blocks[index]
        if block.lstrip().startswith("python"):
            candidates.append(block.lstrip()[len("python") :].lstrip("\n"))
        else:
            candidates.append(block)
    return _clean_code_format(candidates[-1] if candidates else text)


def _append_plugin_check_logs(logs: list[str], report: PluginCheckReport, *, prefix: str) -> None:
    plugin_label = report.plugin.key if report.plugin else report.candidate_path
    logs.append(f"{prefix}结果：{'通过' if report.ok else '失败'} ({plugin_label})")
    for warning in report.warnings[:3]:
        logs.append(f"{prefix}警告：{warning}")
    for check in report.checks:
        if not check.ok:
            detail = check.error or check.detail or check.title
            logs.append(f"{prefix}失败：{check.title} - {detail}")
            break


def _build_plugin_dev_instruction(
    body: PluginDevGenerateRequest,
    current_code: str,
    sandbox_candidate_path: str,
    self_check_command: str,
) -> str:
    version_info = get_version_info().model_dump_json()
    return "\n".join(
        [
            "请根据用户需求修改 NekroAgent 插件代码，并输出完整可运行的单文件插件代码。",
            "在输出插件代码前，必须先参考 /workspace/nekro-agent-source 的当前源码，优先检查插件基类、配置、事件、方法挂载和已有插件示例。",
            "所有 import 路径、类名、函数名、装饰器和枚举必须能在该源码中找到真实定义，不允许凭记忆编造 nekro_agent.*、plugins.* 或其他内部包路径。",
            "如果无法在源码中确认某个导入，必须改用源码中已存在的 API 或说明无法确认，不能输出会导入失败的代码。",
            "不要修改 /workspace/nekro-agent-source；如果参考源码不可用，需在说明中明确指出。",
            "必须以当前插件开发版本信息中的 nekro_agent_release/source_resolved_commit 对应源码为准，不要假设 GitHub main、latest 或其他版本代表当前运行环境。",
            "任务已提供一个可写的插件工作副本路径。请先把候选代码写入该工作副本，再运行提供的插件自检命令。",
            "若插件自检失败，必须继续修复直到通过；若因环境缺少依赖无法执行自检，必须在最终说明中明确指出。",
            "不要省略 import、配置类、插件实例或已有逻辑。",
            "如果无法完成，也要返回最接近可运行的完整代码并在代码注释外避免解释文本。",
            "",
            "用户需求：",
            body.prompt,
            "",
            f"目标文件：{body.file_path}",
            f"当前工作副本路径：{sandbox_candidate_path}",
            f"插件自检命令：{self_check_command}",
            "",
            "当前代码快照：",
            "```python",
            current_code,
            "```",
            "",
            f"当前插件开发版本信息：{version_info}",
        ]
    )


def _task_response(data: dict[str, Any]) -> PluginDevTaskResponse:
    return PluginDevTaskResponse(
        task_id=str(data["task_id"]),
        file_path=str(data["file_path"]),
        status=data["status"],
        summary=str(data.get("summary") or ""),
        logs=list(data.get("logs") or []),
        proposal_id=data.get("proposal_id"),
        diff=str(data.get("diff") or ""),
        result_code=str(data.get("result_code") or ""),
        error=str(data.get("error") or ""),
        version=get_version_info(),
    )


def get_task(task_id: str) -> PluginDevTaskResponse:
    path = _task_path(task_id)
    if not path.exists():
        raise NotFoundError(resource=f"插件生成任务 {task_id}")
    return _task_response(_read_json(path, {}))


def get_proposal(proposal_id: str) -> PluginDevProposalResponse:
    path = _proposal_path(proposal_id)
    if not path.exists():
        raise NotFoundError(resource=f"写入提案 {proposal_id}")
    return PluginDevProposalResponse.model_validate(_read_json(path, {}))


def create_proposal(
    *, task_id: str, file_path: str, before: str, after: str, summary: str
) -> PluginDevProposalResponse:
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    proposal = PluginDevProposalResponse(
        proposal_id=proposal_id,
        task_id=task_id,
        file_path=file_path,
        status="pending",
        diff=_diff(file_path, before, after),
        result_code=after,
        summary=summary,
        created_at=utc_now_iso(),
    )
    _write_json(_proposal_path(proposal_id), proposal.model_dump())
    return proposal


async def _run_task(task_id: str, body: PluginDevGenerateRequest, summary: str) -> None:
    task_data = _read_json(_task_path(task_id), {})
    try:
        current_code = body.current_code
        sandbox_candidate_path = PluginDevSandboxService.prepare_task_workspace(body.file_path, current_code)
        self_check_command = (
            f"python /workspace/nekro-agent-source/run_nekro_cli.py plugin check {sandbox_candidate_path} --json"
        )
        instruction = _build_plugin_dev_instruction(
            body,
            current_code,
            sandbox_candidate_path,
            self_check_command,
        )
        task_data["logs"].append("已注入插件开发版本信息")
        task_data["logs"].append("准备 Nekro Agent 参考源码")
        task_data["logs"].append("参考源码路径：/workspace/nekro-agent-source")
        task_data["logs"].append(f"已同步插件工作副本：{sandbox_candidate_path}")
        task_data["logs"].append(f"已提供自检命令：{self_check_command}")
        task_data["logs"].append("正在启动插件开发专用 Claude Code 沙盒")
        task_data["status"] = "running_cc"
        _write_json(_task_path(task_id), task_data)

        full_response = ""
        async for chunk in PluginDevSandboxService.stream_generate(instruction):
            if isinstance(chunk, str):
                full_response += chunk
                if len(full_response) % 1200 < len(chunk):
                    task_data["logs"].append(f"CC 已返回约 {len(full_response)} 字符")
                    _write_json(_task_path(task_id), task_data)
            elif isinstance(chunk, dict):
                chunk_type = str(chunk.get("type") or "tool")
                name = str(chunk.get("name") or chunk.get("tool_name") or "")
                task_data["logs"].append(f"CC 事件：{chunk_type}{f' {name}' if name else ''}")
                _write_json(_task_path(task_id), task_data)

        result_code = _extract_python_code(full_response)
        if not result_code.strip():
            raise ValidationError(reason="生成结果为空")

        task_data["status"] = "creating_proposal"
        task_data["logs"].append("CC 已返回结果，正在执行宿主机插件自检")
        _write_json(_task_path(task_id), task_data)

        check_report = await run_plugin_self_check(body.file_path, result_code, level="smoke")
        _append_plugin_check_logs(task_data["logs"], check_report, prefix="宿主机自检")
        if not check_report.ok:
            raise ValidationError(reason=f"插件自检未通过: {summarize_plugin_check(check_report)}")

        task_data["logs"].append("宿主机插件自检通过，正在生成 diff 提案")
        _write_json(_task_path(task_id), task_data)

        proposal = create_proposal(
            task_id=task_id,
            file_path=body.file_path,
            before=body.base_code or current_code,
            after=result_code,
            summary=summary,
        )
        task_data.update(
            {
                "status": "waiting_apply",
                "proposal_id": proposal.proposal_id,
                "diff": proposal.diff,
                "result_code": result_code,
            }
        )
        task_data["logs"].append("已创建写入提案，等待用户应用")
    except asyncio.CancelledError:
        latest_task = _read_json(_task_path(task_id), task_data)
        latest_task["status"] = "cancelled"
        latest_logs = list(latest_task.get("logs") or [])
        if not latest_logs or latest_logs[-1] != "任务已取消":
            latest_logs.append("任务已取消")
        latest_task["logs"] = latest_logs
        _write_json(_task_path(task_id), latest_task)
        return
    except Exception as e:
        task_data["status"] = "failed"
        task_data["error"] = str(e)
        task_data["logs"].append(f"任务失败：{e}")
    finally:
        _TASK_HANDLES.pop(task_id, None)
    _write_json(_task_path(task_id), task_data)


async def create_task(body: PluginDevGenerateRequest) -> PluginDevTaskResponse:
    resolve_plugin_file(body.file_path)
    if not body.current_code.strip():
        raise ValidationError(reason="当前插件代码不能为空")

    task_id = f"plugin-dev-{uuid.uuid4().hex}"
    summary = _summary_from_prompt(body.prompt)
    task_data: dict[str, Any] = {
        "task_id": task_id,
        "file_path": body.file_path,
        "status": "pending",
        "summary": summary,
        "logs": ["已创建插件生成任务", "任务已进入后台队列"],
        "proposal_id": None,
        "diff": "",
        "result_code": "",
        "error": "",
    }
    _write_json(_task_path(task_id), task_data)
    task_handle = asyncio.create_task(_run_task(task_id, body, summary))
    _TASK_HANDLES[task_id] = task_handle
    return _task_response(task_data)


async def apply_proposal(proposal_id: str) -> str:
    proposal = get_proposal(proposal_id)
    if proposal.status != "pending":
        raise ValidationError(reason="该提案已处理")
    before = ""
    try:
        before = read_plugin_file(proposal.file_path)
    except Exception:
        before = ""

    check_report = await run_plugin_self_check(proposal.file_path, proposal.result_code, level="smoke")
    if not check_report.ok:
        raise ValidationError(reason=f"插件自检未通过: {summarize_plugin_check(check_report)}")

    write_plugin_file(proposal.file_path, proposal.result_code)
    version_id = record_version(
        file_path=proposal.file_path,
        task_id=proposal.task_id,
        action="apply_plugin_dev_proposal",
        before_content=before,
        after_content=proposal.result_code,
        summary=proposal.summary,
    )

    proposal_data = proposal.model_dump()
    proposal_data["status"] = "applied"
    _write_json(_proposal_path(proposal_id), proposal_data)

    task_path = _task_path(proposal.task_id)
    if task_path.exists():
        task_data = _read_json(task_path, {})
        task_data["status"] = "applied"
        task_data.setdefault("logs", []).append(f"已应用提案，版本号：{version_id}")
        _write_json(task_path, task_data)
    return version_id


async def cancel_task(task_id: str) -> PluginDevTaskResponse:
    task = get_task(task_id)
    if task.status not in {"pending", "running_cc", "creating_proposal"}:
        return task

    data = task.model_dump()
    data["status"] = "cancelled"
    data["logs"] = [*task.logs, "任务已取消"]
    data.pop("version", None)
    _write_json(_task_path(task_id), data)

    task_handle = _TASK_HANDLES.get(task_id)
    if task_handle is not None and not task_handle.done():
        task_handle.cancel()

    try:
        await PluginDevSandboxService.cancel_current_task()
    except Exception:
        pass
    return get_task(task_id)


def discard_proposal(proposal_id: str) -> None:
    proposal = get_proposal(proposal_id)
    if proposal.status != "pending":
        raise ValidationError(reason="该提案已处理")
    data = proposal.model_dump()
    data["status"] = "discarded"
    _write_json(_proposal_path(proposal_id), data)

    task_path = _task_path(proposal.task_id)
    if task_path.exists():
        task_data = _read_json(task_path, {})
        task_data["status"] = "cancelled"
        task_data.setdefault("logs", []).append("提案已丢弃")
        _write_json(task_path, task_data)
