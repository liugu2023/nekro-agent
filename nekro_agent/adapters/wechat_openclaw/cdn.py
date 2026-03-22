"""CDN 媒体上传/下载/加解密模块

处理 iLink Bot API 的 CDN 媒体文件，包括:
- AES-128-ECB 加解密
- CDN 文件上传（getUploadUrl + POST CDN）
- CDN 文件下载 + 解密
- SILK 语音转码（可选依赖）

参考: OpenClaw src/cdn/ + src/media/
"""

import base64
import hashlib
import os
import uuid

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.chat_message import ChatMessageSegmentFile, ChatMessageSegmentImage

from .api_client import ILinkApiClient
from .types import GetUploadUrlReq, UploadMediaType, UploadedFileInfo

logger = get_sub_logger("adapter.wechat_openclaw.cdn")

# SILK 转码可选依赖
try:
    import graiax.silkcoder as silk_coder  # pyright: ignore[reportMissingImports]

    HAS_SILK_CODER = True
except ImportError:
    HAS_SILK_CODER = False
    logger.info("graiax-silkcoder 未安装，语音将以文件形式传递")


# ========================================================================================
# |                              AES-128-ECB 加解密                                        |
# ========================================================================================


def _parse_aes_key(aes_key_str: str) -> bytes:
    """解析 AES 密钥

    iLink 有两种编码格式:
    1. base64(raw 16 bytes) - 解码后直接是 16 字节密钥
    2. base64(hex 32 chars) - 解码后是 32 字符的 hex 字符串，需要再次解码

    Args:
        aes_key_str: base64 编码的 AES 密钥

    Returns:
        16 字节的 AES 密钥
    """
    decoded = base64.b64decode(aes_key_str)

    if len(decoded) == 16:
        # 格式 1: 直接是 16 字节原始密钥
        return decoded

    if len(decoded) == 32:
        # 格式 2: 32 字符的 hex 字符串
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass

    # 尝试作为 hex 字符串解析
    try:
        hex_key = decoded.decode("ascii")
        key_bytes = bytes.fromhex(hex_key)
        if len(key_bytes) == 16:
            return key_bytes
    except (ValueError, UnicodeDecodeError):
        pass

    raise ValueError(f"无法解析 AES 密钥，解码后长度: {len(decoded)}")


def aes_encrypt(data: bytes, aes_key: bytes) -> bytes:
    """AES-128-ECB 加密

    Args:
        data: 原始数据
        aes_key: 16 字节 AES 密钥

    Returns:
        加密后的数据（含 PKCS7 填充）
    """
    padder = PKCS7(128).padder()
    padded_data = padder.update(data) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded_data) + encryptor.finalize()


def aes_decrypt(data: bytes, aes_key: bytes) -> bytes:
    """AES-128-ECB 解密

    Args:
        data: 加密数据
        aes_key: 16 字节 AES 密钥

    Returns:
        解密后的原始数据
    """
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(data) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded_data) + unpadder.finalize()


# ========================================================================================
# |                              CDN 上传                                                  |
# ========================================================================================


def _generate_file_key() -> str:
    """生成随机的 file_key"""
    return uuid.uuid4().hex


def _generate_aes_key() -> tuple[bytes, str]:
    """生成随机 AES 密钥

    Returns:
        (raw_key_bytes, base64_encoded_key)
    """
    raw_key = os.urandom(16)
    b64_key = base64.b64encode(raw_key).decode()
    return raw_key, b64_key


async def upload_media(
    api_client: ILinkApiClient,
    account_id: str,
    file_data: bytes,
    media_type: UploadMediaType,
    cdn_base_url: str,
) -> UploadedFileInfo:
    """上传媒体文件到 CDN

    流程: 读取文件 → MD5 → 生成 filekey/aeskey → getUploadUrl → AES 加密 → POST CDN

    Args:
        api_client: API 客户端
        account_id: 账号 ID
        file_data: 文件原始字节
        media_type: 媒体类型
        cdn_base_url: CDN 基础 URL

    Returns:
        UploadedFileInfo: 上传后的文件信息
    """
    # 1. 计算 MD5
    file_md5 = hashlib.md5(file_data).hexdigest()

    # 2. 生成 file_key 和 AES 密钥
    file_key = _generate_file_key()
    aes_key_raw, aes_key_b64 = _generate_aes_key()

    # 3. 获取上传 URL
    upload_req = GetUploadUrlReq(
        account_id=account_id,
        media_type=media_type.value,
        file_size=len(file_data),
        file_md5=file_md5,
        file_key=file_key,
    )
    upload_resp = await api_client.get_upload_url(upload_req)

    if upload_resp.ret != 0:
        raise RuntimeError(f"获取上传 URL 失败: {upload_resp.errmsg}")

    # 4. AES 加密
    encrypted_data = aes_encrypt(file_data, aes_key_raw)

    # 5. POST 到 CDN（带重试）
    upload_url = upload_resp.upload_url
    if not upload_url.startswith("http"):
        upload_url = f"{cdn_base_url}/{upload_url}"

    max_retries = 3
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    upload_url,
                    content=encrypted_data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                if 400 <= resp.status_code < 500:
                    raise RuntimeError(f"CDN 上传客户端错误 {resp.status_code}: {resp.text}")
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500:
                    raise
                last_error = e
                logger.warning(f"CDN 上传重试 {attempt + 1}/{max_retries}: {e}")
            except httpx.HTTPError as e:
                last_error = e
                logger.warning(f"CDN 上传重试 {attempt + 1}/{max_retries}: {e}")
        else:
            raise RuntimeError(f"CDN 上传失败，已重试 {max_retries} 次") from last_error

    return UploadedFileInfo(
        file_url=upload_url,
        file_key=upload_resp.file_key or file_key,
        file_id=upload_resp.file_id,
        file_size=len(file_data),
        file_md5=file_md5,
        aes_key=aes_key_b64,
    )


# ========================================================================================
# |                              CDN 下载                                                  |
# ========================================================================================


async def download_and_decrypt(
    file_url: str,
    aes_key_str: str,
    cdn_base_url: str,
) -> bytes:
    """下载并解密 CDN 文件

    Args:
        file_url: CDN 文件 URL 或路径
        aes_key_str: base64 编码的 AES 密钥
        cdn_base_url: CDN 基础 URL

    Returns:
        解密后的原始字节
    """
    url = file_url
    if not url.startswith("http"):
        url = f"{cdn_base_url}/{url}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        encrypted_data = resp.content

    aes_key = _parse_aes_key(aes_key_str)
    return aes_decrypt(encrypted_data, aes_key)


# ========================================================================================
# |                              媒体下载辅助函数                                           |
# ========================================================================================


async def download_image(
    file_url: str,
    aes_key: str,
    cdn_base_url: str,
    chat_key: str,
) -> ChatMessageSegmentImage:
    """下载图片并创建 Image 消息段

    Args:
        file_url: CDN 文件 URL
        aes_key: AES 密钥
        cdn_base_url: CDN 基础 URL
        chat_key: 聊天标识

    Returns:
        ChatMessageSegmentImage 消息段
    """
    data = await download_and_decrypt(file_url, aes_key, cdn_base_url)
    return await ChatMessageSegmentImage.create_from_bytes(
        _bytes=data,
        from_chat_key=chat_key,
        file_name="image.jpg",
        use_suffix=".jpg",
    )


async def download_voice(
    file_url: str,
    aes_key: str,
    cdn_base_url: str,
    chat_key: str,
) -> ChatMessageSegmentFile:
    """下载语音并创建消息段

    如果有 graiax-silkcoder，转码为 WAV；否则以 SILK 文件形式传递。

    Args:
        file_url: CDN 文件 URL
        aes_key: AES 密钥
        cdn_base_url: CDN 基础 URL
        chat_key: 聊天标识

    Returns:
        ChatMessageSegmentFile 消息段
    """
    data = await download_and_decrypt(file_url, aes_key, cdn_base_url)

    if HAS_SILK_CODER:
        try:
            wav_data: bytes = await silk_coder.decode(data, to_wav=True)
            return await ChatMessageSegmentFile.create_from_bytes(
                _bytes=wav_data,
                from_chat_key=chat_key,
                file_name="voice.wav",
                use_suffix=".wav",
            )
        except Exception:
            logger.warning("SILK 转码失败，以原始文件传递")

    return await ChatMessageSegmentFile.create_from_bytes(
        _bytes=data,
        from_chat_key=chat_key,
        file_name="voice.silk",
        use_suffix=".silk",
    )


async def download_video(
    file_url: str,
    aes_key: str,
    cdn_base_url: str,
    chat_key: str,
) -> ChatMessageSegmentFile:
    """下载视频并创建消息段"""
    data = await download_and_decrypt(file_url, aes_key, cdn_base_url)
    return await ChatMessageSegmentFile.create_from_bytes(
        _bytes=data,
        from_chat_key=chat_key,
        file_name="video.mp4",
        use_suffix=".mp4",
    )


async def download_file(
    file_url: str,
    aes_key: str,
    cdn_base_url: str,
    chat_key: str,
    file_name: str = "",
) -> ChatMessageSegmentFile:
    """下载文件并创建消息段"""
    data = await download_and_decrypt(file_url, aes_key, cdn_base_url)

    suffix = ""
    if file_name and "." in file_name:
        suffix = "." + file_name.rsplit(".", 1)[1]

    return await ChatMessageSegmentFile.create_from_bytes(
        _bytes=data,
        from_chat_key=chat_key,
        file_name=file_name or "file",
        use_suffix=suffix,
    )
