from __future__ import annotations

import asyncio
import json
import secrets
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi import Path as PathParam
from sse_starlette.sse import EventSourceResponse

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, UnauthorizedError, ValidationError
from nekro_agent.schemas.plugin_check import PluginCheckReport
from nekro_agent.schemas.plugin_dev import (
    PluginDevApplyResponse,
    PluginDevCcModelPresetUpdate,
    PluginDevGenerateRequest,
    PluginDevGenerateResponse,
    PluginDevHistoryResponse,
    PluginDevInternalCheckRequest,
    PluginDevInternalFilePayload,
    PluginDevInternalFileResponse,
    PluginDevInternalProposalRequest,
    PluginDevProposalResponse,
    PluginDevRollbackRequest,
    PluginDevRollbackResponse,
    PluginDevStatusResponse,
    PluginDevTaskResponse,
    PluginDevVersionInfo,
    PluginDevVersionUpdate,
)
from nekro_agent.services.plugin_dev.config import get_plugin_dev_config, update_plugin_dev_config
from nekro_agent.services.plugin_dev.host_file_gateway import (
    list_plugin_files,
    normalize_plugin_file_path,
    plugin_top_dir,
    read_plugin_file,
    sha256_text,
)
from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
from nekro_agent.services.plugin_dev.self_check import run_plugin_self_check
from nekro_agent.services.plugin_dev.tasks import (
    apply_proposal,
    cancel_task,
    create_proposal,
    create_task,
    discard_proposal,
    get_proposal,
    get_task,
    get_task_file_mtime,
    get_task_runtime_snapshot,
    rollback_plugin_file,
)
from nekro_agent.services.plugin_dev.versioning import get_history, get_version_info, update_version_info
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/plugin-dev", tags=["Plugin Dev"])
internal_router = APIRouter(prefix="/internal/plugin-dev", tags=["Plugin Dev Internal"])
_TERMINAL_TASK_STATUSES = {"waiting_apply", "applied", "failed", "cancelled"}
_MAX_INTERNAL_PROPOSAL_BYTES = 512 * 1024
_MAX_INTERNAL_PROPOSAL_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_INTERNAL_PROPOSAL_FILES = 32


def _validate_internal_file_set(
    primary_file_path: str,
    files: list[PluginDevInternalFilePayload],
    deleted_files: list[str],
) -> tuple[str, dict[str, str], set[str]]:
    """校验多文件 payload：路径合法、与主文件同插件根、大小与数量受限。

    返回主文件之外的文件集 {file_path: content}。
    """
    if len(files) + len(deleted_files) > _MAX_INTERNAL_PROPOSAL_FILES:
        raise ValidationError(reason=f"文件数量超过上限（{_MAX_INTERNAL_PROPOSAL_FILES} 个）")
    normalized_primary = normalize_plugin_file_path(primary_file_path)
    top_dir = plugin_top_dir(normalized_primary)

    total_bytes = 0
    extra: dict[str, str] = {}
    written_paths: set[str] = set()
    for item in files:
        normalized_path = normalize_plugin_file_path(item.file_path)
        if normalized_path in written_paths:
            raise ValidationError(reason=f"文件 {normalized_path} 在 files 中重复提交")
        written_paths.add(normalized_path)
        if top_dir is None and normalized_path != normalized_primary:
            raise ValidationError(reason="单文件插件任务只允许提交目标文件本身")
        if top_dir is not None and plugin_top_dir(normalized_path) != top_dir:
            raise ValidationError(reason=f"文件 {normalized_path} 不在插件目录 {top_dir}/ 内")
        content_bytes = len(item.content.encode("utf-8"))
        if content_bytes > _MAX_INTERNAL_PROPOSAL_BYTES:
            raise ValidationError(reason=f"文件 {normalized_path} 内容过大")
        total_bytes += content_bytes
        if normalized_path == normalized_primary:
            continue
        extra[normalized_path] = item.content
    deleted: set[str] = set()
    for deleted_path in deleted_files:
        normalized_path = normalize_plugin_file_path(deleted_path)
        if normalized_path in deleted:
            raise ValidationError(reason=f"文件 {normalized_path} 在 deleted_files 中重复提交")
        if normalized_path == normalized_primary:
            raise ValidationError(reason="不能删除任务主目标文件")
        if top_dir is None or plugin_top_dir(normalized_path) != top_dir:
            raise ValidationError(reason=f"删除文件 {normalized_path} 不在任务插件范围内")
        if normalized_path in written_paths:
            raise ValidationError(reason=f"文件 {normalized_path} 不能同时写入和删除")
        deleted.add(normalized_path)
    if total_bytes > _MAX_INTERNAL_PROPOSAL_TOTAL_BYTES:
        raise ValidationError(reason="文件集总大小超过上限（2MB）")
    return normalized_primary, extra, deleted


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


async def require_plugin_dev_internal_token(
    authorization: str | None = Header(default=None),
    x_internal_api_token: str | None = Header(default=None, alias="X-Internal-API-Token"),
) -> None:
    expected_token = PluginDevSandboxService.get_internal_api_token()
    provided_token = x_internal_api_token or _extract_bearer_token(authorization)
    if not provided_token or not secrets.compare_digest(provided_token, expected_token):
        raise UnauthorizedError()


def _build_status_response(sandbox_status: str) -> PluginDevStatusResponse:
    from nekro_agent.core.cc_model_presets import cc_presets_store

    preset_id = None
    preset_name = None
    raw_preset_id = get_plugin_dev_config().cc_model_preset_id
    if raw_preset_id is not None:
        preset_id = int(raw_preset_id)
        preset = cc_presets_store.get_by_id(preset_id)
    else:
        preset = cc_presets_store.get_default()
        preset_id = preset.id if preset else None
    preset_name = preset.name if preset else None
    active_task_id, queue_length = get_task_runtime_snapshot()
    return PluginDevStatusResponse(
        sandbox_status=sandbox_status,
        active_task_id=active_task_id,
        queue_length=queue_length,
        cc_model_preset_id=preset_id,
        cc_model_preset_name=preset_name,
        version=get_version_info(),
    )


@internal_router.get(
    "/version",
    summary="内部接口：获取插件开发版本信息",
    response_model=PluginDevVersionInfo,
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def get_internal_plugin_dev_version() -> PluginDevVersionInfo:
    return get_version_info()


@internal_router.get(
    "/files",
    summary="内部接口：获取插件文件列表",
    response_model=list[str],
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def get_internal_plugin_files() -> list[str]:
    return list_plugin_files()


@internal_router.get(
    "/file",
    summary="内部接口：读取插件文件",
    response_model=PluginDevInternalFileResponse,
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def get_internal_plugin_file(
    path: str = Query(..., min_length=1),
) -> PluginDevInternalFileResponse:
    normalized_path = normalize_plugin_file_path(path)
    content = read_plugin_file(normalized_path)
    return PluginDevInternalFileResponse(file_path=normalized_path, content=content, sha256=sha256_text(content))


@internal_router.post(
    "/proposals",
    summary="内部接口：创建插件写入提案",
    response_model=PluginDevProposalResponse,
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def create_internal_plugin_proposal(
    body: PluginDevInternalProposalRequest,
) -> PluginDevProposalResponse:
    if len(body.content.encode("utf-8")) > _MAX_INTERNAL_PROPOSAL_BYTES:
        raise ValidationError(reason="写入提案内容过大")
    task = get_task(body.task_id)
    if task.status in _TERMINAL_TASK_STATUSES:
        raise ValidationError(reason=f"任务 {body.task_id} 已结束（{task.status}），不能再创建写入提案")
    normalized_path, extra_contents, deleted_files = _validate_internal_file_set(
        body.file_path, body.files, body.deleted_files
    )
    normalized_task_path = normalize_plugin_file_path(task.file_path)
    if normalized_path != normalized_task_path:
        raise ValidationError(reason=f"提案目标 {normalized_path} 与任务目标 {normalized_task_path} 不一致")
    try:
        before = read_plugin_file(normalized_path)
    except NotFoundError:
        before = ""
    extra_files: dict[str, tuple[str, str]] = {}
    for extra_path, extra_content in extra_contents.items():
        try:
            extra_before = read_plugin_file(extra_path)
        except NotFoundError:
            extra_before = ""
        extra_files[extra_path] = (extra_before, extra_content)
    return create_proposal(
        task_id=body.task_id,
        file_path=normalized_path,
        before=before,
        after=body.content,
        summary=body.summary.strip() or "由插件开发沙盒创建写入提案",
        extra_files=extra_files or None,
        deleted_files=deleted_files or None,
    )


@internal_router.post(
    "/check",
    summary="内部接口：执行插件自检（仅静态检查）",
    response_model=PluginCheckReport,
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def check_internal_plugin_candidate(
    body: PluginDevInternalCheckRequest,
) -> PluginCheckReport:
    if len(body.content.encode("utf-8")) > _MAX_INTERNAL_PROPOSAL_BYTES:
        raise ValidationError(reason="自检候选内容过大")
    normalized_path, extra_contents, deleted_files = _validate_internal_file_set(
        body.file_path, body.files, body.deleted_files
    )
    # 安全约束：内部网关自检固定为 static 级别，绝不执行沙盒提交的候选代码；
    # 执行型检查（smoke）只在用户确认应用提案时进行。
    check_kwargs: dict[str, object] = {"extra_files": extra_contents or None, "level": "static"}
    if deleted_files:
        check_kwargs["deleted_files"] = deleted_files
    report = await run_plugin_self_check(normalized_path, body.content, **check_kwargs)
    if body.level != "static":
        report.warnings.append(f"内部网关自检固定为 static 级别，已忽略请求的 {body.level} 级别；执行型检查将在用户应用提案时进行")
    return report


@router.get("/status", summary="获取插件生成沙盒状态", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def get_plugin_dev_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    status, _ = await PluginDevSandboxService.status()
    return _build_status_response(status)


@router.post("/start", summary="启动插件生成沙盒", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def start_plugin_dev_sandbox(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    workspace = await PluginDevSandboxService.start()
    return _build_status_response("running" if workspace.status == "active" else "stopped")


@router.post("/stop", summary="停止插件生成沙盒", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def stop_plugin_dev_sandbox(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    workspace = await PluginDevSandboxService.stop()
    status = "running" if workspace.status == "active" else workspace.status
    return _build_status_response(status)


@router.put("/cc-model-preset", summary="设置插件开发沙盒 CC 模型组", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def set_plugin_dev_cc_model_preset(
    body: PluginDevCcModelPresetUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    from nekro_agent.core.cc_model_presets import cc_presets_store

    if body.cc_model_preset_id is not None and not cc_presets_store.get_by_id(body.cc_model_preset_id):
        from nekro_agent.schemas.errors import NotFoundError

        raise NotFoundError(resource=f"CC 模型组 {body.cc_model_preset_id}")
    update_plugin_dev_config(cc_model_preset_id=body.cc_model_preset_id)
    PluginDevSandboxService.sync_settings()
    status, _ = await PluginDevSandboxService.status()
    return _build_status_response(status)


@router.get("/version", summary="获取插件开发版本信息", response_model=PluginDevVersionInfo)
@require_role(Role.Admin)
async def get_plugin_dev_version(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevVersionInfo:
    return get_version_info()


@router.put("/version", summary="更新插件开发版本信息", response_model=PluginDevVersionInfo)
@require_role(Role.Admin)
async def put_plugin_dev_version(
    body: PluginDevVersionUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevVersionInfo:
    return update_version_info(body)


@router.post("/generate", summary="提交插件生成任务", response_model=PluginDevGenerateResponse)
@require_role(Role.Admin)
async def generate_plugin_dev_proposal(
    body: PluginDevGenerateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevGenerateResponse:
    task = await create_task(body)
    return PluginDevGenerateResponse(task_id=task.task_id, status=task.status, proposal_id=task.proposal_id)


@router.get("/tasks/{task_id}", summary="获取插件生成任务", response_model=PluginDevTaskResponse)
@require_role(Role.Admin)
async def get_plugin_dev_task(
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevTaskResponse:
    return get_task(task_id)


@router.get("/tasks/{task_id}/stream", summary="流式获取插件生成任务")
@require_role(Role.Admin)
async def stream_plugin_dev_task(
    request: Request,
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EventSourceResponse:
    get_task(task_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        last_payload = ""
        last_mtime = -1.0
        while not is_shutting_down():
            if await request.is_disconnected():
                return

            # 任务文件未变化时跳过读取与序列化，降低轮询开销
            current_mtime = get_task_file_mtime(task_id)
            if current_mtime == last_mtime:
                await asyncio.sleep(0.8)
                continue
            last_mtime = current_mtime

            task = get_task(task_id)
            payload = json.dumps(
                {"type": "task", "task": task.model_dump(mode="json")},
                ensure_ascii=False,
            )
            if payload != last_payload:
                last_payload = payload
                yield payload

            if task.status in _TERMINAL_TASK_STATUSES:
                yield json.dumps({"type": "done", "status": task.status}, ensure_ascii=False)
                return
            await asyncio.sleep(0.8)

    return EventSourceResponse(event_generator())


@router.post("/tasks/{task_id}/cancel", summary="取消插件生成任务", response_model=PluginDevTaskResponse)
@require_role(Role.Admin)
async def cancel_plugin_dev_task(
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevTaskResponse:
    return await cancel_task(task_id)


@router.get("/proposals/{proposal_id}", summary="获取写入提案", response_model=PluginDevProposalResponse)
@require_role(Role.Admin)
async def get_plugin_dev_proposal(
    proposal_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevProposalResponse:
    return get_proposal(proposal_id)


@router.post("/proposals/{proposal_id}/apply", summary="应用写入提案", response_model=PluginDevApplyResponse)
@require_role(Role.Admin)
async def apply_plugin_dev_proposal(
    proposal_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevApplyResponse:
    version_id = await apply_proposal(proposal_id)
    return PluginDevApplyResponse(version_id=version_id)


@router.delete("/proposals/{proposal_id}", summary="丢弃写入提案", response_model=PluginDevApplyResponse)
@require_role(Role.Admin)
async def discard_plugin_dev_proposal(
    proposal_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevApplyResponse:
    await discard_proposal(proposal_id)
    return PluginDevApplyResponse(version_id="")


@router.get("/history/{file_path:path}", summary="查看插件文件历史", response_model=PluginDevHistoryResponse)
@require_role(Role.Admin)
async def get_plugin_dev_history(
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevHistoryResponse:
    normalized_path = normalize_plugin_file_path(file_path)
    return get_history(normalized_path)


@router.post("/rollback/{file_path:path}", summary="回退插件文件", response_model=PluginDevRollbackResponse)
@require_role(Role.Admin)
async def rollback_plugin_dev_file(
    body: PluginDevRollbackRequest,
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevRollbackResponse:
    normalized_path = normalize_plugin_file_path(file_path)
    version_id = await rollback_plugin_file(normalized_path, body.version_id, body.target)
    return PluginDevRollbackResponse(version_id=version_id)
