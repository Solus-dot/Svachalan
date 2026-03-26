# Browser Automation Backend — Tech Spec

## Overview

This project builds a browser automation stack from first principles with three layers:

* a **Chromium-first automation backend** built directly over CDP
* a **workflow runtime** that executes browser actions step by step
* a **compact YAML DSL** for describing low-level automation flows

The system is intended to be deterministic, auditable, and suitable as an execution substrate for AI agents. v1 prioritizes explicit contracts and fail-fast behavior over convenience features.

## Goals

### In scope for v1

* Attach to and control an existing Chromium-based browser via CDP.
* Execute low-level browser actions such as navigation, click, type, wait, extract, assert, and screenshot.
* Run a compact YAML workflow format with strict validation.
* Support namespaced runtime inputs, secret inputs, and extracted outputs.
* Produce structured logs, stable step results, and failure artifacts.
* Enforce policy such as allowed domains and default timeouts.
* Support main-frame actions and explicitly targeted same-origin iframe actions.

### Out of scope for v1

* Launching or managing the browser process.
* Firefox or WebKit support.
* Multiple tabs, popups, or multi-page workflows.
* Cross-origin iframe automation.
* High-level agentic commands.
* Loops, macros, functions, or arbitrary scripting in the DSL.
* Vision-first automation.
* Anti-bot evasion.

## Architecture

The system has five core components:

1. **YAML parser and validator**

   * parses workflow files
   * validates top-level schema, action fields, and policy constraints
   * normalizes workflows into an internal representation

2. **Workflow runtime**

   * executes steps in order
   * manages immutable inputs, runtime outputs, and step state
   * applies timeout, retry, and failure rules

3. **Action dispatcher**

   * maps workflow actions to backend primitives
   * converts backend results into normalized step results

4. **Chromium backend**

   * attaches to an existing Chromium target over CDP
   * owns navigation, frame lookup, DOM lookup, JS evaluation, input dispatch, and screenshots

5. **Artifact and reporting layer**

   * stores logs, screenshots, and final run reports
   * applies redaction rules before persisting step or run data

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
  goto_wait_until: domcontentloaded

vars:
  email: "user@example.com"

secrets:
  password: "${ENV.LOGIN_PASSWORD}"

steps:
  - id: open-login
    action: goto
    url: "https://example.com/login"

  - action: type
    selector: "#email"
    text: "${vars.email}"

  - action: type
    selector: "#password"
    text: "${secrets.password}"

  - action: click
    selector: "button[type=submit]"

  - action: wait_for
    selector: ".dashboard"
    retry_count: 1

  - action: extract_text
    selector: ".account-balance"
    save_as: balance

  - action: extract_text
    frame_selector: "iframe#account-frame"
    selector: ".account-id"
    save_as: framed_account_id
```

### Top-level fields

* `version` required
* `settings` optional
* `vars` optional
* `secrets` optional
* `steps` required

### Settings

* `timeout_ms` optional; default workflow timeout for each step
* `allowed_domains` optional; allowlist applied to top-level and targeted frame navigations
* `screenshot_on_failure` optional; captures a failure artifact when a step fails
* `goto_wait_until` optional; defaults to `domcontentloaded`

### Variable namespaces

* `vars` contains non-secret workflow inputs
* `secrets` contains secret inputs supplied externally or by the invoking system
* `outputs` is runtime-only and stores values created by `save_as`

Interpolation must use explicit namespaces:

* `${vars.email}`
* `${secrets.password}`
* `${outputs.balance}`

Rules for v1:

* `vars` and `secrets` are immutable during a run
* `save_as` writes only into `outputs`
* duplicate `save_as` keys fail validation
* unsupported namespaces fail validation

### Common step fields

* `action` required
* `id` optional; unique if present
* `timeout_ms` optional
* `retry_count` optional; only valid on read-only actions

### Initial action set

* `goto`
* `click`
* `type`
* `wait_for`
* `extract_text`
* `extract_attr`
* `assert_exists`
* `screenshot`

### Per-action fields

* `goto` requires `url`
* `click` requires `selector`
* `type` requires `selector` and `text`
* `wait_for` requires `selector`
* `assert_exists` requires `selector`
* `extract_text` requires `selector` and `save_as`
* `extract_attr` requires `selector`, `attr`, and `save_as`
* `frame_selector` is optional on DOM-targeting actions and resolves the target iframe from the main frame

### Selector and frame semantics

Rules for v1:

* selectors are CSS selectors only
* DOM-targeting actions must resolve to exactly one element
* `click` and `type` require the resolved element to be visible and enabled
* `wait_for` succeeds when exactly one matching element becomes present
* `frame_selector` supports one iframe hop from the main frame
* nested frames are out of scope
* cross-origin frames are unsupported and fail with `unsupported_scope`

### Schema decisions for v1

* steps are a flat ordered list
* actions use a uniform `action` field
* workflow execution is fail-fast by default
* retry behavior is restricted to read-only actions
* all step inputs are normalized before execution

## Execution Model

A workflow is executed sequentially from the first step to the last.

### Step lifecycle

For each step, the runtime:

1. resolves namespaced interpolation
2. validates step-level policy and scope constraints
3. applies step-level timeout and retry settings
4. dispatches the action to the backend
5. sanitizes the step result payload
6. records the step result
7. writes step output into `outputs` if the step succeeded and has `save_as`
8. stops the run on failure

### Runtime state

The runtime maintains:

* current step index
* run status
* immutable `vars`
* immutable `secrets`
* mutable `outputs`
* step results
* artifact references
* final error state if failed

### Navigation semantics

Rules for v1:

* `goto` waits for `domcontentloaded` by default
* application-specific readiness must be modeled with explicit `wait_for` or `assert_exists` steps
* the runtime enforces `allowed_domains` on:
  * the initial `goto`
  * top-level redirects
  * same-page navigations triggered by actions
  * targeted iframe navigations when a frame is used
* subresource requests are out of scope for policy enforcement in v1

### Retry semantics

`retry_count` is allowed only on read-only actions:

* `wait_for`
* `assert_exists`
* `extract_text`
* `extract_attr`
* `screenshot`

Validation rejects `retry_count` on side-effecting actions such as:

* `goto`
* `click`
* `type`

Each retry re-executes the full action attempt within the step timeout budget or a step-specific retry budget if later added.

### Failure behavior

v1 uses fail-fast execution.

A step fails if:

* validation or interpolation fails
* a required selector is not found
* a selector resolves to multiple elements
* a required element is not interactable
* a timeout is exceeded
* navigation fails
* a policy rule is violated
* an unsupported browser or frame scope is requested
* the backend returns a protocol or execution error

If enabled in settings, the runtime captures a screenshot on failure.

The runtime also fails immediately if:

* a popup or new tab is opened
* the controlled target closes unexpectedly
* a cross-origin frame is targeted

## Backend Design

The backend is Chromium-first and communicates directly with the browser using CDP.

### Backend responsibilities

* attach to an existing Chromium instance or target
* manage one active page session
* resolve main-frame and same-origin iframe targets
* navigate to URLs
* resolve DOM elements by CSS selector
* dispatch mouse and keyboard input
* evaluate JavaScript in page context
* extract text and attributes
* wait for selector presence
* capture screenshots
* surface typed errors and artifact references

### CDP domains used initially

* `Target`
* `Page`
* `Runtime`
* `DOM`
* `Input`

Additional domains may be introduced if needed to enforce the same public backend contract without changing workflow semantics.

### Internal backend API

The runtime should call a backend interface rather than raw CDP commands.

Conceptual interface:

```ts
type WaitUntil = "domcontentloaded";

interface ElementTarget {
  selector: string;
  frameSelector?: string;
}

interface ActionOptions {
  timeoutMs?: number;
  stepId?: string;
}

interface NavigationOptions extends ActionOptions {
  waitUntil?: WaitUntil;
}

interface TypeOptions extends ActionOptions {}

interface ScreenshotOptions extends ActionOptions {}

type ErrorCode =
  | "validation_error"
  | "policy_violation"
  | "selector_not_found"
  | "selector_not_unique"
  | "element_not_interactable"
  | "timeout"
  | "navigation_error"
  | "protocol_error"
  | "unsupported_scope"
  | "interpolation_error";

interface ActionError {
  code: ErrorCode;
  message: string;
}

interface ActionResult<T> {
  ok: boolean;
  value?: T;
  error?: ActionError;
  artifacts?: ArtifactRef[];
}

interface AutomationBackend {
  goto(url: string, opts?: NavigationOptions): Promise<ActionResult<void>>;
  click(target: ElementTarget, opts?: ActionOptions): Promise<ActionResult<void>>;
  type(target: ElementTarget, text: string, opts?: TypeOptions): Promise<ActionResult<void>>;
  waitFor(target: ElementTarget, opts?: ActionOptions): Promise<ActionResult<void>>;
  assertExists(target: ElementTarget, opts?: ActionOptions): Promise<ActionResult<void>>;
  extractText(target: ElementTarget, opts?: ActionOptions): Promise<ActionResult<string>>;
  extractAttr(target: ElementTarget, attr: string, opts?: ActionOptions): Promise<ActionResult<string | null>>;
  screenshot(opts?: ScreenshotOptions): Promise<ActionResult<ArtifactRef>>;
}
```

This keeps the workflow runtime independent of backend implementation details while making frame targeting, navigation behavior, and typed errors explicit.

## Logging and Reporting

Each step produces a structured result containing:

* step ID or index
* action
* sanitized inputs
* duration
* status
* output value if any
* typed error if failed
* artifact references

Each run produces a final JSON report containing:

* report schema version
* workflow metadata
* overall status
* sanitized input summary
* `outputs`
* per-step results
* artifacts
* final error if present

### Redaction rules

Rules for v1:

* resolved secret values never appear in step inputs
* final reports never emit secret values
* persisted error messages must not echo secret material
* `vars` may be reported, but only after interpolation and sanitization rules are applied

### Error taxonomy

Step results and final reports must use these error codes:

* `validation_error`
* `policy_violation`
* `selector_not_found`
* `selector_not_unique`
* `element_not_interactable`
* `timeout`
* `navigation_error`
* `protocol_error`
* `unsupported_scope`
* `interpolation_error`

## MVP

The MVP is complete when the system can:

* attach to Chromium over CDP without launching the browser
* execute `goto`, `type`, `click`, `wait_for`, and `extract_text` on the main frame
* execute DOM-targeting actions within explicitly targeted same-origin iframes
* support namespaced interpolation for `vars`, `secrets`, and `outputs`
* reject invalid retry usage and duplicate `save_as` keys at validation time
* enforce allowed-domain policy on supported navigation paths
* fail cleanly with structured typed errors
* capture screenshots on failure
* emit a stable JSON run report with sanitized inputs and redacted secrets

## Validation and Test Targets

Validation should reject:

* retries on side-effecting actions
* duplicate `save_as` keys
* missing required action fields
* unsupported interpolation namespaces
* unsupported selector or frame configurations

Execution and reporting should verify:

* secret values can be used in actions but never appear in step logs or final reports
* off-policy redirects fail with `policy_violation`
* ambiguous selectors fail with `selector_not_unique`
* iframe-targeted actions work for same-origin frames and fail clearly for unsupported frame scopes
* `goto` completes at `domcontentloaded`, with app readiness delegated to explicit wait steps

Scope behavior should verify:

* popup or new-tab creation fails fast with `unsupported_scope`
* unexpected target closure fails cleanly
* attach-only startup is the only supported browser lifecycle in v1

## Recommended Build Order

1. implement CDP transport and attach-only page/session management
2. implement backend primitives for navigation, frame resolution, DOM lookup, click, type, wait, extract, assert, and screenshot
3. implement workflow parsing, validation, interpolation, and runtime state management
4. add structured reporting, error taxonomy, redaction, and failure artifacts
5. extend only after the core action semantics are stable

## Summary

The core idea is to treat browser automation as a deterministic execution system rather than a scripting convenience layer. The backend owns browser control, the runtime owns step semantics, and the YAML DSL stays compact and strictly constrained. In v1, the system favors explicit scope limits, typed errors, and sanitized reporting so that automation runs are predictable, auditable, and safe to consume from higher-level agents.
