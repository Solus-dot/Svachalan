import json
from pathlib import Path

from svachalan import parse_workflow, run_workflow
from svachalan.contracts import (
    ActionError,
    ActionResult,
    ArtifactRef,
    ElementTarget,
    ErrorCode,
    PageState,
    RunOptions,
)


class ReportingBackend:
    def goto(self, url: str, opts=None) -> ActionResult:
        return ActionResult.success()

    def click(self, target: ElementTarget, opts=None) -> ActionResult:
        return ActionResult.success()

    def type(self, target: ElementTarget, text: str, opts=None) -> ActionResult:
        return ActionResult.success()

    def wait_for(self, target: ElementTarget, opts=None) -> ActionResult:
        return ActionResult.success()

    def assert_exists(self, target: ElementTarget, opts=None) -> ActionResult:
        return ActionResult.success()

    def extract_text(self, target: ElementTarget, opts=None) -> ActionResult:
        return ActionResult.success("ok")

    def extract_attr(self, target: ElementTarget, attr: str, opts=None) -> ActionResult:
        return ActionResult.success("ok")

    def screenshot(self, opts=None) -> ActionResult:
        return ActionResult.success()

    def inspect_page(self, opts=None) -> ActionResult:
        return ActionResult.success(
            PageState(
                url="https://example.com",
                title="Example",
                html="<html><body>Example</body></html>",
            )
        )


def test_run_workflow_writes_stable_report_json(tmp_path: Path) -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: goto
    url: "https://example.com"
"""
    )

    report = run_workflow(
        workflow,
        ReportingBackend(),
        RunOptions(output_dir=str(tmp_path), run_id="test-run"),
    )

    report_path = tmp_path / "test-run" / "report.json"

    assert report.report_path == str(report_path)
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "succeeded"


def test_report_store_materializes_inline_and_file_artifacts(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "temp-shot.png"
    screenshot_path.write_bytes(b"png")
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

    class FailingReportingBackend(ReportingBackend):
        def wait_for(self, target: ElementTarget, opts=None) -> ActionResult:
            return ActionResult.failure(
                ActionError(
                    code=ErrorCode.TIMEOUT,
                    message="timeout",
                )
            )

        def screenshot(self, opts=None) -> ActionResult:
            return ActionResult.success(
                value=ArtifactRef(
                    path=str(screenshot_path),
                    label="failure-shot",
                )
            )

    run_workflow(
        workflow,
        FailingReportingBackend(),
        RunOptions(output_dir=str(tmp_path), run_id="materialized"),
    )

    report_data = json.loads(
        (tmp_path / "materialized" / "report.json").read_text(encoding="utf-8")
    )
    artifact_paths = [artifact["path"] for artifact in report_data["artifacts"]]

    assert any("artifacts" in path for path in artifact_paths)
    assert any(Path(path).exists() for path in artifact_paths)
