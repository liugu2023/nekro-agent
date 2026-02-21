from pydantic import Field

from nekro_agent.adapters.interface.base import BaseAdapterConfig


class WxWorkConfig(BaseAdapterConfig):
    """企业微信自建应用适配器配置"""

    # 自建应用配置（必需）
    CORP_ID: str = Field(
        default="",
        title="Corp ID",
        description="企业微信的企业 ID，可在企业微信管理后台『我的企业』->『企业信息』查看",
    )

    CORP_SECRET: str = Field(
        default="",
        title="Corp Secret",
        description="自建应用的应用 Secret，可在企业微信管理后台『应用与集成』->『应用』->『自建应用』查看",
    )

    AGENT_ID: str = Field(
        default="",
        title="Agent ID",
        description="自建应用的应用 ID，可在企业微信管理后台『应用与集成』->『应用』->『自建应用』查看",
    )

    # 回调验证配置（必需）
    TOKEN: str = Field(
        default="",
        title="Token",
        description="回调 URL 配置中的 Token，用于验证消息来源",
    )

    ENCODING_AES_KEY: str = Field(
        default="",
        title="EncodingAESKey",
        description="回调 URL 配置中的 EncodingAESKey（43位随机字符串），用于消息加解密",
    )

    @property
    def is_configured(self) -> bool:
        """检查是否配置完整"""
        return bool(
            self.CORP_ID
            and self.CORP_SECRET
            and self.AGENT_ID
            and self.TOKEN
            and self.ENCODING_AES_KEY
        )

