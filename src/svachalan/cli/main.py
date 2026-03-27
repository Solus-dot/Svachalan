from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from svachalan import (
    create_backend,
    parse_workflow,
    run_workflow,
    start_browser_session,
    validate_workflow,
)
from svachalan.contracts import (
    AttachOptions,
    BackendConfig,
    BrowserSessionMode,
    BrowserSessionOptions,
    LaunchOptions,
    RunOptions,
    WorkflowValidationError,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    workflow_path = Path(args.workflow)
    try:
        workflow = parse_workflow(workflow_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            json.dumps(
                {"code": "validation_error", "message": "Workflow file not found."}
            ),
            file=sys.stderr,
        )
        return 2
    except WorkflowValidationError as exc:
        issues = [issue.model_dump() for issue in exc.issues]
        print(
            json.dumps({"code": "validation_error", "issues": issues}, indent=2),
            file=sys.stderr,
        )
        return 2

    validation = validate_workflow(workflow)
    if not validation.ok:
        issues = [issue.model_dump() for issue in validation.issues]
        print(
            json.dumps({"code": "validation_error", "issues": issues}, indent=2),
            file=sys.stderr,
        )
        return 2

    try:
        vars_bindings = _parse_bindings(args.var)
        secret_bindings = _parse_bindings(args.secret)
    except ValueError as exc:
        print(
            json.dumps({"code": "validation_error", "message": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 2

    if args.validate_only:
        print(
            json.dumps(
                {
                    "status": "validated",
                    "workflow_version": workflow.version,
                    "step_count": len(workflow.steps),
                },
                indent=2,
            )
        )
        return 0

    session_options = _build_session_options(args)
    run_options = RunOptions(
        vars=vars_bindings,
        secrets=secret_bindings,
        output_dir=args.output_dir,
        browser_session_mode=session_options.mode,
    )

    session = None
    backend = None
    try:
        session = start_browser_session(session_options)
        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, run_options)
    except NotImplementedError as exc:
        print(
            json.dumps({"code": "protocol_error", "message": str(exc)}),
            file=sys.stderr,
        )
        return 1
    except (FileNotFoundError, TimeoutError, ValueError) as exc:
        print(
            json.dumps({"code": "protocol_error", "message": str(exc)}),
            file=sys.stderr,
        )
        return 1
    finally:
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.cleanup()
            except Exception:
                pass

    payload = {"status": report.status.value}
    if report.report_path:
        payload["report_path"] = report.report_path
    else:
        payload["report"] = report.model_dump(mode="json")
    print(json.dumps(payload, indent=2))
    return 0 if report.status.value == "succeeded" else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svachalan")
    parser.add_argument("workflow", help="Path to the workflow YAML file.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the workflow and exit without starting a browser session.",
    )
    parser.add_argument(
        "--output-dir",
        default="runs",
        help="Directory for run artifacts and report output.",
    )
    parser.add_argument("--browser-path", help="Explicit Chromium or Chrome executable path.")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the managed browser in headless mode.",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Keep the managed browser open after the run finishes.",
    )
    parser.add_argument(
        "--attach-endpoint",
        help="Attach to an existing CDP websocket or HTTP endpoint.",
    )
    parser.add_argument("--attach-target", help="Optional CDP target id to attach to.")
    parser.add_argument("--secret", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    return parser


def _build_session_options(args: argparse.Namespace) -> BrowserSessionOptions:
    if args.attach_endpoint:
        return BrowserSessionOptions(
            mode=BrowserSessionMode.ATTACH,
            attach=AttachOptions(
                endpoint=args.attach_endpoint,
                target_id=args.attach_target,
            ),
        )
    return BrowserSessionOptions(
        mode=BrowserSessionMode.LAUNCH,
        launch=LaunchOptions(
            browser_path=args.browser_path,
            headless=args.headless,
            keep_browser_open=args.keep_browser_open,
        ),
    )


def _parse_bindings(bindings: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for binding in bindings:
        key, separator, value = binding.partition("=")
        if separator == "" or key == "":
            raise ValueError(f"Invalid binding {binding!r}; expected KEY=VALUE.")
        parsed[key] = value
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
