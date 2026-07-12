import json
from pathlib import Path, PurePosixPath
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from nekro_agent.core.os_env import WORKDIR_PLUGIN_DIR
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.generator import (
    apply_plugin_code,
    generate_plugin_code,
    generate_plugin_code_stream,
    generate_plugin_template,
)
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/plugin-editor", tags=["Plugin Editor"])


class FileContentResponse(BaseModel):
    """文件内容响应"""

    content: str


class ActionResponse(BaseModel):
    ok: bool = True


class ToggleFileResponse(ActionResponse):
    file_path: str


def _resolve_plugin_file(file_path: str, *, must_exist: bool = False) -> tuple[Path, Path]:
    """解析并校验插件文件路径，阻止路径穿越与符号链接越界。"""
    if not WORKDIR_PLUGIN_DIR:
        raise ValidationError(reason="工作目录插件目录未配置")

    normalized_path = file_path.replace("\\", "/")
    relative_path = PurePosixPath(normalized_path)
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise ValidationError(reason="文件路径非法")
    if not normalized_path.endswith((".py", ".py.disabled")):
        raise ValidationError(reason="仅允许操作 Python 插件文件")

    plugin_dir = Path(WORKDIR_PLUGIN_DIR).resolve()
    full_path = (plugin_dir / Path(*relative_path.parts)).resolve(strict=False)
    try:
        full_path.relative_to(plugin_dir)
    except ValueError as e:
        raise ValidationError(reason="文件路径非法") from e

    if must_exist and not full_path.is_file():
        raise NotFoundError(resource=f"文件 {file_path}")
    return plugin_dir, full_path


@router.get("/files", summary="获取插件文件列表", response_model=list[str])
@require_role(Role.Admin)
async def get_plugin_files(
    _current_user: DBUser = Depends(get_current_active_user),
) -> list[str]:
    """获取插件文件列表"""
    if not WORKDIR_PLUGIN_DIR:
        raise ValidationError(reason="工作目录插件目录未配置")

    plugin_dir = Path(WORKDIR_PLUGIN_DIR).resolve()
    if not plugin_dir.exists():
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return []

    files: list[str] = []
    for pattern in ["**/*.py", "**/*.py.disabled"]:
        for file in plugin_dir.glob(pattern):
            try:
                file.resolve().relative_to(plugin_dir)
            except ValueError:
                continue
            files.append(file.relative_to(plugin_dir).as_posix())

    return files


@router.get("/file/{file_path:path}", summary="获取插件文件内容", response_model=FileContentResponse)
@require_role(Role.Admin)
async def get_plugin_file_content(
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> FileContentResponse:
    """获取插件文件内容"""
    _, full_path = _resolve_plugin_file(file_path, must_exist=True)

    content = full_path.read_text(encoding="utf-8")
    return FileContentResponse(content=content)


@router.post("/file/{file_path:path}", summary="保存插件文件", response_model=ActionResponse)
@require_role(Role.Admin)
async def save_plugin_file(
    request: Request,
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """保存插件文件"""
    _, full_path = _resolve_plugin_file(file_path)

    full_path.parent.mkdir(parents=True, exist_ok=True)

    content = await request.body()
    content_str = content.decode("utf-8")

    full_path.write_text(content_str, encoding="utf-8")
    return ActionResponse(ok=True)


@router.delete("/files/{file_path:path}", summary="删除插件文件", response_model=ActionResponse)
@require_role(Role.Admin)
async def delete_plugin_file(
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除插件文件"""
    plugin_dir, full_path = _resolve_plugin_file(file_path, must_exist=True)
    relative_path = full_path.relative_to(plugin_dir)
    module_name = relative_path.parts[0]
    was_loaded = plugin_collector.get_plugin_by_module_name(module_name) is not None

    # 按顶层模块名卸载（包内文件删除时卸载其所属的顶层包插件），
    # 避免已注册的命令、路由继续引用即将删除的旧模块。
    await plugin_collector.unload_plugin_by_module_name(module_name)

    try:
        full_path.unlink()
    except OSError:
        if was_loaded:
            await plugin_collector.reload_plugin_by_module_name(module_name)
        raise

    top_level_package_entry = plugin_dir / relative_path.parts[0] / "__init__.py"
    if len(relative_path.parts) > 1 and top_level_package_entry.exists():
        # 删除普通包内文件后恢复顶层包运行。若删除的是被入口依赖的模块，
        # collector 会记录加载失败并让插件保持卸载，文件删除本身仍视为成功。
        await plugin_collector.reload_plugin_by_module_name(relative_path.parts[0])

    return ActionResponse(ok=True)


@router.post("/toggle/{file_path:path}", summary="启用或禁用插件文件", response_model=ToggleFileResponse)
@require_role(Role.Admin)
async def toggle_plugin_file(
    file_path: str = PathParam(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ToggleFileResponse:
    """通过重命名插件入口文件启用或禁用插件。"""
    plugin_dir, full_path = _resolve_plugin_file(file_path, must_exist=True)
    relative_path = full_path.relative_to(plugin_dir)
    if len(relative_path.parts) > 1 and relative_path.name not in {"__init__.py", "__init__.py.disabled"}:
        raise ValidationError(reason="仅允许启用或禁用插件入口文件")
    is_disabled = full_path.name.endswith(".py.disabled")
    enabled_path = full_path.with_name(full_path.name.removesuffix(".disabled"))
    disabled_path = full_path.with_name(f"{full_path.name}.disabled")
    target_path = enabled_path if is_disabled else disabled_path

    if target_path.exists():
        raise ValidationError(reason=f"目标文件 {target_path.relative_to(plugin_dir).as_posix()} 已存在")

    module_name = relative_path.parts[0]
    if not is_disabled:
        was_loaded = plugin_collector.get_plugin_by_module_name(module_name) is not None
        await plugin_collector.unload_plugin_by_module_name(module_name)
        try:
            full_path.rename(target_path)
        except OSError:
            if was_loaded:
                await plugin_collector.reload_plugin_by_module_name(module_name)
            raise
    else:
        full_path.rename(target_path)
        target_relative_path = target_path.relative_to(plugin_dir)
        reload_ref = target_relative_path.parts[0] if len(target_relative_path.parts) > 1 else target_relative_path.as_posix()
        await plugin_collector.reload_plugin_by_module_name(reload_ref)

    return ToggleFileResponse(ok=True, file_path=target_path.relative_to(plugin_dir).as_posix())


class GenerateCodeRequest(BaseModel):
    """生成代码请求体"""

    file_path: str
    prompt: str
    current_code: Optional[str] = None


class GenerateCodeResponse(BaseModel):
    """生成代码响应体"""

    code: str


@router.post("/generate", summary="生成插件代码", response_model=GenerateCodeResponse)
@require_role(Role.Admin)
async def generate_code(
    body: GenerateCodeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> GenerateCodeResponse:
    """生成插件代码"""
    if not body.prompt:
        raise ValidationError(reason="提示词不能为空")

    code = await generate_plugin_code(
        file_path=body.file_path,
        prompt=body.prompt,
        current_code=body.current_code,
    )
    return GenerateCodeResponse(code=code)


@router.post("/generate/stream", summary="流式生成插件代码")
@require_role(Role.Admin)
async def generate_code_stream(
    request: Request,
    body: GenerateCodeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EventSourceResponse:
    """流式生成插件代码"""

    async def event_generator() -> AsyncGenerator[str, None]:
        async for chunk in generate_plugin_code_stream(
            file_path=body.file_path,
            prompt=body.prompt,
            current_code=body.current_code,
        ):
            if is_shutting_down() or await request.is_disconnected():
                return
            yield json.dumps({"type": "content", "content": chunk})

        if is_shutting_down() or await request.is_disconnected():
            return
        yield json.dumps({"type": "done"})

    return EventSourceResponse(event_generator())


@router.post("/apply", summary="应用生成的代码", response_model=GenerateCodeResponse)
@require_role(Role.Admin)
async def apply_code(
    body: GenerateCodeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> GenerateCodeResponse:
    """应用生成的代码"""
    if not body.prompt or not body.current_code:
        raise ValidationError(reason="参数不完整")

    code = await apply_plugin_code(
        file_path=body.file_path,
        prompt=body.prompt,
        current_code=body.current_code,
    )
    return GenerateCodeResponse(code=code)


class TemplateRequest(BaseModel):
    """模板请求体"""

    name: str
    description: str


class TemplateResponse(BaseModel):
    """模板响应体"""

    template: str


@router.post("/template", summary="生成插件模板", response_model=TemplateResponse)
@require_role(Role.Admin)
async def create_plugin_template(
    body: TemplateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TemplateResponse:
    """生成插件模板"""
    template = generate_plugin_template(name=body.name, description=body.description)
    return TemplateResponse(template=template)
