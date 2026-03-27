from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from svachalan.contracts.backend import ArtifactRef, BrowserSessionMode
from svachalan.contracts.errors import ActionError


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_index: int
    step_id: str | None = None
    action: str
    status: StepStatus
    duration_ms: int
    attempts: int = 1
    sanitized_inputs: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    error: ActionError | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vars: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    output_dir: str | None = None
    run_id: str | None = None
    browser_session_mode: BrowserSessionMode | None = None


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    workflow_version: int
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    browser_session_mode: BrowserSessionMode | None = None
    input_summary: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: ActionError | None = None
    report_path: str | None = None

