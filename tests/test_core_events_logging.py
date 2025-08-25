import logging
from core import events
from core.events import Event


def teardown_function():
    events._subscribers.clear()
    events._global_subscribers.clear()


def test_subscribe_logs(caplog):
    def handler(event: Event) -> None:
        pass
    with caplog.at_level(logging.DEBUG, logger="core.events"):
        events.subscribe("kind", handler)
    assert ("core.events", logging.DEBUG, "Subscribed handler to kind") in caplog.record_tuples


def test_subscribe_all_logs(caplog):
    def handler(event: Event) -> None:
        pass
    with caplog.at_level(logging.DEBUG, logger="core.events"):
        events.subscribe_all(handler)
    assert (
        "core.events",
        logging.DEBUG,
        "Subscribed handler to all events",
    ) in caplog.record_tuples


def test_publish_logs(caplog):
    evt = Event(kind="kind", attrs={"foo": "bar"})
    with caplog.at_level(logging.INFO, logger="core.events"):
        events.publish(evt)
    assert (
        "core.events",
        logging.INFO,
        "Publish event kind attrs={'foo': 'bar'}",
    ) in caplog.record_tuples
