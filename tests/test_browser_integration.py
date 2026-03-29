from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from svachalan import create_backend, parse_workflow, run_workflow, start_browser_session
from svachalan.backend.chromium import ChromiumBackend
from svachalan.browser import lifecycle
from svachalan.contracts import (
    AttachOptions,
    BackendConfig,
    BrowserSession,
    BrowserSessionMode,
    BrowserSessionOptions,
    LaunchOptions,
    RunOptions,
)


class _TestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/frame.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <div class="inside">Frame Value</div>
  </body>
</html>
"""
            )
            return

        if self.path == "/locator.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <button class="action" style="display:none">Hidden Action</button>
    <button class="action" id="visible-action">Visible Action</button>
    <div id="result"></div>
    <script>
      document.querySelector("#visible-action").addEventListener("click", () => {
        document.querySelector("#result").textContent = "clicked visible action";
      });
    </script>
  </body>
</html>
"""
            )
            return

        if self.path == "/scoped.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <div id="card-1" class="card">
      <button class="action">Wrong Action</button>
    </div>
    <div id="card-2" class="card">
      <button class="action" id="target-action">Target Action</button>
    </div>
    <div id="result"></div>
    <script>
      document.querySelector("#target-action").addEventListener("click", () => {
        document.querySelector("#result").textContent = "scoped click succeeded";
      });
    </script>
  </body>
</html>
"""
            )
            return

        if self.path == "/challenge.html":
            self._send_html(
                """
<!doctype html>
<html>
  <head>
    <title>Security Check</title>
  </head>
  <body>
    <h1>Security Check</h1>
    <p>Verify you are human before continuing.</p>
    <input type="password" />
  </body>
</html>
"""
            )
            return

        if self.path == "/admin.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <form action="/admin-saved.html" method="get">
      <label for="setting">Setting</label>
      <input id="setting" name="setting" />
      <button id="save-settings" type="submit">Save settings</button>
    </form>
  </body>
</html>
"""
            )
            return

        if self.path.startswith("/admin-saved.html"):
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <h1>Settings</h1>
    <div id="status">Saved successfully</div>
  </body>
</html>
"""
            )
            return

        if self.path == "/catalog.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <div class="product-card" data-sku="widget-2">
      <h2 class="product-name">Portable Widget</h2>
      <a class="details-link" href="/products/widget-2.html">View details</a>
    </div>
  </body>
</html>
"""
            )
            return

        if self.path == "/products/widget-2.html":
            self._send_html(
                """
<!doctype html>
<html>
  <body>
    <h1 id="product-title">Portable Widget</h1>
    <div id="product-price">$19.99</div>
    <div id="product-stock">In stock</div>
  </body>
</html>
"""
            )
            return

        self._send_html(
            """
<!doctype html>
<html>
  <body>
    <input id="name" />
    <button id="submit">Submit</button>
    <iframe id="frame" src="/frame.html"></iframe>
    <script>
      document.querySelector("#submit").addEventListener("click", () => {
        const result = document.createElement("div");
        result.className = "result";
        result.textContent = `Hello ${document.querySelector("#name").value}`;
        document.body.appendChild(result);
      });
    </script>
  </body>
</html>
"""
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_browser_backend_end_to_end(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/"
  - action: wait_for
    selector: "#name"
  - action: type
    selector: "#name"
    text: "Svachalan"
  - action: click
    selector: "#submit"
  - action: wait_for
    selector: ".result"
  - action: extract_text
    selector: ".result"
    save_as: greeting
  - action: wait_for
    frame_selector: "iframe#frame"
    selector: ".inside"
  - action: extract_text
    frame_selector: "iframe#frame"
    selector: ".inside"
    save_as: frame_text
  - action: screenshot
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(
            workflow,
            backend,
            RunOptions(
                output_dir=str(tmp_path),
                run_id="browser-e2e",
                browser_session_mode=session.mode,
            ),
        )
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "succeeded"
    assert report.outputs["greeting"] == "Hello Svachalan"
    assert report.outputs["frame_text"] == "Frame Value"
    assert any(artifact.path.endswith(".png") for artifact in report.artifacts)


def test_start_browser_session_resolves_browser_websocket_endpoint(monkeypatch) -> None:
    wait_calls: list[str] = []

    def fake_wait_for_http_endpoint(endpoint: str, timeout_seconds: float = 10.0) -> None:
        del timeout_seconds
        wait_calls.append(endpoint)

    def fake_select_page_target(
        http_endpoint: str,
        target_id: str | None,
        *,
        create_if_missing: bool,
    ) -> dict[str, str]:
        assert http_endpoint == "http://127.0.0.1:9222"
        assert target_id == "page-2"
        assert create_if_missing is True
        return {
            "id": "page-2",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-2",
        }

    monkeypatch.setattr(lifecycle, "_wait_for_http_endpoint", fake_wait_for_http_endpoint)
    monkeypatch.setattr(lifecycle, "_select_page_target", fake_select_page_target)

    session = lifecycle.start_browser_session(
        BrowserSessionOptions(
            mode=BrowserSessionMode.ATTACH,
            attach=AttachOptions(
                endpoint="ws://127.0.0.1:9222/devtools/browser/browser-id",
                target_id="page-2",
            ),
        )
    )
    try:
        assert wait_calls == ["http://127.0.0.1:9222"]
        assert session.http_endpoint == "http://127.0.0.1:9222"
        assert session.target_id == "page-2"
        assert session.ws_endpoint == "ws://127.0.0.1:9222/devtools/page/page-2"
    finally:
        session.cleanup()


def test_chromium_backend_screenshot_allows_default_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeConnection:
        def __init__(self, ws_endpoint: str):
            self.ws_endpoint = ws_endpoint

        def call(
            self,
            method: str,
            params: dict[str, str] | None = None,
            *,
            timeout_seconds: float = 10.0,
        ) -> dict[str, str]:
            del params, timeout_seconds
            if method == "Page.captureScreenshot":
                return {
                    "data": base64.b64encode(b"png-bytes").decode("ascii"),
                }
            return {}

        def close(self) -> None:
            return None

    monkeypatch.setattr("svachalan.backend.chromium._CDPConnection", FakeConnection)

    backend = ChromiumBackend(
        BrowserSession(
            mode=BrowserSessionMode.LAUNCH,
            ws_endpoint="ws://127.0.0.1:9222/devtools/page/page-1",
            artifact_dir=str(tmp_path),
        )
    )

    result = backend.screenshot()

    assert result.ok is True
    assert result.value is not None
    assert result.value.label == "screenshot"
    assert Path(result.value.path).exists()


def test_browser_backend_supports_fallback_selectors_and_first_visible_matching() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/locator.html"
  - action: click
    selectors:
      - ".missing-action"
      - "button.action"
    match: first_visible
  - action: wait_for
    selector: "#result"
  - action: extract_text
    selector: "#result"
    save_as: result_text
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, RunOptions())
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "succeeded"
    assert report.outputs["result_text"] == "clicked visible action"


def test_browser_backend_supports_within_scoping() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/scoped.html"
  - action: click
    selector: ".action"
    within:
      selector: "#card-2"
  - action: extract_text
    selector: "#result"
    save_as: result_text
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, RunOptions())
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "succeeded"
    assert report.outputs["result_text"] == "scoped click succeeded"


def test_browser_backend_reports_handoff_for_challenge_page() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/challenge.html"
  - action: wait_for
    selector: "#never-appears"
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, RunOptions())
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "failed"
    assert report.handoff_required is True
    assert report.error is not None
    assert report.error.code.value == "human_handoff_required"


def test_browser_backend_supports_admin_style_url_and_text_assertions() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/admin.html"
  - action: type
    selector: "#setting"
    text: "enabled"
  - action: click
    selector: "#save-settings"
  - action: wait_for_url_contains
    url: "/admin-saved.html"
  - action: assert_url_contains
    url: "/admin-saved.html"
  - action: assert_text_contains
    selector: "#status"
    text: "Saved"
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, RunOptions())
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "succeeded"
    assert report.steps[3].action == "wait_for_url_contains"
    assert report.steps[5].action == "assert_text_contains"


def test_browser_backend_supports_multi_page_extraction_flow() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    workflow = parse_workflow(
        f"""
version: 1
settings:
  allowed_domains: ["127.0.0.1"]
steps:
  - action: goto
    url: "http://127.0.0.1:{server.server_address[1]}/catalog.html"
  - action: click
    selector: ".details-link"
    within:
      selector: ".product-card[data-sku='widget-2']"
  - action: wait_for_url_contains
    url: "/products/widget-2.html"
  - action: extract_text
    selector: "#product-title"
    save_as: product_title
  - action: extract_text
    selector: "#product-price"
    save_as: product_price
  - action: assert_text_contains
    selector: "#product-stock"
    text: "In stock"
"""
    )

    session = None
    backend = None
    try:
        try:
            session = start_browser_session(
                BrowserSessionOptions(launch=LaunchOptions(headless=True))
            )
        except FileNotFoundError as exc:
            pytest.skip(str(exc))

        backend = create_backend(BackendConfig(session=session))
        report = run_workflow(workflow, backend, RunOptions())
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if session is not None:
            session.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert report.status.value == "succeeded"
    assert report.outputs["product_title"] == "Portable Widget"
    assert report.outputs["product_price"] == "$19.99"
