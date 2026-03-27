import json
from pathlib import Path

from svachalan import parse_workflow, run_workflow
from svachalan.contracts import ActionResult, ElementTarget, RunOptions


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
