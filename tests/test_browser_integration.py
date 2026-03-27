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
