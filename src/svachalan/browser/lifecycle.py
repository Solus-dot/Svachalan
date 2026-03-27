from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from svachalan.contracts.backend import (
    AttachOptions,
    BrowserSession,
    BrowserSessionMode,
    BrowserSessionOptions,
    LaunchOptions,
)

_DEFAULT_BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)


def start_browser_session(options: BrowserSessionOptions) -> BrowserSession:
    if options.mode == BrowserSessionMode.ATTACH:
        return _start_attach_session(options.attach)
    return _start_launch_session(options.launch)


def _start_launch_session(launch: LaunchOptions | None) -> BrowserSession:
    launch = launch or LaunchOptions()
    browser_path = _resolve_browser_path(launch.browser_path)
    debugging_port = launch.debugging_port or _reserve_port()

    created_user_data_dir = launch.user_data_dir is None
    user_data_dir = Path(
        launch.user_data_dir or tempfile.mkdtemp(prefix="svachalan-profile-")
    ).resolve()
    artifact_dir = Path(tempfile.mkdtemp(prefix="svachalan-artifacts-")).resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    command = [
        browser_path,
        f"--remote-debugging-port={debugging_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "about:blank",
    ]
    if launch.headless:
        command.append("--headless=new")

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    http_endpoint = f"http://127.0.0.1:{debugging_port}"
    try:
        _wait_for_http_endpoint(http_endpoint)
        target = _select_page_target(http_endpoint, target_id=None, create_if_missing=True)
    except Exception:
        process.terminate()
        process.wait(timeout=5)
        if created_user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        shutil.rmtree(artifact_dir, ignore_errors=True)
        raise

    session = BrowserSession(
        mode=BrowserSessionMode.LAUNCH,
        ws_endpoint=target["webSocketDebuggerUrl"],
        http_endpoint=http_endpoint,
        target_id=target["id"],
        artifact_dir=str(artifact_dir),
        browser_path=browser_path,
        user_data_dir=str(user_data_dir),
        debugging_port=debugging_port,
    )
    session.set_cleanup_callback(
        lambda: _cleanup_launch_session(
            process=process,
            artifact_dir=artifact_dir,
            user_data_dir=user_data_dir,
            remove_user_data_dir=created_user_data_dir and not launch.keep_browser_open,
            keep_browser_open=launch.keep_browser_open,
        )
    )
    return session


def _start_attach_session(attach: AttachOptions | None) -> BrowserSession:
    if attach is None:
        raise ValueError("Attach mode requires attach options.")

    artifact_dir = Path(tempfile.mkdtemp(prefix="svachalan-artifacts-")).resolve()
    if attach.endpoint.startswith(("ws://", "wss://")):
        if _is_page_websocket_endpoint(attach.endpoint) and attach.target_id is None:
            session = BrowserSession(
                mode=BrowserSessionMode.ATTACH,
                ws_endpoint=attach.endpoint,
                artifact_dir=str(artifact_dir),
            )
            session.set_cleanup_callback(lambda: shutil.rmtree(artifact_dir, ignore_errors=True))
            return session

        http_endpoint = _http_endpoint_from_ws_endpoint(attach.endpoint)
        _wait_for_http_endpoint(http_endpoint)
        target = _select_page_target(http_endpoint, attach.target_id, create_if_missing=True)
        session = BrowserSession(
            mode=BrowserSessionMode.ATTACH,
            ws_endpoint=target["webSocketDebuggerUrl"],
            http_endpoint=http_endpoint,
            target_id=target["id"],
            artifact_dir=str(artifact_dir),
        )
        session.set_cleanup_callback(lambda: shutil.rmtree(artifact_dir, ignore_errors=True))
        return session

    http_endpoint = _normalize_http_endpoint(attach.endpoint)
    _wait_for_http_endpoint(http_endpoint)
    target = _select_page_target(http_endpoint, attach.target_id, create_if_missing=True)
    session = BrowserSession(
        mode=BrowserSessionMode.ATTACH,
        ws_endpoint=target["webSocketDebuggerUrl"],
        http_endpoint=http_endpoint,
        target_id=target["id"],
        artifact_dir=str(artifact_dir),
    )
    session.set_cleanup_callback(lambda: shutil.rmtree(artifact_dir, ignore_errors=True))
    return session


def _resolve_browser_path(explicit_path: str | None) -> str:
    candidates = [explicit_path] if explicit_path else []
    candidates.extend(_DEFAULT_BROWSER_CANDIDATES)
    for candidate in candidates:
        if not candidate:
            continue
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("Could not find a Chromium or Chrome executable.")


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_endpoint(http_endpoint: str, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{http_endpoint}/json/version", timeout=1.0)
            response.raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for CDP endpoint {http_endpoint!r}.")


def _select_page_target(
    http_endpoint: str,
    target_id: str | None,
    *,
    create_if_missing: bool,
) -> dict[str, str]:
    response = httpx.get(f"{http_endpoint}/json/list", timeout=5.0)
    response.raise_for_status()
    targets = response.json()

    if target_id:
        for target in targets:
            if target.get("id") == target_id:
                return target
        raise ValueError(f"Could not find target id {target_id!r} at {http_endpoint!r}.")

    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target

    if not create_if_missing:
        raise ValueError(f"No debuggable page target was available at {http_endpoint!r}.")

    for method_name in ("put", "get"):
        try:
            request = getattr(httpx, method_name)
            response = request(f"{http_endpoint}/json/new?about:blank", timeout=5.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            continue
    raise ValueError(f"Could not create a new page target at {http_endpoint!r}.")


def _normalize_http_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported CDP endpoint {endpoint!r}.")
    path = parsed.path.rstrip("/")
    if path.endswith("/json/version"):
        path = path[: -len("/json/version")]
    elif path.endswith("/json/list"):
        path = path[: -len("/json/list")]
    elif path.endswith("/json"):
        path = path[: -len("/json")]
    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return normalized.geturl().rstrip("/")


def _http_endpoint_from_ws_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"Unsupported CDP endpoint {endpoint!r}.")
    scheme = "https" if parsed.scheme == "wss" else "http"
    normalized = parsed._replace(
        scheme=scheme,
        path="",
        params="",
        query="",
        fragment="",
    )
    return normalized.geturl().rstrip("/")


def _is_page_websocket_endpoint(endpoint: str) -> bool:
    return "/devtools/page/" in urlparse(endpoint).path


def _cleanup_launch_session(
    *,
    process: subprocess.Popen[bytes],
    artifact_dir: Path,
    user_data_dir: Path,
    remove_user_data_dir: bool,
    keep_browser_open: bool,
) -> None:
    if not keep_browser_open and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    if remove_user_data_dir:
        shutil.rmtree(user_data_dir, ignore_errors=True)
    if not keep_browser_open:
        shutil.rmtree(artifact_dir, ignore_errors=True)
