"""Thread-safe event bus for decoupling backend workers from UI consumers."""

from collections import defaultdict
import threading
from typing import Any, Callable

EventCallback = Callable[[Any], None]


class EventBus:
    """Thread-safe publish-subscribe event bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)

    def subscribe(self, event_name: str, callback: EventCallback) -> None:
        """Register a callback for the given event name."""
        with self._lock:
            listeners = self._subscribers[event_name]
            if callback not in listeners:
                listeners.append(callback)

    def unsubscribe(self, event_name: str, callback: EventCallback) -> None:
        """Unregister a previously registered callback."""
        with self._lock:
            listeners = self._subscribers.get(event_name)
            if listeners and callback in listeners:
                listeners.remove(callback)

    def emit(self, event_name: str, payload: Any = None) -> None:
        """Broadcast an event payload to all subscribers without holding the lock."""
        with self._lock:
            listeners = list(self._subscribers.get(event_name, ()))

        for callback in listeners:
            try:
                callback(payload)
            except Exception:
                pass


bus = EventBus()
