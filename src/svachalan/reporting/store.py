from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from svachalan.contracts.run import RunReport


class ReportStore:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)

    def write(self, report: RunReport, *, run_id: str | None = None) -> RunReport:
        resolved_run_id = run_id or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
        run_dir = self.output_root / resolved_run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        report_path = run_dir / "report.json"
        report_with_path = report.model_copy(update={"report_path": str(report_path)})
        report_path.write_text(report_with_path.model_dump_json(indent=2), encoding="utf-8")
        return report_with_path

