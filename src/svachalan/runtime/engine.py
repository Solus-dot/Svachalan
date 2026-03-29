from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from svachalan.contracts.backend import (
    ActionOptions,
    ActionResult,
    ArtifactRef,
    AutomationBackend,
    ElementMatch,
    ElementTarget,
    NavigationOptions,
    PageState,
    ScreenshotOptions,
    TypeOptions,
)
from svachalan.contracts.errors import ActionError, ErrorCode, ExecutionFailure
from svachalan.contracts.run import RunOptions, RunReport, RunStatus, StepResult, StepStatus
from svachalan.contracts.workflow import (
    DOM_ACTIONS,
    READ_ONLY_ACTIONS,
    WorkflowBranch,
    WorkflowDocument,
    WorkflowLocator,
    WorkflowStep,
)
from svachalan.reporting.store import ReportStore
from svachalan.runtime.parser import ensure_valid_workflow

_INTERPOLATION_TOKEN = re.compile(r"\$\{([^}]+)\}")
_SECRET_REDACTION = "[REDACTED]"
_LOCATOR_ACTIONS = DOM_ACTIONS | {"if_exists"}


@dataclass
class _RunState:
    vars_context: dict[str, Any]
    secrets_context: dict[str, Any]
    outputs: dict[str, Any] = field(default_factory=dict)
    step_results: list[StepResult] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    next_step_index: int = 0
    final_error: ActionError | None = None
    handoff_required: bool = False
    handoff_reason: str | None = None


def run_workflow(
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    options: RunOptions | None = None,
) -> RunReport:
    options = options or RunOptions()
    workflow = ensure_valid_workflow(workflow)

    started_at = datetime_now()
    state = _RunState(
        vars_context={**workflow.vars, **options.vars},
        secrets_context={**workflow.secrets, **options.secrets},
    )

    _execute_steps(workflow.steps, workflow, backend, state, branch_path=[])

    finished_at = datetime_now()
    report = RunReport(
        workflow_version=workflow.version,
        status=RunStatus.FAILED if state.final_error else RunStatus.SUCCEEDED,
        started_at=started_at,
        finished_at=finished_at,
        browser_session_mode=options.browser_session_mode,
        input_summary={
            "vars": state.vars_context,
            "secret_keys": sorted(state.secrets_context.keys()),
        },
        outputs=state.outputs,
        steps=state.step_results,
        artifacts=state.artifacts,
        error=state.final_error,
        handoff_required=state.handoff_required,
        handoff_reason=state.handoff_reason,
    )

    if options.output_dir:
        store = ReportStore(options.output_dir)
        report = store.write(report, run_id=options.run_id)

    return report


def datetime_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _execute_steps(
    steps: list[WorkflowStep],
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> bool:
    for step in steps:
        if not _execute_step(step, workflow, backend, state, branch_path=branch_path):
            return False
    return True


def _execute_step(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> bool:
    if step.action == "if_exists":
        return _execute_if_exists(step, workflow, backend, state, branch_path=branch_path)
    if step.action == "one_of":
        return _execute_one_of(step, workflow, backend, state, branch_path=branch_path)
    return _execute_simple_step(step, workflow, backend, state, branch_path=branch_path)


def _execute_simple_step(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> bool:
    step_index = state.next_step_index
    state.next_step_index += 1
    step_started = time.perf_counter()
    attempts = 0

    try:
        sanitized_inputs, resolved_inputs = _resolve_step_inputs(
            step,
            state.vars_context,
            state.secrets_context,
            state.outputs,
        )
    except ExecutionFailure as exc:
        duration_ms = int((time.perf_counter() - step_started) * 1000)
        failure_error, failure_artifacts, failure_details = _prepare_failure(
            exc.error,
            step,
            workflow,
            backend,
            state.secrets_context,
        )
        state.final_error = failure_error
        state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
        state.handoff_reason = (
            failure_details.get("handoff_reason") if state.handoff_required else None
        )
        state.artifacts.extend(failure_artifacts)
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=attempts,
                sanitized_inputs={},
                error=failure_error,
                artifacts=failure_artifacts,
                details=_details_with_branch_path(failure_details, branch_path),
            )
        )
        return False

    while True:
        attempts += 1
        try:
            result = _dispatch_step(
                step,
                resolved_inputs,
                workflow,
                backend,
                state,
                branch_path=branch_path,
            )
        except ExecutionFailure as exc:
            result = ActionResult.failure(exc.error)
        if result.ok:
            break
        if step.action not in READ_ONLY_ACTIONS:
            break
        max_attempts = 1 + (step.retry_count or 0)
        if attempts >= max_attempts:
            break

    duration_ms = int((time.perf_counter() - step_started) * 1000)
    step_artifacts = _collect_artifacts(result)
    state.artifacts.extend(step_artifacts)

    if result.ok:
        if step.save_as:
            state.outputs[step.save_as] = result.value
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.SUCCEEDED,
                duration_ms=duration_ms,
                attempts=attempts,
                sanitized_inputs=sanitized_inputs,
                output=result.value,
                artifacts=step_artifacts,
                details=_details_with_branch_path(result.details, branch_path),
            )
        )
        return True

    failure_error, failure_artifacts, failure_details = _prepare_failure(
        result.error,
        step,
        workflow,
        backend,
        state.secrets_context,
    )
    step_artifacts.extend(failure_artifacts)
    state.artifacts.extend(failure_artifacts)
    state.final_error = failure_error
    state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
    state.handoff_reason = failure_details.get("handoff_reason") if state.handoff_required else None
    merged_details = dict(result.details)
    merged_details.update(failure_details)
    state.step_results.append(
        StepResult(
            step_index=step_index,
            step_id=step.id,
            action=step.action,
            status=StepStatus.FAILED,
            duration_ms=duration_ms,
            attempts=attempts,
            sanitized_inputs=sanitized_inputs,
            error=failure_error,
            artifacts=step_artifacts,
            details=_details_with_branch_path(merged_details, branch_path),
        )
    )
    return False


def _execute_if_exists(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> bool:
    step_index = state.next_step_index
    state.next_step_index += 1
    step_started = time.perf_counter()

    try:
        sanitized_inputs, resolved_inputs = _resolve_step_inputs(
            step,
            state.vars_context,
            state.secrets_context,
            state.outputs,
        )
    except ExecutionFailure as exc:
        duration_ms = int((time.perf_counter() - step_started) * 1000)
        failure_error, failure_artifacts, failure_details = _prepare_failure(
            exc.error,
            step,
            workflow,
            backend,
            state.secrets_context,
        )
        state.final_error = failure_error
        state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
        state.handoff_reason = (
            failure_details.get("handoff_reason") if state.handoff_required else None
        )
        state.artifacts.extend(failure_artifacts)
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=0,
                sanitized_inputs={},
                error=failure_error,
                artifacts=failure_artifacts,
                details=_details_with_branch_path(failure_details, branch_path),
            )
        )
        return False

    guard_result, attempts = _evaluate_exists_guard(step, resolved_inputs, workflow, backend)
    duration_ms = int((time.perf_counter() - step_started) * 1000)

    if guard_result.ok:
        chosen_steps = step.then_steps or []
        branch_taken = "then"
    elif guard_result.error and guard_result.error.code == ErrorCode.SELECTOR_NOT_FOUND:
        chosen_steps = step.else_steps or []
        branch_taken = "else"
    else:
        failure_error, failure_artifacts, failure_details = _prepare_failure(
            guard_result.error,
            step,
            workflow,
            backend,
            state.secrets_context,
        )
        state.final_error = failure_error
        state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
        state.handoff_reason = (
            failure_details.get("handoff_reason") if state.handoff_required else None
        )
        state.artifacts.extend(failure_artifacts)
        merged_details = dict(guard_result.details)
        merged_details.update(failure_details)
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=attempts,
                sanitized_inputs=sanitized_inputs,
                error=failure_error,
                artifacts=failure_artifacts,
                details=_details_with_branch_path(merged_details, branch_path),
            )
        )
        return False

    details = dict(guard_result.details)
    details["branch_taken"] = branch_taken
    state.step_results.append(
        StepResult(
            step_index=step_index,
            step_id=step.id,
            action=step.action,
            status=StepStatus.SUCCEEDED,
            duration_ms=duration_ms,
            attempts=attempts,
            sanitized_inputs=sanitized_inputs,
            artifacts=[],
            details=_details_with_branch_path(details, branch_path),
        )
    )

    if not chosen_steps:
        return True
    return _execute_steps(
        chosen_steps,
        workflow,
        backend,
        state,
        branch_path=[*branch_path, step.id or step.action, branch_taken],
    )


def _execute_one_of(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> bool:
    step_index = state.next_step_index
    state.next_step_index += 1
    step_started = time.perf_counter()
    branch_details: dict[str, Any] = {"branches_evaluated": []}

    selected_branch: WorkflowBranch | None = None
    selected_name: str | None = None
    error_result: ActionResult | None = None
    for branch in step.branches or []:
        branch_name = branch.name or f"branch-{len(branch_details['branches_evaluated']) + 1}"
        matched, result, details = _evaluate_branch(branch, backend, state, workflow)
        branch_entry = {"name": branch_name, **details}
        branch_details["branches_evaluated"].append(branch_entry)
        if (
            result is not None
            and result.error
            and result.error.code != ErrorCode.SELECTOR_NOT_FOUND
        ):
            error_result = result
            break
        if matched:
            selected_branch = branch
            selected_name = branch_name
            break

    duration_ms = int((time.perf_counter() - step_started) * 1000)
    if error_result is not None:
        failure_error, failure_artifacts, failure_details = _prepare_failure(
            error_result.error,
            step,
            workflow,
            backend,
            state.secrets_context,
        )
        state.final_error = failure_error
        state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
        state.handoff_reason = (
            failure_details.get("handoff_reason") if state.handoff_required else None
        )
        state.artifacts.extend(failure_artifacts)
        branch_details.update(failure_details)
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=1,
                sanitized_inputs={},
                error=failure_error,
                artifacts=failure_artifacts,
                details=_details_with_branch_path(branch_details, branch_path),
            )
        )
        return False

    if selected_branch is None:
        error = ActionError(
            code=ErrorCode.NO_BRANCH_MATCHED,
            message="No branch matched the current page state.",
            details={
                "branches_evaluated": [
                    entry["name"] for entry in branch_details["branches_evaluated"]
                ]
            },
        )
        failure_error, failure_artifacts, failure_details = _prepare_failure(
            error,
            step,
            workflow,
            backend,
            state.secrets_context,
        )
        state.final_error = failure_error
        state.handoff_required = failure_error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED
        state.handoff_reason = (
            failure_details.get("handoff_reason") if state.handoff_required else None
        )
        state.artifacts.extend(failure_artifacts)
        branch_details.update(failure_details)
        state.step_results.append(
            StepResult(
                step_index=step_index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=1,
                sanitized_inputs={},
                error=failure_error,
                artifacts=failure_artifacts,
                details=_details_with_branch_path(branch_details, branch_path),
            )
        )
        return False

    branch_details["selected_branch"] = selected_name
    state.step_results.append(
        StepResult(
            step_index=step_index,
            step_id=step.id,
            action=step.action,
            status=StepStatus.SUCCEEDED,
            duration_ms=duration_ms,
            attempts=1,
            sanitized_inputs={},
            artifacts=[],
            details=_details_with_branch_path(branch_details, branch_path),
        )
    )
    return _execute_steps(
        selected_branch.steps,
        workflow,
        backend,
        state,
        branch_path=[*branch_path, step.id or step.action, selected_name or "branch"],
    )


def _dispatch_step(
    step: WorkflowStep,
    resolved_inputs: dict[str, Any],
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    state: _RunState,
    *,
    branch_path: list[str],
) -> ActionResult:
    del state, branch_path
    timeout_ms = step.timeout_ms or workflow.settings.timeout_ms
    common_options = ActionOptions(timeout_ms=timeout_ms, step_id=step.id)

    if step.action == "goto":
        _enforce_allowed_domain(resolved_inputs["url"], workflow)
        return backend.goto(
            resolved_inputs["url"],
            NavigationOptions(
                timeout_ms=timeout_ms,
                step_id=step.id,
                wait_until=workflow.settings.goto_wait_until,
            ),
        )

    if step.action == "screenshot":
        return backend.screenshot(ScreenshotOptions(timeout_ms=timeout_ms, step_id=step.id))

    if step.action == "wait_for_url_contains":
        return _wait_for_url_contains(
            resolved_inputs["url"],
            backend,
            common_options,
            timeout_ms=timeout_ms,
        )
    if step.action == "assert_url_contains":
        return _assert_url_contains(resolved_inputs["url"], backend, common_options)

    target = _build_target_from_inputs(step, resolved_inputs)
    if step.action == "click":
        return backend.click(target, common_options)
    if step.action == "type":
        return backend.type(
            target,
            resolved_inputs["text"],
            TypeOptions(timeout_ms=timeout_ms, step_id=step.id),
        )
    if step.action == "wait_for":
        return backend.wait_for(target, common_options)
    if step.action == "assert_exists":
        return backend.assert_exists(target, common_options)
    if step.action == "extract_text":
        return backend.extract_text(target, common_options)
    if step.action == "extract_attr":
        return backend.extract_attr(target, resolved_inputs["attr"], common_options)
    if step.action == "assert_text_contains":
        return _assert_text_contains(
            target,
            resolved_inputs["text"],
            backend,
            common_options,
        )

    return ActionResult.failure(
        ActionError(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"Unsupported action {step.action!r}.",
        )
    )


def _resolve_step_inputs(
    step: WorkflowStep,
    vars_context: dict[str, Any],
    secrets_context: dict[str, Any],
    outputs_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    sanitized_inputs: dict[str, Any] = {}
    resolved_inputs: dict[str, Any] = {}

    if step.action in _LOCATOR_ACTIONS or step.action == "assert_text_contains":
        locator_sanitized, locator_resolved = _resolve_target_inputs(
            selector=step.selector,
            selectors=step.selectors,
            frame_selector=step.frame_selector,
            match=step.match,
            within=step.within,
            vars_context=vars_context,
            secrets_context=secrets_context,
            outputs_context=outputs_context,
        )
        sanitized_inputs.update(locator_sanitized)
        resolved_inputs.update(locator_resolved)

    for field_name in ("text", "url", "attr"):
        raw_value = getattr(step, field_name)
        if raw_value is None:
            continue
        resolved_value = _interpolate(raw_value, vars_context, secrets_context, outputs_context)
        resolved_inputs[field_name] = resolved_value
        sanitized_inputs[field_name] = _sanitize_interpolated_value(raw_value, resolved_value)

    return sanitized_inputs, resolved_inputs


def _resolve_target_inputs(
    *,
    selector: str | None,
    selectors: list[str] | None,
    frame_selector: str | None,
    match: ElementMatch | None,
    within: WorkflowLocator | None,
    vars_context: dict[str, Any],
    secrets_context: dict[str, Any],
    outputs_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    sanitized_inputs: dict[str, Any] = {}
    resolved_inputs: dict[str, Any] = {}

    if selector is not None:
        resolved_selector = _interpolate(selector, vars_context, secrets_context, outputs_context)
        resolved_inputs["selector"] = resolved_selector
        sanitized_inputs["selector"] = _sanitize_interpolated_value(selector, resolved_selector)

    if selectors is not None:
        resolved_selectors = [
            _interpolate(value, vars_context, secrets_context, outputs_context)
            for value in selectors
        ]
        resolved_inputs["selectors"] = resolved_selectors
        sanitized_inputs["selectors"] = [
            _sanitize_interpolated_value(raw_value, resolved_value)
            for raw_value, resolved_value in zip(selectors, resolved_selectors, strict=True)
        ]

    if frame_selector is not None:
        resolved_frame_selector = _interpolate(
            frame_selector,
            vars_context,
            secrets_context,
            outputs_context,
        )
        resolved_inputs["frame_selector"] = resolved_frame_selector
        sanitized_inputs["frame_selector"] = _sanitize_interpolated_value(
            frame_selector,
            resolved_frame_selector,
        )

    if match is not None:
        resolved_inputs["match"] = match

    if within is not None:
        within_sanitized, within_resolved = _resolve_target_inputs(
            selector=within.selector,
            selectors=within.selectors,
            frame_selector=None,
            match=within.match,
            within=None,
            vars_context=vars_context,
            secrets_context=secrets_context,
            outputs_context=outputs_context,
        )
        resolved_inputs["within"] = within_resolved
        sanitized_inputs["within"] = within_sanitized

    return sanitized_inputs, resolved_inputs


def _build_target_from_inputs(step: WorkflowStep, resolved_inputs: dict[str, Any]) -> ElementTarget:
    return ElementTarget(
        selector=resolved_inputs.get("selector"),
        selectors=resolved_inputs.get("selectors", []),
        frame_selector=resolved_inputs.get("frame_selector"),
        match=resolved_inputs.get("match", step.match or ElementMatch.UNIQUE),
        within=_build_within_target(resolved_inputs.get("within")),
    )


def _build_within_target(within_inputs: dict[str, Any] | None) -> ElementTarget | None:
    if within_inputs is None:
        return None
    return ElementTarget(
        selector=within_inputs.get("selector"),
        selectors=within_inputs.get("selectors", []),
        match=within_inputs.get("match", ElementMatch.UNIQUE),
        within=_build_within_target(within_inputs.get("within")),
    )


def _wait_for_url_contains(
    expected_substring: str,
    backend: AutomationBackend,
    opts: ActionOptions,
    *,
    timeout_ms: int,
) -> ActionResult:
    timeout_seconds = max(timeout_ms / 1000.0, 0.001)
    deadline = time.monotonic() + timeout_seconds
    last_state: PageState | None = None
    while time.monotonic() < deadline:
        inspected = backend.inspect_page(opts)
        if not inspected.ok:
            return inspected
        page_state = inspected.value if isinstance(inspected.value, PageState) else None
        if page_state is None:
            return ActionResult.failure(
                ActionError(code=ErrorCode.PROTOCOL_ERROR, message="Invalid page state response.")
            )
        last_state = page_state
        if page_state.handoff_required:
            return ActionResult.failure(
                ActionError(
                    code=ErrorCode.HUMAN_HANDOFF_REQUIRED,
                    message=page_state.handoff_reason or "Human intervention required.",
                    details={"detected_indicators": page_state.detected_indicators},
                ),
                details={"current_url": page_state.url},
            )
        if expected_substring in (page_state.url or ""):
            return ActionResult.success(
                details={"current_url": page_state.url, "url_contains": expected_substring}
            )
        time.sleep(0.1)
    return ActionResult.failure(
        ActionError(
            code=ErrorCode.TIMEOUT,
            message=f"Timed out waiting for URL containing {expected_substring!r}.",
            details={"last_url": last_state.url if last_state else None},
        ),
        details={"current_url": last_state.url if last_state else None},
    )


def _assert_url_contains(
    expected_substring: str,
    backend: AutomationBackend,
    opts: ActionOptions,
) -> ActionResult:
    inspected = backend.inspect_page(opts)
    if not inspected.ok:
        return inspected
    page_state = inspected.value if isinstance(inspected.value, PageState) else None
    if page_state is None:
        return ActionResult.failure(
            ActionError(code=ErrorCode.PROTOCOL_ERROR, message="Invalid page state response.")
        )
    if page_state.handoff_required:
        return ActionResult.failure(
            ActionError(
                code=ErrorCode.HUMAN_HANDOFF_REQUIRED,
                message=page_state.handoff_reason or "Human intervention required.",
                details={"detected_indicators": page_state.detected_indicators},
            ),
            details={"current_url": page_state.url},
        )
    if expected_substring not in (page_state.url or ""):
        return ActionResult.failure(
            ActionError(
                code=ErrorCode.ASSERTION_FAILED,
                message=f"Current URL does not contain {expected_substring!r}.",
                details={"current_url": page_state.url or ""},
            ),
            details={"current_url": page_state.url},
        )
    return ActionResult.success(
        details={
            "current_url": page_state.url,
            "url_contains": expected_substring,
        }
    )


def _assert_text_contains(
    target: ElementTarget,
    expected_text: str,
    backend: AutomationBackend,
    opts: ActionOptions,
) -> ActionResult:
    result = backend.extract_text(target, opts)
    if not result.ok:
        return result
    actual_text = result.value if isinstance(result.value, str) else str(result.value)
    if expected_text not in actual_text:
        return ActionResult.failure(
            ActionError(
                code=ErrorCode.ASSERTION_FAILED,
                message=f"Extracted text does not contain {expected_text!r}.",
                details={"actual_text": actual_text[:500]},
            ),
            details={**result.details, "actual_text": actual_text},
        )
    return ActionResult.success(
        value=actual_text,
        artifacts=result.artifacts,
        details={**result.details, "actual_text": actual_text, "text_contains": expected_text},
    )


def _evaluate_exists_guard(
    step: WorkflowStep,
    resolved_inputs: dict[str, Any],
    workflow: WorkflowDocument,
    backend: AutomationBackend,
) -> tuple[ActionResult, int]:
    timeout_ms = step.timeout_ms or workflow.settings.timeout_ms
    common_options = ActionOptions(timeout_ms=timeout_ms, step_id=step.id)
    target = _build_target_from_inputs(step, resolved_inputs)
    attempts = 0
    while True:
        attempts += 1
        result = backend.assert_exists(target, common_options)
        if result.ok:
            return result, attempts
        max_attempts = 1 + (step.retry_count or 0)
        if attempts >= max_attempts:
            return result, attempts
        if result.error and result.error.code == ErrorCode.SELECTOR_NOT_FOUND:
            time.sleep(0.1)
            continue
        return result, attempts


def _evaluate_branch(
    branch: WorkflowBranch,
    backend: AutomationBackend,
    state: _RunState,
    workflow: WorkflowDocument,
) -> tuple[bool, ActionResult | None, dict[str, Any]]:
    branch_details: dict[str, Any] = {"default": branch.default}
    matched = True
    try:
        if branch.url is not None:
            resolved_url = _interpolate(
                branch.url,
                state.vars_context,
                state.secrets_context,
                state.outputs,
            )
            branch_details["url_contains"] = resolved_url
            inspected = backend.inspect_page(ActionOptions(timeout_ms=workflow.settings.timeout_ms))
            if not inspected.ok:
                return False, inspected, branch_details
            page_state = inspected.value if isinstance(inspected.value, PageState) else None
            if page_state is None:
                return False, ActionResult.failure(
                    ActionError(
                        code=ErrorCode.PROTOCOL_ERROR,
                        message="Invalid page state response.",
                    )
                ), branch_details
            branch_details["current_url"] = page_state.url
            if page_state.handoff_required:
                return False, ActionResult.failure(
                    ActionError(
                        code=ErrorCode.HUMAN_HANDOFF_REQUIRED,
                        message=page_state.handoff_reason or "Human intervention required.",
                        details={"detected_indicators": page_state.detected_indicators},
                    )
                ), branch_details
            matched = matched and resolved_url in (page_state.url or "")

        if branch.selector is not None or branch.selectors is not None:
            _, resolved_target = _resolve_target_inputs(
                selector=branch.selector,
                selectors=branch.selectors,
                frame_selector=branch.frame_selector,
                match=branch.match,
                within=branch.within,
                vars_context=state.vars_context,
                secrets_context=state.secrets_context,
                outputs_context=state.outputs,
            )
            target = ElementTarget(
                selector=resolved_target.get("selector"),
                selectors=resolved_target.get("selectors", []),
                frame_selector=resolved_target.get("frame_selector"),
                match=resolved_target.get("match", branch.match or ElementMatch.UNIQUE),
                within=_build_within_target(resolved_target.get("within")),
            )
            result = backend.assert_exists(
                target,
                ActionOptions(timeout_ms=workflow.settings.timeout_ms),
            )
            branch_details.update(result.details)
            if not result.ok:
                if result.error and result.error.code == ErrorCode.SELECTOR_NOT_FOUND:
                    matched = False
                else:
                    return False, result, branch_details
            else:
                matched = matched and True
    except ExecutionFailure as exc:
        branch_details["guard_error"] = exc.error.message
        return False, ActionResult.failure(exc.error), branch_details

    if not matched and branch.default:
        matched = True

    return matched, None, branch_details


def _collect_artifacts(result: ActionResult) -> list[ArtifactRef]:
    artifacts = list(result.artifacts)
    if isinstance(result.value, ArtifactRef):
        artifacts.append(result.value)
    return artifacts


def _prepare_failure(
    error: ActionError | None,
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    secrets_context: dict[str, Any],
) -> tuple[ActionError, list[ArtifactRef], dict[str, Any]]:
    inspected_artifacts: list[ArtifactRef] = []
    details: dict[str, Any] = {}
    try:
        inspected_state = _inspect_page(step, workflow, backend)
    except Exception as exc:  # pragma: no cover - defensive against backend transport failures
        inspected_state = None
        details["inspection_error"] = str(exc)
    if inspected_state is not None:
        page_state, page_artifacts = inspected_state
        inspected_artifacts.extend(page_artifacts)
        if page_state.url is not None:
            details["current_url"] = page_state.url
        if page_state.title is not None:
            details["page_title"] = page_state.title
        if page_state.detected_indicators:
            details["detected_indicators"] = page_state.detected_indicators
        if page_state.handoff_required:
            details["handoff_reason"] = page_state.handoff_reason
            error = ActionError(
                code=ErrorCode.HUMAN_HANDOFF_REQUIRED,
                message=page_state.handoff_reason or "Human intervention required.",
                details={"detected_indicators": page_state.detected_indicators},
            )

    failure_artifacts = _capture_failure_artifacts(step, workflow, backend, secrets_context)
    inspected_artifacts.extend(failure_artifacts)
    sanitized_error = _sanitize_error(error, secrets_context)
    return sanitized_error, inspected_artifacts, details


def _inspect_page(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
) -> tuple[PageState, list[ArtifactRef]] | None:
    if not hasattr(backend, "inspect_page"):
        return None
    inspected = backend.inspect_page(
        ActionOptions(
            timeout_ms=step.timeout_ms or workflow.settings.timeout_ms,
            step_id=step.id,
        )
    )
    if not inspected.ok:
        return None
    page_state = inspected.value if isinstance(inspected.value, PageState) else None
    if page_state is None:
        return None
    artifacts: list[ArtifactRef] = []
    if page_state.html is not None:
        label = step.id or "page-state"
        artifacts.append(
            ArtifactRef(
                path=f"inline://{label}-page.html",
                kind="html",
                label=f"{label}-page",
                contents=page_state.html,
                mime_type="text/html",
            )
        )
    return page_state, artifacts


def _details_with_branch_path(details: dict[str, Any], branch_path: list[str]) -> dict[str, Any]:
    merged = dict(details)
    if branch_path:
        merged["branch_path"] = branch_path
    return merged


def _interpolate(
    value: str,
    vars_context: dict[str, Any],
    secrets_context: dict[str, Any],
    outputs_context: dict[str, Any],
) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        namespace, _, key = token.partition(".")
        namespace_map = {
            "vars": vars_context,
            "secrets": secrets_context,
            "outputs": outputs_context,
        }.get(namespace)
        if namespace_map is None:
            raise ExecutionFailure(
                ActionError(
                    code=ErrorCode.INTERPOLATION_ERROR,
                    message=f"Unsupported namespace {namespace!r}.",
                )
            )
        if key not in namespace_map:
            raise ExecutionFailure(
                ActionError(
                    code=ErrorCode.INTERPOLATION_ERROR,
                    message=f"Missing interpolation value for {token!r}.",
                )
            )
        return str(namespace_map[key])

    return _INTERPOLATION_TOKEN.sub(replace, value)


def _sanitize_interpolated_value(raw_value: str, resolved_value: str) -> str:
    if "${secrets." in raw_value:
        return _SECRET_REDACTION
    return resolved_value


def _sanitize_error(error: ActionError | None, secrets_context: dict[str, Any]) -> ActionError:
    if error is None:
        return ActionError(code=ErrorCode.PROTOCOL_ERROR, message="Unknown execution error.")
    message = error.message
    secret_values = sorted(
        {str(value) for value in secrets_context.values() if value not in (None, "")},
        key=len,
        reverse=True,
    )
    for secret_value in secret_values:
        message = message.replace(secret_value, _SECRET_REDACTION)
    return ActionError(code=error.code, message=message, details=error.details)


def _capture_failure_artifacts(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    secrets_context: dict[str, Any],
) -> list[ArtifactRef]:
    if not workflow.settings.screenshot_on_failure:
        return []

    screenshot = backend.screenshot(
        ScreenshotOptions(
            timeout_ms=step.timeout_ms or workflow.settings.timeout_ms,
            step_id=step.id,
        )
    )
    if screenshot.ok:
        if screenshot.artifacts:
            return screenshot.artifacts
        if isinstance(screenshot.value, ArtifactRef):
            return [screenshot.value]
        return []

    sanitized_error = _sanitize_error(screenshot.error, secrets_context)
    return [
        ArtifactRef(
            path="inline://failure-screenshot-error.txt",
            kind="error",
            label="failure-screenshot-error",
            contents=sanitized_error.message,
            mime_type="text/plain",
        )
    ]


def _enforce_allowed_domain(url: str, workflow: WorkflowDocument) -> None:
    allowed_domains = workflow.settings.allowed_domains
    if not allowed_domains:
        return

    host = urlparse(url).hostname
    if host is None:
        raise ExecutionFailure(
            ActionError(
                code=ErrorCode.POLICY_VIOLATION,
                message=f"URL {url!r} does not contain a valid hostname.",
            )
        )

    if host in allowed_domains:
        return

    raise ExecutionFailure(
        ActionError(
            code=ErrorCode.POLICY_VIOLATION,
            message=f"Navigation to host {host!r} is not permitted.",
        )
    )
