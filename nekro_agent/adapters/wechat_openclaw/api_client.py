"""iLink Bot API HTTP 客户端

封装所有 iLink Bot API 端点的 HTTP 调用。
参考: OpenClaw src/api/api.ts
"""

import base64
import random
import struct
from typing import Any

import httpx

from nekro_agent.core.logger import get_sub_logger

from .config import WeChatOpenClawConfig
from .types import (
    BaseInfo,
    GetConfigReq,
    GetConfigResp,
    GetUpdatesReq,
    GetUpdatesResp,
    GetUploadUrlReq,
    GetUploadUrlResp,
    QRCodeResp,
    QRCodeStatusResp,
    SendMessageReq,
    SendMessageResp,
    SendTypingReq,
    SendTypingResp,
)

logger = get_sub_logger("adapter.wechat_openclaw.api")


def _random_uin() -> str:
    """生成随机 UIN（base64 编码的随机 uint32）"""
    rand_uint32 = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(rand_uint32).encode()).decode()


class ILinkApiClient:
    """iLink Bot API HTTP 客户端"""

    def __init__(self, config: WeChatOpenClawConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=40.0,  # 长轮询需要较长读取超时
                    write=10.0,
                    pool=10.0,
                ),
            )
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _headers(self, *, skip_auth: bool = False) -> dict[str, str]:
        """构建通用请求头"""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-WECHAT-UIN": _random_uin(),
        }
        if not skip_auth and self._config.BOT_TOKEN:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self._config.BOT_TOKEN}"
        return headers

    async def _post(
        self,
        endpoint: str,
        data: dict[str, Any],
        timeout: float | None = None,
        *,
        skip_auth: bool = False,
    ) -> dict[str, Any]:
        """统一 POST 请求"""
        url = f"{self._config.BASE_URL}/{endpoint}"
        client = self._get_client()

        request_timeout = httpx.Timeout(
            connect=10.0,
            read=timeout or 40.0,
            write=10.0,
            pool=10.0,
        ) if timeout else None

        resp = await client.post(
            url,
            json=data,
            headers=self._headers(skip_auth=skip_auth),
            timeout=request_timeout,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _get(
        self,
        endpoint: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
        *,
        skip_auth: bool = False,
    ) -> dict[str, Any]:
        """统一 GET 请求"""
        url = f"{self._config.BASE_URL}/{endpoint}"
        client = self._get_client()

        request_timeout = httpx.Timeout(
            connect=10.0,
            read=timeout or 40.0,
            write=10.0,
            pool=10.0,
        ) if timeout else None

        resp = await client.get(
            url,
            params=params,
            headers=self._headers(skip_auth=skip_auth),
            timeout=request_timeout,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # ======================== API 方法 ========================

    async def get_updates(self, account_id: str, sync_buf: str = "") -> GetUpdatesResp:
        """长轮询拉取新消息

        Args:
            account_id: 账号 ID
            sync_buf: 同步游标

        Returns:
            GetUpdatesResp: 消息响应，超时返回空消息列表
        """
        req = GetUpdatesReq(
            account_id=account_id,
            sync_buf=sync_buf,
        )
        try:
            data = await self._post(
                "ilink/bot/getupdates",
                req.model_dump(),
                timeout=float(self._config.POLL_TIMEOUT + 10),
            )
            return GetUpdatesResp.model_validate(data)
        except httpx.TimeoutException:
            # 长轮询超时是正常的，返回空响应
            return GetUpdatesResp(ret=0, msgs=[])
        except Exception:
            logger.exception("getUpdates 请求失败")
            raise

    async def send_message(self, req: SendMessageReq) -> SendMessageResp:
        """发送消息

        Args:
            req: 发送消息请求

        Returns:
            SendMessageResp: 发送结果
        """
        data = await self._post(
            "ilink/bot/sendmessage",
            req.model_dump(),
            timeout=15.0,
        )
        return SendMessageResp.model_validate(data)

    async def get_upload_url(self, req: GetUploadUrlReq) -> GetUploadUrlResp:
        """获取 CDN 上传预签名 URL

        Args:
            req: 上传 URL 请求

        Returns:
            GetUploadUrlResp: 包含上传 URL 和 file_id/file_key
        """
        data = await self._post(
            "ilink/bot/getuploadurl",
            req.model_dump(),
            timeout=15.0,
        )
        return GetUploadUrlResp.model_validate(data)

    async def get_config(self, req: GetConfigReq) -> GetConfigResp:
        """获取 Bot 配置（含 typing_ticket）

        Args:
            req: 配置请求

        Returns:
            GetConfigResp: 配置响应
        """
        data = await self._post(
            "ilink/bot/getconfig",
            req.model_dump(),
            timeout=10.0,
        )
        return GetConfigResp.model_validate(data)

    async def send_typing(self, req: SendTypingReq) -> SendTypingResp:
        """发送/取消"正在输入"指示器

        Args:
            req: 打字状态请求

        Returns:
            SendTypingResp: 响应
        """
        data = await self._post(
            "ilink/bot/sendtyping",
            req.model_dump(),
            timeout=10.0,
        )
        return SendTypingResp.model_validate(data)

    async def get_qrcode(self, bot_type: str = "3") -> QRCodeResp:
        """获取登录二维码

        Args:
            bot_type: Bot 类型，默认 "3"

        Returns:
            QRCodeResp: 包含二维码 URL 和 session_key
        """
        data = await self._get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": bot_type},
            timeout=10.0,
            skip_auth=True,
        )
        logger.info(f"get_qrcode 原始响应: {data}")
        return QRCodeResp.model_validate(data)

    async def poll_qr_status(self, qrcode: str) -> QRCodeStatusResp:
        """轮询二维码扫描状态

        Args:
            qrcode: 二维码标识

        Returns:
            QRCodeStatusResp: 扫码状态
        """
        try:
            data = await self._get(
                "ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode},
                timeout=40.0,
                skip_auth=True,
            )
            logger.info(f"poll_qr_status 原始响应: {data}")
            return QRCodeStatusResp.model_validate(data)
        except httpx.TimeoutException:
            return QRCodeStatusResp(ret=0, status=0)
