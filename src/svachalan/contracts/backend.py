from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from svachalan.contracts.errors import ActionError


class WaitUntil(StrEnum):
    DOMCONTENTLOADED = "domcontentloaded"


class BrowserSessionMode(StrEnum):
    LAUNCH = "launch"
    ATTACH = "attach"


class LaunchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser_path: str | None = None
    headless: bool = False
    keep_browser_open: bool = False
    user_data_dir: str | None = None
    debugging_port: int | None = None


class AttachOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str
    target_id: str | None = None


class BrowserSessionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: BrowserSessionMode = BrowserSessionMode.LAUNCH
    launch: LaunchOptions | None = None
    attach: AttachOptions | None = None


class BrowserSession(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    mode: BrowserSessionMode
    ws_endpoint: str
    http_endpoint: str | None = None
    target_id: str | None = None
    artifact_dir: str | None = None
    browser_path: str | None = None
    user_data_dir: str | None = None
    debugging_port: int | None = None

    _cleanup_callback: Callable[[], None] | None = PrivateAttr(default=None)

    def set_cleanup_callback(self, callback: Callable[[], None]) -> None:
        self._cleanup_callback = callback

    def cleanup(self) -> None:
        if self._cleanup_callback is not None:
            self._cleanup_callback()


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: BrowserSession | None = None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str = "file"
    label: str | None = None


class ElementTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str
    frame_selector: str | None = None


class ActionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int | None = None
    step_id: str | None = None


class NavigationOptions(ActionOptions):
    wait_until: WaitUntil | None = None


class TypeOptions(ActionOptions):
    pass


class ScreenshotOptions(ActionOptions):
    pass


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: Any | None = None
    error: ActionError | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    @classmethod
    def success(
        cls,
        value: Any = None,
        *,
        artifacts: list[ArtifactRef] | None = None,
    ) -> ActionResult:
        return cls(ok=True, value=value, artifacts=artifacts or [])

    @classmethod
    def failure(
        cls,
        error: ActionError,
        *,
        artifacts: list[ArtifactRef] | None = None,
    ) -> ActionResult:
        return cls(ok=False, error=error, artifacts=artifacts or [])


@runtime_checkable
class AutomationBackend(Protocol):
    def goto(self, url: str, opts: NavigationOptions | None = None) -> ActionResult: ...

    def click(self, target: ElementTarget, opts: ActionOptions | None = None) -> ActionResult: ...

    def type(
        self,
        target: ElementTarget,
        text: str,
        opts: TypeOptions | None = None,
    ) -> ActionResult: ...

    def wait_for(
        self,
        target: ElementTarget,
        opts: ActionOptions | None = None,
    ) -> ActionResult: ...

    def assert_exists(
        self,
        target: ElementTarget,
        opts: ActionOptions | None = None,
    ) -> ActionResult: ...

    def extract_text(
        self,
        target: ElementTarget,
        opts: ActionOptions | None = None,
    ) -> ActionResult: ...

    def extract_attr(
        self,
        target: ElementTarget,
        attr: str,
        opts: ActionOptions | None = None,
    ) -> ActionResult: ...

    def screenshot(self, opts: ScreenshotOptions | None = None) -> ActionResult: ...
