"""Run async callables from sync code, even if an event loop is already active."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
from typing import Any, Callable, TypeVar

import anyio

T = TypeVar("T")


def run_async(func: Callable[..., Any], *args: Any) -> Any:
    """Execute *func(*args)* in an async context and return the result.

    If no event loop is running on the current thread, this delegates to
    ``anyio.run(func, *args)`` which creates a fresh loop.

    When an event loop **is** already running (e.g. the call originates from
    inside a FastAPI background thread that shares an event-loop with the
    server), ``anyio.run`` raises ``RuntimeError``.  In that case we spin up a
    short-lived worker thread whose sole job is to call ``anyio.run`` in a
    clean context, then block on its result with a ``Future``.

    This keeps the calling code synchronous while guaranteeing the async
    callable always gets a proper event loop.
    """
    try:
        asyncio.get_running_loop()
        # An event loop is active — cannot use anyio.run() here.
    except RuntimeError:
        # No active loop: the simple path.
        return anyio.run(func, *args)

    # Fallback: run in a new thread that has no event loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fmj-async-compat") as pool:
        future = pool.submit(anyio.run, func, *args)
        return future.result()
