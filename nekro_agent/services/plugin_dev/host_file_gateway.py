from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from nekro_agent.core.os_env import WORKDIR_PLUGIN_DIR
from nekro_agent.schemas.errors import NotFoundError, ValidationError

_ALLOWED_SUFFIXES = (".py", ".py.disabled")


def plugin_root() -> Path:
    if not WORKDIR_PLUGIN_DIR:
        raise ValidationError(reason="工作目录插件目录未配置")
    root = Path(WORKDIR_PLUGIN_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_file_slug(file_path: str) -> str:
    return hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_plugin_file_path(file_path: str) -> str:
    """将外部输入规范化为唯一的 POSIX 插件相对路径。

    内部网关协议只接受规范 POSIX 路径，显式拒绝空段、`.`、`..` 与反斜杠，
    避免同一文件通过路径别名绕过去重、插件根目录或写删冲突校验。
    """
    if not file_path or file_path.strip() != file_path or "\\" in file_path:
        raise ValidationError(reason="插件文件路径必须是规范的 POSIX 相对路径")
    parts = file_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(reason="插件文件路径不能包含空目录、. 或 ..")
    path = PurePosixPath(file_path)
    if path.is_absolute():
        raise ValidationError(reason="插件文件路径不能是绝对路径")
    normalized = path.as_posix()
    resolve_plugin_file(normalized)
    return normalized


def plugin_top_dir(file_path: str) -> str | None:
    """返回包形式插件的顶层目录名；顶层单文件插件返回 None。"""
    parts = Path(file_path).parts
    return parts[0] if len(parts) > 1 else None


def resolve_plugin_file(file_path: str, *, must_exist: bool = False) -> Path:
    if not file_path or file_path.strip() != file_path:
        raise ValidationError(reason="插件文件路径非法")
    raw = Path(file_path)
    if raw.is_absolute():
        raise ValidationError(reason="插件文件路径不能是绝对路径")
    if not (file_path.endswith(".py") or file_path.endswith(".py.disabled")):
        raise ValidationError(reason="仅允许操作 .py 或 .py.disabled 插件文件")

    root = plugin_root()
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValidationError(reason="插件文件路径非法") from e

    if target.exists() and not target.is_file():
        raise ValidationError(reason="目标路径不是文件")
    if must_exist and not target.exists():
        raise NotFoundError(resource=f"插件文件 {file_path}")
    return target


def list_plugin_files() -> list[str]:
    root = plugin_root()
    files: list[str] = []
    for pattern in ("**/*.py", "**/*.py.disabled"):
        for item in root.glob(pattern):
            if item.is_file():
                files.append(item.relative_to(root).as_posix())
    return sorted(files)


def read_plugin_file(file_path: str) -> str:
    return resolve_plugin_file(file_path, must_exist=True).read_text(encoding="utf-8")


def write_plugin_file(file_path: str, content: str) -> None:
    target = resolve_plugin_file(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
