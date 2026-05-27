from __future__ import annotations

import hashlib
from pathlib import Path

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
                files.append(str(item.relative_to(root)))
    return sorted(files)


def read_plugin_file(file_path: str) -> str:
    return resolve_plugin_file(file_path, must_exist=True).read_text(encoding="utf-8")


def write_plugin_file(file_path: str, content: str) -> None:
    target = resolve_plugin_file(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
