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
            f"Timed out waiting for selector {target.selector!r}.",
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
            return ActionResult.success(payload.get("value"))

        error = payload.get("error")
        if not isinstance(error, dict):
            return _failure(
                ErrorCode.PROTOCOL_ERROR,
                "DOM action returned an invalid error payload.",
            )
        try:
            return ActionResult.failure(
                ActionError(code=ErrorCode(error["code"]), message=str(error["message"]))
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
        self._socket = connect(ws_endpoint, open_timeout=10, close_timeout=5)
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
            "selector": target.selector,
            "frameSelector": target.frame_selector,
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

  const resolveRoot = () => {{
    if (!args.frameSelector) {{
      return {{ root: document }};
    }}
    const frames = document.querySelectorAll(args.frameSelector);
    if (frames.length === 0) {{
      return fail("selector_not_found", `Frame selector ${{args.frameSelector}} was not found.`);
    }}
    if (frames.length > 1) {{
      return fail(
        "selector_not_unique",
        `Frame selector ${{args.frameSelector}} matched multiple elements.`,
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

  const resolveElement = (root) => {{
    const matches = root.querySelectorAll(args.selector);
    if (matches.length === 0) {{
      return fail("selector_not_found", `Selector ${{args.selector}} was not found.`);
    }}
    if (matches.length > 1) {{
      return fail("selector_not_unique", `Selector ${{args.selector}} matched multiple elements.`);
    }}
    return {{ element: matches[0] }};
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

  const rootResult = resolveRoot();
  if (rootResult.ok === false) {{
    return rootResult;
  }}

  const elementResult = resolveElement(rootResult.root);
  if (elementResult.ok === false) {{
    return elementResult;
  }}

  const element = elementResult.element;
  if (args.action === "exists") {{
    return succeed();
  }}
  if (args.action === "extract_text") {{
    return succeed(
      typeof element.innerText === "string"
        ? element.innerText
        : (element.textContent ?? "")
    );
  }}
  if (args.action === "extract_attr") {{
    return succeed(element.getAttribute(args.attr));
  }}
  if (!isVisible(element) || !isEnabled(element)) {{
    return fail("element_not_interactable", `Selector ${{args.selector}} is not interactable.`);
  }}
  if (args.action === "click") {{
    element.click();
    return succeed();
  }}
  if (args.action === "type") {{
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {{
      element.focus();
      element.value = args.text ?? "";
      element.dispatchEvent(new Event("input", {{ bubbles: true }}));
      element.dispatchEvent(new Event("change", {{ bubbles: true }}));
      return succeed();
    }}
    if (element.isContentEditable) {{
      element.focus();
      element.textContent = args.text ?? "";
      element.dispatchEvent(new Event("input", {{ bubbles: true }}));
      return succeed();
    }}
    return fail(
      "element_not_interactable",
      `Selector ${{args.selector}} is not a typable element.`,
    );
  }}
  return fail("protocol_error", `Unsupported DOM action ${{args.action}}.`);
}})()
""".strip()


def _failure(code: ErrorCode, message: str) -> ActionResult:
    return ActionResult.failure(ActionError(code=code, message=message))


def _timeout_seconds(opts: ActionOptions | None) -> float:
    timeout_ms = opts.timeout_ms if opts and opts.timeout_ms is not None else 10_000
    return max(timeout_ms / 1000.0, 0.001)
