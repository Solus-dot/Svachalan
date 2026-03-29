from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from svachalan.contracts.backend import ArtifactRef
from svachalan.contracts.run import RunReport


class ReportStore:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)

    def write(self, report: RunReport, *, run_id: str | None = None) -> RunReport:
        resolved_run_id = run_id or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
        run_dir = self.output_root / resolved_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        cache: dict[tuple[str, str | None], ArtifactRef] = {}
        materialized_steps = []
        for step in report.steps:
            materialized_artifacts = [
                self._materialize_artifact(artifact, artifacts_dir, cache)
                for artifact in step.artifacts
            ]
            materialized_output = step.output
            if isinstance(step.output, ArtifactRef):
                materialized_output = self._materialize_artifact(step.output, artifacts_dir, cache)
            materialized_steps.append(
                step.model_copy(
                    update={
                        "artifacts": materialized_artifacts,
                        "output": materialized_output,
                    }
                )
            )

        materialized_report_artifacts = [
            self._materialize_artifact(artifact, artifacts_dir, cache)
            for artifact in report.artifacts
        ]

        report_path = run_dir / "report.json"
        report_with_path = report.model_copy(
            update={
                "steps": materialized_steps,
                "artifacts": materialized_report_artifacts,
                "report_path": str(report_path),
            }
        )
        report_path.write_text(report_with_path.model_dump_json(indent=2), encoding="utf-8")
        return report_with_path

    def _materialize_artifact(
        self,
        artifact: ArtifactRef,
        artifacts_dir: Path,
        cache: dict[tuple[str, str | None], ArtifactRef],
    ) -> ArtifactRef:
        key = (artifact.path, artifact.contents)
        if key in cache:
            return cache[key]

        target_name = self._artifact_filename(artifact)
        target_path = self._unique_path(artifacts_dir / target_name)

        if artifact.contents is not None:
            target_path.write_text(artifact.contents, encoding="utf-8")
        else:
            source_path = Path(artifact.path)
            if source_path.exists():
                shutil.copy2(source_path, target_path)
            else:
                cache[key] = artifact
                return artifact

        materialized = artifact.model_copy(
            update={
                "path": str(target_path),
                "contents": None,
            }
        )
        cache[key] = materialized
        return materialized

    def _artifact_filename(self, artifact: ArtifactRef) -> str:
        if artifact.path.startswith("inline://"):
            name = artifact.path.removeprefix("inline://")
            if name:
                return name
        source_name = Path(artifact.path).name
        if source_name:
            return source_name
        label = artifact.label or "artifact"
        return f"{label}.txt"

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        index = 1
        while True:
            candidate = path.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
            index += 1
