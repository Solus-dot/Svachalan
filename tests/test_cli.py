from __future__ import annotations

import json
from pathlib import Path

from svachalan.cli.main import main


def test_cli_validate_only_succeeds_without_browser_backend(
    tmp_path: Path,
    capsys,
) -> None:
    workflow_path = tmp_path / "workflow.yml"
    workflow_path.write_text(
        """
version: 1
steps:
  - action: goto
    url: "https://example.com"
""".strip(),
        encoding="utf-8",
    )

    exit_code = main([str(workflow_path), "--validate-only"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["status"] == "validated"
    assert captured.err == ""


def test_cli_rejects_malformed_bindings_without_traceback(tmp_path: Path, capsys) -> None:
    workflow_path = tmp_path / "workflow.yml"
    workflow_path.write_text(
        """
version: 1
steps:
  - action: goto
    url: "https://example.com"
""".strip(),
        encoding="utf-8",
    )

    exit_code = main([str(workflow_path), "--validate-only", "--var", "BROKEN"])

    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["code"] == "validation_error"
    assert "expected KEY=VALUE" in payload["message"]
