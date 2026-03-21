# Browser Automation Backend — Tech Spec

## Overview

This project builds a browser automation stack from first principles with three layers:

* a **Chromium-first automation backend** built directly over CDP
* a **workflow runtime** that executes browser actions step by step
* a **compact YAML DSL** for describing low-level automation flows

The system is intended to be deterministic, auditable, and suitable as an execution substrate for AI agents.

## Goals

### In scope for v1

* Connect to and control Chromium-based browsers via CDP.
* Execute low-level browser actions such as navigation, click, type, wait, extract, and screenshot.
* Run a compact YAML workflow format with strict validation.
* Support runtime variables and extracted outputs.
* Produce structured logs, step results, and failure artifacts.
* Enforce basic policy such as allowed domains and default timeouts.

### Out of scope for v1

* Firefox or WebKit support.
* High-level agentic commands.
* Loops, macros, functions, or arbitrary scripting in the DSL.
* Vision-first automation.
* Anti-bot evasion.

## Architecture

The system has five core components:

1. **YAML parser and validator**

   * parses workflow files
   * validates schema and action fields
   * normalizes workflows into an internal representation

2. **Workflow runtime**

   * executes steps in order
   * manages variables, outputs, and step state
   * applies timeout and failure rules

3. **Action dispatcher**

   * maps workflow actions to backend primitives
   * returns structured step results

4. **Chromium backend**

   * implements browser control over CDP
   * owns navigation, DOM lookup, JS evaluation, input dispatch, and screenshots

5. **Artifact and reporting layer**

   * stores logs, screenshots, and final run reports

Conceptually:

```text
YAML workflow → parser/validator → runtime → action dispatcher → Chromium backend → browser
```

## Workflow Schema

The DSL is intentionally compact and low-level.

### Top-level structure

```yaml
version: 1

settings:
  timeout_ms: 10000
  allowed_domains: ["example.com"]
  screenshot_on_failure: true

vars:
  email: "user@example.com"
  password: "${SECRET_PASSWORD}"

steps:
  - action: goto
    url: "https://example.com/login"

  - action: type
    selector: "#email"
    text: "${email}"

  - action: type
    selector: "#password"
    text: "${password}"

  - action: click
    selector: "button[type=submit]"

  - action: wait_for
    selector: ".dashboard"

  - action: extract_text
    selector: ".account-balance"
    save_as: balance
```

### Top-level fields

* `version` required
* `settings` optional
* `vars` optional
* `steps` required

### Common step fields

* `action` required
* `id` optional
* `timeout_ms` optional
* `retry_count` optional

### Initial action set

* `goto`
* `click`
* `type`
* `wait_for`
* `extract_text`
* `extract_attr`
* `assert_exists`
* `screenshot`

### Schema decisions for v1

* steps are a flat ordered list
* actions use a uniform `action` field
* target resolution uses plain `selector` fields
* interpolation uses only simple `${var}` syntax
* extracted values are stored in the same runtime variable namespace
* workflow execution is fail-fast by default

## Execution Model

A workflow is executed sequentially from the first step to the last.

For each step, the runtime:

1. resolves variable interpolation
2. applies step-level timeout or retry settings
3. dispatches the action to the backend
4. records the step result
5. updates runtime variables if the step produces output
6. stops the run on failure

### Runtime state

The runtime maintains:

* current step index
* run status
* variable store
* extracted outputs
* step logs
* artifact references
* final error state if failed

### Failure behavior

v1 uses fail-fast execution.

A step fails if:

* a required selector is not found
* a timeout is exceeded
* navigation fails
* variable interpolation fails
* the backend returns a protocol or execution error

If enabled in settings, the runtime captures a screenshot on failure.

## Backend Design

The backend is Chromium-first and communicates directly with the browser using CDP.

### Backend responsibilities

* connect to or launch a Chromium instance
* manage page sessions
* navigate to URLs
* resolve DOM elements by CSS selector
* dispatch mouse and keyboard input
* evaluate JavaScript in page context
* extract text and attributes
* wait for selector presence
* capture screenshots

### CDP domains used initially

* `Target`
* `Page`
* `Runtime`
* `DOM`
* `Input`

### Internal backend API

The runtime should call a backend interface rather than raw CDP commands.

Conceptual interface:

```ts
interface AutomationBackend {
  goto(url: string, opts?: ActionOptions): Promise<ActionResult<void>>;
  click(selector: string, opts?: ActionOptions): Promise<ActionResult<void>>;
  type(selector: string, text: string, opts?: TypeOptions): Promise<ActionResult<void>>;
  waitFor(selector: string, opts?: ActionOptions): Promise<ActionResult<void>>;
  extractText(selector: string, opts?: ActionOptions): Promise<ActionResult<string>>;
  extractAttr(selector: string, attr: string, opts?: ActionOptions): Promise<ActionResult<string | null>>;
  screenshot(opts?: ScreenshotOptions): Promise<ActionResult<ArtifactRef>>;
}
```

This keeps the workflow runtime independent of backend implementation details.

## Logging and Reporting

Each step produces a structured result containing:

* step ID or index
* action
* resolved inputs
* duration
* success or failure
* output value if any
* error code and message if failed
* artifact references

Each run produces a final report summarizing:

* workflow metadata
* overall status
* per-step results
* collected variables
* artifacts
* final error if present

## MVP

The MVP is complete when the system can:

* connect to Chromium over CDP
* execute `goto`, `type`, `click`, `wait_for`, and `extract_text`
* support variable interpolation
* fail cleanly with structured errors
* capture screenshots on failure
* emit a final JSON run report

## Open Questions

These decisions should be finalized during implementation:

* whether the system supports both managed browser launch and attach-only mode in v1
* what readiness condition `goto` should wait for by default
* whether backend actions should re-resolve selectors every time or keep element handles internally
* how much iframe support is required in v1

## Recommended Build Order

1. implement the CDP transport and page/session model
2. implement backend primitives: navigate, query, click, type, wait, extract, screenshot
3. implement the workflow parser, validator, and runtime
4. add structured reporting and failure artifacts
5. extend only after the core action semantics are stable

## Summary

The core idea is to treat browser automation as a deterministic execution system rather than a scripting convenience layer. The backend owns browser control, the runtime owns step semantics, and the YAML DSL stays compact and strictly constrained.
