from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator


@contextmanager
def initialize_ui_automation_in_current_thread() -> Iterator[None]:
    """Initialize Windows COM/UIAutomation for the calling worker thread."""

    if os.name != "nt":
        yield
        return

    try:
        from uiautomation import UIAutomationInitializerInThread
    except ImportError:
        # Draft-only jobs do not require UI automation. Export jobs will still
        # report the dependency error when pyJianYingDraft loads its controller.
        yield
        return

    with UIAutomationInitializerInThread():
        yield
