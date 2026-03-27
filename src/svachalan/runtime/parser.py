from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import ValidationError

from svachalan.contracts.errors import ValidationIssue, ValidationResult, WorkflowValidationError
from svachalan.contracts.workflow import (
    ALLOWED_ACTIONS,
    DOM_ACTIONS,
    READ_ONLY_ACTIONS,
    WorkflowDocument,
    WorkflowStep,
)

_INTERPOLATION_TOKEN = re.compile(r"\$\{([^}]+)\}")
_ACTION_REQUIRED_FIELDS = {
    "goto": {"url"},
    "click": {"selector"},
    "type": {"selector", "text"},
    "wait_for": {"selector"},
    "extract_text": {"selector", "save_as"},
    "extract_attr": {"selector", "attr", "save_as"},
    "assert_exists": {"selector"},
    "screenshot": set(),
}
_ACTION_ALLOWED_FIELDS = {
    "goto": {"id", "action", "timeout_ms", "retry_count", "url"},
    "click": {"id", "action", "timeout_ms", "retry_count", "selector", "frame_selector"},
    "type": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "frame_selector",
        "text",
    },
    "wait_for": {"id", "action", "timeout_ms", "retry_count", "selector", "frame_selector"},
    "extract_text": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "frame_selector",
        "save_as",
    },
    "extract_attr": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "frame_selector",
        "save_as",
        "attr",
    },
    "assert_exists": {"id", "action", "timeout_ms", "retry_count", "selector", "frame_selector"},
    "screenshot": {"id", "action", "timeout_ms", "retry_count"},
}
_STEP_FIELDS_TO_SCAN = ("selector", "frame_selector", "text", "url", "save_as", "attr")


def parse_workflow(source: str) -> WorkflowDocument:
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise WorkflowValidationError(
            [ValidationIssue(path="$", message=f"Invalid YAML: {exc}")]
        ) from exc

    if not isinstance(data, dict):
        raise WorkflowValidationError(
            [ValidationIssue(path="$", message="Workflow must be a mapping at the top level.")]
        )

    try:
        return WorkflowDocument.model_validate(data)
    except ValidationError as exc:
        issues = [
            ValidationIssue(path=_format_location(error["loc"]), message=error["msg"])
            for error in exc.errors()
        ]
        raise WorkflowValidationError(issues) from exc


def validate_workflow(doc: WorkflowDocument) -> ValidationResult:
    issues: list[ValidationIssue] = []

    if doc.version != 1:
        issues.append(
            ValidationIssue(
                path="version",
                message="Only workflow version 1 is supported.",
            )
        )

    if not doc.steps:
        issues.append(ValidationIssue(path="steps", message="At least one step is required."))

    if doc.settings.timeout_ms <= 0:
        issues.append(
            ValidationIssue(
                path="settings.timeout_ms",
                message="timeout_ms must be greater than 0.",
            )
        )

    step_ids: set[str] = set()
    declared_output_keys: set[str] = set()
    available_outputs: set[str] = set()
    for index, step in enumerate(doc.steps):
        issues.extend(
            _validate_step(
                step,
                index,
                step_ids,
                declared_output_keys,
                available_outputs,
            )
        )

    return ValidationResult(ok=not issues, issues=issues)


def ensure_valid_workflow(doc: WorkflowDocument) -> WorkflowDocument:
    result = validate_workflow(doc)
    if result.issues:
        raise WorkflowValidationError(result.issues)
    return doc


def _validate_step(
    step: WorkflowStep,
    index: int,
    step_ids: set[str],
    declared_output_keys: set[str],
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    path = f"steps[{index}]"

    if step.action not in ALLOWED_ACTIONS:
        issues.append(
            ValidationIssue(path=f"{path}.action", message=f"Unsupported action {step.action!r}.")
        )
        return issues

    if step.id:
        if step.id in step_ids:
            issues.append(
                ValidationIssue(path=f"{path}.id", message=f"Duplicate step id {step.id!r}.")
            )
        step_ids.add(step.id)

    if step.timeout_ms is not None and step.timeout_ms <= 0:
        issues.append(
            ValidationIssue(path=f"{path}.timeout_ms", message="timeout_ms must be greater than 0.")
        )

    if step.retry_count is not None:
        if step.retry_count < 0:
            issues.append(
                ValidationIssue(
                    path=f"{path}.retry_count",
                    message="retry_count cannot be negative.",
                )
            )
        if step.action not in READ_ONLY_ACTIONS:
            issues.append(
                ValidationIssue(
                    path=f"{path}.retry_count",
                    message=f"retry_count is not allowed on {step.action!r}.",
                )
            )

    required_fields = _ACTION_REQUIRED_FIELDS[step.action]
    allowed_fields = _ACTION_ALLOWED_FIELDS[step.action]
    present_fields = {name for name, value in step.model_dump().items() if value is not None}

    for field_name in required_fields:
        if getattr(step, field_name) in (None, ""):
            issues.append(
                ValidationIssue(path=f"{path}.{field_name}", message=f"{field_name} is required.")
            )

    for field_name in present_fields - allowed_fields:
        issues.append(
            ValidationIssue(
                path=f"{path}.{field_name}",
                message=f"{field_name} is not valid for action {step.action!r}.",
            )
        )

    if step.frame_selector is not None and step.action not in DOM_ACTIONS:
        issues.append(
            ValidationIssue(
                path=f"{path}.frame_selector",
                message=(
                    "frame_selector is only valid on DOM-targeting actions, "
                    f"not {step.action!r}."
                ),
            )
        )

    for field_name in _STEP_FIELDS_TO_SCAN:
        value = getattr(step, field_name)
        if isinstance(value, str):
            issues.extend(
                _validate_interpolation(
                    value,
                    f"{path}.{field_name}",
                    available_outputs=available_outputs,
                )
            )

    if step.save_as:
        if step.save_as in declared_output_keys:
            issues.append(
                ValidationIssue(
                    path=f"{path}.save_as",
                    message=f"Duplicate save_as key {step.save_as!r}.",
                )
            )
        declared_output_keys.add(step.save_as)
        available_outputs.add(step.save_as)

    return issues


def _validate_interpolation(
    value: str,
    path: str,
    *,
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for match in _INTERPOLATION_TOKEN.finditer(value):
        token = match.group(1)
        namespace, dot, key = token.partition(".")
        if dot == "" or key == "":
            issues.append(
                ValidationIssue(
                    path=path,
                    message=(
                        f"Interpolation token {match.group(0)!r} "
                        "must use namespace.key syntax."
                    ),
                )
            )
            continue
        if namespace not in {"vars", "secrets", "outputs"}:
            issues.append(
                ValidationIssue(
                    path=path,
                    message=f"Unsupported interpolation namespace {namespace!r}.",
                )
            )
            continue
        if namespace == "outputs" and key not in available_outputs:
            issues.append(
                ValidationIssue(
                    path=path,
                    message=(
                        f"Output {key!r} is not available from any previous step."
                    ),
                )
            )
    return issues


def _format_location(location: tuple[Any, ...]) -> str:
    rendered: list[str] = []
    for item in location:
        if isinstance(item, int):
            if rendered:
                rendered[-1] = f"{rendered[-1]}[{item}]"
            else:
                rendered.append(f"[{item}]")
        else:
            rendered.append(str(item))
    return ".".join(rendered) or "$"
