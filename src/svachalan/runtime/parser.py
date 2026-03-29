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
    WorkflowBranch,
    WorkflowDocument,
    WorkflowLocator,
    WorkflowStep,
)

_INTERPOLATION_TOKEN = re.compile(r"\$\{([^}]+)\}")
_LOCATOR_ACTIONS = DOM_ACTIONS | {"if_exists"}
_ACTION_REQUIRED_FIELDS = {
    "goto": {"url"},
    "click": set(),
    "type": {"text"},
    "wait_for": set(),
    "wait_for_url_contains": {"url"},
    "extract_text": {"save_as"},
    "extract_attr": {"attr", "save_as"},
    "assert_exists": set(),
    "assert_url_contains": {"url"},
    "assert_text_contains": {"text"},
    "if_exists": {"then_steps"},
    "one_of": {"branches"},
    "screenshot": set(),
}
_ACTION_ALLOWED_FIELDS = {
    "goto": {"id", "action", "timeout_ms", "retry_count", "url"},
    "click": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
    },
    "type": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "text",
        "within",
    },
    "wait_for": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
    },
    "wait_for_url_contains": {"id", "action", "timeout_ms", "retry_count", "url"},
    "extract_text": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
        "save_as",
    },
    "extract_attr": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
        "save_as",
        "attr",
    },
    "assert_exists": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
    },
    "assert_url_contains": {"id", "action", "timeout_ms", "retry_count", "url"},
    "assert_text_contains": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
        "text",
    },
    "if_exists": {
        "id",
        "action",
        "timeout_ms",
        "retry_count",
        "selector",
        "selectors",
        "frame_selector",
        "match",
        "within",
        "then_steps",
        "else_steps",
    },
    "one_of": {"id", "action", "timeout_ms", "retry_count", "branches"},
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
                f"steps[{index}]",
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
    path: str,
    step_ids: set[str],
    declared_output_keys: set[str],
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

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

    if step.frame_selector is not None and step.action not in _LOCATOR_ACTIONS:
        issues.append(
            ValidationIssue(
                path=f"{path}.frame_selector",
                message=(
                    "frame_selector is only valid on DOM-targeting actions, "
                    f"not {step.action!r}."
                ),
            )
        )

    if step.within is not None and step.action not in _LOCATOR_ACTIONS:
        issues.append(
            ValidationIssue(
                path=f"{path}.within",
                message=f"within is not valid for action {step.action!r}.",
            )
        )

    if step.action in _LOCATOR_ACTIONS:
        issues.extend(_validate_locator_fields(step, path))
        if step.within is not None:
            issues.extend(
                _validate_locator(
                    step.within,
                    f"{path}.within",
                    available_outputs=available_outputs,
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
    if step.selectors is not None:
        for selector_index, selector in enumerate(step.selectors):
            issues.extend(
                _validate_interpolation(
                    selector,
                    f"{path}.selectors[{selector_index}]",
                    available_outputs=available_outputs,
                )
            )

    if step.action == "if_exists":
        if not step.then_steps:
            issues.append(
                ValidationIssue(path=f"{path}.then", message="then must contain at least one step.")
            )
    if step.action == "one_of" and not step.branches:
        issues.append(
            ValidationIssue(
                path=f"{path}.branches",
                message="branches must contain at least one branch.",
            )
        )

    if step.then_steps or step.else_steps:
        issues.extend(
            _validate_exclusive_step_sets(
                [
                    ("then", step.then_steps or []),
                    ("else", step.else_steps or []),
                ],
                path,
                step_ids,
                declared_output_keys,
                available_outputs,
            )
        )
    if step.branches:
        issues.extend(
            _validate_exclusive_branches(
                step.branches,
                path,
                step_ids,
                declared_output_keys,
                available_outputs,
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


def _validate_locator_fields(step: WorkflowStep, path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    has_selector = step.selector not in (None, "")
    has_selectors = step.selectors is not None and len(step.selectors) > 0

    if not has_selector and not has_selectors:
        issues.append(
            ValidationIssue(
                path=f"{path}.selector",
                message="At least one selector is required.",
            )
        )

    if step.selectors is not None:
        if not step.selectors:
            issues.append(
                ValidationIssue(
                    path=f"{path}.selectors",
                    message="selectors cannot be empty when provided.",
                )
            )
        for selector_index, selector in enumerate(step.selectors):
            if selector == "":
                issues.append(
                    ValidationIssue(
                        path=f"{path}.selectors[{selector_index}]",
                        message="selectors entries cannot be empty.",
                    )
                )

    return issues


def _validate_locator(
    locator: WorkflowLocator,
    path: str,
    *,
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    has_selector = locator.selector not in (None, "")
    has_selectors = locator.selectors is not None and len(locator.selectors) > 0

    if not has_selector and not has_selectors:
        issues.append(
            ValidationIssue(
                path=f"{path}.selector",
                message="At least one selector is required.",
            )
        )

    if isinstance(locator.selector, str):
        issues.extend(
            _validate_interpolation(
                locator.selector,
                f"{path}.selector",
                available_outputs=available_outputs,
            )
        )

    if locator.selectors is not None:
        if not locator.selectors:
            issues.append(
                ValidationIssue(
                    path=f"{path}.selectors",
                    message="selectors cannot be empty when provided.",
                )
            )
        for selector_index, selector in enumerate(locator.selectors):
            if selector == "":
                issues.append(
                    ValidationIssue(
                        path=f"{path}.selectors[{selector_index}]",
                        message="selectors entries cannot be empty.",
                    )
                )
            issues.extend(
                _validate_interpolation(
                    selector,
                    f"{path}.selectors[{selector_index}]",
                    available_outputs=available_outputs,
                )
            )

    return issues


def _validate_branch(
    branch: WorkflowBranch,
    path: str,
    step_ids: set[str],
    declared_output_keys: set[str],
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    has_selector = branch.selector not in (None, "")
    has_selectors = branch.selectors is not None and len(branch.selectors) > 0
    has_url_guard = branch.url not in (None, "")

    if not branch.steps:
        issues.append(
            ValidationIssue(path=f"{path}.steps", message="steps must contain at least one step.")
        )

    if not branch.default and not has_url_guard and not has_selector and not has_selectors:
        issues.append(
            ValidationIssue(
                path=path,
                message="Each branch must define a guard or be marked default.",
            )
        )

    if has_url_guard and isinstance(branch.url, str):
        issues.extend(
            _validate_interpolation(
                branch.url,
                f"{path}.url",
                available_outputs=available_outputs,
            )
        )

    if branch.frame_selector is not None and not (has_selector or has_selectors):
        issues.append(
            ValidationIssue(
                path=f"{path}.frame_selector",
                message="frame_selector requires a branch selector.",
            )
        )

    if has_selector or has_selectors:
        issues.extend(
            _validate_locator(
                WorkflowLocator(
                    selector=branch.selector,
                    selectors=branch.selectors,
                    match=branch.match,
                ),
                path,
                available_outputs=available_outputs,
            )
        )
    if branch.within is not None:
        issues.extend(
            _validate_locator(
                branch.within,
                f"{path}.within",
                available_outputs=available_outputs,
            )
        )

    for nested_index, nested_step in enumerate(branch.steps):
        issues.extend(
            _validate_step(
                nested_step,
                f"{path}.steps[{nested_index}]",
                step_ids,
                declared_output_keys,
                available_outputs,
            )
        )

    return issues


def _validate_exclusive_step_sets(
    step_sets: list[tuple[str, list[WorkflowStep]]],
    path: str,
    step_ids: set[str],
    declared_output_keys: set[str],
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    base_declared_outputs = set(declared_output_keys)
    base_available_outputs = set(available_outputs)
    branch_declared_sets: list[set[str]] = []
    branch_available_sets: list[set[str]] = []

    for label, nested_steps in step_sets:
        nested_declared = set(declared_output_keys)
        nested_available = set(available_outputs)
        for nested_index, nested_step in enumerate(nested_steps):
            issues.extend(
                _validate_step(
                    nested_step,
                    f"{path}.{label}[{nested_index}]",
                    step_ids,
                    nested_declared,
                    nested_available,
                )
            )
        branch_declared_sets.append(nested_declared)
        branch_available_sets.append(nested_available)

    for nested_declared in branch_declared_sets:
        declared_output_keys.update(nested_declared - base_declared_outputs)
    if branch_available_sets:
        guaranteed_available = set.intersection(*branch_available_sets)
        available_outputs.update(guaranteed_available - base_available_outputs)

    return issues


def _validate_exclusive_branches(
    branches: list[WorkflowBranch],
    path: str,
    step_ids: set[str],
    declared_output_keys: set[str],
    available_outputs: set[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    base_declared_outputs = set(declared_output_keys)
    base_available_outputs = set(available_outputs)
    branch_declared_sets: list[set[str]] = []
    branch_available_sets: list[set[str]] = []

    for branch_index, branch in enumerate(branches):
        nested_declared = set(declared_output_keys)
        nested_available = set(available_outputs)
        issues.extend(
            _validate_branch(
                branch,
                f"{path}.branches[{branch_index}]",
                step_ids,
                nested_declared,
                nested_available,
            )
        )
        branch_declared_sets.append(nested_declared)
        branch_available_sets.append(nested_available)

    for nested_declared in branch_declared_sets:
        declared_output_keys.update(nested_declared - base_declared_outputs)
    if branch_available_sets:
        guaranteed_available = set.intersection(*branch_available_sets)
        available_outputs.update(guaranteed_available - base_available_outputs)

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
