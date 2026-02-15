import asyncio
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

import magic

from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.adapters.interface.schemas.platform import PlatformSendResponse
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core.config import config as core_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.agent_message import (
    AgentMessageSegment,
    AgentMessageSegmentType,
    convert_agent_message_to_prompt,
)
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.quota_service import quota_service
from nekro_agent.tools.common_util import (
    check_content_trigger,
    check_forbidden_message,
    copy_to_upload_dir,
    random_chat_check,
)

logger = get_sub_logger("message_pipeline")
class MessageService:
    """消息服务类，处理所有类型的消息推送"""

    def __init__(self):
        # 全局状态追踪
        self.running_tasks: Dict[str, asyncio.Task] = {}  # 记录每个频道正在执行的agent任务
        self.debounce_timers: Dict[str, float] = {}  # 记录每个频道的防抖计时器
        self.pending_messages: Dict[str, ChatMessage] = {}  # 记录每个频道待处理的最新消息

    async def stop_chat_task(self, chat_key: str) -> bool:
        """终止指定频道的当前任务

        Args:
            chat_key: 频道标识

        Returns:
            bool: 是否成功终止任务
        """
        from nekro_agent.services.sandbox.runner import stop_sandbox_container

        stopped = False

        # 1. 只清理防抖计时器，保留待处理消息让它后续继续处理
        self.debounce_timers.pop(chat_key, None)

        # 2. 停止沙盒容器
        container_stopped = await stop_sandbox_container(chat_key)
        if container_stopped:
            logger.info(f"已停止频道 {chat_key} 的沙盒容器")
            stopped = True

        # 3. 取消正在运行的 asyncio 任务
        if chat_key in self.running_tasks:
            task = self.running_tasks[chat_key]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info(f"已取消频道 {chat_key} 的 Agent 任务")
                stopped = True
            # 注意：不在这里删除 running_tasks，因为 finally 可能会创建新任务
            # running_tasks 的清理由 _run_chat_agent_task 的 finally 负责

        return stopped

    async def _message_validation_check(self, message: ChatMessage) -> bool:
        """消息校验"""
        plaint_text = message.content_text.strip().replace(" ", "").lower()
        is_fake_message = False

        # 检查伪造消息
        if re.match(r"<.{4,12}\|messageseparator>", plaint_text):
            is_fake_message = True
        if re.match(r"<.{4,12}\|messageseperator>", plaint_text):
            is_fake_message = True

        if "message" in plaint_text and "(id:" in plaint_text:
            is_fake_message = True
        if "from_id:" in plaint_text:  # noqa: SIM103
            is_fake_message = True

        if is_fake_message:
            logger.warning(f"检测到伪造消息: {message.content_text} | 跳过本次处理...")
            return False

        return True

    async def schedule_agent_task(
        self,
        chat_key: Optional[str] = None,
        message: Optional[ChatMessage] = None,
        ctx: Optional[AgentCtx] = None,
    ):
        """调度 agent 任务，实现防抖和任务控制"""
        if not message:
            if not chat_key:
                logger.error("调度 Agent 执行失败，目标 chat_key 为空")
                return
            message = ChatMessage.create_empty(chat_key)
        chat_key = message.chat_key

        current_time = time.time()

        # 更新待处理消息和防抖计时器
        self.pending_messages[chat_key] = message
        self.debounce_timers[chat_key] = current_time

        # 如果已有正在执行的任务，直接返回
        if chat_key in self.running_tasks and not self.running_tasks[chat_key].done():
            return

        # 创建防抖任务
        asyncio.create_task(self._debounce_task(chat_key, current_time, ctx))

    async def _debounce_task(self, chat_key: str, start_time: float, ctx: Optional[AgentCtx] = None):
        """防抖任务处理

        Args:
            chat_key (str): 频道标识
            start_time (float): 任务开始时间
        """
        db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
        config = await db_chat_channel.get_effective_config()
        # 等待防抖时间
        await asyncio.sleep(config.AI_DEBOUNCE_WAIT_SECONDS)

        # 检查是否在防抖期间有新消息
        if start_time != self.debounce_timers[chat_key]:
            return

        # 获取最终要处理的消息
        final_message = self.pending_messages.pop(chat_key, None)
        if not final_message:
            return

        # 创建新的agent任务
        task = asyncio.create_task(
            self._run_chat_agent_task(
                chat_key=chat_key,
                message=final_message if not final_message.is_empty() else None,
                ctx=ctx,
            ),
        )
        self.running_tasks[chat_key] = task

    async def _run_chat_agent_task(self, chat_key: str, message: Optional[ChatMessage] = None, ctx: Optional[AgentCtx] = None):
        """执行agent任务"""
        from nekro_agent.services.agent.run_agent import run_agent

        adapter = await adapter_utils.get_adapter_for_chat(chat_key)

        logger.info(f"Message From {chat_key} is ToMe, Running Chat Agent...")

        # 设置处理emoji
        if message and adapter.config.SESSION_PROCESSING_WITH_EMOJI and message.message_id:
            await adapter.set_message_reaction(message.message_id, True)

        was_cancelled = False
        try:
            for _i in range(3):
                try:
                    await run_agent(chat_key=chat_key, chat_message=message, ctx=ctx)
                except asyncio.CancelledError:
                    logger.info(f"频道 {chat_key} 的 Agent 任务被取消")
                    was_cancelled = True
                    raise  # 重新抛出以便正确处理
                except Exception as e:
                    logger.exception(f"执行失败: {e}")
                else:
                    break
            else:
                logger.error("Failed to Run Chat Agent.")
        except asyncio.CancelledError:
            # 任务被取消，标记状态
            was_cancelled = True
        finally:
            # 清理任务状态
            if chat_key in self.running_tasks:
                del self.running_tasks[chat_key]

            # 取消处理emoji（如果设置过）
            if adapter.config.SESSION_PROCESSING_WITH_EMOJI and message and message.message_id:
                await adapter.set_message_reaction(message.message_id, False)

            final_message = self.pending_messages.pop(chat_key, None)
            self.debounce_timers.pop(chat_key, None)

            # 如果有待处理消息，创建新的任务处理
            if final_message:
                if was_cancelled:
                    logger.info(f"频道 {chat_key} 任务被取消，继续处理待处理消息")
                new_task = asyncio.create_task(self._run_chat_agent_task(chat_key=chat_key, message=final_message, ctx=ctx))
                self.running_tasks[chat_key] = new_task

    async def push_human_message(
        self,
        message: ChatMessage,
        user: Optional[DBUser] = None,
        trigger_agent: bool = False,
        db_chat_channel: Optional[DBChatChannel] = None,
    ):
        """推送人类用户消息"""
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=message.chat_key)
        config = await db_chat_channel.get_effective_config()
        preset = await db_chat_channel.get_preset()

        if not await self._message_validation_check(message):
            logger.warning("消息校验失败，跳过本次处理...")
            return

        content_data = [o.model_dump() for o in message.content_data]

        if check_forbidden_message(message.content_text, config):
            logger.info(f"消息 {message.content_text} 被禁止，跳过本次处理...")
            return

        ctx: AgentCtx = await AgentCtx.create_by_chat_key(chat_key=message.chat_key)
        ctx._trigger_db_user = user  # noqa: SLF001

        signal = await plugin_collector.handle_on_user_message(ctx, message)
        if signal == MsgSignal.BLOCK_ALL:
            logger.info(f"用户消息 {message.content_text} 被插件阻止响应，跳过本次处理...")
            return

        # 添加聊天记录
        await DBChatMessage.create(
            message_id=message.message_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            sender_nickname=message.sender_nickname,
            adapter_key=message.adapter_key,
            platform_userid=message.platform_userid,
            is_tome=message.is_tome,
            is_recalled=message.is_recalled,
            chat_key=message.chat_key,
            chat_type=message.chat_type,
            content_text=message.content_text,
            content_data=json.dumps(content_data, ensure_ascii=False),
            raw_cq_code=message.raw_cq_code,
            ext_data=json.dumps(message.ext_data, ensure_ascii=False),
            send_timestamp=int(time.time()),  # 使用处理后的时间戳
        )

        should_ignore = (user and user.is_prevent_trigger) or (user and not user.is_active)

        # 检查是否需要触发回复
        should_trigger = (
            trigger_agent
            or signal == MsgSignal.FORCE_TRIGGER
            or preset.name in message.content_text
            or message.is_tome
            or random_chat_check(config)
            or check_content_trigger(message.content_text, config)
        )

        if not should_ignore and should_trigger:
            if not db_chat_channel.is_active:
                logger.info(f"聊天频道 {message.chat_key} 已被禁用，跳过本次处理...")
                return

            # 检查每日回复数量限制（系统超管和白名单用户不受限制）
            if (
                config.AI_CHAT_DAILY_REPLY_LIMIT > 0
                and message.sender_id not in core_config.SUPER_USERS
                and message.sender_id not in core_config.AI_CHAT_DAILY_REPLY_WHITELIST_USERS
            ):
                daily_count = await DBChatMessage.get_daily_bot_reply_count(message.chat_key)
                effective_limit = config.AI_CHAT_DAILY_REPLY_LIMIT + quota_service.get_boost(message.chat_key)
                if daily_count >= effective_limit:
                    logger.info(
                        f"聊天频道 {message.chat_key} 今日回复数量已达上限 {effective_limit}，跳过本次处理...",
                    )
                    from nekro_agent.services.chat.universal_chat_service import (
                        universal_chat_service,
                    )

                    await universal_chat_service.send_operation_message(
                        chat_key=message.chat_key,
                        message=f"今日回复数量已达上限 ({daily_count}/{effective_limit})，明天再来吧~",
                    )
                    return

            if signal not in [MsgSignal.CONTINUE, MsgSignal.FORCE_TRIGGER]:
                logger.info(f"用户消息 {message.content_text} 被插件阻止触发，跳过本次处理...")
                return

            await self.schedule_agent_task(message=message, ctx=ctx)

    async def push_bot_message(
        self,
        chat_key: str,
        agent_messages: Union[str, List[AgentMessageSegment]],
        plt_response: Optional[PlatformSendResponse] = None,
        db_chat_channel: Optional[DBChatChannel] = None,
        ref_msg_id: Optional[str] = None,
    ):
        """推送机器人消息"""
        logger.info(f"Pushing Bot Message To Chat {chat_key}")
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=chat_key)
        preset = await db_chat_channel.get_preset()

        if isinstance(agent_messages, str):
            agent_messages = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=agent_messages)]

        content_text = convert_agent_message_to_prompt(agent_messages)

        content_data = []
        for msg in agent_messages:
            if msg.type == AgentMessageSegmentType.FILE:
                # 使用magic库检测文件MIME类型
                file_path = Path(msg.content)
                if file_path.exists():
                    mime_type = magic.from_buffer(file_path.read_bytes(), mime=True)
                    if mime_type.startswith("image/"):
                        # 复制文件到uploads目录
                        local_path, file_name = await copy_to_upload_dir(
                            str(file_path),
                            file_name=file_path.name,
                            from_chat_key=chat_key,
                        )

                        content_data.append(
                            {
                                "type": "image",
                                "text": "",
                                "file_name": file_name,
                                "local_path": local_path,  # 使用复制后的路径
                                "remote_url": "",
                            },
                        )
            elif msg.type == AgentMessageSegmentType.TEXT:
                content_data.append(
                    {
                        "type": "text",
                        "text": msg.content,
                    },
                )

        adapter = adapter_utils.get_adapter(db_chat_channel.adapter_key)
        await DBChatMessage.create(
            message_id=plt_response.message_id if plt_response and plt_response.message_id else "",
            sender_id=-1,
            sender_name=preset.name,
            sender_nickname=preset.name,
            adapter_key=db_chat_channel.adapter_key,
            platform_userid=(await adapter.get_self_info()).user_id,
            is_tome=0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=db_chat_channel.chat_type,
            content_text=content_text,
            content_data=json.dumps(content_data, ensure_ascii=False),
            raw_cq_code="",
            ext_data=json.dumps(PlatformMessageExt(ref_msg_id=ref_msg_id or "").model_dump(), ensure_ascii=False),
            send_timestamp=int(time.time()),
        )

    async def push_system_message(
        self,
        chat_key: str,
        agent_messages: Union[str, List[AgentMessageSegment]],
        trigger_agent: bool = False,
        db_chat_channel: Optional[DBChatChannel] = None,
    ):
        """推送系统消息"""
        logger.info(f"Pushing System Message To Chat {chat_key}")
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=chat_key)

        if isinstance(agent_messages, str):
            agent_messages = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=agent_messages)]

        content_text = convert_agent_message_to_prompt(agent_messages)

        ctx: AgentCtx = await AgentCtx.create_by_chat_key(chat_key=chat_key)

        signal = await plugin_collector.handle_on_system_message(ctx, content_text)
        if signal == MsgSignal.BLOCK_ALL:
            logger.info(f"系统消息 {content_text} 被插件阻止响应，跳过本次处理...")
            return

        await DBChatMessage.create(
            message_id="",
            sender_id=-1,
            sender_name="SYSTEM",
            sender_nickname="SYSTEM",
            adapter_key=db_chat_channel.adapter_key,
            platform_userid="0",
            is_tome=1 if trigger_agent else 0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=db_chat_channel.chat_type,
            content_text=content_text,
            content_data=json.dumps([], ensure_ascii=False),
            raw_cq_code="",
            ext_data={},
            send_timestamp=int(time.time()),
        )

        if trigger_agent or signal == MsgSignal.FORCE_TRIGGER:
            if not db_chat_channel.is_active:
                logger.info(f"聊天频道 {chat_key} 已被禁用，跳过本次处理...")
                return

            # 系统消息触发的回复不受配额限制

            if signal not in [MsgSignal.CONTINUE, MsgSignal.FORCE_TRIGGER]:
                logger.info(f"系统消息 {content_text} 被插件阻止触发，跳过本次处理...")
                return
            await self.schedule_agent_task(chat_key=chat_key, ctx=ctx)


# 全局消息服务实例
message_service = MessageService()
