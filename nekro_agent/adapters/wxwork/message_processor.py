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
            # 企业微信自建应用使用 FromUserName，智能机器人使用 FromUserID
            from_user = message_data.get("FromUserName") or message_data.get("FromUserID", "")
            to_user = message_data.get("ToUserName") or message_data.get("ToUserID", "")
            chat_id = message_data.get("ChatID", "")

            logger.debug(
                f"处理企业微信消息: type={msg_type}, from={from_user}, to={to_user}, chat={chat_id}"
            )

            # 如果没有发送者信息（比如系统事件），则跳过处理
            if not from_user:
                logger.warning(f"消息没有发送者信息，跳过处理: msg_type={msg_type}, event={message_data.get('Event', '')}")
                return

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

    async def process_kf_message(self, message_data: dict, open_kfid: str) -> None:
        """处理企业微信客服消息

        Args:
            message_data: 从 sync_msg 接口获取的单条消息数据
            open_kfid: 客服账号 ID
        """
        try:
            # 提取消息信息
            msgid = message_data.get("msgid", "")
            external_userid = message_data.get("external_userid", "")
            send_time = message_data.get("send_time", 0)
            origin = message_data.get("origin", 0)  # 3-客户回复 4-系统推送
            msgtype = message_data.get("msgtype", "")

            logger.debug(f"处理客服消息: msgid={msgid}, customer={external_userid}, type={msgtype}")

            # 忽略系统消息
            if origin != 3:
                logger.debug(f"忽略非客户消息 (origin={origin})")
                return

            # 如果没有客户ID，跳过
            if not external_userid:
                logger.warning("客服消息缺少 external_userid")
                return

            # 构造频道和用户信息
            channel_id = f"wxwork-kf_{open_kfid}"
            channel_name = open_kfid

            platform_channel = PlatformChannel(
                platform_name=self.adapter.key,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_type=ChatType.PRIVATE,
            )

            platform_user = PlatformUser(
                platform_name=self.adapter.key,
                user_id=external_userid,
                user_name=external_userid,
                user_avatar="",
            )

            # 处理消息内容
            content_segments = await self._process_kf_message_content(message_data)
            content_text = self._extract_text_content(content_segments)

            # 构造平台消息
            platform_message = PlatformMessage(
                message_id=msgid,
                sender_id=external_userid,
                sender_name=external_userid,
                content_text=content_text,
                content_data=content_segments,
                is_self=False,
                is_tome=True,
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
            logger.error(f"处理企业微信客服消息异常: {e}")

    async def _process_kf_message_content(self, message_data: dict) -> List[ChatMessageSegment]:
        """处理客服消息内容，转换为标准消息段

        Args:
            message_data: 客服消息数据

        Returns:
            消息段列表
        """
        segments: List[ChatMessageSegment] = []
        msgtype = message_data.get("msgtype", "")

        try:
            # 文本消息
            if msgtype == "text":
                text_obj = message_data.get("text", {})
                content = text_obj.get("content", "")
                if content:
                    segment = ChatMessageSegment(
                        type=ChatMessageSegmentType.TEXT,
                        text=content,
                    )
                    segments.append(segment)

            # 图片消息
            elif msgtype == "image":
                image_obj = message_data.get("image", {})
                media_id = image_obj.get("media_id", "")
                if media_id:
                    image_bytes = await self._download_media(media_id)
                    if image_bytes:
                        chat_key = self.adapter.build_chat_key(message_data.get("external_userid", ""))
                        filename = f"image_{media_id[:20]}.jpg"
                        segment = await ChatMessageSegmentImage.create_from_bytes(
                            image_bytes,
                            from_chat_key=chat_key,
                            file_name=filename,
                        )
                        segments.append(segment)

            # 语音消息
            elif msgtype == "voice":
                voice_obj = message_data.get("voice", {})
                media_id = voice_obj.get("media_id", "")
                if media_id:
                    voice_bytes = await self._download_media(media_id)
                    if voice_bytes:
                        chat_key = self.adapter.build_chat_key(message_data.get("external_userid", ""))
                        filename = f"voice_{media_id[:20]}.mp3"
                        segment = await ChatMessageSegmentFile.create_from_bytes(
                            voice_bytes,
                            from_chat_key=chat_key,
                            file_name=filename,
                        )
                        segments.append(segment)

            # 视频消息
            elif msgtype == "video":
                video_obj = message_data.get("video", {})
                media_id = video_obj.get("media_id", "")
                if media_id:
                    video_bytes = await self._download_media(media_id)
                    if video_bytes:
                        chat_key = self.adapter.build_chat_key(message_data.get("external_userid", ""))
                        filename = f"video_{media_id[:20]}.mp4"
                        segment = await ChatMessageSegmentFile.create_from_bytes(
                            video_bytes,
                            from_chat_key=chat_key,
                            file_name=filename,
                        )
                        segments.append(segment)

            # 文件消息
            elif msgtype == "file":
                file_obj = message_data.get("file", {})
                media_id = file_obj.get("media_id", "")
                if media_id:
                    file_bytes = await self._download_media(media_id)
                    if file_bytes:
                        chat_key = self.adapter.build_chat_key(message_data.get("external_userid", ""))
                        filename = f"file_{media_id[:20]}"
                        segment = await ChatMessageSegmentFile.create_from_bytes(
                            file_bytes,
                            from_chat_key=chat_key,
                            file_name=filename,
                        )
                        segments.append(segment)

            # 位置消息
            elif msgtype == "location":
                location_obj = message_data.get("location", {})
                name = location_obj.get("name", "")
                address = location_obj.get("address", "")
                text = f"📍 {name}\n{address}" if name else "📍 位置消息"
                segment = ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=text,
                )
                segments.append(segment)

            else:
                # 未支持的消息类型
                segment = ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=f"[{msgtype}] 消息类型暂不支持",
                )
                segments.append(segment)

        except Exception as e:
            logger.error(f"处理客服消息内容异常: {e}")
            segment = ChatMessageSegment(
                type=ChatMessageSegmentType.TEXT,
                text="[错误] 消息处理失败",
            )
            segments.append(segment)

        return segments if segments else [
            ChatMessageSegment(
                type=ChatMessageSegmentType.TEXT,
                text="[空消息]",
            )
        ]

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
