"""Best-effort asynchronous notifications through an ntfy server."""

import os
import queue
import threading
import urllib.parse
import urllib.request


class NtfyNotifier:
    """Publish notifications without blocking acquisition or output threads."""

    _STOP = object()

    def __init__(
        self,
        topic,
        server_url="https://ntfy.sh",
        token_env="NTFY_TOKEN",
        timeout_seconds=5.0,
        timing_log=None,
        opener=None,
    ):
        if topic.startswith(("http://", "https://")):
            self._url = topic
        else:
            self._url = (
                server_url.rstrip("/") + "/" + urllib.parse.quote(topic, safe="")
            )
        self._token = os.environ.get(token_env) if token_env else None
        self._timeout_seconds = timeout_seconds
        self._timing_log = timing_log
        self._opener = opener or urllib.request.urlopen
        self._queue = queue.Queue()
        self._closed = False
        self._worker = threading.Thread(
            target=self._publish_pending,
            name="ntfy-notifier",
            daemon=True,
        )
        self._worker.start()

    @classmethod
    def from_config(
        cls,
        config,
        timing_log=None,
        topic_attribute="ntfy_topic_info",
    ):
        topic = getattr(config, topic_attribute, None)
        if not topic:
            return None
        return cls(
            topic,
            server_url=getattr(config, "ntfy_server_url", "https://ntfy.sh"),
            token_env=getattr(config, "ntfy_token_env", "NTFY_TOKEN"),
            timeout_seconds=getattr(config, "ntfy_timeout_seconds", 5.0),
            timing_log=timing_log,
        )

    def notify(self, title, message, priority=3, tags=("warning",)):
        if self._closed:
            return
        self._queue.put((title, message, priority, tuple(tags)))

    def _publish_pending(self):
        while True:
            notification = self._queue.get()
            try:
                if notification is self._STOP:
                    return
                self._publish(*notification)
            finally:
                self._queue.task_done()

    def _publish(self, title, message, priority, tags):
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Priority": str(priority),
            "Tags": ",".join(tags),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self._url,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                response.read()
        except Exception as exc:
            self._record(
                "notification.failed",
                title=title,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        else:
            self._record("notification.sent", title=title)

    def _record(self, event, **fields):
        if self._timing_log is None:
            return
        try:
            self._timing_log.record(event, **fields)
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._queue.put(self._STOP)
        self._worker.join()
