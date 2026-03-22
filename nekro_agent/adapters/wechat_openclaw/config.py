"""WeChat OpenClaw 适配器配置"""

from pydantic import Field

from nekro_agent.adapters.interface.base import BaseAdapterConfig
from nekro_agent.core.core_utils import ExtraField


class WeChatOpenClawConfig(BaseAdapterConfig):
    """WeChat OpenClaw 适配器配置类"""

    BASE_URL: str = Field(
        default="https://ilinkai.weixin.qq.com",
        title="iLink API 地址",
        description="iLink Bot API 的基础 URL",
    )

    CDN_BASE_URL: str = Field(
        default="https://cdn.weixin.qq.com",
        title="CDN 地址",
        description="微信 CDN 媒体服务的基础 URL",
    )

    ACCOUNT_ID: str = Field(
        default="",
        title="账号 ID",
        description="iLink Bot 的 account_id，登录后自动获取",
        json_schema_extra=ExtraField(required=True).model_dump(),
    )

    BOT_TOKEN: str = Field(
        default="",
        title="Bot Token",
        description="iLink Bot API 的认证 Token，登录后自动获取",
        json_schema_extra=ExtraField(is_secret=True, required=True).model_dump(),
    )

    LINKED_USER_ID: str = Field(
        default="",
        title="关联用户 ID",
        description="微信登录账号的 user_id（文件传输助手等场景使用）",
    )

    POLL_TIMEOUT: int = Field(
        default=35,
        title="长轮询超时(秒)",
        description="getUpdates 长轮询超时时间",
    )

    TYPING_INDICATOR_ENABLED: bool = Field(
        default=True,
        title="启用打字指示器",
        description="发送消息前是否显示'正在输入'状态",
    )

    TYPING_INTERVAL: int = Field(
        default=5,
        title="打字指示器间隔(秒)",
        description="打字指示器发送间隔",
    )

    SESSION_PROCESSING_WITH_EMOJI: bool = Field(
        default=False,
        title="显示处理中表情反馈",
        description="微信不支持此功能，保持关闭",
    )

    @property
    def is_configured(self) -> bool:
        """检查配置是否完整"""
        return bool(self.BOT_TOKEN and self.ACCOUNT_ID)
