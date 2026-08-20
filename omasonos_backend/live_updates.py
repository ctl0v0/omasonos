from __future__ import annotations

import logging
import os
import queue
import threading
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

    def drain_items(self) -> list[Any]:
        while True:
            try:
                os.read(self.read_fd, 4096)
            except BlockingIOError:
                break
        items: list[Any] = []
        while True:
            try:
                items.append(self.get_nowait())
            except queue.Empty:
                return items

    def drain(self) -> int:
        return len(self.drain_items())

    def close(self) -> None:
        os.close(self.read_fd)
        os.close(self.write_fd)


class TaggedEventQueue:
    """Attach the subscription identity without touching SoCo callback threads."""

    def __init__(self, target: WakeQueue, subscription_key: str) -> None:
        self.target = target
        self.subscription_key = subscription_key

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        self.target.put(
            {"subscriptionKey": self.subscription_key, "event": item},
            block=block,
            timeout=timeout,
        )


class EventSubscriptionManager:
    def __init__(self, event_queue: WakeQueue) -> None:
        self.event_queue = event_queue
        self.subscriptions: dict[str, Any] = {}
        self.errors: list[str] = []
        self.complete = False
        self._invalid: set[str] = set()
        self._lock = threading.RLock()

    def reconcile(self, services: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            desired = set(services)
            for key, subscription in list(self.subscriptions.items()):
                healthy = bool(getattr(subscription, "is_subscribed", True))
                time_left = getattr(subscription, "time_left", None)
                if time_left is not None and time_left <= 0:
                    healthy = False
                if key not in desired or key in self._invalid or not healthy:
                    self._unsubscribe(key)
            self._invalid.clear()

            self.errors = []
            for key, service in services.items():
                if key in self.subscriptions:
                    continue
                try:
                    subscription = service.subscribe(
                        requested_timeout=SUBSCRIPTION_LEASE_SEC,
                        auto_renew=True,
                        event_queue=TaggedEventQueue(self.event_queue, key),
                        strict=True,
                    )
                    subscription.auto_renew_fail = (
                        lambda exc, subscription_key=key: self._auto_renew_failed(
                            subscription_key, exc
                        )
                    )
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

        with self._lock:
            self.complete = (
                bool(services)
                and not self._invalid
                and len(self.subscriptions) == len(services)
            )
            subscribed = len(self.subscriptions)
        return {
            "mode": "events" if self.complete else "polling",
            "listener": listener,
            "subscribed": subscribed,
            "requested": len(services),
            "errors": list(self.errors),
        }

    def _auto_renew_failed(self, key: str, exc: Exception) -> None:
        message = f"{key} auto-renew: {type(exc).__name__}: {exc}"
        with self._lock:
            self.errors.append(message)
            self._invalid.add(key)
            self.complete = False
        LOG.warning("Sonos event subscription renewal failed: %s", message)
        # Wake the protocol loop immediately so reconcile can replace the dead
        # subscription instead of waiting for the next background poll.
        self.event_queue.put({"type": "subscription-renewal-failed", "key": key})

    def _unsubscribe(self, key: str) -> None:
        with self._lock:
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
        with self._lock:
            for key in list(self.subscriptions):
                self._unsubscribe(key)
        self.event_queue.close()
        self.complete = False
