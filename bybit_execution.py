from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pybit.unified_trading import WebSocket

logger = logging.getLogger(__name__)


def _create_websocket(**options: Any) -> WebSocket:
    from pybit.unified_trading import WebSocket

    return WebSocket(**options)


class ExecutionStreamListener:
    """Consume Bybit's authenticated private execution stream off the socket thread."""

    def __init__(
        self,
        *,
        testnet: bool,
        domain: str,
        tld: str,
        api_key: str,
        api_secret: str,
        execution_handler: Callable[[object], None],
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Private execution stream requires Bybit API credentials.")
        self._options: dict[str, Any] = {
            "testnet": testnet,
            "domain": domain,
            "tld": tld,
            "api_key": api_key,
            "api_secret": api_secret,
        }
        self._execution_handler = execution_handler
        self._websocket: WebSocket | None = None
        self._queue: queue.Queue[object | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._websocket is not None:
            raise RuntimeError("Bybit execution listener has already been started.")
        self._worker = threading.Thread(
            target=self._dispatch, name="bybit-execution-handler", daemon=True
        )
        self._worker.start()
        try:
            self._websocket = _create_websocket(
                channel_type="private", **self._options
            )
            self._websocket.execution_stream(callback=self._on_message)
        except Exception:
            self.stop()
            raise
        logger.info("Bybit private execution stream subscribed.")

    def stop(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            websocket.exit()
        worker = self._worker
        self._worker = None
        if worker is not None:
            self._queue.put(None)
            worker.join(timeout=10)
            if worker.is_alive():
                raise RuntimeError("Bybit execution handler did not stop cleanly.")

    def _on_message(self, message: object) -> None:
        self._queue.put(message)

    def _dispatch(self) -> None:
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                self._execution_handler(message)
            except Exception:
                logger.exception("Failed to process Bybit execution event.")
