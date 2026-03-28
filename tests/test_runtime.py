from __future__ import annotations

from collections import deque

from svachalan import parse_workflow, run_workflow
from svachalan.contracts import (
    ActionError,
    ActionResult,
    ArtifactRef,
    ElementMatch,
    ElementTarget,
    ErrorCode,
    RunOptions,
)


class FakeBackend:
    def __init__(self) -> None:
        self.extract_text_results: deque[ActionResult] = deque()
        self.click_calls: list[ElementTarget] = []
        self.type_calls: list[tuple[ElementTarget, str]] = []
        self.wait_for_results: deque[ActionResult] = deque()
        self.screenshot_calls = 0

    def goto(self, url: str, opts=None) -> ActionResult:
        return ActionResult.success()

    def click(self, target: ElementTarget, opts=None) -> ActionResult:
        self.click_calls.append(target)
        return ActionResult.success()

    def type(self, target: ElementTarget, text: str, opts=None) -> ActionResult:
        self.type_calls.append((target, text))
        return ActionResult.success()

    def wait_for(self, target: ElementTarget, opts=None) -> ActionResult:
        if self.wait_for_results:
            return self.wait_for_results.popleft()
        return ActionResult.success()

    def assert_exists(self, target: ElementTarget, opts=None) -> ActionResult:
        return ActionResult.success()

    def extract_text(self, target: ElementTarget, opts=None) -> ActionResult:
        if self.extract_text_results:
            return self.extract_text_results.popleft()
        return ActionResult.success("default-value")

    def extract_attr(self, target: ElementTarget, attr: str, opts=None) -> ActionResult:
        return ActionResult.success("attr-value")

    def screenshot(self, opts=None) -> ActionResult:
        self.screenshot_calls += 1
        artifact = ArtifactRef(path=f"/tmp/failure-{self.screenshot_calls}.png", label="failure")
        return ActionResult.success(value=artifact)


def test_run_workflow_interpolates_outputs_and_redacts_secrets() -> None:
    workflow = parse_workflow(
        """
version: 1
settings:
  allowed_domains: ["example.com"]
secrets:
  password: super-secret
steps:
  - id: open
    action: goto
    url: "https://example.com/login"
  - id: password
    action: type
    selector: "#password"
    text: "${secrets.password}"
  - id: balance
    action: extract_text
    selector: ".balance"
    save_as: balance
  - id: mirror
    action: type
    selector: "#mirror"
    text: "${outputs.balance}"
"""
    )
    backend = FakeBackend()
    backend.extract_text_results.append(ActionResult.success("123.00"))

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert report.outputs == {"balance": "123.00"}
    assert report.steps[1].sanitized_inputs["text"] == "[REDACTED]"
    assert backend.type_calls[0][1] == "super-secret"
    assert backend.type_calls[1][1] == "123.00"


def test_run_workflow_retries_read_only_steps_and_captures_failure_screenshot() -> None:
    workflow = parse_workflow(
        """
version: 1
settings:
  screenshot_on_failure: true
steps:
  - id: wait
    action: wait_for
    selector: ".missing"
    retry_count: 1
"""
    )
    backend = FakeBackend()
    backend.wait_for_results.extend(
        [
            ActionResult.failure(
                ActionError(
                    code=ErrorCode.TIMEOUT,
                    message="waited on super-secret for too long",
                )
            ),
            ActionResult.failure(
                ActionError(
                    code=ErrorCode.TIMEOUT,
                    message="waited on super-secret for too long",
                )
            ),
        ]
    )

    report = run_workflow(workflow, backend, RunOptions(secrets={"token": "super-secret"}))

    assert report.status.value == "failed"
    assert report.steps[0].attempts == 2
    assert report.error is not None
    assert "[REDACTED]" in report.error.message
    assert "super-secret" not in report.error.message
    assert backend.screenshot_calls == 1
    assert report.steps[0].artifacts[0].path.endswith(".png")


def test_run_workflow_preserves_successful_screenshot_artifacts() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - id: shot
    action: screenshot
"""
    )
    backend = FakeBackend()

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert report.steps[0].artifacts[0].path.endswith(".png")
    assert report.artifacts[0].path.endswith(".png")


def test_run_workflow_passes_fallback_selectors_and_match_to_backend() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - id: click-primary
    action: click
    selectors:
      - ".missing-button"
      - "button.primary"
    match: first_visible
"""
    )
    backend = FakeBackend()

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert len(backend.click_calls) == 1
    assert backend.click_calls[0].selector is None
    assert backend.click_calls[0].selectors == [".missing-button", "button.primary"]
    assert backend.click_calls[0].match == ElementMatch.FIRST_VISIBLE
