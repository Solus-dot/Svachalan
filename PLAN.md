# PLAN.md — Browser Automation Backend v1 Implementation Plan

## Summary

Build the project as a **Python** codebase with a **CLI plus library** interface and **local filesystem** artifact storage. The first goal is a thin but complete vertical slice: attach to an existing Chromium target over CDP, execute a validated YAML workflow, and emit a stable JSON run report with secret redaction and typed errors.

Implementation should proceed in phases that establish the contracts first, then the backend transport, then the runtime, then reporting and hardening. Because the repo currently contains only `TechSpec.md`, the first milestone includes bootstrapping the package structure and developer tooling.

## Key Implementation Changes

### 1. Bootstrap the repository and core contracts

* Create a Python package with a library entrypoint and a CLI entrypoint.
* Standardize on Python 3.12+, `uv`, `pytest`, and a `src/` layout.
* Establish the initial package structure around:
  * `src/svachalan/contracts` for shared models and error types
  * `src/svachalan/backend` for CDP transport and Chromium actions
  * `src/svachalan/runtime` for workflow execution
  * `src/svachalan/reporting` for artifacts and JSON reports
  * `src/svachalan/cli` for the workflow runner command
* Define the v1 public models first so implementation is anchored to stable contracts.

### 2. Implement workflow schema, parsing, and validation

* Add YAML loading plus schema validation for top-level fields, action-specific required fields, `frame_selector`, retry restrictions, duplicate `save_as`, and namespaced interpolation syntax.
* Normalize workflows into an internal representation before execution.
* Keep `vars` and `secrets` immutable and reserve `outputs` for runtime writes only.
* Validate unsupported scope early.

### 3. Build the CDP transport and backend primitives

* Implement attach-only CDP connection logic to an existing Chromium websocket endpoint or target.
* Build a page/session abstraction limited to one active page, the main frame plus one same-origin iframe hop, and fail-fast on popup/new-tab creation or unexpected target closure.
* Implement backend methods in this order: `goto`, `wait_for`, `assert_exists`, `extract_text`, `extract_attr`, `click`, `type`, `screenshot`.
* Lock backend semantics immediately around CSS selectors, unique matches, interactability, `domcontentloaded`, and `unsupported_scope`.

### 4. Implement the workflow runtime and execution engine

* Build a sequential runtime that interpolates, validates policy, executes actions, sanitizes results, records step results, writes `outputs`, and stops on failure.
* Implement runtime policy enforcement for supported navigation paths.
* Implement retry handling only for read-only actions.
* Keep runtime concerns separate from backend concerns.

### 5. Add reporting, artifacts, and CLI usability

* Create a local run-output directory layout for each execution.
* Emit a stable JSON run report.
* Enforce redaction centrally.
* Implement a CLI command that loads a workflow, accepts existing CDP target details, executes the workflow, exits non-zero on failure, and prints the report path or concise JSON summary.

### 6. Harden with focused tests before expanding scope

* Finish the MVP only after the full vertical slice works end to end.
* Do not add multi-tab, popup handling beyond fail-fast detection, browser launch, or broader automation ergonomics before the v1 contracts are stable.
* Expand only after core semantics are stable.

## Public APIs and Interfaces

* Library surface:
  * `parse_workflow(source: str) -> WorkflowDocument`
  * `validate_workflow(doc: WorkflowDocument) -> ValidationResult`
  * `create_backend(config: BackendConfig) -> AutomationBackend`
  * `run_workflow(workflow: WorkflowDocument, backend: AutomationBackend, options: RunOptions) -> RunReport`
* CLI surface:
  * one command to execute a workflow against an existing CDP target
  * explicit flags for workflow path, CDP endpoint/target, output directory, and optional secret injection
* Data contracts:
  * versioned JSON run report schema
  * typed error object using the v1 `ErrorCode` taxonomy
  * sanitized step input representation rather than raw resolved inputs

## Test Plan

* Parser/validator tests for missing fields, bad namespaces, duplicate `save_as`, invalid retries, and frame configuration.
* Backend tests for target attachment, frame resolution, selector errors, interactability, and unsupported scope.
* Runtime tests for interpolation, policy enforcement, retries, fail-fast behavior, and screenshot-on-failure behavior.
* Reporting tests for redaction and stable typed report output.
* End-to-end tests for a minimal login flow, a same-origin iframe extraction flow, and CLI/report behavior.

## Assumptions and Defaults

* Implementation stack: Python
* Tooling: `uv`
* Test framework: `pytest`
* Primary entrypoint: CLI plus library
* Artifact storage: local filesystem
* Browser lifecycle: attach-only
* Runtime scope: one active page with explicit same-origin iframe targeting only
* Initial implementation prioritizes the end-to-end vertical slice over extra abstractions
