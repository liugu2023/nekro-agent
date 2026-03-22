"""FastAPI 路由 - 扫码登录 + 状态查询

参考: OpenClaw src/auth/login-qr.ts
"""

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger

if TYPE_CHECKING:
    pass

logger = get_sub_logger("adapter.wechat_openclaw.routers")

router = APIRouter()


# ========================================================================================
# |                              请求/响应模型                                              |
# ========================================================================================


class QRLoginStartResponse(BaseModel):
    """扫码登录启动响应"""

    success: bool = True
    qrcode_url: str = ""
    session_key: str = ""
    message: str = ""


class QRLoginWaitResponse(BaseModel):
    """扫码登录等待响应"""

    success: bool = False
    status: int = 0  # 0=等待, 1=已扫码, 2=已确认, 3=过期
    message: str = ""
    account_id: str = ""
    bot_token: str = ""


class StatusResponse(BaseModel):
    """状态查询响应"""

    connected: bool = False
    account_id: str = ""
    monitor_running: bool = False
    session_paused: bool = False
    session_remaining_seconds: int = 0


class LogoutResponse(BaseModel):
    """登出响应"""

    success: bool = True
    message: str = ""


# ========================================================================================
# |                              工具函数                                                   |
# ========================================================================================


def _get_adapter() -> Any:
    """获取适配器实例"""
    from nekro_agent.adapters import loaded_adapters

    adapter = loaded_adapters.get("wechat_openclaw")
    if adapter is None:
        raise RuntimeError("WeChat OpenClaw 适配器未加载")
    return adapter


# ========================================================================================
# |                              路由                                                      |
# ========================================================================================


@router.post("/login/qr/start", response_model=QRLoginStartResponse)
async def login_qr_start() -> QRLoginStartResponse:
    """启动扫码登录 - 获取二维码"""
    try:
        adapter = _get_adapter()
        resp = await adapter.api_client.get_qrcode(bot_type="3")

        if resp.ret != 0:
            return QRLoginStartResponse(
                success=False,
                message=f"获取二维码失败: {resp.errmsg}",
            )

        return QRLoginStartResponse(
            success=True,
            qrcode_url=resp.qrcode_img_content,
            session_key=resp.qrcode,
        )
    except Exception as e:
        logger.exception("启动扫码登录失败")
        return QRLoginStartResponse(
            success=False,
            message=f"启动扫码登录失败: {e!s}",
        )


@router.post("/login/qr/wait", response_model=QRLoginWaitResponse)
async def login_qr_wait(session_key: str = "") -> QRLoginWaitResponse:
    """等待扫码确认

    长轮询等待扫码确认，最长 480s，QR 过期自动刷新最多 3 次。
    """
    try:
        adapter = _get_adapter()

        qrcode = session_key
        max_refreshes = 3
        refresh_count = 0
        max_polls = 16  # 每次 30s 超时，16 次 ≈ 480s

        for _poll in range(max_polls):
            resp = await adapter.api_client.poll_qr_status(qrcode)

            if resp.status == 2:
                # 扫码确认成功 - 保存凭证
                if resp.bot_token and resp.account_id:
                    adapter.config.BOT_TOKEN = resp.bot_token
                    adapter.config.ACCOUNT_ID = resp.account_id
                    adapter.config.dump_config(adapter.config_path)

                    # 重新初始化 API 客户端并启动 monitor
                    await adapter._reinit_after_login()

                return QRLoginWaitResponse(
                    success=True,
                    status=2,
                    message="登录成功",
                    account_id=resp.account_id,
                    bot_token=resp.bot_token,
                )

            if resp.status == 3:
                # 二维码过期 - 尝试刷新
                refresh_count += 1
                if refresh_count > max_refreshes:
                    return QRLoginWaitResponse(
                        success=False,
                        status=3,
                        message="二维码已过期，已达最大刷新次数",
                    )
                # 获取新二维码
                new_qr = await adapter.api_client.get_qrcode(bot_type="3")
                if new_qr.ret != 0:
                    return QRLoginWaitResponse(
                        success=False,
                        status=3,
                        message="刷新二维码失败",
                    )
                qrcode = new_qr.qrcode
                continue

            if resp.status == 1:
                return QRLoginWaitResponse(
                    success=False,
                    status=1,
                    message="已扫码，等待确认",
                )

            # status == 0，继续等待
            await asyncio.sleep(1)

        return QRLoginWaitResponse(
            success=False,
            status=0,
            message="等待超时",
        )

    except Exception as e:
        logger.exception("等待扫码确认失败")
        return QRLoginWaitResponse(
            success=False,
            message=f"等待扫码确认失败: {e!s}",
        )


@router.post("/logout", response_model=LogoutResponse)
async def logout() -> LogoutResponse:
    """登出 - 停止 monitor 并清除凭证"""
    try:
        adapter = _get_adapter()

        # 停止 monitor
        if adapter.monitor and adapter.monitor.is_running:
            await adapter.monitor.stop()

        if adapter._monitor_task and not adapter._monitor_task.done():
            adapter._monitor_task.cancel()

        # 清除凭证
        adapter.config.BOT_TOKEN = ""
        adapter.config.ACCOUNT_ID = ""
        adapter.config.dump_config(adapter.config_path)

        return LogoutResponse(success=True, message="已登出")

    except Exception as e:
        logger.exception("登出失败")
        return LogoutResponse(success=False, message=f"登出失败: {e!s}")


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    """获取连接状态"""
    try:
        adapter = _get_adapter()
        config = adapter.config

        is_connected = bool(config.BOT_TOKEN and config.ACCOUNT_ID)
        monitor_running = adapter.monitor is not None and adapter.monitor.is_running
        session_paused = adapter.session_guard.is_paused(config.ACCOUNT_ID) if config.ACCOUNT_ID else False
        remaining = adapter.session_guard.remaining_seconds(config.ACCOUNT_ID) if config.ACCOUNT_ID else 0

        return StatusResponse(
            connected=is_connected,
            account_id=config.ACCOUNT_ID,
            monitor_running=monitor_running,
            session_paused=session_paused,
            session_remaining_seconds=remaining,
        )

    except Exception:
        logger.exception("获取状态失败")
        return StatusResponse()
