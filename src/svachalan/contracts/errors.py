from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    POLICY_VIOLATION = "policy_violation"
    SELECTOR_NOT_FOUND = "selector_not_found"
    SELECTOR_NOT_UNIQUE = "selector_not_unique"
    ELEMENT_NOT_INTERACTABLE = "element_not_interactable"
    TIMEOUT = "timeout"
    NAVIGATION_ERROR = "navigation_error"
    PROTOCOL_ERROR = "protocol_error"
    UNSUPPORTED_SCOPE = "unsupported_scope"
    INTERPOLATION_ERROR = "interpolation_error"
    ASSERTION_FAILED = "assertion_failed"
    NO_BRANCH_MATCHED = "no_branch_matched"
    HUMAN_HANDOFF_REQUIRED = "human_handoff_required"


class ActionError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    message: str


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    issues: list[ValidationIssue]


class WorkflowValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__(self._render_message())

    def _render_message(self) -> str:
        return "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)


class ExecutionFailure(Exception):
    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(error.message)
