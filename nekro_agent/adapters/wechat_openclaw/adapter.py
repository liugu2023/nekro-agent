"""WeChat OpenClaw 适配器主类

基于 iLink Bot API 的微信适配器，通过 OpenClaw 代理服务与微信对接。
支持私聊消息收发、CDN 媒体上传/下载（AES-128-ECB 加密）、扫码登录等。
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Optional

from nekro_agent.adapters.interface.base import AdapterMetadata, BaseAdapter
from nekro_agent.adapters.interface.schemas.platform import (
    ChatType,
    PlatformChannel,
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformSendSegmentType,
    PlatformUser,
)
from nekro_agent.core.logger import get_sub_logger

from . import sender
from .api_client import ILinkApiClient
from .config import WeChatOpenClawConfig
from .config_cache import ConfigCacheManager
from .context_token import ContextTokenStore
from .monitor import WeixinMonitor
from .sender import TypingManager
from .session_guard import SessionGuard

logger = get_sub_logger("adapter.wechat_openclaw")


class WeChatOpenClawAdapter(BaseAdapter[WeChatOpenClawConfig]):
    """基于 iLink Bot API 的微信 OpenClaw 适配器"""

    def __init__(self, config_cls: type[WeChatOpenClawConfig] = WeChatOpenClawConfig):
        super().__init__(config_cls)

        # 核心组件
        self.api_client = ILinkApiClient(self.config)
        self.context_store = ContextTokenStore()
        self.session_guard = SessionGuard()
        self.config_cache = ConfigCacheManager()
        self.typing_manager = TypingManager(
            self.api_client, self.config, self.context_store, self.config_cache,
        )

        # Monitor
        self.monitor: Optional[WeixinMonitor] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None

    @property
    def key(self) -> str:
        return "wechat_openclaw"

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            name="WeChat OpenClaw",
            description="基于 iLink Bot API 的微信适配器（通过 OpenClaw 代理服务）",
            version="1.0.0",
            author="nekro-agent",
            tags=["wechat", "openclaw", "ilink"],
        )

    @property
    def chat_key_rules(self) -> list[str]:
        return [
            "私聊: `wechat_openclaw-{user_id@im.wechat}` (用户微信 ID)",
        ]

    def get_adapter_router(self):
        """获取适配器路由"""
        from .routers import router
        return router

    # ======================== 生命周期 ========================

    async def init(self) -> None:
        """初始化适配器"""
        if not self.config.is_configured:
            logger.warning("WeChat OpenClaw 适配器未配置 BOT_TOKEN 或 ACCOUNT_ID，跳过初始化")
            logger.info("请先通过扫码登录或手动配置凭证")
            return

        await self._start_monitor()
        logger.info("WeChat OpenClaw 适配器初始化成功")

    async def cleanup(self) -> None:
        """清理适配器"""
        await self._stop_monitor()
        await self.api_client.close()
        logger.info("WeChat OpenClaw 适配器已清理")

    async def _start_monitor(self) -> None:
        """启动消息监控"""
        self.monitor = WeixinMonitor(
            adapter=self,
            api_client=self.api_client,
            context_store=self.context_store,
            session_guard=self.session_guard,
        )

        async def _run_with_error_handling() -> None:
            try:
                await self.monitor.start()  # type: ignore[union-attr]
            except Exception:
                logger.exception("WeixinMonitor 运行异常")

        self._monitor_task = asyncio.create_task(_run_with_error_handling())

    async def _stop_monitor(self) -> None:
        """停止消息监控"""
        if self.monitor and self.monitor.is_running:
            await self.monitor.stop()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task

    async def _reinit_after_login(self) -> None:
        """登录成功后重新初始化"""
        await self._stop_monitor()
        # 重新创建 API 客户端（使用新 token）
        await self.api_client.close()
        self.api_client = ILinkApiClient(self.config)
        self.typing_manager = TypingManager(
            self.api_client, self.config, self.context_store, self.config_cache,
        )
        await self._start_monitor()
        logger.info("登录成功，已重新初始化适配器")

    # ======================== 消息发送 ========================

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        """推送消息到微信"""
        try:
            _, channel_id = self.parse_chat_key(request.chat_key)
            account_id = self.config.ACCOUNT_ID

            # 获取 context_token
            context_token = self.context_store.get(account_id, channel_id)
            if not context_token:
                return PlatformSendResponse(
                    success=False,
                    error_message="缺少 context_token，请等待用户先发送消息",
                )

            # 检查会话暂停
            if self.session_guard.is_paused(account_id):
                remaining = self.session_guard.remaining_seconds(account_id)
                return PlatformSendResponse(
                    success=False,
                    error_message=f"会话暂停中，剩余 {remaining}s",
                )

            # 启动 typing
            await self.typing_manager.start_typing_loop(account_id, channel_id)

            try:
                message_ids: list[str] = []

                for segment in request.segments:
                    if segment.type == PlatformSendSegmentType.TEXT:
                        if segment.content and segment.content.strip():
                            await sender.send_text(
                                self.api_client, account_id, channel_id,
                                context_token, segment.content,
                            )
                            message_ids.append("text")

                    elif segment.type == PlatformSendSegmentType.AT:
                        # 微信私聊无 AT 功能，转为文本
                        if segment.at_info:
                            at_text = f"@{segment.at_info.nickname or segment.at_info.platform_user_id}"
                            await sender.send_text(
                                self.api_client, account_id, channel_id,
                                context_token, at_text,
                            )
                            message_ids.append("at")

                    elif segment.type == PlatformSendSegmentType.IMAGE:
                        if segment.file_path and Path(segment.file_path).exists():
                            await sender.send_image_message(
                                self.api_client, account_id, channel_id,
                                context_token, segment.file_path,
                                self.config.CDN_BASE_URL,
                            )
                            message_ids.append("image")

                    elif segment.type == PlatformSendSegmentType.FILE:
                        if segment.file_path and Path(segment.file_path).exists():
                            # 判断是否为视频
                            suffix = Path(segment.file_path).suffix.lower()
                            if suffix in (".mp4", ".avi", ".mov", ".mkv"):
                                await sender.send_video_message(
                                    self.api_client, account_id, channel_id,
                                    context_token, segment.file_path,
                                    self.config.CDN_BASE_URL,
                                )
                            else:
                                await sender.send_file_message(
                                    self.api_client, account_id, channel_id,
                                    context_token, segment.file_path,
                                    self.config.CDN_BASE_URL,
                                )
                            message_ids.append("file")

            finally:
                # 停止 typing
                self.typing_manager.cancel_typing(channel_id)

            return PlatformSendResponse(
                success=len(message_ids) > 0,
                message_id=",".join(message_ids) if message_ids else None,
            )

        except Exception as e:
            logger.exception("消息发送失败")
            return PlatformSendResponse(
                success=False,
                error_message=f"消息发送失败: {e!s}",
            )

    # ======================== 信息查询 ========================

    async def get_self_info(self) -> PlatformUser:
        """获取自身信息"""
        return PlatformUser(
            platform_name=self.key,
            user_id=self.config.LINKED_USER_ID or self.config.ACCOUNT_ID,
            user_name="WeChat Bot",
        )

    async def get_user_info(self, user_id: str, channel_id: str) -> PlatformUser:
        """获取用户信息（iLink API 不提供用户信息查询）"""
        return PlatformUser(
            platform_name=self.key,
            user_id=user_id,
            user_name=user_id,
        )

    async def get_channel_info(self, channel_id: str) -> PlatformChannel:
        """获取频道信息"""
        return PlatformChannel(
            channel_id=channel_id,
            channel_name=channel_id,
            channel_type=ChatType.PRIVATE,
        )
