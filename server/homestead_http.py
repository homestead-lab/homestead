"""Bounded HTTP connections and deadlines, including slow trickle clients."""
import socket
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDLE_SECONDS = 15
HEADER_SECONDS = 15
BODY_SECONDS = 30
MAX_CONNECTIONS = 64


@contextmanager
def deadline(connection, seconds):
    def expire():
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, max_connections=MAX_CONNECTIONS, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(IDLE_SECONDS)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LimitedHandler(BaseHTTPRequestHandler):
    """For standalone helpers: header deadline plus strict small body framing."""
    timeout = IDLE_SECONDS
    max_body = 4096

    def handle_one_request(self):
        self._header_deadline = deadline(self.connection, HEADER_SECONDS)
        self._header_deadline.__enter__()
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True
        finally:
            if self._header_deadline:
                self._header_deadline.__exit__(None, None, None)

    def parse_request(self):
        valid = super().parse_request()
        self._header_deadline.__exit__(None, None, None)
        self._header_deadline = None
        if not valid:
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) > 1 or
                lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
            self.send_error(400, "invalid request framing")
            self.close_connection = True
            return False
        if lengths and int(lengths[0]) > self.max_body:
            self.send_error(413)
            self.close_connection = True
            return False
        if self.command not in ("POST", "PUT", "PATCH") and lengths and int(lengths[0]):
            self.send_error(400, "unexpected request body")
            self.close_connection = True
            return False
        return True
