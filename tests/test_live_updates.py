from omasonos_backend.live_updates import EventSubscriptionManager, WakeQueue


class FakeSubscription:
    def __init__(self):
        self.auto_renew_fail = None
        self.unsubscribed = False

    def unsubscribe(self, strict=True):
        self.unsubscribed = True


class FakeService:
    def __init__(self, fail=False):
        self.fail = fail
        self.subscription = FakeSubscription()

    def subscribe(self, **kwargs):
        if self.fail:
            raise OSError("callback blocked")
        self.kwargs = kwargs
        return self.subscription


def test_event_subscriptions_reconcile_and_clean_up():
    event_queue = WakeQueue()
    manager = EventSubscriptionManager(event_queue)
    topology = FakeService()
    transport = FakeService()
    try:
        diagnostics = manager.reconcile(
            {"topology": topology, "transport:R1": transport}
        )
        assert diagnostics["mode"] == "events"
        assert diagnostics["subscribed"] == 2
        assert topology.kwargs["event_queue"] is event_queue
        assert topology.kwargs["auto_renew"] is True
        assert topology.kwargs["requested_timeout"] == 300

        manager.reconcile({"topology": topology})
        assert transport.subscription.unsubscribed is True
    finally:
        manager.close()


def test_subscription_failure_keeps_polling_fallback():
    event_queue = WakeQueue()
    manager = EventSubscriptionManager(event_queue)
    try:
        diagnostics = manager.reconcile(
            {"topology": FakeService(), "transport:R1": FakeService(fail=True)}
        )
        assert diagnostics["mode"] == "polling"
        assert diagnostics["subscribed"] == 1
        assert diagnostics["requested"] == 2
        assert "callback blocked" in diagnostics["errors"][0]
    finally:
        manager.close()


def test_wake_queue_notifies_and_drains_event_bursts():
    event_queue = WakeQueue()
    try:
        event_queue.put(object())
        event_queue.put(object())
        assert event_queue.drain() == 2
    finally:
        event_queue.close()
