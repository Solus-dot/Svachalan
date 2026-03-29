from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.sync.client import connect

from svachalan.contracts.backend import (
    ActionOptions,
    ActionResult,
    ArtifactRef,
    BrowserSession,
    ElementTarget,
    NavigationOptions,
    PageState,
    ScreenshotOptions,
    TypeOptions,
)
from svachalan.contracts.errors import ActionError, ErrorCode


class ChromiumBackend:
    def __init__(self, session: BrowserSession):
        self._session = session
        self._connection = _CDPConnection(session.ws_endpoint)
        self._connection.call("Page.enable")
        self._connection.call("Runtime.enable")

    def close(self) -> None:
        self._connection.close()

    def goto(self, url: str, opts: NavigationOptions | None = None) -> ActionResult:
        timeout_seconds = _timeout_seconds(opts)
        try:
            self._connection.discard_events("Page.domContentEventFired")
            result = self._connection.call(
                "Page.navigate",
                {"url": url},
                timeout_seconds=timeout_seconds,
            )
            if result.get("errorText"):
                return _failure(ErrorCode.NAVIGATION_ERROR, result["errorText"])
            self._connection.wait_for_event(
                "Page.domContentEventFired",
                timeout_seconds=timeout_seconds,
            )
            return ActionResult.success()
        except TimeoutError:
            return _failure(ErrorCode.TIMEOUT, f"Timed out navigating to {url!r}.")
        except _CDPError as exc:
            return _failure(ErrorCode.PROTOCOL_ERROR, str(exc))

    def click(self, target: ElementTarget, opts: ActionOptions | None = None) -> ActionResult:
        return self._execute_dom_action(target, "click", opts=opts)

    def type(
        self,
        target: ElementTarget,
        text: str,
        opts: TypeOptions | None = None,
    ) -> ActionResult:
        return self._execute_dom_action(target, "type", text=text, opts=opts)

    def wait_for(self, target: ElementTarget, opts: ActionOptions | None = None) -> ActionResult:
        timeout_seconds = _timeout_seconds(opts)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self._execute_dom_action(target, "exists", opts=opts)
            if result.ok:
                return ActionResult.success()
            if result.error and result.error.code == ErrorCode.SELECTOR_NOT_FOUND:
                time.sleep(0.1)
                continue
            return result
        return _failure(
            ErrorCode.TIMEOUT,
            f"Timed out waiting for target {_target_description(target)!r}.",
        )

    def assert_exists(
        self,
        target: ElementTarget,
        opts: ActionOptions | None = None,
    ) -> ActionResult:
        return self._execute_dom_action(target, "exists", opts=opts)

    def extract_text(
        self,
        target: ElementTarget,
        opts: ActionOptions | None = None,
    ) -> ActionResult:
        return self._execute_dom_action(target, "extract_text", opts=opts)

    def extract_attr(
        self,
        target: ElementTarget,
        attr: str,
        opts: ActionOptions | None = None,
    ) -> ActionResult:
        return self._execute_dom_action(target, "extract_attr", attr=attr, opts=opts)

    def screenshot(self, opts: ScreenshotOptions | None = None) -> ActionResult:
        timeout_seconds = _timeout_seconds(opts)
        try:
            result = self._connection.call(
                "Page.captureScreenshot",
                {"format": "png"},
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError:
            return _failure(ErrorCode.TIMEOUT, "Timed out capturing screenshot.")
        except _CDPError as exc:
            return _failure(ErrorCode.PROTOCOL_ERROR, str(exc))

        data = result.get("data")
        if not isinstance(data, str):
            return _failure(ErrorCode.PROTOCOL_ERROR, "Screenshot response was missing image data.")

        artifact_dir = Path(self._session.artifact_dir or ".").resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        label = opts.step_id if opts and opts.step_id else "screenshot"
        filename = f"{label}-{int(time.time() * 1000)}.png"
        output_path = artifact_dir / filename
        output_path.write_bytes(base64.b64decode(data))

        artifact = ArtifactRef(path=str(output_path), label=label)
        return ActionResult.success(value=artifact)

    def inspect_page(self, opts: ActionOptions | None = None) -> ActionResult:
        timeout_seconds = _timeout_seconds(opts)
        try:
            payload = self._evaluate(
                _build_page_state_expression(),
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError:
            return _failure(ErrorCode.TIMEOUT, "Timed out inspecting page state.")
        except _CDPError as exc:
            return _failure(ErrorCode.PROTOCOL_ERROR, str(exc))

        if not isinstance(payload, dict):
            return _failure(
                ErrorCode.PROTOCOL_ERROR,
                "Page inspection returned an invalid response.",
            )
        try:
            page_state = PageState.model_validate(payload)
        except Exception as exc:  # pragma: no cover - defensive shape guard
            return _failure(ErrorCode.PROTOCOL_ERROR, f"Invalid page state payload: {exc}")
        return ActionResult.success(
            page_state,
            details={
                "current_url": page_state.url,
                "page_title": page_state.title,
                "detected_indicators": page_state.detected_indicators,
                "handoff_required": page_state.handoff_required,
            },
        )

    def _execute_dom_action(
        self,
        target: ElementTarget,
        action: str,
        *,
        text: str | None = None,
        attr: str | None = None,
        opts: ActionOptions | None = None,
    ) -> ActionResult:
        timeout_seconds = _timeout_seconds(opts)
        expression = _build_dom_expression(
            target=target,
            action=action,
            text=text,
            attr=attr,
        )
        try:
            payload = self._evaluate(expression, timeout_seconds=timeout_seconds)
        except TimeoutError:
            return _failure(ErrorCode.TIMEOUT, f"Timed out executing {action!r}.")
        except _CDPError as exc:
            return _failure(ErrorCode.PROTOCOL_ERROR, str(exc))

        if not isinstance(payload, dict):
            return _failure(ErrorCode.PROTOCOL_ERROR, "DOM action returned an invalid response.")
        if payload.get("ok") is True:
            details = payload.get("details", {})
            if not isinstance(details, dict):
                details = {}
            return ActionResult.success(payload.get("value"), details=details)

        error = payload.get("error")
        if not isinstance(error, dict):
            return _failure(
                ErrorCode.PROTOCOL_ERROR,
                "DOM action returned an invalid error payload.",
            )
        error_details = error.get("details") if isinstance(error.get("details"), dict) else None
        try:
            return ActionResult.failure(
                ActionError(
                    code=ErrorCode(error["code"]),
                    message=str(error["message"]),
                    details=error_details,
                ),
                details=error_details,
            )
        except (KeyError, ValueError):
            return _failure(ErrorCode.PROTOCOL_ERROR, "DOM action returned an unknown error code.")

    def _evaluate(self, expression: str, *, timeout_seconds: float) -> Any:
        result = self._connection.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout_seconds=timeout_seconds,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = details.get("text") or "JavaScript evaluation failed."
            raise _CDPError(description)
        remote_value = result.get("result", {})
        return remote_value.get("value")


class _CDPConnection:
    def __init__(self, ws_endpoint: str):
        self._socket = connect(
            ws_endpoint,
            open_timeout=10,
            close_timeout=5,
            max_size=None,
        )
        self._next_id = 0
        self._events: list[dict[str, Any]] = []

    def close(self) -> None:
        self._socket.close()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        command_id = self._next_id
        self._socket.send(
            json.dumps({"id": command_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            message = self._receive(deadline)
            if message.get("id") == command_id:
                if "error" in message:
                    error = message["error"]
                    raise _CDPError(error.get("message", f"CDP call {method!r} failed."))
                return message.get("result", {})
            if "method" in message:
                self._events.append(message)

    def wait_for_event(
        self,
        method: str,
        *,
        timeout_seconds: float = 10.0,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        predicate = predicate or (lambda _event: True)
        while True:
            for index, event in enumerate(self._events):
                if event.get("method") == method and predicate(event):
                    return self._events.pop(index)
            event = self._receive(deadline)
            if event.get("method") == method and predicate(event):
                return event
            if "method" in event:
                self._events.append(event)

    def discard_events(self, method: str) -> None:
        self._events = [event for event in self._events if event.get("method") != method]

    def _receive(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for CDP response.")
        message = self._socket.recv(timeout=remaining)
        if not isinstance(message, str):
            raise _CDPError("CDP returned a non-text websocket payload.")
        return json.loads(message)


class _CDPError(RuntimeError):
    pass


def _build_dom_expression(
    *,
    target: ElementTarget,
    action: str,
    text: str | None = None,
    attr: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "target": target.model_dump(mode="json", exclude_none=True),
            "action": action,
            "text": text,
            "attr": attr,
        }
    )
    return f"""
(() => {{
  const args = {payload};
  const fail = (code, message) => ({{ ok: false, error: {{ code, message }} }});
  const succeed = (value = null) => ({{ ok: true, value }});

  const selectorsFor = (locator) => {{
    const selectors = [];
    if (locator.selector) {{
      selectors.push(locator.selector);
    }}
    if (Array.isArray(locator.selectors)) {{
      selectors.push(...locator.selectors);
    }}
    return selectors;
  }};

  const describeSelectors = (selectors) => selectors.join(", ");

  const resolveRoot = (frameSelector) => {{
    if (!frameSelector) {{
      return {{ root: document }};
    }}
    const frames = document.querySelectorAll(frameSelector);
    if (frames.length === 0) {{
      return fail("selector_not_found", `Frame selector ${{frameSelector}} was not found.`);
    }}
    if (frames.length > 1) {{
      return fail(
        "selector_not_unique",
        `Frame selector ${{frameSelector}} matched multiple elements.`,
      );
    }}
    const frame = frames[0];
    if (!(frame instanceof HTMLIFrameElement)) {{
      return fail("unsupported_scope", "frame_selector must resolve to an iframe element.");
    }}
    try {{
      const root = frame.contentDocument;
      if (!root) {{
        return fail("unsupported_scope", "Cross-origin frames are unsupported.");
      }}
      return {{ root }};
    }} catch (_error) {{
      return fail("unsupported_scope", "Cross-origin frames are unsupported.");
    }}
  }};

  const resolveElement = (root, locator) => {{
    if (locator.within) {{
      const scopeResult = resolveElement(root, locator.within);
      if (scopeResult.ok === false) {{
        return scopeResult;
      }}
      root = scopeResult.element;
    }}
    const selectors = selectorsFor(locator);
    let nonUniqueSelector = null;
    for (const selector of selectors) {{
      const matches = Array.from(root.querySelectorAll(selector));
      if (matches.length === 0) {{
        continue;
      }}
      if (locator.match === "first_visible") {{
        const visibleMatch = matches.find((match) => isVisible(match));
        if (visibleMatch) {{
          return {{ element: visibleMatch, selector, attemptedSelectors: selectors }};
        }}
        continue;
      }}
      if (matches.length > 1) {{
        nonUniqueSelector = selector;
        continue;
      }}
      return {{ element: matches[0], selector, attemptedSelectors: selectors }};
    }}
    if (nonUniqueSelector) {{
      return {{
        ok: false,
        error: {{
          code: "selector_not_unique",
          message: `Selector ${{nonUniqueSelector}} matched multiple elements.`,
          details: {{ attempted_selectors: selectors }},
        }},
      }};
    }}
    return {{
      ok: false,
      error: {{
        code: "selector_not_found",
        message: `No selectors matched: ${{describeSelectors(selectors)}}.`,
        details: {{ attempted_selectors: selectors }},
      }},
    }};
  }};

  const isVisible = (element) => {{
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return (
      style.visibility !== "hidden" &&
      style.display !== "none" &&
      rect.width > 0 &&
      rect.height > 0
    );
  }};

  const isEnabled = (element) => !("disabled" in element) || !element.disabled;

  const rootResult = resolveRoot(args.target.frameSelector);
  if (rootResult.ok === false) {{
    return rootResult;
  }}

  const elementResult = resolveElement(rootResult.root, args.target);
  if (elementResult.ok === false) {{
    return elementResult;
  }}

  const element = elementResult.element;
  const matchedSelector = elementResult.selector;
  if (args.action === "exists") {{
    return {{
      ok: true,
      value: null,
      details: {{
        matched_selector: matchedSelector,
        attempted_selectors: elementResult.attemptedSelectors,
      }},
    }};
  }}
  if (args.action === "extract_text") {{
    return {{
      ok: true,
      value: typeof element.innerText === "string"
        ? element.innerText
        : (element.textContent ?? ""),
      details: {{
        matched_selector: matchedSelector,
        attempted_selectors: elementResult.attemptedSelectors,
      }},
    }};
  }}
  if (args.action === "extract_attr") {{
    return {{
      ok: true,
      value: element.getAttribute(args.attr),
      details: {{
        matched_selector: matchedSelector,
        attempted_selectors: elementResult.attemptedSelectors,
      }},
    }};
  }}
  if (!isVisible(element) || !isEnabled(element)) {{
    return {{
      ok: false,
      error: {{
        code: "element_not_interactable",
        message: `Selector ${{matchedSelector}} is not interactable.`,
        details: {{
          matched_selector: matchedSelector,
          attempted_selectors: elementResult.attemptedSelectors,
        }},
      }},
    }};
  }}
  if (args.action === "click") {{
    element.click();
    return {{
      ok: true,
      value: null,
      details: {{
        matched_selector: matchedSelector,
        attempted_selectors: elementResult.attemptedSelectors,
      }},
    }};
  }}
  if (args.action === "type") {{
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {{
      element.focus();
      element.value = args.text ?? "";
      element.dispatchEvent(new Event("input", {{ bubbles: true }}));
      element.dispatchEvent(new Event("change", {{ bubbles: true }}));
      return {{
        ok: true,
        value: null,
        details: {{
          matched_selector: matchedSelector,
          attempted_selectors: elementResult.attemptedSelectors,
        }},
      }};
    }}
    if (element.isContentEditable) {{
      element.focus();
      element.textContent = args.text ?? "";
      element.dispatchEvent(new Event("input", {{ bubbles: true }}));
      return {{
        ok: true,
        value: null,
        details: {{
          matched_selector: matchedSelector,
          attempted_selectors: elementResult.attemptedSelectors,
        }},
      }};
    }}
    return {{
      ok: false,
      error: {{
        code: "element_not_interactable",
        message: `Selector ${{matchedSelector}} is not a typable element.`,
        details: {{
          matched_selector: matchedSelector,
          attempted_selectors: elementResult.attemptedSelectors,
        }},
      }},
    }};
  }}
  return fail("protocol_error", `Unsupported DOM action ${{args.action}}.`);
}})()
""".strip()


def _failure(code: ErrorCode, message: str) -> ActionResult:
    return ActionResult.failure(ActionError(code=code, message=message))


def _target_description(target: ElementTarget) -> str:
    return ", ".join(target.all_selectors())


def _build_page_state_expression() -> str:
    return """
(() => {
  const text = document.body?.innerText ?? "";
  const normalizedText = text.toLowerCase();
  const title = document.title ?? "";
  const normalizedTitle = title.toLowerCase();
  const indicators = [];
  let handoffReason = null;

  const hasCaptcha =
    normalizedText.includes("captcha") ||
    normalizedText.includes("enter the characters you see below") ||
    document.querySelector("#captchacharacters, iframe[src*='captcha']") !== null;
  if (hasCaptcha) {
    indicators.push("captcha");
    handoffReason = handoffReason ?? "Captcha or bot challenge detected.";
  }

  const hasPasswordField = document.querySelector("input[type='password']") !== null;
  const signInLanguage =
    normalizedText.includes("sign in") ||
    normalizedText.includes("log in") ||
    normalizedTitle.includes("sign in") ||
    normalizedTitle.includes("log in");
  if (hasPasswordField && signInLanguage) {
    indicators.push("login");
    handoffReason = handoffReason ?? "Login page detected.";
  }

    const otpSelector =
      "input[autocomplete='one-time-code'], input[name*='otp'], input[name*='code']";
  const hasOtp =
    document.querySelector(otpSelector) !== null ||
    normalizedText.includes("verification code") ||
    normalizedText.includes("two-step verification") ||
    normalizedText.includes("one-time passcode");
  if (hasOtp) {
    indicators.push("2fa");
    handoffReason = handoffReason ?? "Verification or 2FA challenge detected.";
  }

  const hasSecurityCheck =
    normalizedText.includes("security check") ||
    normalizedText.includes("verify you are human");
  if (hasSecurityCheck) {
    indicators.push("security_check");
    handoffReason = handoffReason ?? "Security verification detected.";
  }

  return {
    url: window.location.href,
    title: document.title ?? null,
    html: document.documentElement.outerHTML,
    text,
    handoff_required: handoffReason !== null,
    handoff_reason: handoffReason,
    detected_indicators: indicators,
  };
})()
""".strip()


def _timeout_seconds(opts: ActionOptions | None) -> float:
    timeout_ms = opts.timeout_ms if opts and opts.timeout_ms is not None else 10_000
    return max(timeout_ms / 1000.0, 0.001)
