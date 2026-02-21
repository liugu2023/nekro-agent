"""
企业微信消息处理器

用于解析企业微信回调消息、构造平台消息对象、处理媒体文件等
"""

import asyncio
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Optional, List

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformChannel,
    PlatformMessage,
    PlatformUser,
    ChatType,
)
from nekro_agent.schemas.chat_message import (
    ChatMessageSegment,
    ChatMessageSegmentType,
    ChatMessageSegmentImage,
    ChatMessageSegmentFile,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.tools.common_util import download_file_from_bytes

if TYPE_CHECKING:
    from nekro_agent.adapters.wxwork.adapter import WxWorkAdapter

logger = get_sub_logger("adapter.wxwork")


class WxWorkMessageProcessor:
    """企业微信消息处理器"""

    def __init__(self, adapter: "WxWorkAdapter"):
        self.adapter = adapter

    async def process_message(
        self,
        message_data: dict,
    ) -> None:
        """处理企业微信消息

        Args:
            message_data: 解密后的消息数据（JSON 格式）
        """
        try:
            # 提取基本信息
            msg_type = message_data.get("MsgType", "")
            from_user = message_data.get("FromUserID", "")
            to_user = message_data.get("ToUserID", "")
            chat_id = message_data.get("ChatID", "")

            logger.debug(
                f"处理企业微信消息: type={msg_type}, from={from_user}, to={to_user}, chat={chat_id}"
            )

            # 判断是群聊还是私聊
            is_group = bool(chat_id)
            is_tome = self._is_to_agent(to_user)

            # 获取频道信息
            if is_group:
                channel_id = f"wxwork-group_{chat_id}"
                channel_name = message_data.get("ChatTitle", chat_id)
                channel_type = ChatType.GROUP
            else:
                channel_id = f"wxwork-private_{from_user}"
                channel_name = from_user
                channel_type = ChatType.PRIVATE

            platform_channel = PlatformChannel(
                platform_name=self.adapter.key,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_type=channel_type,
            )

            # 获取用户信息
            user_info = await self.adapter.api_client.get_user_info(from_user)
            user_name = user_info.get("name") if user_info else from_user
            user_avatar = user_info.get("avatar", "") if user_info else ""

            platform_user = PlatformUser(
                platform_name=self.adapter.key,
                user_id=from_user,
                user_name=user_name,
                user_avatar=user_avatar,
            )

            # 处理消息内容
            content_segments = await self._process_message_content(message_data)
            content_text = self._extract_text_content(content_segments)

            # 构造平台消息
            msg_id = message_data.get("MsgID", "")
            platform_message = PlatformMessage(
                message_id=str(msg_id),
                sender_id=from_user,
                sender_name=user_name,
                content_text=content_text,
                content_data=content_segments,
                is_self=False,
                is_tome=is_tome,
            )

            # 收集消息
            from nekro_agent.adapters.interface.collector import collect_message

            await collect_message(
                self.adapter,
                platform_channel,
                platform_user,
                platform_message,
            )

        except Exception as e:
            logger.error(f"处理企业微信消息异常: {e}")

    async def _process_message_content(self, message_data: dict) -> List[ChatMessageSegment]:
        """处理消息内容，转换为标准消息段

        Args:
            message_data: 解密后的消息数据

        Returns:
            消息段列表
        """
        segments: List[ChatMessageSegment] = []
        msg_type = message_data.get("MsgType", "")

        # 处理文本消息
        if msg_type == "text":
            text = message_data.get("Content", "")
            if text:
                segment = ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=text,
                )
                segments.append(segment)

        # 处理图片消息
        elif msg_type == "image":
            media_id = message_data.get("MediaID", "")
            if media_id:
                image_bytes = await self._download_media(media_id)
                if image_bytes:
                    chat_key = self.adapter.build_chat_key(
                        message_data.get("ChatID") or message_data.get("FromUserID")
                    )
                    filename = f"image_{media_id[:20]}.jpg"
                    segment = await ChatMessageSegmentImage.create_from_bytes(
                        image_bytes,
                        from_chat_key=chat_key,
                        file_name=filename,
                    )
                    segments.append(segment)

        # 处理语音消息
        elif msg_type == "voice":
            media_id = message_data.get("MediaID", "")
            if media_id:
                voice_bytes = await self._download_media(media_id)
                if voice_bytes:
                    chat_key = self.adapter.build_chat_key(
                        message_data.get("ChatID") or message_data.get("FromUserID")
                    )
                    filename = f"voice_{media_id[:20]}.mp3"
                    segment = await ChatMessageSegmentFile.create_from_bytes(
                        voice_bytes,
                        from_chat_key=chat_key,
                        file_name=filename,
                    )
                    segments.append(segment)

        # 处理视频消息
        elif msg_type == "video":
            media_id = message_data.get("MediaID", "")
            if media_id:
                video_bytes = await self._download_media(media_id)
                if video_bytes:
                    chat_key = self.adapter.build_chat_key(
                        message_data.get("ChatID") or message_data.get("FromUserID")
                    )
                    filename = f"video_{media_id[:20]}.mp4"
                    segment = await ChatMessageSegmentFile.create_from_bytes(
                        video_bytes,
                        from_chat_key=chat_key,
                        file_name=filename,
                    )
                    segments.append(segment)

        # 处理文件消息
        elif msg_type == "file":
            media_id = message_data.get("MediaID", "")
            file_name = message_data.get("FileName", f"file_{media_id[:20]}")
            if media_id:
                file_bytes = await self._download_media(media_id)
                if file_bytes:
                    chat_key = self.adapter.build_chat_key(
                        message_data.get("ChatID") or message_data.get("FromUserID")
                    )
                    segment = await ChatMessageSegmentFile.create_from_bytes(
                        file_bytes,
                        from_chat_key=chat_key,
                        file_name=file_name,
                    )
                    segments.append(segment)

        # 处理链接消息
        elif msg_type == "link":
            title = message_data.get("Title", "")
            url = message_data.get("Url", "")
            description = message_data.get("Desc", "")
            text = f"{title}\n{description}\n{url}" if description else f"{title}\n{url}"
            if text:
                segment = ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=text,
                )
                segments.append(segment)

        # 如果没有识别的消息类型，至少返回一个文本段说明
        if not segments:
            segment = ChatMessageSegment(
                type=ChatMessageSegmentType.TEXT,
                text=f"[{msg_type}] 消息类型暂不支持",
            )
            segments.append(segment)

        return segments

    async def _download_media(self, media_id: str) -> Optional[bytes]:
        """下载企业微信媒体文件

        Args:
            media_id: 媒体 ID

        Returns:
            文件字节内容，失败返回 None
        """
        try:
            access_token = await self.adapter.api_client.get_access_token()

            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://qyapi.weixin.qq.com/cgi-bin/media/get",
                    params={
                        "access_token": access_token,
                        "media_id": media_id,
                    },
                    timeout=30.0,
                )
                response.raise_for_status()

            return response.content

        except Exception as e:
            logger.error(f"下载企业微信媒体失败 {media_id}: {e}")
            return None

    def _is_to_agent(self, to_user: str) -> bool:
        """检查消息是否发送给机器人

        Args:
            to_user: 消息接收者（通常是应用的 agent_id）

        Returns:
            bool: 是否发送给机器人
        """
        # 消息已经被解密并路由到了应用，说明就是发给机器人的
        return True

    def _extract_text_content(self, segments: List[ChatMessageSegment]) -> str:
        """提取文本内容

        Args:
            segments: 消息段列表

        Returns:
            纯文本内容
        """
        text_parts = []
        for segment in segments:
            if segment.type == ChatMessageSegmentType.TEXT:
                text_parts.append(segment.text)
        return "".join(text_parts)
