from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.plugin_dev import (
    PluginDevHistoryItem,
    PluginDevHistoryResponse,
    PluginDevVersionInfo,
    PluginDevVersionUpdate,
)
from nekro_agent.services.plugin_dev.host_file_gateway import (
    read_plugin_file,
    resolve_plugin_file,
    safe_file_slug,
    sha256_text,
    write_plugin_file,
)
from nekro_agent.services.plugin_dev.paths import PLUGIN_DEV_HISTORY_DIR, PLUGIN_DEV_VERSION_PATH


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def version_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValidationError(reason=f"版本文件格式错误: {path}") from e
    if not isinstance(data, dict):
        raise ValidationError(reason=f"版本文件结构错误: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_version_info() -> PluginDevVersionInfo:
    data = _read_json(
        PLUGIN_DEV_VERSION_PATH,
        {
            "schema_version": 1,
            "nekro_agent_channel": "preview",
            "nekro_agent_release": "",
            "nekro_agent_git_commit": "",
            "source_origin": "runtime_snapshot",
            "source_repo_url": "",
            "source_ref": "",
            "source_resolved_commit": "",
            "source_path": "",
            "source_dirty": False,
            "source_locked_at": "",
            "plugin_api_version": "preview",
            "stable_plugin_api_version": "stable",
            "template_version": "1",
            "updated_at": utc_now_iso(),
            "notes": "当前插件开发沙盒默认按预览版插件 API 生成。",
        },
    )
    info = PluginDevVersionInfo.model_validate(data)
    if not PLUGIN_DEV_VERSION_PATH.exists():
        _write_json(PLUGIN_DEV_VERSION_PATH, info.model_dump())
    return info


def update_version_info(body: PluginDevVersionUpdate) -> PluginDevVersionInfo:
    current = get_version_info().model_dump()
    updates = body.model_dump(exclude_none=True)
    current.update(updates)
    current["updated_at"] = utc_now_iso()
    info = PluginDevVersionInfo.model_validate(current)
    _write_json(PLUGIN_DEV_VERSION_PATH, info.model_dump())
    return info


def update_source_lock_info(
    *,
    repo_url: str,
    source_ref: str,
    resolved_commit: str,
    channel: str,
    release: str,
    source_origin: str,
    source_path: str,
    source_dirty: bool,
    notes: str,
) -> PluginDevVersionInfo:
    current = get_version_info().model_dump()
    current.update(
        {
            "nekro_agent_channel": channel,
            "nekro_agent_release": release,
            "nekro_agent_git_commit": resolved_commit,
            "source_origin": source_origin,
            "source_repo_url": repo_url,
            "source_ref": source_ref,
            "source_resolved_commit": resolved_commit,
            "source_path": source_path,
            "source_dirty": source_dirty,
            "source_locked_at": utc_now_iso(),
            "plugin_api_version": "runtime",
            "updated_at": utc_now_iso(),
            "notes": notes,
        }
    )
    info = PluginDevVersionInfo.model_validate(current)
    _write_json(PLUGIN_DEV_VERSION_PATH, info.model_dump())
    return info


def _history_dir(file_path: str) -> Path:
    return PLUGIN_DEV_HISTORY_DIR / safe_file_slug(file_path)


def _manifest_path(file_path: str) -> Path:
    return _history_dir(file_path) / "manifest.json"


_MAX_VERSIONS_PER_FILE = 50


def _partition_versions_for_prune(
    versions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(versions) <= _MAX_VERSIONS_PER_FILE:
        return [], versions
    return versions[:-_MAX_VERSIONS_PER_FILE], versions[-_MAX_VERSIONS_PER_FILE:]


def _delete_version_snapshots(history_dir: Path, versions: list[dict[str, Any]]) -> None:
    for item in versions:
        old_version_id = str(item.get("version_id") or "")
        if not old_version_id:
            continue
        for suffix in ("before", "after"):
            try:
                (history_dir / f"{old_version_id}-{suffix}.py").unlink(missing_ok=True)
            except OSError:
                # manifest 已提交后，旧快照清理失败只会遗留无引用文件，不能反向破坏新版本记录。
                continue


def get_history(file_path: str) -> PluginDevHistoryResponse:
    manifest = _read_json(
        _manifest_path(file_path),
        {"file_path": file_path, "current_version_id": None, "versions": []},
    )
    versions = [PluginDevHistoryItem.model_validate(item) for item in manifest.get("versions", [])]
    return PluginDevHistoryResponse(
        file_path=str(manifest.get("file_path") or file_path),
        current_version_id=manifest.get("current_version_id"),
        versions=versions,
    )


def record_version(
    *,
    file_path: str,
    task_id: str,
    action: str,
    before_content: str,
    after_content: str,
    before_exists: bool = True,
    after_exists: bool = True,
    summary: str,
) -> str:
    version = get_version_info()
    version_id = version_id_now()
    history_dir = _history_dir(file_path)
    history_dir.mkdir(parents=True, exist_ok=True)

    before_path = history_dir / f"{version_id}-before.py"
    after_path = history_dir / f"{version_id}-after.py"

    item = PluginDevHistoryItem(
        version_id=version_id,
        task_id=task_id,
        action=action,
        before_sha256=sha256_text(before_content),
        after_sha256=sha256_text(after_content),
        before_exists=before_exists,
        after_exists=after_exists,
        plugin_api_version=version.plugin_api_version,
        nekro_agent_git_commit=version.nekro_agent_git_commit,
        created_at=utc_now_iso(),
        summary=summary,
    )

    manifest_path = _manifest_path(file_path)
    try:
        before_path.write_text(before_content, encoding="utf-8")
        after_path.write_text(after_content, encoding="utf-8")
        manifest = _read_json(manifest_path, {"file_path": file_path, "current_version_id": None, "versions": []})
        versions = list(manifest.get("versions", []))
        versions.append(item.model_dump())
        pruned, kept = _partition_versions_for_prune(versions)
        manifest["versions"] = kept
        manifest["current_version_id"] = version_id
        _write_json(manifest_path, manifest)
    except Exception:
        before_path.unlink(missing_ok=True)
        after_path.unlink(missing_ok=True)
        raise
    _delete_version_snapshots(history_dir, pruned)
    return version_id


def remove_version_record(file_path: str, version_id: str) -> None:
    """撤销一次已成功写入的版本记录，用于多文件应用失败回滚。"""
    manifest_path = _manifest_path(file_path)
    manifest = _read_json(manifest_path, {"file_path": file_path, "current_version_id": None, "versions": []})
    versions = [item for item in manifest.get("versions", []) if str(item.get("version_id") or "") != version_id]
    manifest["versions"] = versions
    if manifest.get("current_version_id") == version_id:
        manifest["current_version_id"] = str(versions[-1].get("version_id") or "") if versions else None
    _write_json(manifest_path, manifest)
    history_dir = _history_dir(file_path)
    for suffix in ("before", "after"):
        (history_dir / f"{version_id}-{suffix}.py").unlink(missing_ok=True)


def rollback(file_path: str, version_id: str, target: str) -> str:
    history = get_history(file_path)
    history_item = next((item for item in history.versions if item.version_id == version_id), None)
    if history_item is None:
        raise NotFoundError(resource=f"文件 {file_path} 的版本 {version_id}")

    history_dir = _history_dir(file_path)
    source = history_dir / f"{version_id}-{target}.py"
    target_exists = history_item.before_exists if target == "before" else history_item.after_exists
    if target_exists and not source.exists():
        raise NotFoundError(resource=f"版本 {version_id}")

    try:
        current = read_plugin_file(file_path)
        current_exists = True
    except NotFoundError:
        current = ""
        current_exists = False
    restored = source.read_text(encoding="utf-8") if target_exists else ""

    if target_exists:
        write_plugin_file(file_path, restored)
    else:
        resolve_plugin_file(file_path).unlink(missing_ok=True)
    try:
        return record_version(
            file_path=file_path,
            task_id=f"rollback-{version_id}",
            action="rollback",
            before_content=current,
            after_content=restored,
            before_exists=current_exists,
            after_exists=target_exists,
            summary=f"回退到 {version_id} 的 {target} 内容",
        )
    except Exception:
        if current_exists:
            write_plugin_file(file_path, current)
        else:
            resolve_plugin_file(file_path).unlink(missing_ok=True)
        raise
