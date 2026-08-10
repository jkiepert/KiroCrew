"""code-review-sage builtin app — GitHub PR review (KiroCrew OSS port)."""
import asyncio
import logging

from .backend.routes import register_routes
from .sage_lib import chat_session

logger = logging.getLogger(__name__)

__all__ = ["register_routes", "on_disable"]


def on_disable(app: object) -> None:
    """Close every retained review chat when the app is disabled.

    Disabling an app has to withdraw its runtime, not just its UI. A retained chat
    holds a live ACP session and a batch lease on the shared kiro-cli subprocess,
    so without this the process stays pinned and the session stays promptable —
    a disabled app that can still run tools.

    Invoked by ``apps/routes.py`` on the disable path, which is synchronous, so the
    close is scheduled on the running loop when there is one and skipped when the
    registry was never built (nothing to release).
    """
    registry = chat_session.peek_registry()
    if registry is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop (CLI/teardown)
        logger.debug("no running loop; retained chats close with the process")
        return
    # Fire-and-forget: the disable response must not block on subprocess teardown,
    # and a failure here is logged rather than failing the disable.

    def _report(task: "asyncio.Task") -> None:
        exc = task.exception()
        if exc is not None:
            logger.warning("closing retained review chats on disable failed: %s",
                           exc)

    loop.create_task(registry.close_all()).add_done_callback(_report)
