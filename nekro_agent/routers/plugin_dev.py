from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.plugin_dev import (
    PluginDevApplyResponse,
    PluginDevCcModelPresetUpdate,
    PluginDevGenerateRequest,
    PluginDevGenerateResponse,
    PluginDevHistoryResponse,
    PluginDevProposalResponse,
    PluginDevRollbackRequest,
    PluginDevRollbackResponse,
    PluginDevStatusResponse,
    PluginDevTaskResponse,
    PluginDevVersionInfo,
    PluginDevVersionUpdate,
)
from nekro_agent.services.plugin_dev.config import get_plugin_dev_config, update_plugin_dev_config
from nekro_agent.services.plugin_dev.host_file_gateway import resolve_plugin_file
from nekro_agent.services.plugin_dev.sandbox import PluginDevSandboxService
from nekro_agent.services.plugin_dev.tasks import (
    apply_proposal,
    cancel_task,
    create_task,
    discard_proposal,
    get_proposal,
    get_task,
)
from nekro_agent.services.plugin_dev.versioning import get_history, get_version_info, rollback, update_version_info
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/plugin-dev", tags=["Plugin Dev"])


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
    return PluginDevStatusResponse(
        sandbox_status=sandbox_status,
        cc_model_preset_id=preset_id,
        cc_model_preset_name=preset_name,
        version=get_version_info(),
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
