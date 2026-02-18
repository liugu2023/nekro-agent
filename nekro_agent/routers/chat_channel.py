from typing import Any, Dict, List, Optional

import json5
from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from tortoise.expressions import Q

from nekro_agent.adapters import get_adapter
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/chat-channel", tags=["ChatChannel"])

logger = get_sub_logger("chat_channel_api")


class ChatChannelItem(BaseModel):
    id: int
    chat_key: str
    channel_name: Optional[str]
    is_active: bool
    chat_type: str
    message_count: int
    create_time: str
    update_time: str
    last_message_time: Optional[str]


class ChatChannelListResponse(BaseModel):
    total: int
    items: List[ChatChannelItem]


class ChatChannelDetail(ChatChannelItem):
    unique_users: int
    conversation_start_time: str
    preset_id: Optional[int]
    can_send: bool = False


class ChatMessage(BaseModel):
    id: int
    sender_id: str
    sender_name: str
    sender_nickname: str
    platform_userid: str
    content: str
    content_data: List[Dict[str, Any]]
    chat_key: str
    create_time: str


class ChatMessageListResponse(BaseModel):
    total: int
    items: List[ChatMessage]


class ActionResponse(BaseModel):
    ok: bool = True


@router.get("/list", summary="获取聊天频道列表")
@require_role(Role.Admin)
async def get_chat_channel_list(
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    chat_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatChannelListResponse:
    """获取聊天频道列表"""
    query = DBChatChannel

    if search:
        query = query.filter(
            Q(chat_key__contains=search) | Q(channel_name__contains=search),
        )
    if chat_type:
        query = query.filter(chat_key__contains=f"{chat_type}_")
    if is_active is not None:
        query = query.filter(is_active=is_active)

    channels = await query.all()

    channel_info_list = []
    for channel in channels:
        message_count = await DBChatMessage.filter(
            chat_key=channel.chat_key,
            create_time__gte=channel.conversation_start_time,
        ).count()

        last_message = await DBChatMessage.filter(chat_key=channel.chat_key).order_by("-create_time").first()

        conversation_start_time = channel.conversation_start_time
        if conversation_start_time.tzinfo is not None:
            conversation_start_time = conversation_start_time.replace(tzinfo=None)

        if last_message:
            last_message_time = last_message.create_time
            if last_message_time.tzinfo is not None:
                last_message_time = last_message_time.replace(tzinfo=None)
            last_active_time = max(conversation_start_time, last_message_time)
        else:
            last_active_time = conversation_start_time

        channel_info_list.append(
            {
                "channel": channel,
                "message_count": message_count,
                "last_active_time": last_active_time,
                "last_message_time": last_message_time if last_message else None,
            },
        )

    channel_info_list.sort(key=lambda x: x["last_active_time"], reverse=True)

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paged_channels = channel_info_list[start_idx:end_idx]

    result: List[ChatChannelItem] = []
    for info in paged_channels:
        channel = info["channel"]
        result.append(
            ChatChannelItem(
                id=channel.id,
                chat_key=channel.chat_key,
                channel_name=channel.channel_name,
                is_active=channel.is_active,
                chat_type=channel.chat_type.value,
                message_count=info["message_count"],
                create_time=channel.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                update_time=channel.update_time.strftime("%Y-%m-%d %H:%M:%S"),
                last_message_time=(
                    info["last_message_time"].strftime("%Y-%m-%d %H:%M:%S")
                    if info["last_message_time"] is not None
                    else None
                ),
            ),
        )

    return ChatChannelListResponse(
        total=len(channels),
        items=result,
    )


@router.get("/detail/{chat_key}", summary="获取聊天频道详情")
@require_role(Role.Admin)
async def get_chat_channel_detail(chat_key: str, _current_user: DBUser = Depends(get_current_active_user)) -> ChatChannelDetail:
    """获取聊天频道详情"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    message_count = await DBChatMessage.filter(chat_key=chat_key, create_time__gte=channel.conversation_start_time).count()
    last_message = await DBChatMessage.filter(chat_key=chat_key).order_by("-create_time").first()
    last_message_time = last_message.create_time if last_message else None
    unique_users = await DBChatMessage.filter(chat_key=chat_key).distinct().values_list("sender_id", flat=True)

    # 检测适配器是否支持 WebUI 发送
    can_send = False
    try:
        adapter = get_adapter(channel.adapter_key)
        can_send = adapter.supports_webui_send
    except Exception:
        pass

    return ChatChannelDetail(
        id=channel.id,
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        is_active=channel.is_active,
        chat_type=channel.chat_type.value,
        message_count=message_count,
        unique_users=len(unique_users),
        create_time=channel.create_time.strftime("%Y-%m-%d %H:%M:%S"),
        update_time=channel.update_time.strftime("%Y-%m-%d %H:%M:%S"),
        last_message_time=last_message_time.strftime("%Y-%m-%d %H:%M:%S") if last_message_time else None,
        conversation_start_time=channel.conversation_start_time.strftime("%Y-%m-%d %H:%M:%S"),
        preset_id=channel.preset_id,
        can_send=can_send,
    )


@router.post("/{chat_key}/active", summary="设置聊天频道激活状态")
@require_role(Role.Admin)
async def set_chat_channel_active(
    chat_key: str,
    is_active: bool,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置聊天频道激活状态"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    await channel.set_active(is_active)
    return ActionResponse(ok=True)


@router.post("/{chat_key}/reset", summary="重置聊天频道状态")
@require_role(Role.Admin)
async def reset_chat_channel(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重置聊天频道状态"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    await channel.reset_channel()
    return ActionResponse(ok=True)


@router.get("/{chat_key}/messages", summary="获取聊天频道消息列表")
@require_role(Role.Admin)
async def get_chat_channel_messages(
    chat_key: str,
    before_id: Optional[int] = None,
    page_size: int = 32,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatMessageListResponse:
    """获取聊天频道消息列表"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    query = DBChatMessage.filter(chat_key=chat_key, create_time__gte=channel.conversation_start_time)
    if before_id:
        query = query.filter(id__lt=before_id)

    total = await query.count()
    messages = await query.order_by("-id").limit(page_size)

    def _parse_content_data(raw: str) -> List[Dict[str, Any]]:
        try:
            return json5.loads(raw) if raw else []
        except Exception:
            return []

    return ChatMessageListResponse(
        total=total,
        items=[
            ChatMessage(
                id=msg.id,
                sender_id=str(msg.sender_id),
                sender_name=msg.sender_name,
                sender_nickname=msg.sender_nickname or msg.sender_name,
                platform_userid=msg.platform_userid or "",
                content=msg.content_text,
                content_data=_parse_content_data(msg.content_data),
                chat_key=msg.chat_key,
                create_time=msg.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            for msg in messages
        ],
    )


@router.post("/{chat_key}/preset", summary="设置聊天频道人设")
@require_role(Role.Admin)
async def set_chat_channel_preset(
    chat_key: str,
    preset_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置聊天频道人设，传入 preset_id=None 则使用默认人设"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    await channel.set_preset(preset_id)
    return ActionResponse(ok=True)


class SendMessageRequest(BaseModel):
    message: str


class SendMessageResponse(BaseModel):
    ok: bool = True
    error: str = ""


@router.post("/{chat_key}/send", summary="向聊天频道发送消息")
@require_role(Role.Admin)
async def send_message_to_channel(
    chat_key: str,
    message: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SendMessageResponse:
    """从 WebUI 向聊天频道发送消息（支持文本和/或文件）"""
    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if not channel:
        raise NotFoundError(resource="聊天频道")

    # 检测适配器是否支持 WebUI 发送
    try:
        adapter = get_adapter(channel.adapter_key)
        if not adapter.supports_webui_send:
            return SendMessageResponse(ok=False, error="当前适配器不支持从 WebUI 发送消息")
    except KeyError:
        return SendMessageResponse(ok=False, error="适配器未加载")

    text = message.strip()
    if not text and not file:
        return SendMessageResponse(ok=False, error="消息内容不能为空")

    try:
        from pathlib import Path

        from nekro_agent.core.os_env import USER_UPLOAD_DIR
        from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        segments: list[AgentMessageSegment] = []

        # 文本段
        if text:
            segments.append(AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=text))

        # 文件段
        is_file_mode = False
        if file and file.filename:
            safe_chat_key = Path(chat_key).name
            safe_filename = Path(file.filename).name
            upload_dir = Path(USER_UPLOAD_DIR) / safe_chat_key
            upload_dir.mkdir(parents=True, exist_ok=True)
            save_path = upload_dir / safe_filename
            # 分块写入，避免大文件一次性占满内存
            with save_path.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    f.write(chunk)
            # 使用沙盒风格路径（/app/uploads/filename），让 _preprocess_messages 的 convert_to_host_path 正确转换
            segments.append(AgentMessageSegment(type=AgentMessageSegmentType.FILE, content=f"/app/uploads/{safe_filename}"))
            # 非图片文件使用 FILE 模式发送
            is_file_mode = not (file.content_type or "").startswith("image/")

        await universal_chat_service.send_agent_message(
            chat_key=chat_key,
            messages=segments,
            adapter=adapter,
            record=True,
            file_mode=is_file_mode,
        )
        return SendMessageResponse(ok=True)
    except Exception as e:
        logger.error(f"WebUI 发送消息到 {chat_key} 失败: {e}")
        return SendMessageResponse(ok=False, error=str(e))
