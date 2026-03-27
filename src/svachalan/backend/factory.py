from __future__ import annotations

from svachalan.backend.chromium import ChromiumBackend
from svachalan.contracts.backend import AutomationBackend, BackendConfig


def create_backend(config: BackendConfig) -> AutomationBackend:
    if config.session is None:
        raise ValueError("BackendConfig.session is required.")
    return ChromiumBackend(config.session)
