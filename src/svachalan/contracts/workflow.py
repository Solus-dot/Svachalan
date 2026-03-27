from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from svachalan.contracts.backend import WaitUntil

ALLOWED_ACTIONS = {
    "goto",
    "click",
    "type",
    "wait_for",
    "extract_text",
    "extract_attr",
    "assert_exists",
    "screenshot",
}

READ_ONLY_ACTIONS = {
    "wait_for",
    "extract_text",
    "extract_attr",
    "assert_exists",
    "screenshot",
}

DOM_ACTIONS = {
    "click",
    "type",
    "wait_for",
    "extract_text",
    "extract_attr",
    "assert_exists",
}


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = 10_000
    allowed_domains: list[str] = Field(default_factory=list)
    screenshot_on_failure: bool = False
    goto_wait_until: WaitUntil = WaitUntil.DOMCONTENTLOADED


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    id: str | None = None
    timeout_ms: int | None = None
    retry_count: int | None = None
    selector: str | None = None
    frame_selector: str | None = None
    text: str | None = None
    url: str | None = None
    save_as: str | None = None
    attr: str | None = None


class WorkflowDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    vars: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep]

