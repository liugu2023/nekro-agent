"""WeixinMonitor 长轮询主循环

从 iLink Bot API 拉取消息并分发到 NA 消息收集器。
参考: OpenClaw src/monitor/monitor.ts + src/messaging/process-message.ts
"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from nekro_agent.adapters.interface.collector import collect_message
from nekro_agent.adapters.interface.schemas.platform import (
    ChatType,
    PlatformChannel,
    PlatformMessage,
    PlatformUser,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.schemas.chat_message import (
    ChatMessageSegment,
    ChatMessageSegmentType,
)

from . import cdn
from .api_client import ILinkApiClient
from .context_token import ContextTokenStore
from .session_guard import SessionGuard
from .types import MessageItemType, WeixinMessage

if TYPE_CHECKING:
    from .adapter import WeChatOpenClawAdapter

logger = get_sub_logger("adapter.wechat_openclaw.monitor")


class WeixinMonitor:
    """微信消息长轮询监控器"""

    def __init__(
        self,
        adapter: "WeChatOpenClawAdapter",
        api_client: ILinkApiClient,
        context_store: ContextTokenStore,
        session_guard: SessionGuard,
    ) -> None:
        self._adapter = adapter
        self._api = api_client
        self._context_store = context_store
        self._session_guard = session_guard
        self._sync_buf = ""
        self._running = False
        self._consecutive_failures = 0
        self._sync_buf_path = Path(OsEnv.DATA_DIR) / "configs" / "wechat_openclaw" / "sync_buf.json"

    @property
    def is_running(self) -> bool:
        return self._running

    def _load_sync_buf(self) -> None:
        """从持久化文件加载 sync_buf"""
        if self._sync_buf_path.exists():
            try:
                data = json.loads(self._sync_buf_path.read_text(encoding="utf-8"))
                self._sync_buf = data.get("sync_buf", "")
                logger.info("已恢复 sync_buf")
            except Exception:
                logger.warning("加载 sync_buf 失败，将从头开始同步")
                self._sync_buf = ""

    def _save_sync_buf(self) -> None:
        """持久化 sync_buf"""
        try:
            self._sync_buf_path.parent.mkdir(parents=True, exist_ok=True)
            self._sync_buf_path.write_text(
                json.dumps({"sync_buf": self._sync_buf}),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("保存 sync_buf 失败")

    async def start(self) -> None:
        """启动长轮询主循环"""
        self._running = True
        self._load_sync_buf()
        account_id = self._adapter.config.ACCOUNT_ID

        logger.info(f"WeixinMonitor 启动，account_id={account_id}")

        while self._running:
            # 检查会话暂停
            if self._session_guard.is_paused(account_id):
                remaining = self._session_guard.remaining_seconds(account_id)
                logger.info(f"账号 {account_id} 处于暂停状态，剩余 {remaining}s")
                await asyncio.sleep(min(remaining, 30))
                continue

            try:
                resp = await self._api.get_updates(account_id, self._sync_buf)

                # 错误处理
                if resp.errcode == -14:
                    logger.warning("收到 errcode=-14，会话过期，暂停 1 小时")
                    self._session_guard.pause(account_id, 3600)
                    continue

                if resp.ret != 0 and resp.errcode != 0:
                    raise RuntimeError(f"getUpdates 错误: ret={resp.ret}, errcode={resp.errcode}, msg={resp.errmsg}")

                # 重置连续失败计数
                self._consecutive_failures = 0

                # 更新 sync_buf
                if resp.sync_buf:
                    self._sync_buf = resp.sync_buf
                    self._save_sync_buf()

                # 处理消息
                for msg in resp.msgs:
                    try:
                        await self._process_message(msg)
                    except Exception:
                        logger.exception(f"处理消息失败: msg_id={msg.msg_id}")

            except RuntimeError:
                raise
            except Exception:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    delay = 30
                    logger.exception(f"连续失败 {self._consecutive_failures} 次，等待 {delay}s")
                else:
                    delay = 2
                    logger.warning(f"getUpdates 失败，{delay}s 后重试")
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        """停止长轮询"""
        self._running = False
        self._save_sync_buf()
        logger.info("WeixinMonitor 已停止")

    async def _process_message(self, msg: WeixinMessage) -> None:
        """处理单条微信消息"""
        account_id = self._adapter.config.ACCOUNT_ID
        config = self._adapter.config

        # 1. 缓存 context_token
        if msg.context_token:
            self._context_store.set(account_id, msg.from_user_id, msg.context_token)

        # 2. 跳过自己发送的消息
        if msg.from_user_id == config.LINKED_USER_ID:
            return

        # 3. 构建 chat_key
        chat_key = self._adapter.build_chat_key(msg.from_user_id)
        cdn_base_url = config.CDN_BASE_URL

        # 4. 解析消息项
        segments: list[ChatMessageSegment] = []
        content_parts: list[str] = []

        for item in msg.item_list:
            item_type = item.item_type

            if item_type == MessageItemType.TEXT:
                # 文本消息
                text = item.text_item.content if item.text_item else ""

                # 处理引用消息
                if item.ref_message and (item.ref_message.title or item.ref_message.content):
                    ref_title = item.ref_message.title or ""
                    ref_content = item.ref_message.content or ""
                    ref_text = f"[引用: {ref_title} | {ref_content}]"
                    if text:
                        text = f"{ref_text}\n{text}"
                    else:
                        text = ref_text

                if text:
                    segments.append(ChatMessageSegment(
                        type=ChatMessageSegmentType.TEXT,
                        text=text,
                    ))
                    content_parts.append(text)

            elif item_type == MessageItemType.IMAGE:
                # 图片消息
                if item.image_item and item.image_item.cdn.file_url:
                    try:
                        img_seg = await cdn.download_image(
                            file_url=item.image_item.cdn.file_url,
                            aes_key=item.image_item.cdn.aes_key,
                            cdn_base_url=cdn_base_url,
                            chat_key=chat_key,
                        )
                        segments.append(img_seg)
                        content_parts.append("[Image]")
                    except Exception:
                        logger.exception("下载图片失败")
                        segments.append(ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text="[图片下载失败]",
                        ))
                        content_parts.append("[图片下载失败]")

            elif item_type == MessageItemType.VOICE:
                # 语音消息
                if item.voice_item:
                    # 优先使用语音转文字结果
                    if item.voice_item.text:
                        segments.append(ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text=item.voice_item.text,
                        ))
                        content_parts.append(item.voice_item.text)
                    elif item.voice_item.cdn.file_url:
                        try:
                            voice_seg = await cdn.download_voice(
                                file_url=item.voice_item.cdn.file_url,
                                aes_key=item.voice_item.cdn.aes_key,
                                cdn_base_url=cdn_base_url,
                                chat_key=chat_key,
                            )
                            segments.append(voice_seg)
                            content_parts.append("[Voice]")
                        except Exception:
                            logger.exception("下载语音失败")
                            segments.append(ChatMessageSegment(
                                type=ChatMessageSegmentType.TEXT,
                                text="[语音下载失败]",
                            ))
                            content_parts.append("[语音下载失败]")

            elif item_type == MessageItemType.VIDEO:
                # 视频消息
                if item.video_item and item.video_item.cdn.file_url:
                    try:
                        video_seg = await cdn.download_video(
                            file_url=item.video_item.cdn.file_url,
                            aes_key=item.video_item.cdn.aes_key,
                            cdn_base_url=cdn_base_url,
                            chat_key=chat_key,
                        )
                        segments.append(video_seg)
                        content_parts.append("[Video]")
                    except Exception:
                        logger.exception("下载视频失败")
                        segments.append(ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text="[视频下载失败]",
                        ))
                        content_parts.append("[视频下载失败]")

            elif item_type == MessageItemType.FILE:
                # 文件消息
                if item.file_item and item.file_item.cdn.file_url:
                    try:
                        file_seg = await cdn.download_file(
                            file_url=item.file_item.cdn.file_url,
                            aes_key=item.file_item.cdn.aes_key,
                            cdn_base_url=cdn_base_url,
                            chat_key=chat_key,
                            file_name=item.file_item.file_name,
                        )
                        segments.append(file_seg)
                        content_parts.append(f"[File: {item.file_item.file_name}]")
                    except Exception:
                        logger.exception("下载文件失败")
                        segments.append(ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text="[文件下载失败]",
                        ))
                        content_parts.append("[文件下载失败]")

        if not segments:
            return

        # 5. 构建平台数据结构
        platform_channel = PlatformChannel(
            channel_id=msg.from_user_id,
            channel_name=msg.from_user_id,
            channel_type=ChatType.PRIVATE,
        )

        platform_user = PlatformUser(
            platform_name=self._adapter.key,
            user_id=msg.from_user_id,
            user_name=msg.from_user_id,
        )

        content_text = " ".join(content_parts)

        platform_message = PlatformMessage(
            message_id=msg.msg_id or str(msg.timestamp),
            sender_id=msg.from_user_id,
            sender_name=msg.from_user_id,
            sender_nickname=msg.from_user_id,
            content_data=segments,
            content_text=content_text,
            is_tome=True,  # 私聊消息总是 @bot
            timestamp=msg.timestamp,
        )

        # 6. 收集消息
        await collect_message(self._adapter, platform_channel, platform_user, platform_message)
