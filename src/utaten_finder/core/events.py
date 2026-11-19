"""Thread-safe event bus and console log capture for decoupling backend from UI."""

from collections import defaultdict, deque
from datetime import datetime
import sys
import threading
from typing import Any, Callable, TextIO

EventCallback = Callable[[Any], None]

_MAX_LOG_ENTRIES = 400
_log_lock = threading.Lock()
_recent_logs: deque[dict[str, str]] = deque(maxlen=_MAX_LOG_ENTRIES)
_capture_installed = False
_capture_guard = threading.local()


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


def _infer_log_level(message: str, default_level: str) -> str:
    lowered = message.lower()
    if default_level == "error" or "[error]" in lowered or "❌" in message:
        return "error"
    if "[warn" in lowered or "⚠️" in message:
        return "warn"
    if "[debug" in lowered:
        return "debug"
    return default_level


def _is_console_progress_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and "% |" in stripped and ("█" in stripped or "░" in stripped)


def emit_log(message: str, level: str = "info") -> dict[str, str]:
    """Record a log entry in the recent buffer and broadcast it over the event bus."""
    entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    with _log_lock:
        _recent_logs.append(entry)
    bus.emit("log_message", entry)
    return entry


def get_recent_logs() -> list[dict[str, str]]:
    """Return a copy of buffered log entries."""
    with _log_lock:
        return list(_recent_logs)


def clear_recent_logs() -> None:
    """Clear the buffered log history."""
    with _log_lock:
        _recent_logs.clear()


class _LogTeeStream:
    """Tee stdout/stderr writes into EventBus logs safely, even under pythonw.exe."""

    def __init__(self, original: TextIO | None, default_level: str) -> None:
        self._original = original
        self._default_level = default_level
        self._buffer = ""
        self._stream_lock = threading.Lock()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._original, "errors", "replace") or "replace"

    def isatty(self) -> bool:
        if self._original is not None and hasattr(self._original, "isatty"):
            try:
                return bool(self._original.isatty())
            except Exception:
                return False
        return False

    def fileno(self) -> int:
        if self._original is not None and hasattr(self._original, "fileno"):
            return self._original.fileno()
        raise OSError("Stream does not have a file descriptor")

    def reconfigure(self, *args: Any, **kwargs: Any) -> None:
        if self._original is not None and hasattr(self._original, "reconfigure"):
            self._original.reconfigure(*args, **kwargs)

    def write(self, text: str) -> int:
        if not text:
            return 0

        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                pass

        if getattr(_capture_guard, "active", False):
            return len(text)

        completed_lines: list[str] = []
        with self._stream_lock:
            if "\r" in text and "\n" not in text:
                # Console progress bar overwrite; ignore partial buffer noise.
                self._buffer = ""
                return len(text)

            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if "\r" in line:
                    line = line.rsplit("\r", 1)[-1]
                cleaned = line.rstrip()
                if cleaned and not _is_console_progress_line(cleaned):
                    completed_lines.append(cleaned)

        if completed_lines:
            _capture_guard.active = True
            try:
                for line in completed_lines:
                    level = _infer_log_level(line, self._default_level)
                    emit_log(line, level=level)
            finally:
                _capture_guard.active = False

        return len(text)

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass


def install_console_log_capture() -> None:
    """Wrap sys.stdout and sys.stderr once to mirror console output into EventBus."""
    global _capture_installed
    with _log_lock:
        if _capture_installed:
            return
        sys.stdout = _LogTeeStream(sys.stdout, "info")  # type: ignore[assignment]
        sys.stderr = _LogTeeStream(sys.stderr, "error")  # type: ignore[assignment]
        _capture_installed = True
