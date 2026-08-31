"""Compatibility shim for the removed direct MDC-NG dispatcher.

The 115 plugin intentionally stops at a local STRM file. MDC-NG should watch
the configured incoming directory so it can apply its own AV/UC/porn routing,
scraping, and hardlink policy. Keeping this no-op entry point avoids import
errors for older installations while ensuring no organizer task is submitted.
"""

from __future__ import annotations

from typing import Any


async def process_pipeline_once(_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return without dispatching a remote organizer task."""
    return []
