from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from svachalan.contracts.backend import ElementMatch, WaitUntil

ALLOWED_ACTIONS = {
    "goto",
    "click",
    "type",
    "wait_for",
    "wait_for_url_contains",
    "extract_text",
    "extract_attr",
    "assert_exists",
    "assert_url_contains",
    "assert_text_contains",
    "if_exists",
    "one_of",
    "screenshot",
}

READ_ONLY_ACTIONS = {
    "wait_for",
    "wait_for_url_contains",
    "extract_text",
    "extract_attr",
    "assert_exists",
    "assert_url_contains",
    "assert_text_contains",
    "if_exists",
    "one_of",
    "screenshot",
}

DOM_ACTIONS = {
    "click",
    "type",
    "wait_for",
    "extract_text",
    "extract_attr",
    "assert_exists",
    "assert_text_contains",
}


class WorkflowLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str | None = None
    selectors: list[str] | None = None
    match: ElementMatch | None = None


class WorkflowBranch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str | None = None
    selector: str | None = None
    selectors: list[str] | None = None
    frame_selector: str | None = None
    match: ElementMatch | None = None
    within: WorkflowLocator | None = None
    url: str | None = None
    default: bool = False
    steps: list[WorkflowStep]


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = 10_000
    allowed_domains: list[str] = Field(default_factory=list)
    screenshot_on_failure: bool = False
    goto_wait_until: WaitUntil = WaitUntil.DOMCONTENTLOADED


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: str
    id: str | None = None
    timeout_ms: int | None = None
    retry_count: int | None = None
    selector: str | None = None
    selectors: list[str] | None = None
    frame_selector: str | None = None
    match: ElementMatch | None = None
    within: WorkflowLocator | None = None
    text: str | None = None
    url: str | None = None
    save_as: str | None = None
    attr: str | None = None
    then_steps: list[WorkflowStep] | None = Field(default=None, alias="then")
    else_steps: list[WorkflowStep] | None = Field(default=None, alias="else")
    branches: list[WorkflowBranch] | None = None


class WorkflowDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    vars: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep]


WorkflowBranch.model_rebuild()
WorkflowStep.model_rebuild()
