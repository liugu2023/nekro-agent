from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PluginCheckLevel = Literal["load", "smoke", "strict"]


class PluginCheckFailure(BaseModel):
    module_name: str
    file_path: str
    error_message: str
    error_type: str
    stack_trace: str | None = None


class PluginCheckItem(BaseModel):
    id: str
    title: str
    ok: bool
    detail: str = ""
    error: str = ""


class PluginCheckPluginInfo(BaseModel):
    name: str
    module_name: str
    author: str
    version: str
    key: str
    enabled: bool
    is_builtin: bool
    is_package: bool
    sandbox_method_count: int = 0
    webhook_count: int = 0
    command_count: int = 0
    has_router: bool = False


class PluginCheckReport(BaseModel):
    ok: bool = False
    level: PluginCheckLevel = "smoke"
    candidate_path: str
    runtime_data_dir: str = ""
    staged_path: str = ""
    staged_entry_path: str = ""
    stage_mode: Literal["file", "package"] = "file"
    plugin: PluginCheckPluginInfo | None = None
    checks: list[PluginCheckItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    load_failures: list[PluginCheckFailure] = Field(default_factory=list)
