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
from nekro_agent.services.plugin_dev.host_file_gateway import read_plugin_file, resolve_plugin_file, write_plugin_file
from nekro_agent.services.plugin_dev.paths import PLUGIN_DEV_PROPOSAL_DIR, PLUGIN_DEV_TASK_DIR
from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
from nekro_agent.services.plugin_dev.self_check import run_plugin_self_check, summarize_plugin_check
from nekro_agent.services.plugin_dev.versioning import get_version_info, record_version, utc_now_iso

_TASK_HANDLES: dict[str, asyncio.Task[None]] = {}
_TASK_QUEUE_LOCK = asyncio.Lock()
_ACTIVE_TASK_ID: str | None = None
_MAX_SELF_CHECK_REPAIR_ATTEMPTS = 3
_REQUIRED_SANDBOX_WRITE_TOOLS = {"write", "edit", "multiedit"}
_SANDBOX_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}
_TOOL_PRIMARY_KEYS = ("command", "file_path", "pattern", "url", "query", "prompt", "notebook_path", "path")


def get_task_runtime_snapshot() -> tuple[str | None, int]:
    live_task_ids = [task_id for task_id, handle in _TASK_HANDLES.items() if not handle.done()]
    active_task_id = _ACTIVE_TASK_ID if _ACTIVE_TASK_ID in live_task_ids else None
    queue_length = len(live_task_ids) - (1 if active_task_id else 0)
    return active_task_id, max(queue_length, 0)


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


def _format_cc_event_log(chunk: dict, tool_names_by_id: dict[str, str]) -> str:
    chunk_type = str(chunk.get("type") or "tool")
    name = str(chunk.get("name") or chunk.get("tool_name") or "").strip()
    tool_use_id = str(chunk.get("tool_use_id") or chunk.get("id") or "").strip()
    if chunk_type == "tool_call":
        if tool_use_id and name:
            tool_names_by_id[tool_use_id] = name
        payload = _build_tool_call_log_payload(chunk, name=name, tool_use_id=tool_use_id)
        return f"工具调用：{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    if chunk_type == "tool_result":
        if not name and tool_use_id:
            name = tool_names_by_id.get(tool_use_id, "")
        payload = _build_tool_result_log_payload(chunk, name=name, tool_use_id=tool_use_id)
        return f"工具结果：{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    return f"CC 事件：{chunk_type}{f' {name}' if name else ''}"


def _coerce_tool_payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): payload_value for key, payload_value in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        if isinstance(parsed, dict):
            return {str(key): payload_value for key, payload_value in parsed.items()}
    return {}


def _extract_tool_input(chunk: dict) -> dict[str, Any]:
    for key in ("input", "arguments", "args", "params"):
        payload = _coerce_tool_payload(chunk.get(key))
        if payload:
            return payload

    ignored_keys = {"type", "name", "tool_name", "tool_use_id", "id", "content", "result", "is_error"}
    return {key: value for key, value in chunk.items() if key not in ignored_keys}


def _stringify_tool_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _extract_tool_result_content(chunk: dict) -> str:
    for key in ("content", "result", "output", "stdout", "stderr", "message"):
        if key in chunk:
            return _stringify_tool_value(chunk.get(key))
    return ""


def _pick_primary_tool_value(payload: dict[str, Any]) -> tuple[str, str]:
    for key in _TOOL_PRIMARY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()

    for key, value in payload.items():
        if key == "description":
            continue
        text = _stringify_tool_value(value).strip()
        if text:
            return key, text
    return "", ""


def _build_tool_call_log_payload(chunk: dict, *, name: str, tool_use_id: str) -> dict[str, Any]:
    tool_input = _extract_tool_input(chunk)
    primary_key, primary_value = _pick_primary_tool_value(tool_input)
    description = tool_input.get("description")
    return {
        "name": name or "unknown",
        "tool_use_id": tool_use_id,
        "input": tool_input,
        "description": description if isinstance(description, str) else "",
        "primary_key": primary_key,
        "primary_value": primary_value,
    }


def _build_tool_result_log_payload(chunk: dict, *, name: str, tool_use_id: str) -> dict[str, Any]:
    return {
        "name": name or "unknown",
        "tool_use_id": tool_use_id,
        "content": _extract_tool_result_content(chunk),
        "is_error": bool(chunk.get("is_error")),
    }


def _tool_use_id(chunk: dict) -> str:
    return str(chunk.get("tool_use_id") or chunk.get("id") or "").strip()


def _is_sandbox_self_check_call(chunk: dict, self_check_command: str) -> bool:
    if str(chunk.get("type") or "") != "tool_call":
        return False
    name = str(chunk.get("name") or chunk.get("tool_name") or "").strip().lower()
    if name != "bash":
        return False
    command = _extract_tool_input(chunk).get("command")
    return isinstance(command, str) and self_check_command in command


def _is_sandbox_write_call(chunk: dict) -> bool:
    if str(chunk.get("type") or "") != "tool_call":
        return False
    name = str(chunk.get("name") or chunk.get("tool_name") or "").strip().lower()
    return name in _SANDBOX_WRITE_TOOLS


def _summarize_sandbox_self_check_result(chunk: dict) -> tuple[bool, str]:
    content = _extract_tool_result_content(chunk)
    if bool(chunk.get("is_error")):
        return False, content or "CC 沙盒插件自检命令执行失败"

    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            report = json.loads(stripped)
        except json.JSONDecodeError:
            return True, ""
        if isinstance(report, dict) and report.get("ok") is False:
            try:
                return False, summarize_plugin_check(PluginCheckReport.model_validate(report))
            except Exception:
                fallback = report.get("error") or report.get("detail") or report.get("errors") or "CC 沙盒插件自检未通过"
                return False, _stringify_tool_value(fallback)
    return True, ""


def _refresh_workspace_preview(
    task_data: dict[str, Any],
    *,
    file_path: str,
    candidate_host_path: Path,
    before_code: str,
) -> None:
    if not candidate_host_path.exists() or not candidate_host_path.is_file():
        return
    candidate_code = candidate_host_path.read_text(encoding="utf-8")
    if not candidate_code or candidate_code == before_code:
        return
    if task_data.get("result_code") == candidate_code:
        return
    task_data["file_path"] = file_path
    task_data["result_code"] = candidate_code
    task_data["diff"] = _diff(file_path, before_code, candidate_code)


def _summarize_cc_response(text: str, *, max_length: int = 500) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[:max_length]}..."


def _detect_cc_model_error(text: str) -> str:
    normalized = _summarize_cc_response(text, max_length=800)
    lowered = normalized.lower()
    if "selected model" in lowered and ("may not exist" in lowered or "access to it" in lowered):
        return (
            "CC 模型配置不可用：当前插件开发模型不存在或账号无权限。"
            f"请在插件开发配置里切换到可用的 CC 模型组。原始返回：{normalized}"
        )
    return ""


def _validate_sandbox_tools(tools: list[str]) -> None:
    normalized_tools = {tool.strip().lower() for tool in tools if tool.strip()}
    if not normalized_tools:
        raise ValidationError(reason="CC 沙盒没有返回可用工具列表，无法执行插件开发任务")
    if "bash" not in normalized_tools:
        raise ValidationError(reason=f"CC 沙盒缺少 Bash 工具，无法运行插件自检。当前工具：{', '.join(tools)}")
    if normalized_tools.isdisjoint(_REQUIRED_SANDBOX_WRITE_TOOLS):
        raise ValidationError(reason=f"CC 沙盒缺少 Write/Edit/MultiEdit 工具，无法写入插件工作副本。当前工具：{', '.join(tools)}")


def _format_plugin_check_report_for_repair(report: PluginCheckReport) -> str:
    lines = [
        f"自检结果：{'通过' if report.ok else '失败'}",
        f"检查级别：{report.level}",
        f"候选路径：{report.candidate_path}",
    ]
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        detail = check.error or check.detail
        lines.append(f"[{status}] {check.title}{f': {detail}' if detail else ''}")
    if report.errors:
        lines.append("错误：")
        lines.extend(f"- {error}" for error in report.errors[:5])
    if report.warnings:
        lines.append("警告：")
        lines.extend(f"- {warning}" for warning in report.warnings[:5])
    return "\n".join(lines)


def _build_plugin_dev_repair_instruction(
    *,
    task_id: str,
    body: PluginDevGenerateRequest,
    failed_code: str,
    sandbox_candidate_path: str,
    self_check_command: str,
    failure_report: str,
    attempt: int,
) -> str:
    return "\n".join(
        [
            f"第 {attempt - 1} 轮候选插件没有通过接收条件、CC 沙盒自检或宿主机复核，请继续修复。",
            "必须基于下面的失败报告定位问题，把修复后的候选代码写入当前工作副本路径，再由 CC 沙盒运行插件自检命令。",
            "本轮必须调用沙盒工具执行，至少使用 Write/Edit/MultiEdit 或 Bash 写入当前工作副本；纯文本回复不会被后端接收。",
            "不要只回复代码块；后端只接收工作副本变更或内部网关 proposal，不会对默认/当前代码快照自检。",
            "只有 CC 沙盒自检通过后才能创建 proposal；如果使用内部网关创建 proposal，task_id 仍然必须使用当前任务 ID；不要直接写真实插件文件。",
            "",
            f"任务 ID：{task_id}",
            f"目标文件：{body.file_path}",
            f"当前工作副本路径：{sandbox_candidate_path}",
            f"插件自检命令：{self_check_command}",
            "",
            "失败报告：",
            failure_report,
            "",
            "上一轮候选代码：",
            "```python",
            failed_code,
            "```",
        ]
    )


def _discard_failed_proposal_for_retry(proposal: PluginDevProposalResponse, *, reason: str) -> None:
    data = proposal.model_dump()
    data["status"] = "discarded"
    data["summary"] = f"{proposal.summary}\n\n自动丢弃：{reason}"
    _write_json(_proposal_path(proposal.proposal_id), data)


def _read_changed_workspace_candidate(candidate_path: Path, rejected_codes: set[str]) -> str | None:
    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    candidate_code = candidate_path.read_text(encoding="utf-8")
    if candidate_code in rejected_codes:
        return None
    return candidate_code


def _build_plugin_dev_instruction(
    task_id: str,
    body: PluginDevGenerateRequest,
    current_code: str,
    sandbox_candidate_path: str,
    self_check_command: str,
) -> str:
    version_info = get_version_info().model_dump_json()
    return "\n".join(
        [
            "请根据用户需求修改 NekroAgent 插件代码，并交付完整可运行的单文件插件代码。",
            "在输出插件代码前，必须先参考 /workspace/nekro-agent-source 的当前源码，优先检查插件基类、配置、事件、方法挂载和已有插件示例。",
            "所有 import 路径、类名、函数名、装饰器和枚举必须能在该源码中找到真实定义，不允许凭记忆编造 nekro_agent.*、plugins.* 或其他内部包路径。",
            "如果无法在源码中确认某个导入，必须改用源码中已存在的 API 或说明无法确认，不能输出会导入失败的代码。",
            "不要修改 /workspace/nekro-agent-source；如果参考源码不可用，需在说明中明确指出。",
            "必须以 /workspace/nekro-agent-source 中的本地运行环境快照为准，不要假设 GitHub main、latest 或其他远端版本代表当前运行环境。",
            "任务已提供一个可写的插件工作副本路径。必须先把候选代码写入该工作副本，再由 CC 沙盒运行提供的插件自检命令。",
            "若插件自检失败，必须继续修复直到通过；若因环境缺少依赖无法执行自检，必须在最终说明中明确指出。",
            "本任务必须调用沙盒工具执行：先用 Read/Grep/Glob/Bash 查看参考源码或工作副本，再用 Write/Edit/MultiEdit 或 Bash 写入候选代码。",
            "不要只在最终回复里粘贴代码；后端不会把纯文本回复当作可检查候选，也不会对默认/当前代码快照做自检。",
            "CC 沙盒自检通过后，优先通过内部网关 /proposals 创建写入提案；不要在自检前创建 proposal。如果网关不可用，至少确保工作副本路径里的文件已经是最终候选代码。",
            "不要省略 import、配置类、插件实例或已有逻辑。",
            "如果无法完成，也要把最接近可运行的完整代码写入工作副本，并在最终说明中解释原因。",
            "参考源码默认来自当前 NekroAgent 运行环境的本地快照；以版本信息中的 source_origin/source_dirty/source_resolved_commit 了解来源和可信度。",
            "",
            "用户需求：",
            body.prompt,
            "",
            f"任务 ID：{task_id}",
            f"目标文件：{body.file_path}",
            f"当前工作副本路径：{sandbox_candidate_path}",
            f"插件自检命令：{self_check_command}",
            "内部插件文件网关：如需读取真实插件文件或提交写入提案，使用环境变量 NEKRO_PLUGIN_DEV_INTERNAL_API_BASE；请求头 X-Internal-API-Token 使用 INTERNAL_API_TOKEN。",
            "通过内部网关创建 proposal 时，task_id 必须使用上方任务 ID，content 必须是完整插件文件内容。",
            "内部网关仅允许获取版本、列出文件、读取文件、执行自检和创建 proposal，不允许直接写入真实插件文件。",
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


def get_latest_pending_proposal_for_task(task_id: str) -> PluginDevProposalResponse | None:
    if not PLUGIN_DEV_PROPOSAL_DIR.exists():
        return None

    proposals: list[PluginDevProposalResponse] = []
    for path in PLUGIN_DEV_PROPOSAL_DIR.glob("proposal-*.json"):
        proposal = PluginDevProposalResponse.model_validate(_read_json(path, {}))
        if proposal.task_id == task_id and proposal.status == "pending":
            proposals.append(proposal)
    if not proposals:
        return None
    return max(proposals, key=lambda proposal: (proposal.created_at, proposal.proposal_id))


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


async def _execute_task(task_id: str, body: PluginDevGenerateRequest, summary: str) -> None:
    task_data = _read_json(_task_path(task_id), {})
    try:
        current_code = body.current_code
        sandbox_candidate_path = PluginDevSandboxService.prepare_task_workspace(body.file_path, current_code)
        sandbox_candidate_host_path = PluginDevSandboxService.resolve_workspace_host_path(sandbox_candidate_path)
        self_check_command = PluginDevSandboxService.build_self_check_command(
            sandbox_candidate_path,
            body.file_path,
            task_id,
            "smoke",
        )
        instruction = _build_plugin_dev_instruction(
            task_id,
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

        sandbox_runtime = await PluginDevSandboxService.inspect_runtime(refresh_tools=True)
        task_data["logs"].append(
            "CC 沙盒已启动："
            f"{sandbox_runtime.container_name or sandbox_runtime.container_id or 'unknown'} "
            f"({sandbox_runtime.api_endpoint}，健康：{'是' if sandbox_runtime.healthy else '否'})"
        )
        task_data["logs"].append(
            "CC 模型组："
            f"{sandbox_runtime.preset_name or '未命名'}"
            f"{f' #{sandbox_runtime.preset_id}' if sandbox_runtime.preset_id is not None else ''}"
            f" · {sandbox_runtime.model_type or 'unknown'} · {sandbox_runtime.model_label or '未配置'}"
        )
        if not sandbox_runtime.healthy:
            raise ValidationError(reason=f"CC 沙盒 API 健康检查失败：{sandbox_runtime.api_endpoint}")
        _validate_sandbox_tools(sandbox_runtime.tools)
        preview_tools = ", ".join(sandbox_runtime.tools[:12])
        extra_count = max(len(sandbox_runtime.tools) - 12, 0)
        task_data["logs"].append(f"CC 沙盒工具可用：{preview_tools}{f' 等 {extra_count} 个' if extra_count else ''}")
        _write_json(_task_path(task_id), task_data)

        proposal: PluginDevProposalResponse | None = None
        result_code = ""
        next_instruction = instruction
        last_failure = ""
        last_checked_code = current_code
        tool_names_by_id: dict[str, str] = {}

        for attempt in range(1, _MAX_SELF_CHECK_REPAIR_ATTEMPTS + 1):
            task_data["status"] = "running_cc"
            task_data["logs"].append(f"CC 第 {attempt} 轮生成/修复开始")
            _write_json(_task_path(task_id), task_data)

            full_response = ""
            pending_self_check_tool_ids: set[str] = set()
            pending_write_tool_ids: set[str] = set()
            sandbox_self_check_passed = False
            sandbox_self_check_failure = ""
            async for chunk in PluginDevSandboxService.stream_generate(next_instruction):
                if isinstance(chunk, str):
                    full_response += chunk
                    if len(full_response) % 1200 < len(chunk):
                        task_data["logs"].append(f"CC 已返回约 {len(full_response)} 字符")
                        _write_json(_task_path(task_id), task_data)
                elif isinstance(chunk, dict):
                    if _is_sandbox_self_check_call(chunk, self_check_command):
                        tool_use_id = _tool_use_id(chunk)
                        if tool_use_id:
                            pending_self_check_tool_ids.add(tool_use_id)
                    elif _is_sandbox_write_call(chunk):
                        tool_use_id = _tool_use_id(chunk)
                        if tool_use_id:
                            pending_write_tool_ids.add(tool_use_id)
                    elif str(chunk.get("type") or "") == "tool_result" and _tool_use_id(chunk) in pending_self_check_tool_ids:
                        sandbox_self_check_passed, sandbox_self_check_failure = _summarize_sandbox_self_check_result(chunk)
                        pending_self_check_tool_ids.discard(_tool_use_id(chunk))
                    elif str(chunk.get("type") or "") == "tool_result" and _tool_use_id(chunk) in pending_write_tool_ids:
                        if not bool(chunk.get("is_error")):
                            _refresh_workspace_preview(
                                task_data,
                                file_path=body.file_path,
                                candidate_host_path=sandbox_candidate_host_path,
                                before_code=body.base_code or current_code,
                            )
                        pending_write_tool_ids.discard(_tool_use_id(chunk))
                    task_data["logs"].append(_format_cc_event_log(chunk, tool_names_by_id))
                    _write_json(_task_path(task_id), task_data)

            candidate_source = ""
            rejected_codes = {current_code, last_checked_code}
            proposal = get_latest_pending_proposal_for_task(task_id)
            if proposal is not None:
                if proposal.result_code in rejected_codes:
                    last_failure = "内部网关提案内容与上一轮候选代码一致，未产生新的可检查候选"
                    _discard_failed_proposal_for_retry(proposal, reason=last_failure)
                    task_data["logs"].append(f"已丢弃未变化的内部提案：{proposal.proposal_id}")
                    proposal = None
                    result_code = ""
                else:
                    task_data["logs"].append(f"检测到内部网关写入提案：{proposal.proposal_id}")
                    task_data["file_path"] = proposal.file_path
                    result_code = proposal.result_code
                    candidate_source = "内部网关提案"
            else:
                candidate_code = _read_changed_workspace_candidate(sandbox_candidate_host_path, rejected_codes)
                if candidate_code is None:
                    result_code = ""
                else:
                    result_code = candidate_code
                    candidate_source = "沙盒工作副本"
                    task_data["logs"].append(f"检测到 CC 已修改工作副本：{sandbox_candidate_path}")

            if not result_code.strip():
                model_error = _detect_cc_model_error(full_response)
                if model_error:
                    task_data["logs"].append(f"第 {attempt} 轮 CC 运行错误：{model_error}")
                    raise ValidationError(reason=model_error)
                last_failure = (
                    last_failure
                    or "没有检测到 CC 沙盒提交新的候选代码；未对默认/当前代码执行自检。"
                    "请使用 Edit/Write 修改工作副本，或调用内部网关 /proposals 提交完整插件内容。"
                )
                task_data["logs"].append(f"第 {attempt} 轮未提交可检查候选：{last_failure}")
                response_summary = _summarize_cc_response(full_response)
                if response_summary:
                    task_data["logs"].append(f"第 {attempt} 轮 CC 文本回复摘要：{response_summary}")
                if attempt >= _MAX_SELF_CHECK_REPAIR_ATTEMPTS:
                    raise ValidationError(reason=last_failure)
                next_instruction = _build_plugin_dev_repair_instruction(
                    task_id=task_id,
                    body=body,
                    failed_code=last_checked_code,
                    sandbox_candidate_path=sandbox_candidate_path,
                    self_check_command=self_check_command,
                    failure_report=last_failure,
                    attempt=attempt + 1,
                )
                task_data["logs"].append("已要求 CC 使用沙盒工具提交候选代码")
                _write_json(_task_path(task_id), task_data)
                continue

            if not sandbox_self_check_passed:
                last_checked_code = result_code
                last_failure = sandbox_self_check_failure or (
                    "CC 沙盒未运行通过插件自检命令；请先写入工作副本，运行提供的插件自检命令，"
                    "确认通过后再创建 proposal。"
                )
                if proposal is not None:
                    _discard_failed_proposal_for_retry(proposal, reason=last_failure)
                    task_data["logs"].append(f"已丢弃未通过 CC 沙盒自检的内部提案：{proposal.proposal_id}")
                    proposal = None
                task_data["logs"].append(f"第 {attempt} 轮未通过 CC 沙盒自检：{last_failure}")
                if attempt >= _MAX_SELF_CHECK_REPAIR_ATTEMPTS:
                    raise ValidationError(reason=last_failure)
                next_instruction = _build_plugin_dev_repair_instruction(
                    task_id=task_id,
                    body=body,
                    failed_code=result_code,
                    sandbox_candidate_path=sandbox_candidate_path,
                    self_check_command=self_check_command,
                    failure_report=last_failure,
                    attempt=attempt + 1,
                )
                task_data["logs"].append("已要求 CC 先运行并通过沙盒自检后再提交提案")
                _write_json(_task_path(task_id), task_data)
                continue

            task_data["status"] = "creating_proposal"
            task_data["logs"].append(f"第 {attempt} 轮检测到{candidate_source}且 CC 沙盒自检通过，正在执行宿主机复核")
            _write_json(_task_path(task_id), task_data)

            check_file_path = proposal.file_path if proposal is not None else body.file_path
            check_report = await run_plugin_self_check(check_file_path, result_code, level="smoke")
            _append_plugin_check_logs(task_data["logs"], check_report, prefix=f"第 {attempt} 轮宿主机复核")
            if check_report.ok:
                break

            last_checked_code = result_code
            last_failure = summarize_plugin_check(check_report)
            if proposal is not None:
                _discard_failed_proposal_for_retry(proposal, reason=last_failure)
                task_data["logs"].append(f"已丢弃未通过宿主机复核的内部提案：{proposal.proposal_id}")
                proposal = None
            if attempt >= _MAX_SELF_CHECK_REPAIR_ATTEMPTS:
                raise ValidationError(reason=f"插件宿主机复核未通过: {last_failure}")

            repair_report = _format_plugin_check_report_for_repair(check_report)
            next_instruction = _build_plugin_dev_repair_instruction(
                task_id=task_id,
                body=body,
                failed_code=result_code,
                sandbox_candidate_path=sandbox_candidate_path,
                self_check_command=self_check_command,
                failure_report=repair_report,
                attempt=attempt + 1,
            )
            task_data["logs"].append(f"第 {attempt} 轮宿主机复核未通过，已将失败报告交回 CC 自动修复")
            _write_json(_task_path(task_id), task_data)
        else:
            raise ValidationError(reason=f"插件复核未通过: {last_failure or '达到最大修复轮次'}")

        task_data["logs"].append("宿主机插件复核通过，正在确认 diff 提案")
        _write_json(_task_path(task_id), task_data)

        if proposal is None:
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
                "file_path": proposal.file_path,
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
    _write_json(_task_path(task_id), task_data)


async def _run_task(task_id: str, body: PluginDevGenerateRequest, summary: str) -> None:
    global _ACTIVE_TASK_ID

    try:
        async with _TASK_QUEUE_LOCK:
            latest_task = _read_json(_task_path(task_id), {})
            if latest_task.get("status") == "cancelled":
                return
            _ACTIVE_TASK_ID = task_id
            await _execute_task(task_id, body, summary)
    except asyncio.CancelledError:
        latest_task = _read_json(_task_path(task_id), {})
        latest_task["status"] = "cancelled"
        latest_logs = list(latest_task.get("logs") or [])
        if not latest_logs or latest_logs[-1] != "任务已取消":
            latest_logs.append("任务已取消")
        latest_task["logs"] = latest_logs
        _write_json(_task_path(task_id), latest_task)
    finally:
        if _ACTIVE_TASK_ID == task_id:
            _ACTIVE_TASK_ID = None
        _TASK_HANDLES.pop(task_id, None)


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
        raise ValidationError(reason=f"插件复核未通过: {summarize_plugin_check(check_report)}")

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
