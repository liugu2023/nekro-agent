"""消息发送模块

负责将 NA 消息段转换为 iLink Bot API 格式并发送。
包括 Markdown 转纯文本、typing 指示器、CDN 媒体上传。
参考: OpenClaw src/messaging/send.ts + src/messaging/send-media.ts
"""

import asyncio
import re
from pathlib import Path
from typing import Any

from nekro_agent.core.logger import get_sub_logger

from . import cdn
from .api_client import ILinkApiClient
from .config import WeChatOpenClawConfig
from .config_cache import ConfigCacheManager
from .context_token import ContextTokenStore
from .types import (
    BaseInfo,
    GetConfigReq,
    MessageItemType,
    SendMessageReq,
    SendTypingReq,
    TypingStatus,
    UploadMediaType,
)

logger = get_sub_logger("adapter.wechat_openclaw.sender")


# ========================================================================================
# |                              Markdown → 纯文本                                        |
# ========================================================================================


def markdown_to_plain(text: str) -> str:
    """将 Markdown 转换为纯文本

    - 代码块保留内容
    - 去链接语法
    - 去表格分隔
    - 去粗体斜体
    """
    # 代码块: 保留内容，去除 ``` 标记
    text = re.sub(r"```\w*\n(.*?)```", r"\1", text, flags=re.DOTALL)

    # 行内代码: 保留内容
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 链接: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 图片: ![alt](url) → [图片]
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[\1]" if r"\1" else "[图片]", text)

    # 粗体斜体
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"___(.+?)___", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)

    # 删除线
    text = re.sub(r"~~(.+?)~~", r"\1", text)

    # 标题标记
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 表格分隔行
    text = re.sub(r"^\|?[\s\-:|]+\|?$", "", text, flags=re.MULTILINE)

    # 表格单元格
    text = re.sub(r"\|", " ", text)

    # 引用标记
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # 水平线
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)

    # 无序列表标记 → 保留缩进
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1", text, flags=re.MULTILINE)

    # 有序列表标记
    text = re.sub(r"^(\s*)\d+\.\s+", r"\1", text, flags=re.MULTILINE)

    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ========================================================================================
# |                              Typing 指示器                                             |
# ========================================================================================


class TypingManager:
    """打字指示器管理器"""

    def __init__(
        self,
        api_client: ILinkApiClient,
        config: WeChatOpenClawConfig,
        context_store: ContextTokenStore,
        config_cache: ConfigCacheManager,
    ) -> None:
        self._api = api_client
        self._config = config
        self._context_store = context_store
        self._config_cache = config_cache
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}

    async def _get_typing_ticket(self, account_id: str, user_id: str) -> str:
        """获取 typing_ticket（带缓存）"""
        # 先检查缓存
        cached = self._config_cache.get(account_id, user_id)
        if cached:
            return cached

        # 检查是否可以重试
        if not self._config_cache.should_retry(account_id, user_id):
            return ""

        context_token = self._context_store.get(account_id, user_id)
        if not context_token:
            return ""

        try:
            req = GetConfigReq(
                account_id=account_id,
                to_user_id=user_id,
                context_token=context_token,
            )
            resp = await self._api.get_config(req)
            if resp.ret == 0 and resp.typing_ticket:
                self._config_cache.set(account_id, user_id, resp.typing_ticket)
                return resp.typing_ticket
            self._config_cache.record_failure(account_id, user_id)
            return ""
        except Exception:
            self._config_cache.record_failure(account_id, user_id)
            logger.debug("获取 typing_ticket 失败")
            return ""

    async def start_typing_loop(self, account_id: str, user_id: str) -> None:
        """开始循环发送打字指示器"""
        if not self._config.TYPING_INDICATOR_ENABLED:
            return

        # 取消已有的 typing 任务
        self.cancel_typing(user_id)

        async def _loop() -> None:
            try:
                while True:
                    typing_ticket = await self._get_typing_ticket(account_id, user_id)
                    if not typing_ticket:
                        return

                    context_token = self._context_store.get(account_id, user_id)
                    req = SendTypingReq(
                        account_id=account_id,
                        to_user_id=user_id,
                        typing_status=TypingStatus.TYPING,
                        typing_ticket=typing_ticket,
                        context_token=context_token,
                    )
                    try:
                        await self._api.send_typing(req)
                    except Exception:
                        logger.debug("发送 typing 失败")
                        return

                    await asyncio.sleep(self._config.TYPING_INTERVAL)
            except asyncio.CancelledError:
                pass

        self._typing_tasks[user_id] = asyncio.create_task(_loop())

    def cancel_typing(self, user_id: str) -> None:
        """取消打字指示器"""
        task = self._typing_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()


# ========================================================================================
# |                              消息发送                                                   |
# ========================================================================================


async def send_text(
    api_client: ILinkApiClient,
    account_id: str,
    to_user_id: str,
    context_token: str,
    text: str,
) -> None:
    """发送文本消息

    Args:
        api_client: API 客户端
        account_id: 账号 ID
        to_user_id: 接收者 user_id
        context_token: 会话 token
        text: 文本内容（将做 Markdown 转换）
    """
    plain_text = markdown_to_plain(text)
    if not plain_text:
        return

    req = SendMessageReq(
        account_id=account_id,
        to_user_id=to_user_id,
        context_token=context_token,
        item_list=[{
            "item_type": MessageItemType.TEXT,
            "text_item": {"content": plain_text},
        }],
    )
    resp = await api_client.send_message(req)
    if resp.ret != 0:
        logger.error(f"发送文本消息失败: {resp.errmsg}")


async def send_image_message(
    api_client: ILinkApiClient,
    account_id: str,
    to_user_id: str,
    context_token: str,
    file_path: str,
    cdn_base_url: str,
) -> None:
    """发送图片消息

    Args:
        api_client: API 客户端
        account_id: 账号 ID
        to_user_id: 接收者 user_id
        context_token: 会话 token
        file_path: 本地图片路径
        cdn_base_url: CDN 基础 URL
    """
    file_data = Path(file_path).read_bytes()

    upload_info = await cdn.upload_media(
        api_client=api_client,
        account_id=account_id,
        file_data=file_data,
        media_type=UploadMediaType.IMAGE,
        cdn_base_url=cdn_base_url,
    )

    req = SendMessageReq(
        account_id=account_id,
        to_user_id=to_user_id,
        context_token=context_token,
        item_list=[{
            "item_type": MessageItemType.IMAGE,
            "image_item": {
                "cdn": {
                    "file_url": upload_info.file_url,
                    "file_key": upload_info.file_key,
                    "file_size": upload_info.file_size,
                    "aes_key": upload_info.aes_key,
                    "file_md5": upload_info.file_md5,
                },
            },
        }],
    )
    resp = await api_client.send_message(req)
    if resp.ret != 0:
        logger.error(f"发送图片消息失败: {resp.errmsg}")


async def send_video_message(
    api_client: ILinkApiClient,
    account_id: str,
    to_user_id: str,
    context_token: str,
    file_path: str,
    cdn_base_url: str,
) -> None:
    """发送视频消息"""
    file_data = Path(file_path).read_bytes()

    upload_info = await cdn.upload_media(
        api_client=api_client,
        account_id=account_id,
        file_data=file_data,
        media_type=UploadMediaType.VIDEO,
        cdn_base_url=cdn_base_url,
    )

    req = SendMessageReq(
        account_id=account_id,
        to_user_id=to_user_id,
        context_token=context_token,
        item_list=[{
            "item_type": MessageItemType.VIDEO,
            "video_item": {
                "cdn": {
                    "file_url": upload_info.file_url,
                    "file_key": upload_info.file_key,
                    "file_size": upload_info.file_size,
                    "aes_key": upload_info.aes_key,
                    "file_md5": upload_info.file_md5,
                },
            },
        }],
    )
    resp = await api_client.send_message(req)
    if resp.ret != 0:
        logger.error(f"发送视频消息失败: {resp.errmsg}")


async def send_file_message(
    api_client: ILinkApiClient,
    account_id: str,
    to_user_id: str,
    context_token: str,
    file_path: str,
    cdn_base_url: str,
) -> None:
    """发送文件消息"""
    p = Path(file_path)
    file_data = p.read_bytes()
    file_name = p.name

    upload_info = await cdn.upload_media(
        api_client=api_client,
        account_id=account_id,
        file_data=file_data,
        media_type=UploadMediaType.FILE,
        cdn_base_url=cdn_base_url,
    )

    req = SendMessageReq(
        account_id=account_id,
        to_user_id=to_user_id,
        context_token=context_token,
        item_list=[{
            "item_type": MessageItemType.FILE,
            "file_item": {
                "cdn": {
                    "file_url": upload_info.file_url,
                    "file_key": upload_info.file_key,
                    "file_size": upload_info.file_size,
                    "aes_key": upload_info.aes_key,
                    "file_md5": upload_info.file_md5,
                },
                "file_name": file_name,
            },
        }],
    )
    resp = await api_client.send_message(req)
    if resp.ret != 0:
        logger.error(f"发送文件消息失败: {resp.errmsg}")
