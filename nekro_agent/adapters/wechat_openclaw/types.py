"""WeChat OpenClaw 协议类型定义

基于 OpenClaw @tencent-weixin/openclaw-weixin v1.0.2 的 iLink Bot API 类型。
"""

from enum import IntEnum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ========================================================================================
# |                              枚举类型                                                  |
# ========================================================================================


class MessageItemType(IntEnum):
    """消息项类型"""

    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


class MessageType(IntEnum):
    """消息类型"""

    NORMAL = 0
    SYSTEM = 1


class MessageState(IntEnum):
    """消息状态"""

    NORMAL = 0
    RECALLED = 1


class UploadMediaType(IntEnum):
    """上传媒体类型"""

    IMAGE = 1
    VIDEO = 2
    FILE = 3


class TypingStatus(IntEnum):
    """打字状态"""

    TYPING = 1
    CANCEL = 2


# ========================================================================================
# |                              消息项数据结构                                             |
# ========================================================================================


class TextItem(BaseModel):
    """文本消息项"""

    content: str = ""


class CDNMedia(BaseModel):
    """CDN 媒体信息"""

    file_url: str = ""
    file_key: str = ""
    file_size: int = 0
    aes_key: str = ""
    file_md5: str = ""


class ImageItem(BaseModel):
    """图片消息项"""

    cdn: CDNMedia = Field(default_factory=CDNMedia)


class VoiceItem(BaseModel):
    """语音消息项"""

    cdn: CDNMedia = Field(default_factory=CDNMedia)
    text: str = ""  # 语音转文字结果
    duration: int = 0  # 语音时长(毫秒)


class FileItem(BaseModel):
    """文件消息项"""

    cdn: CDNMedia = Field(default_factory=CDNMedia)
    file_name: str = ""


class VideoItem(BaseModel):
    """视频消息项"""

    cdn: CDNMedia = Field(default_factory=CDNMedia)
    thumb_cdn: Optional[CDNMedia] = None
    duration: int = 0


class RefMessage(BaseModel):
    """引用消息"""

    title: str = ""
    content: str = ""


class MessageItem(BaseModel):
    """消息项"""

    item_type: int = 0
    text_item: Optional[TextItem] = None
    image_item: Optional[ImageItem] = None
    voice_item: Optional[VoiceItem] = None
    file_item: Optional[FileItem] = None
    video_item: Optional[VideoItem] = None
    ref_message: Optional[RefMessage] = None


# ========================================================================================
# |                              API 消息结构                                               |
# ========================================================================================


class WeixinMessage(BaseModel):
    """微信消息（来自 getUpdates）"""

    msg_id: str = ""
    from_user_id: str = ""
    to_user_id: str = ""
    msg_type: int = 0
    msg_state: int = 0
    item_list: list[MessageItem] = Field(default_factory=list)
    context_token: str = ""
    timestamp: int = 0


# ========================================================================================
# |                              API 响应数据结构                                            |
# ========================================================================================


class GetUpdatesResp(BaseModel):
    """getUpdates 响应"""

    ret: int = 0
    errmsg: str = ""
    errcode: int = 0
    msgs: list[WeixinMessage] = Field(default_factory=list)
    sync_buf: str = ""


class GetUploadUrlResp(BaseModel):
    """getUploadUrl 响应"""

    ret: int = 0
    errmsg: str = ""
    upload_url: str = ""
    file_id: str = ""
    file_key: str = ""


class GetConfigResp(BaseModel):
    """getConfig 响应"""

    ret: int = 0
    errmsg: str = ""
    typing_ticket: str = ""


class UploadedFileInfo(BaseModel):
    """上传后的文件信息"""

    file_url: str = ""
    file_key: str = ""
    file_id: str = ""
    file_size: int = 0
    file_md5: str = ""
    aes_key: str = ""  # base64 编码的 AES 密钥


class SendMessageResp(BaseModel):
    """sendMessage 响应"""

    ret: int = 0
    errmsg: str = ""


class SendTypingResp(BaseModel):
    """sendTyping 响应"""

    ret: int = 0
    errmsg: str = ""


class QRCodeResp(BaseModel):
    """getQRCode 响应"""

    ret: int = 0
    errmsg: str = ""
    qrcode: str = ""  # 二维码标识，用于轮询状态
    qrcode_img_content: str = ""  # 二维码图片 URL


class QRCodeStatusResp(BaseModel):
    """getQRCodeStatus 响应"""

    ret: int = 0
    errmsg: str = ""
    status: int = 0  # 0=等待扫码, 1=已扫码待确认, 2=已确认, 3=已过期
    bot_token: str = ""
    account_id: str = ""


# ========================================================================================
# |                              API 请求数据结构                                            |
# ========================================================================================


class BaseInfo(BaseModel):
    """通用请求基础信息"""

    channel_version: str = "1.0.2"


class GetUpdatesReq(BaseModel):
    """getUpdates 请求"""

    base_info: BaseInfo = Field(default_factory=BaseInfo)
    account_id: str = ""
    sync_buf: str = ""


class SendMessageReq(BaseModel):
    """sendMessage 请求"""

    base_info: BaseInfo = Field(default_factory=BaseInfo)
    account_id: str = ""
    to_user_id: str = ""
    context_token: str = ""
    item_list: list[dict[str, Any]] = Field(default_factory=list)


class GetUploadUrlReq(BaseModel):
    """getUploadUrl 请求"""

    base_info: BaseInfo = Field(default_factory=BaseInfo)
    account_id: str = ""
    media_type: int = 0
    file_size: int = 0
    file_md5: str = ""
    file_key: str = ""


class GetConfigReq(BaseModel):
    """getConfig 请求"""

    base_info: BaseInfo = Field(default_factory=BaseInfo)
    account_id: str = ""
    to_user_id: str = ""
    context_token: str = ""


class SendTypingReq(BaseModel):
    """sendTyping 请求"""

    base_info: BaseInfo = Field(default_factory=BaseInfo)
    account_id: str = ""
    to_user_id: str = ""
    typing_status: int = 1
    typing_ticket: str = ""
    context_token: str = ""
