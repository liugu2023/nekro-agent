"""
企业微信自建应用适配器

支持企业微信自建应用的消息接收和主动发送功能
"""

from typing import List, Optional, Type, Tuple

from fastapi import APIRouter

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

from .api_client import WxWorkApiClient
from .config import WxWorkConfig
from .crypto import WxWorkBotCrypt
from .message_processor import WxWorkMessageProcessor

logger = get_sub_logger("adapter.wxwork")


class WxWorkAdapter(BaseAdapter[WxWorkConfig]):
    """企业微信自建应用适配器"""

    api_client: Optional[WxWorkApiClient]
    crypto: Optional[WxWorkBotCrypt]
    message_processor: Optional[WxWorkMessageProcessor]

    def __init__(self, config_cls: Type[WxWorkConfig] = WxWorkConfig):
        super().__init__(config_cls)

        self.api_client = None
        self.crypto = None
        self.message_processor = None

    @property
    def key(self) -> str:
        return "wxwork"

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            name="企业微信",
            description="连接到企业微信自建应用的适配器，支持消息接收和主动发送",
            version="2.0.0",
            author="KroMiose",
            homepage="https://github.com/KroMiose/nekro-agent",
            tags=["wxwork", "wechat", "企业微信", "自建应用", "chat", "im"],
        )

    @property
    def chat_key_rules(self) -> List[str]:
        return [
            "私聊: `wxwork-private_{userid}` (如 `wxwork-private_zhangsan`)",
            "群聊: `wxwork-group_{chatid}` (如 `wxwork-group_wrkSFfCgAAxxxxxxxxx`)",
        ]

    def build_chat_key(self, chat_id_or_data) -> str:
        """构造聊天标识

        Args:
            chat_id_or_data: 可以是聊天 ID 或来自消息的数据

        Returns:
            str: 聊天标识，格式为 wxwork-{type}_{id}
        """
        if isinstance(chat_id_or_data, str):
            # 如果是字符串，判断是 userid（私聊）还是 chatid（群聊）
            # 这个启发式方法：企业微信的 chatid 通常包含特殊字符，userid 通常是简单字符
            if chat_id_or_data.startswith("wrk"):
                return f"{self.key}-group_{chat_id_or_data}"
            else:
                return f"{self.key}-private_{chat_id_or_data}"

        return f"{self.key}-private_{chat_id_or_data}"

    def parse_chat_key(self, chat_key: str) -> Tuple[str, str]:
        """解析聊天标识

        Args:
            chat_key: 聊天标识，如 wxwork-group_wrkSFfCgAAxxxxxxxxx

        Returns:
            Tuple[str, str]: (adapter_key, channel_id)

        Raises:
            ValueError: 聊天标识格式无效
        """
        parts = chat_key.split("-", 1)
        if len(parts) != 2:
            raise ValueError(f"无效的聊天标识: {chat_key}")

        adapter_key = parts[0]
        channel_id = parts[1]

        return adapter_key, channel_id

    async def init(self) -> None:
        """初始化适配器"""
        if not self.config.is_configured:
            logger.warning(
                "企业微信自建应用配置不完整，请检查 CORP_ID、CORP_SECRET、AGENT_ID、TOKEN、ENCODING_AES_KEY 配置"
            )
            return

        try:
            # 初始化 API 客户端
            self.api_client = WxWorkApiClient(
                corp_id=self.config.CORP_ID,
                corp_secret=self.config.CORP_SECRET,
                agent_id=self.config.AGENT_ID,
            )

            # 初始化加密工具
            self.crypto = WxWorkBotCrypt(
                token=self.config.TOKEN,
                encoding_aes_key=self.config.ENCODING_AES_KEY,
            )

            # 初始化消息处理器
            self.message_processor = WxWorkMessageProcessor(self)

            # 获取初始 access_token 以验证配置
            await self.api_client.get_access_token()

            logger.info("企业微信自建应用适配器初始化成功")
            logger.info("请在企业微信后台配置回调 URL:")
            logger.info("  - URL: http://your-domain/adapters/wxwork/callback")
            logger.info(f"  - Token: {self.config.TOKEN}")
            logger.info(f"  - EncodingAESKey: {self.config.ENCODING_AES_KEY}")

        except Exception as e:
            logger.error(f"企业微信自建应用适配器初始化失败: {e}")
            self.api_client = None
            self.crypto = None
            self.message_processor = None

    async def cleanup(self) -> None:
        """清理适配器"""
        logger.info("企业微信自建应用适配器已清理")

    async def forward_message(
        self,
        request: PlatformSendRequest,
    ) -> PlatformSendResponse:
        """转发消息到企业微信

        Args:
            request: 协议端发送请求

        Returns:
            PlatformSendResponse: 发送结果
        """
        if not self.api_client:
            return PlatformSendResponse(
                success=False,
                error_message="企业微信适配器未初始化",
            )

        try:
            # 解析聊天键获取目标
            _, channel_id = self.parse_chat_key(request.chat_key)

            # 判断是群聊还是私聊
            is_group = channel_id.startswith("group_")
            target_id = channel_id.split("_", 1)[1] if "_" in channel_id else channel_id

            to_user = ""
            to_party = ""
            to_tag = ""

            if is_group:
                # 群聊
                to_party = target_id
            else:
                # 私聊
                to_user = target_id

            message_ids = []

            # 处理消息段
            for segment in request.segments:
                if segment.type == PlatformSendSegmentType.TEXT:
                    if segment.content and segment.content.strip():
                        success = await self.api_client.send_text_message(
                            to_user=to_user,
                            to_party=to_party,
                            content=segment.content,
                        )
                        if success:
                            message_ids.append("text")

                elif segment.type == PlatformSendSegmentType.IMAGE:
                    if segment.file_path:
                        # 上传图片
                        media_id = await self.api_client.upload_media(
                            media_type="image",
                            file_path=segment.file_path,
                        )
                        if media_id:
                            success = await self.api_client.send_image_message(
                                to_user=to_user,
                                to_party=to_party,
                                media_id=media_id,
                            )
                            if success:
                                message_ids.append("image")

                elif segment.type == PlatformSendSegmentType.FILE:
                    if segment.file_path:
                        # 上传文件
                        media_id = await self.api_client.upload_media(
                            media_type="file",
                            file_path=segment.file_path,
                        )
                        if media_id:
                            success = await self.api_client.send_file_message(
                                to_user=to_user,
                                to_party=to_party,
                                media_id=media_id,
                            )
                            if success:
                                message_ids.append("file")

                elif segment.type == PlatformSendSegmentType.MARKDOWN:
                    if segment.content and segment.content.strip():
                        success = await self.api_client.send_markdown_message(
                            to_user=to_user,
                            to_party=to_party,
                            content=segment.content,
                        )
                        if success:
                            message_ids.append("markdown")

                elif segment.type == PlatformSendSegmentType.AT:
                    # 企业微信不支持直接的 @ 功能，转换为文本
                    if segment.at_info:
                        at_text = f"@{segment.at_info.nickname or segment.at_info.platform_user_id}"
                        success = await self.api_client.send_text_message(
                            to_user=to_user,
                            to_party=to_party,
                            content=at_text,
                        )
                        if success:
                            message_ids.append("at")

            if message_ids:
                return PlatformSendResponse(
                    success=True,
                    message_id=",".join(message_ids),
                )
            return PlatformSendResponse(
                success=True,
                message_id="empty",
            )

        except Exception as e:
            error_msg = f"企业微信消息发送失败: {e!s}"
            logger.error(error_msg)
            return PlatformSendResponse(success=False, error_message=error_msg)

    async def get_self_info(self) -> PlatformUser:
        """获取自身信息"""
        return PlatformUser(
            platform_name=self.key,
            user_id=self.config.AGENT_ID,
            user_name="企业微信应用",
            user_avatar="",
        )

    async def get_user_info(self, user_id: str, channel_id: str) -> PlatformUser:
        """获取用户信息

        Args:
            user_id: 用户 ID
            channel_id: 频道 ID

        Returns:
            PlatformUser: 用户信息
        """
        if not self.api_client:
            return PlatformUser(
                platform_name=self.key,
                user_id=user_id,
                user_name=user_id,
                user_avatar="",
            )

        try:
            user_info = await self.api_client.get_user_info(user_id)
            if user_info:
                return PlatformUser(
                    platform_name=self.key,
                    user_id=user_info.get("userid", user_id),
                    user_name=user_info.get("name", user_id),
                    user_avatar=user_info.get("avatar", ""),
                )
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")

        return PlatformUser(
            platform_name=self.key,
            user_id=user_id,
            user_name=user_id,
            user_avatar="",
        )

    async def get_channel_info(self, channel_id: str) -> PlatformChannel:
        """获取频道信息

        Args:
            channel_id: 频道 ID，如 group_wrkSFfCgAAxxxxxxxxx

        Returns:
            PlatformChannel: 频道信息
        """
        is_group = channel_id.startswith("group_")
        channel_type = ChatType.GROUP if is_group else ChatType.PRIVATE

        return PlatformChannel(
            platform_name=self.key,
            channel_id=channel_id,
            channel_name=channel_id,
            channel_type=channel_type,
        )

    def get_adapter_router(self) -> APIRouter:
        """获取适配器路由"""
        from .routers import router, set_adapter

        set_adapter(self)
        return router
