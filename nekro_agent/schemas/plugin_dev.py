from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PluginDevTaskStatus = Literal[
    "pending",
    "running_cc",
    "creating_proposal",
    "waiting_apply",
    "applied",
    "failed",
    "cancelled",
]


class PluginDevVersionInfo(BaseModel):
    schema_version: int = 1
    nekro_agent_channel: Literal["stable", "preview"] = "preview"
    nekro_agent_release: str = ""
    nekro_agent_git_commit: str = ""
    source_origin: Literal[
        "runtime_snapshot",
        "cached_runtime",
        "remote_git",
        "cached_remote",
        "disabled",
        "unavailable",
    ] = "runtime_snapshot"
    source_repo_url: str = ""
    source_ref: str = ""
    source_resolved_commit: str = ""
    source_path: str = ""
    source_dirty: bool = False
    source_locked_at: str = ""
    plugin_api_version: str = "preview"
    stable_plugin_api_version: str = "stable"
    template_version: str = "1"
    updated_at: str
    notes: str = ""


class PluginDevStatusResponse(BaseModel):
    enabled: bool = True
    sandbox_status: Literal["stopped", "running", "failed"] = "stopped"
    active_task_id: str | None = None
    queue_length: int = 0
    cc_model_preset_id: int | None = None
    cc_model_preset_name: str | None = None
    version: PluginDevVersionInfo


class PluginDevCcModelPresetUpdate(BaseModel):
    cc_model_preset_id: int | None = None


class PluginDevVersionUpdate(BaseModel):
    nekro_agent_channel: Literal["stable", "preview"] | None = None
    nekro_agent_release: str | None = None
    nekro_agent_git_commit: str | None = None
    source_origin: Literal[
        "runtime_snapshot",
        "cached_runtime",
        "remote_git",
        "cached_remote",
        "disabled",
        "unavailable",
    ] | None = None
    source_repo_url: str | None = None
    source_ref: str | None = None
    source_resolved_commit: str | None = None
    source_path: str | None = None
    source_dirty: bool | None = None
    source_locked_at: str | None = None
    plugin_api_version: str | None = None
    stable_plugin_api_version: str | None = None
    template_version: str | None = None
    notes: str | None = None


class PluginDevGenerateRequest(BaseModel):
    file_path: str
    prompt: str = Field(..., min_length=1)
    current_code: str
    base_code: str = ""
    dirty: bool = False
    mode: Literal["proposal"] = "proposal"


class PluginDevInternalFileResponse(BaseModel):
    file_path: str
    content: str
    sha256: str


class PluginDevInternalFilePayload(BaseModel):
    file_path: str
    content: str


class PluginDevInternalProposalRequest(BaseModel):
    file_path: str
    content: str = Field(..., min_length=1)
    task_id: str = Field(default="plugin-dev-internal", min_length=1)
    summary: str = Field(default="由插件开发沙盒创建写入提案")
    files: list[PluginDevInternalFilePayload] = Field(default_factory=list)


class PluginDevInternalCheckRequest(BaseModel):
    file_path: str
    content: str = Field(..., min_length=1)
    task_id: str = Field(default="plugin-dev-internal", min_length=1)
    level: Literal["static", "load", "smoke", "strict"] = "static"
    files: list[PluginDevInternalFilePayload] = Field(default_factory=list)


class PluginDevGenerateResponse(BaseModel):
    task_id: str
    status: PluginDevTaskStatus
    proposal_id: str | None = None


class PluginDevTaskResponse(BaseModel):
    task_id: str
    file_path: str
    status: PluginDevTaskStatus
    summary: str = ""
    logs: list[str] = Field(default_factory=list)
    proposal_id: str | None = None
    diff: str = ""
    result_code: str = ""
    error: str = ""
    version: PluginDevVersionInfo


class PluginDevProposalFile(BaseModel):
    file_path: str
    content: str
    before_sha256: str = ""


class PluginDevProposalResponse(BaseModel):
    proposal_id: str
    task_id: str
    file_path: str
    status: Literal["pending", "applied", "discarded"]
    diff: str
    result_code: str
    summary: str
    created_at: str
    before_sha256: str = ""
    # 插件的完整文件集（含主文件）；为空表示旧版单文件提案，以 file_path/result_code 为准
    files: list[PluginDevProposalFile] = Field(default_factory=list)


class PluginDevApplyResponse(BaseModel):
    ok: bool = True
    version_id: str


class PluginDevHistoryItem(BaseModel):
    version_id: str
    task_id: str
    action: str
    before_sha256: str
    after_sha256: str
    plugin_api_version: str
    nekro_agent_git_commit: str
    created_at: str
    summary: str


class PluginDevHistoryResponse(BaseModel):
    file_path: str
    current_version_id: str | None = None
    versions: list[PluginDevHistoryItem] = Field(default_factory=list)


class PluginDevRollbackRequest(BaseModel):
    version_id: str
    target: Literal["before", "after"] = "before"


class PluginDevRollbackResponse(BaseModel):
    ok: bool = True
    version_id: str
