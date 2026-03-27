# Svachalan

Svachalan is a deterministic browser automation runner for low-level Chromium workflows.

The project is structured around three layers:

- a Chromium-first backend over CDP
- a sequential workflow runtime
- a compact YAML DSL for auditable browser actions

## Status

The current implementation provides:

- workflow parsing and semantic validation
- namespaced interpolation for `vars`, `secrets`, and `outputs`
- a sequential runtime with retries for read-only actions
- failure redaction and stable JSON report writing
- a managed Chromium launch path and attach-mode session resolution
- a Chromium backend for navigation, DOM actions, extraction, waits, and screenshots
- a CLI execution path
- a Python library surface for parsing, validating, and running workflows against a backend

Browser execution requires a locally installed Chromium-based browser. If Chrome or Chromium is not available, the CLI returns a structured `protocol_error`.

## Requirements

- Python `3.12+`
- [`uv`](https://docs.astral.sh/uv/)

## Install

Create the environment and install dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run pytest -q
```

Run lint:

```bash
uv run ruff check
```

## Workflow Format

Example workflow:

```yaml
version: 1

settings:
  timeout_ms: 10000
  allowed_domains: ["example.com"]
  screenshot_on_failure: true
  goto_wait_until: domcontentloaded

vars:
  email: "user@example.com"

secrets:
  password: "super-secret"

steps:
  - id: open-login
    action: goto
    url: "https://example.com/login"

  - id: type-email
    action: type
    selector: "#email"
    text: "${vars.email}"

  - id: type-password
    action: type
    selector: "#password"
    text: "${secrets.password}"

  - id: submit
    action: click
    selector: "button[type=submit]"

  - id: wait-dashboard
    action: wait_for
    selector: ".dashboard"
    retry_count: 1

  - id: balance
    action: extract_text
    selector: ".account-balance"
    save_as: balance
```

Supported actions in v1:

- `goto`
- `click`
- `type`
- `wait_for`
- `extract_text`
- `extract_attr`
- `assert_exists`
- `screenshot`

Interpolation must use explicit namespaces:

- `${vars.email}`
- `${secrets.password}`
- `${outputs.balance}`

## CLI Usage

Validate a workflow:

```bash
uv run svachalan workflow.yml --validate-only
```

Example output:

```json
{
  "status": "validated",
  "workflow_version": 1,
  "step_count": 6
}
```

Inject variables and secrets during validation:

```bash
uv run svachalan workflow.yml \
  --validate-only \
  --var email=user@example.com \
  --secret password=super-secret
```

Show CLI help:

```bash
uv run svachalan --help
```

Notes:

- malformed `--var` or `--secret` bindings return a structured validation error
- when a local Chromium-based browser is available, the CLI can execute workflows directly

Run a workflow in headless mode:

```bash
uv run svachalan workflow.yml --headless --output-dir runs
```

Attach to an existing CDP endpoint:

```bash
uv run svachalan workflow.yml \
  --attach-endpoint http://127.0.0.1:9222 \
  --output-dir runs
```

## Library Usage

Parse and validate a workflow:

```python
from pathlib import Path

from svachalan import parse_workflow, validate_workflow

source = Path("workflow.yml").read_text(encoding="utf-8")
workflow = parse_workflow(source)
validation = validate_workflow(workflow)

if not validation.ok:
    for issue in validation.issues:
        print(issue.path, issue.message)
```

Run a workflow against a provided backend implementation:

```python
from svachalan import parse_workflow, run_workflow
from svachalan.contracts import ActionResult, RunOptions


class MyBackend:
    def goto(self, url, opts=None):
        return ActionResult.success()

    def click(self, target, opts=None):
        return ActionResult.success()

    def type(self, target, text, opts=None):
        return ActionResult.success()

    def wait_for(self, target, opts=None):
        return ActionResult.success()

    def assert_exists(self, target, opts=None):
        return ActionResult.success()

    def extract_text(self, target, opts=None):
        return ActionResult.success("value")

    def extract_attr(self, target, attr, opts=None):
        return ActionResult.success("value")

    def screenshot(self, opts=None):
        return ActionResult.success()


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
    MyBackend(),
    RunOptions(output_dir="runs"),
)

print(report.status)
print(report.report_path)
```

## Reports

When `RunOptions.output_dir` is set, Svachalan writes:

- a run directory under the given output root
- a `report.json` file with run status, sanitized inputs, outputs, step results, artifacts, and final error details

Secret values are never emitted into the final report. The report stores secret keys only.

## Current Limitations

- no multi-tab or popup workflows
- no cross-origin iframe support
- no high-level agentic commands

## Repository Layout

- [src/svachalan/contracts](/Users/sohom/Desktop/Svachalan/src/svachalan/contracts) shared models and error types
- [src/svachalan/runtime](/Users/sohom/Desktop/Svachalan/src/svachalan/runtime) parser, validation, and execution engine
- [src/svachalan/reporting](/Users/sohom/Desktop/Svachalan/src/svachalan/reporting) report storage
- [src/svachalan/cli](/Users/sohom/Desktop/Svachalan/src/svachalan/cli) command-line interface
- [src/svachalan/browser](/Users/sohom/Desktop/Svachalan/src/svachalan/browser) browser lifecycle and session startup
- [src/svachalan/backend](/Users/sohom/Desktop/Svachalan/src/svachalan/backend) Chromium backend and backend factory

## Next Step

The next major hardening slice is popup/new-tab fail-fast detection, stronger policy enforcement around post-click navigations, and broader backend test coverage on real browsers.
