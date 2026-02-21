"""
企业微信 API 客户端

用于调用企业微信服务端 API 进行消息发送、素材上传、用户查询等操作
"""

import asyncio
import time
from typing import Optional, Dict, Any

import httpx

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("adapter.wxwork")


class WxWorkApiClient:
    """企业微信 API 客户端"""

    BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, corp_id: str, corp_secret: str, agent_id: str):
        """初始化 API 客户端

        Args:
            corp_id: 企业 ID
            corp_secret: 应用 Secret
            agent_id: 应用 ID
        """
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.agent_id = agent_id

        # Token 缓存
        self._access_token: Optional[str] = None
        self._token_expire_time: float = 0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """获取/缓存 access_token

        access_token 有效期 7200 秒

        Returns:
            str: access_token

        Raises:
            Exception: 获取 token 失败
        """
        # 检查缓存是否有效
        if self._access_token and time.time() < self._token_expire_time:
            return self._access_token

        # 使用锁防止并发请求
        async with self._lock:
            # 再次检查缓存（防止其他任务已刷新）
            if self._access_token and time.time() < self._token_expire_time:
                return self._access_token

            # 获取新 token
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"{self.BASE_URL}/gettoken",
                        params={
                            "corpid": self.corp_id,
                            "corpsecret": self.corp_secret,
                        },
                        timeout=10.0,
                    )
                    response.raise_for_status()

                data = response.json()
                if data.get("errcode") != 0:
                    raise Exception(f"获取 token 失败: {data.get('errmsg')}")

                self._access_token = data["access_token"]
                # 缓存 7000 秒（留 200 秒余量）
                self._token_expire_time = time.time() + 7000

                logger.debug(f"企业微信 token 已更新")
                return self._access_token

            except Exception as e:
                logger.error(f"获取企业微信 token 失败: {e}")
                raise

    async def send_text_message(
        self,
        to_user: str = "",
        to_party: str = "",
        to_tag: str = "",
        content: str = "",
    ) -> bool:
        """发送文本消息

        Args:
            to_user: 成员 ID 列表（多个用|分隔）
            to_party: 部门 ID 列表（多个用|分隔）
            to_tag: 标签 ID 列表（多个用|分隔）
            content: 消息内容

        Returns:
            bool: 发送成功
        """
        return await self._send_message(
            {
                "msgtype": "text",
                "text": {"content": content},
            },
            to_user=to_user,
            to_party=to_party,
            to_tag=to_tag,
        )

    async def send_markdown_message(
        self,
        to_user: str = "",
        to_party: str = "",
        to_tag: str = "",
        content: str = "",
    ) -> bool:
        """发送 Markdown 消息

        Args:
            to_user: 成员 ID 列表（多个用|分隔）
            to_party: 部门 ID 列表（多个用|分隔）
            to_tag: 标签 ID 列表（多个用|分隔）
            content: Markdown 内容

        Returns:
            bool: 发送成功
        """
        return await self._send_message(
            {
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
            to_user=to_user,
            to_party=to_party,
            to_tag=to_tag,
        )

    async def send_image_message(
        self,
        to_user: str = "",
        to_party: str = "",
        to_tag: str = "",
        media_id: str = "",
    ) -> bool:
        """发送图片消息

        Args:
            to_user: 成员 ID 列表（多个用|分隔）
            to_party: 部门 ID 列表（多个用|分隔）
            to_tag: 标签 ID 列表（多个用|分隔）
            media_id: 图片的 media_id

        Returns:
            bool: 发送成功
        """
        return await self._send_message(
            {
                "msgtype": "image",
                "image": {"media_id": media_id},
            },
            to_user=to_user,
            to_party=to_party,
            to_tag=to_tag,
        )

    async def send_file_message(
        self,
        to_user: str = "",
        to_party: str = "",
        to_tag: str = "",
        media_id: str = "",
    ) -> bool:
        """发送文件消息

        Args:
            to_user: 成员 ID 列表（多个用|分隔）
            to_party: 部门 ID 列表（多个用|分隔）
            to_tag: 标签 ID 列表（多个用|分隔）
            media_id: 文件的 media_id

        Returns:
            bool: 发送成功
        """
        return await self._send_message(
            {
                "msgtype": "file",
                "file": {"media_id": media_id},
            },
            to_user=to_user,
            to_party=to_party,
            to_tag=to_tag,
        )

    async def upload_media(
        self,
        media_type: str,
        file_path: str,
    ) -> Optional[str]:
        """上传临时素材

        Args:
            media_type: 媒体类型（image/voice/video/file）
            file_path: 文件路径

        Returns:
            str: media_id，如果上传失败返回 None
        """
        try:
            access_token = await self.get_access_token()

            with open(file_path, "rb") as f:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.BASE_URL}/media/upload",
                        params={
                            "access_token": access_token,
                            "type": media_type,
                        },
                        files={"media": f},
                        timeout=30.0,
                    )
                    response.raise_for_status()

            data = response.json()
            if data.get("errcode") != 0:
                logger.error(f"上传媒体失败: {data.get('errmsg')}")
                return None

            return data.get("media_id")

        except Exception as e:
            logger.error(f"上传媒体异常: {e}")
            return None

    async def get_user_info(self, userid: str) -> Optional[Dict[str, Any]]:
        """获取成员信息

        Args:
            userid: 成员 ID

        Returns:
            dict: 用户信息，包含 name、avatar 等，失败返回 None
        """
        try:
            access_token = await self.get_access_token()

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/user/get",
                    params={
                        "access_token": access_token,
                        "userid": userid,
                    },
                    timeout=10.0,
                )
                response.raise_for_status()

            data = response.json()
            if data.get("errcode") != 0:
                logger.error(f"获取用户信息失败: {data.get('errmsg')}")
                return None

            return {
                "userid": data.get("userid"),
                "name": data.get("name"),
                "avatar": data.get("avatar"),
                "mobile": data.get("mobile"),
                "email": data.get("email"),
            }

        except Exception as e:
            logger.error(f"获取用户信息异常: {e}")
            return None

    async def _send_message(
        self,
        message: dict,
        to_user: str = "",
        to_party: str = "",
        to_tag: str = "",
    ) -> bool:
        """发送消息的通用方法

        Args:
            message: 消息对象（包含 msgtype 和具体内容）
            to_user: 成员 ID 列表（多个用|分隔）
            to_party: 部门 ID 列表（多个用|分隔）
            to_tag: 标签 ID 列表（多个用|分隔）

        Returns:
            bool: 发送成功
        """
        try:
            access_token = await self.get_access_token()

            # 构建请求体
            payload = {
                "touser": to_user,
                "toparty": to_party,
                "totag": to_tag,
                "agentid": int(self.agent_id),
                **message,
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/message/send",
                    params={"access_token": access_token},
                    json=payload,
                    timeout=10.0,
                )
                response.raise_for_status()

            data = response.json()
            if data.get("errcode") != 0:
                logger.error(f"发送消息失败: {data.get('errmsg')}")
                return False

            return True

        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return False
