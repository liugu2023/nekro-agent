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
from nekro_agent.schemas.plugin_dev import (
    PluginDevApplyResponse,
    PluginDevCcModelPresetUpdate,
    PluginDevGenerateRequest,
    PluginDevGenerateResponse,
    PluginDevHistoryResponse,
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
    read_plugin_file,
    resolve_plugin_file,
    sha256_text,
)
from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
from nekro_agent.services.plugin_dev.tasks import (
    apply_proposal,
    cancel_task,
    create_proposal,
    create_task,
    discard_proposal,
    get_proposal,
    get_task,
    get_task_runtime_snapshot,
)
from nekro_agent.services.plugin_dev.versioning import get_history, get_version_info, rollback, update_version_info
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/plugin-dev", tags=["Plugin Dev"])
internal_router = APIRouter(prefix="/internal/plugin-dev", tags=["Plugin Dev Internal"])
_TERMINAL_TASK_STATUSES = {"waiting_apply", "applied", "failed", "cancelled"}
_MAX_INTERNAL_PROPOSAL_BYTES = 512 * 1024


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


def _build_status_response(sandbox_status: str, workspace) -> PluginDevStatusResponse:
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
    content = read_plugin_file(path)
    return PluginDevInternalFileResponse(file_path=path, content=content, sha256=sha256_text(content))


@internal_router.post(
    "/proposals",
    summary="内部接口：创建插件写入提案",
    response_model=PluginDevProposalResponse,
    dependencies=[Depends(require_plugin_dev_internal_token)],
)
async def create_internal_plugin_proposal(
    body: PluginDevInternalProposalRequest,
) -> PluginDevProposalResponse:
    resolve_plugin_file(body.file_path)
    if len(body.content.encode("utf-8")) > _MAX_INTERNAL_PROPOSAL_BYTES:
        raise ValidationError(reason="写入提案内容过大")
    try:
        before = read_plugin_file(body.file_path)
    except NotFoundError:
        before = ""
    return create_proposal(
        task_id=body.task_id,
        file_path=body.file_path,
        before=before,
        after=body.content,
        summary=body.summary.strip() or "由插件开发沙盒创建写入提案",
    )


@router.get("/status", summary="获取插件生成沙盒状态", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def get_plugin_dev_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    status, workspace = await PluginDevSandboxService.status()
    return _build_status_response(status, workspace)


@router.post("/start", summary="启动插件生成沙盒", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def start_plugin_dev_sandbox(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    workspace = await PluginDevSandboxService.start()
    return _build_status_response("running" if workspace.status == "active" else "stopped", workspace)


@router.post("/stop", summary="停止插件生成沙盒", response_model=PluginDevStatusResponse)
@require_role(Role.Admin)
async def stop_plugin_dev_sandbox(
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevStatusResponse:
    workspace = await PluginDevSandboxService.stop()
    return _build_status_response("running" if workspace.status == "active" else "stopped", workspace)


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
    status, sandbox_state = await PluginDevSandboxService.status()
    return _build_status_response(status, sandbox_state)


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
        while not is_shutting_down():
            if await request.is_disconnected():
                return

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
    discard_proposal(proposal_id)
    return PluginDevApplyResponse(version_id="")


@router.get("/history/{file_path:path}", summary="查看插件文件历史", response_model=PluginDevHistoryResponse)
@require_role(Role.Admin)
async def get_plugin_dev_history(
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevHistoryResponse:
    resolve_plugin_file(file_path)
    return get_history(file_path)


@router.post("/rollback/{file_path:path}", summary="回退插件文件", response_model=PluginDevRollbackResponse)
@require_role(Role.Admin)
async def rollback_plugin_dev_file(
    body: PluginDevRollbackRequest,
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDevRollbackResponse:
    resolve_plugin_file(file_path)
    version_id = rollback(file_path, body.version_id, body.target)
    return PluginDevRollbackResponse(version_id=version_id)
