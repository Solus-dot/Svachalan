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
    PageState,
    RunOptions,
)


class FakeBackend:
    def __init__(self) -> None:
        self.extract_text_results: deque[ActionResult] = deque()
        self.click_calls: list[ElementTarget] = []
        self.type_calls: list[tuple[ElementTarget, str]] = []
        self.wait_for_results: deque[ActionResult] = deque()
        self.assert_exists_results: deque[ActionResult] = deque()
        self.inspect_results: deque[ActionResult] = deque()
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
        if self.assert_exists_results:
            return self.assert_exists_results.popleft()
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

    def inspect_page(self, opts=None) -> ActionResult:
        if self.inspect_results:
            return self.inspect_results.popleft()
        return ActionResult.success(
            PageState(
                url="https://example.com/current",
                title="Current Page",
                html="<html><body>Current Page</body></html>",
            )
        )


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
    assert any(artifact.path.endswith(".png") for artifact in report.steps[0].artifacts)
    assert any(artifact.path.endswith(".html") for artifact in report.steps[0].artifacts)


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


def test_run_workflow_supports_if_exists_branching() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - id: maybe-click
    action: if_exists
    selector: ".banner"
    then:
      - action: click
        selector: ".banner button"
"""
    )
    backend = FakeBackend()
    backend.assert_exists_results.append(
        ActionResult.success(details={"matched_selector": ".banner"})
    )

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert report.steps[0].details["branch_taken"] == "then"
    assert len(backend.click_calls) == 1


def test_run_workflow_supports_one_of_with_url_guard() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - id: choose-state
    action: one_of
    branches:
      - name: cart
        url: "/cart"
        steps:
          - action: extract_text
            selector: "#cart-title"
            save_as: page_name
      - name: default
        default: true
        steps:
          - action: extract_text
            selector: "h1"
            save_as: page_name
"""
    )
    backend = FakeBackend()
    backend.inspect_results.append(
        ActionResult.success(
            PageState(
                url="https://example.com/cart",
                title="Cart",
                html="<html><body>Cart</body></html>",
            )
        )
    )
    backend.extract_text_results.append(ActionResult.success("Cart Page"))

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert report.steps[0].details["selected_branch"] == "cart"
    assert report.outputs["page_name"] == "Cart Page"


def test_run_workflow_supports_url_and_text_assertions() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: wait_for_url_contains
    url: "/dashboard"
  - action: assert_url_contains
    url: "/dashboard"
  - action: assert_text_contains
    selector: "#message"
    text: "Ready"
"""
    )
    backend = FakeBackend()
    backend.inspect_results.extend(
        [
            ActionResult.success(
                PageState(
                    url="https://example.com/dashboard",
                    title="Dashboard",
                    html="<html></html>",
                )
            ),
            ActionResult.success(
                PageState(
                    url="https://example.com/dashboard",
                    title="Dashboard",
                    html="<html></html>",
                )
            ),
        ]
    )
    backend.extract_text_results.append(ActionResult.success("System Ready"))

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "succeeded"
    assert report.steps[0].action == "wait_for_url_contains"
    assert report.steps[1].action == "assert_url_contains"
    assert report.steps[2].action == "assert_text_contains"


def test_run_workflow_marks_handoff_when_page_inspection_detects_challenge() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: wait_for
    selector: ".missing"
"""
    )
    backend = FakeBackend()
    backend.wait_for_results.append(
        ActionResult.failure(
            ActionError(code=ErrorCode.TIMEOUT, message="still waiting")
        )
    )
    backend.inspect_results.append(
        ActionResult.success(
            PageState(
                url="https://example.com/challenge",
                title="Security Check",
                html="<html>Security Check</html>",
                handoff_required=True,
                handoff_reason="Security verification detected.",
                detected_indicators=["security_check"],
            )
        )
    )

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "failed"
    assert report.handoff_required is True
    assert report.error is not None
    assert report.error.code == ErrorCode.HUMAN_HANDOFF_REQUIRED


def test_run_workflow_survives_page_inspection_failures() -> None:
    class FlakyInspectBackend(FakeBackend):
        def inspect_page(self, opts=None) -> ActionResult:
            raise RuntimeError("cdp disconnected")

    workflow = parse_workflow(
        """
version: 1
settings:
  screenshot_on_failure: true
steps:
  - action: wait_for
    selector: ".missing"
"""
    )
    backend = FlakyInspectBackend()
    backend.wait_for_results.append(
        ActionResult.failure(
            ActionError(code=ErrorCode.TIMEOUT, message="still waiting")
        )
    )

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "failed"
    assert report.error is not None
    assert report.error.code == ErrorCode.TIMEOUT
    assert report.steps[0].details["inspection_error"] == "cdp disconnected"


def test_run_workflow_handles_one_of_guard_interpolation_failures() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - id: choose-state
    action: one_of
    branches:
      - name: guarded
        url: "${vars.missing_url_fragment}"
        steps:
          - action: wait_for
            selector: "#ready"
      - name: fallback
        default: true
        steps:
          - action: wait_for
            selector: "#fallback"
"""
    )
    backend = FakeBackend()

    report = run_workflow(workflow, backend, RunOptions())

    assert report.status.value == "failed"
    assert report.error is not None
    assert "Missing interpolation value" in report.error.message
    assert report.steps[0].details["branches_evaluated"][0]["guard_error"].startswith(
        "Missing interpolation value"
    )
