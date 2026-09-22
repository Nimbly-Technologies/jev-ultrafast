"""Direct Chrome DevTools Protocol over one WebSocket, optionally through an SSH tunnel. No daemon.

CDP_URL is the browser's HTTP debugging endpoint as seen from CDP_SSH, or from this machine if CDP_SSH is empty.
"""

import atexit
import itertools
import json
import os
import socket
import subprocess
import threading
import time
from collections import deque
from urllib.parse import urlparse
from urllib.request import urlopen

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


class Connection:
    def __init__(self, url, ssh=None):
        endpoint = urlparse(url)
        host, port = endpoint.hostname or "127.0.0.1", endpoint.port or 9222
        self.tunnel = None
        if ssh:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                local = probe.getsockname()[1]
            self.tunnel = subprocess.Popen(
                ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
                 "-L", f"127.0.0.1:{local}:{host}:{port}", ssh],
                stdin=subprocess.DEVNULL,
            )
            host, port = "127.0.0.1", local
        # Chrome rejects a Host header that is not an IP or localhost, so both the HTTP probe and the
        # WebSocket URL are addressed by IP (or the tunnel's 127.0.0.1 port), never by hostname.
        version = self._version(host, port)
        self.user_agent = version["User-Agent"]
        ws_url = urlparse(version["webSocketDebuggerUrl"])._replace(netloc=f"{host}:{port}").geturl()
        self.ws = connect(ws_url, max_size=None, open_timeout=10, proxy=None)
        self.ids = itertools.count(1)
        self.pending = {}
        self.lock = threading.Lock()
        self.events = deque(maxlen=2000)
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _version(self, host, port):
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://{host}:{port}/json/version", timeout=3) as response:
                    return json.load(response)
            except OSError:
                if self.tunnel and self.tunnel.poll() is not None:
                    raise RuntimeError(f"SSH tunnel exited with {self.tunnel.returncode}") from None
                if time.monotonic() > deadline:
                    raise RuntimeError(f"No CDP endpoint at {host}:{port}") from None
                time.sleep(0.1)

    def _read(self):
        try:
            for raw in self.ws:
                message = json.loads(raw)
                if "id" in message:
                    with self.lock:
                        slot = self.pending.pop(message["id"], None)
                    if slot:
                        slot[1] = message
                        slot[0].set()
                else:
                    self.events.append(
                        {"method": message["method"], "params": message.get("params", {}),
                         "session_id": message.get("sessionId")}
                    )
        except ConnectionClosed:
            pass
        finally:
            self.closed = True
            with self.lock:
                slots, self.pending = list(self.pending.values()), {}
            for slot in slots:
                slot[0].set()

    def call(self, method, session_id=None, timeout=30, **params):
        if self.closed:
            raise RuntimeError("CDP connection closed")
        slot = [threading.Event(), None]
        with self.lock:
            key = next(self.ids)
            self.pending[key] = slot
        message = {"id": key, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        self.ws.send(json.dumps(message))
        if not slot[0].wait(timeout):
            with self.lock:
                self.pending.pop(key, None)
            raise RuntimeError(f"{method} timed out after {timeout}s")
        response = slot[1]
        if response is None:
            raise RuntimeError("CDP connection closed")
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result", {})

    def drain(self):
        events = []
        while self.events:
            events.append(self.events.popleft())
        return events

    def close(self):
        self.ws.close()
        if self.tunnel:
            self.tunnel.terminate()
            self.tunnel.wait(timeout=5)


_connection = None
_connection_lock = threading.Lock()


def connection():
    global _connection
    with _connection_lock:
        if _connection is None or _connection.closed:
            _connection = Connection(
                os.environ.get("CDP_URL", "http://127.0.0.1:9222"), os.environ.get("CDP_SSH") or None
            )
            atexit.register(_connection.close)
        return _connection


def cdp(method, session_id=None, **params):
    return connection().call(method, session_id=session_id, **params)


def drain_events():
    return connection().drain()
