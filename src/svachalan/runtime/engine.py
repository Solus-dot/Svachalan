from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from svachalan.contracts.backend import (
    ActionOptions,
    ActionResult,
    ArtifactRef,
    AutomationBackend,
    ElementTarget,
    NavigationOptions,
    ScreenshotOptions,
    TypeOptions,
)
from svachalan.contracts.errors import ActionError, ErrorCode, ExecutionFailure
from svachalan.contracts.run import RunOptions, RunReport, RunStatus, StepResult, StepStatus
from svachalan.contracts.workflow import READ_ONLY_ACTIONS, WorkflowDocument, WorkflowStep
from svachalan.reporting.store import ReportStore
from svachalan.runtime.parser import ensure_valid_workflow

_INTERPOLATION_TOKEN = re.compile(r"\$\{([^}]+)\}")
_SECRET_REDACTION = "[REDACTED]"


def run_workflow(
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    options: RunOptions | None = None,
) -> RunReport:
    options = options or RunOptions()
    workflow = ensure_valid_workflow(workflow)

    started_at = datetime.now(UTC)
    vars_context = {**workflow.vars, **options.vars}
    secrets_context = {**workflow.secrets, **options.secrets}
    outputs: dict[str, Any] = {}
    step_results: list[StepResult] = []
    artifacts: list[ArtifactRef] = []
    final_error: ActionError | None = None

    for index, step in enumerate(workflow.steps):
        step_started = time.perf_counter()
        attempts = 0
        try:
            sanitized_inputs, resolved_inputs = _resolve_step_inputs(
                step,
                vars_context,
                secrets_context,
                outputs,
            )
        except ExecutionFailure as exc:
            result = ActionResult.failure(exc.error)
            duration_ms = int((time.perf_counter() - step_started) * 1000)
            final_error = _sanitize_error(result.error, secrets_context)
            failure_artifacts = _capture_failure_artifacts(step, workflow, backend, secrets_context)
            artifacts.extend(failure_artifacts)
            step_results.append(
                StepResult(
                    step_index=index,
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.FAILED,
                    duration_ms=duration_ms,
                    attempts=attempts,
                    sanitized_inputs={},
                    error=final_error,
                    artifacts=failure_artifacts,
                )
            )
            break

        while True:
            attempts += 1
            try:
                result = _dispatch_step(step, resolved_inputs, workflow, backend)
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
        artifacts.extend(step_artifacts)

        if result.ok:
            if step.save_as:
                outputs[step.save_as] = result.value
            step_results.append(
                StepResult(
                    step_index=index,
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.SUCCEEDED,
                    duration_ms=duration_ms,
                    attempts=attempts,
                    sanitized_inputs=sanitized_inputs,
                    output=result.value,
                    artifacts=step_artifacts,
                )
            )
            continue

        final_error = _sanitize_error(result.error, secrets_context)
        failure_artifacts = _capture_failure_artifacts(step, workflow, backend, secrets_context)
        step_artifacts.extend(failure_artifacts)
        artifacts.extend(failure_artifacts)
        step_results.append(
            StepResult(
                step_index=index,
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                duration_ms=duration_ms,
                attempts=attempts,
                sanitized_inputs=sanitized_inputs,
                error=final_error,
                artifacts=step_artifacts,
            )
        )
        break

    finished_at = datetime.now(UTC)
    report = RunReport(
        workflow_version=workflow.version,
        status=RunStatus.FAILED if final_error else RunStatus.SUCCEEDED,
        started_at=started_at,
        finished_at=finished_at,
        browser_session_mode=options.browser_session_mode,
        input_summary={
            "vars": vars_context,
            "secret_keys": sorted(secrets_context.keys()),
        },
        outputs=outputs,
        steps=step_results,
        artifacts=artifacts,
        error=final_error,
    )

    if options.output_dir:
        store = ReportStore(options.output_dir)
        report = store.write(report, run_id=options.run_id)

    return report


def _collect_artifacts(result: ActionResult) -> list[ArtifactRef]:
    artifacts = list(result.artifacts)
    if isinstance(result.value, ArtifactRef):
        artifacts.append(result.value)
    return artifacts


def _dispatch_step(
    step: WorkflowStep,
    resolved_inputs: dict[str, Any],
    workflow: WorkflowDocument,
    backend: AutomationBackend,
) -> ActionResult:
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

    target = ElementTarget(
        selector=resolved_inputs["selector"],
        frame_selector=resolved_inputs.get("frame_selector"),
    )
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

    return ActionResult.failure(
        ActionError(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"Unsupported action {step.action!r}.",
        )
    )


def _resolve_step_inputs(
    step: WorkflowStep,
    vars_context: Mapping[str, Any],
    secrets_context: Mapping[str, Any],
    outputs_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    sanitized_inputs: dict[str, Any] = {}
    resolved_inputs: dict[str, Any] = {}

    for field_name in ("selector", "frame_selector", "text", "url", "attr"):
        raw_value = getattr(step, field_name)
        if raw_value is None:
            continue
        resolved_value = _interpolate(raw_value, vars_context, secrets_context, outputs_context)
        resolved_inputs[field_name] = resolved_value
        sanitized_inputs[field_name] = _sanitize_interpolated_value(raw_value, resolved_value)

    return sanitized_inputs, resolved_inputs


def _interpolate(
    value: str,
    vars_context: Mapping[str, Any],
    secrets_context: Mapping[str, Any],
    outputs_context: Mapping[str, Any],
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


def _sanitize_error(error: ActionError | None, secrets_context: Mapping[str, Any]) -> ActionError:
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
    return ActionError(code=error.code, message=message)


def _capture_failure_artifacts(
    step: WorkflowStep,
    workflow: WorkflowDocument,
    backend: AutomationBackend,
    secrets_context: Mapping[str, Any],
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
            path="inline://failure-screenshot-error",
            kind="error",
            label=sanitized_error.message,
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
