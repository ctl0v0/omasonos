from __future__ import annotations

import logging
import os
import queue
from typing import Any

LOG = logging.getLogger(__name__)
SUBSCRIPTION_LEASE_SEC = 300


class WakeQueue(queue.Queue[Any]):
    """Thread-safe event queue which also wakes a select-based protocol loop."""

    def __init__(self) -> None:
        super().__init__()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        super().put(item, block=block, timeout=timeout)
        try:
            os.write(self.write_fd, b"\0")
        except (BlockingIOError, OSError):
            pass

    def drain(self) -> int:
        count = 0
        while True:
            try:
                os.read(self.read_fd, 4096)
            except BlockingIOError:
                break
        while True:
            try:
                self.get_nowait()
                count += 1
            except queue.Empty:
                return count

    def close(self) -> None:
        os.close(self.read_fd)
        os.close(self.write_fd)


class EventSubscriptionManager:
    def __init__(self, event_queue: WakeQueue) -> None:
        self.event_queue = event_queue
        self.subscriptions: dict[str, Any] = {}
        self.errors: list[str] = []
        self.complete = False

    def reconcile(self, services: dict[str, Any]) -> dict[str, Any]:
        desired = set(services)
        for key in list(self.subscriptions):
            if key not in desired:
                self._unsubscribe(key)

        self.errors = []
        for key, service in services.items():
            if key in self.subscriptions:
                continue
            try:
                subscription = service.subscribe(
                    requested_timeout=SUBSCRIPTION_LEASE_SEC,
                    auto_renew=True,
                    event_queue=self.event_queue,
                    strict=True,
                )
                subscription.auto_renew_fail = self._auto_renew_failed
                self.subscriptions[key] = subscription
            except Exception as exc:  # noqa: BLE001 - network fallback is intentional
                message = f"{key}: {type(exc).__name__}: {exc}"
                self.errors.append(message)
                LOG.warning("Sonos event subscription failed: %s", message)

        listener = ""
        try:
            from soco.events import event_listener

            if event_listener.is_running:
                host, port = event_listener.address
                listener = f"{host}:{port}"
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"listener: {type(exc).__name__}: {exc}")

        self.complete = bool(services) and len(self.subscriptions) == len(services)
        return {
            "mode": "events" if self.complete else "polling",
            "listener": listener,
            "subscribed": len(self.subscriptions),
            "requested": len(services),
            "errors": list(self.errors),
        }

    def _auto_renew_failed(self, exc: Exception) -> None:
        message = f"auto-renew: {type(exc).__name__}: {exc}"
        self.errors.append(message)
        self.complete = False
        LOG.warning("Sonos event subscription renewal failed: %s", message)

    def _unsubscribe(self, key: str) -> None:
        subscription = self.subscriptions.pop(key, None)
        if subscription is None:
            return
        try:
            # A synchronous UNSUBSCRIBE can block once per Sonos service and
            # make shell reloads take minutes. Cancel locally; the short lease
            # expires on the speaker even if the process disappears.
            cancel = getattr(subscription, "_cancel_subscription", None)
            if callable(cancel):
                cancel("OmaSonos local subscription shutdown")
            else:
                subscription.unsubscribe(strict=False)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Could not unsubscribe %s: %s", key, exc)

    def close(self) -> None:
        for key in list(self.subscriptions):
            self._unsubscribe(key)
        self.event_queue.close()
        self.complete = False
