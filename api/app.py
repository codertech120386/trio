#!/usr/bin/env python3
"""api — the internal service of the trio, and the TARGET of east-west calls.

Not public: no domain is published for it. `web` reaches it by the compose service name `api`,
which Fleet registers as a docker network alias inside the group's own network. When the group is
spilled across nodes, fleetd rewrites that name to its egress proxy and the call leaves the box.

/info is deliberately identical in shape to web's, so a caller can compare hostnames and tell
whether the replica that answered was co-located or across the overlay.

Port 9090 by default — DIFFERENT from web's 8080 on purpose. See docker-compose.collision.yml for
the variant that deliberately makes them collide.
"""

import http.server
import json
import os
import socket
import threading
import time

PORT = int(os.environ.get("PORT", "9090"))
NAME = os.environ.get("SERVICE_NAME", "api")


_started = time.time()
_ballast: list[bytearray] = []
_ballast_lock = threading.Lock()
_requests = 0
_requests_lock = threading.Lock()


def _held_mb() -> int:
    with _ballast_lock:
        return sum(len(b) for b in _ballast) // (1024 * 1024)


def _identity(client: str = "") -> dict:
    return {
        "service": NAME,
        # The peer address as THIS service saw it. Co-located siblings arrive straight from the
        # caller's container IP (same docker subnet). A cross-node call arrives via fleetd's
        # ingress proxy, which dials us from the node itself — a different address entirely.
        # This is the only evidence available inside the container about which path was used.
        "client_ip": client,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _started, 1),
        "held_mb": _held_mb(),
        "requests": _requests,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        global _requests
        with _requests_lock:
            _requests += 1

        path, _, query = self.path.partition("?")
        args = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "/healthz":
            return self._send(200, {"ok": True, **_identity(self.client_address[0])})

        if path in ("/", "/info"):
            return self._send(200, _identity(self.client_address[0]))

        if path == "/mem":
            mb = max(0, min(int(args.get("mb", "64")), 4096))
            block = bytearray(mb * 1024 * 1024)
            for i in range(0, len(block), 4096):
                block[i] = 1
            with _ballast_lock:
                _ballast.append(block)
            return self._send(200, {"allocated_mb": mb, **_identity()})

        if path == "/mem/release":
            with _ballast_lock:
                _ballast.clear()
            return self._send(200, {"released": True, **_identity()})

        if path == "/slow":
            time.sleep(min(float(args.get("ms", "1000")) / 1000.0, 30.0))
            return self._send(200, _identity())

        if path == "/crash":
            self._send(200, {"crashing": True, **_identity()})
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(1)), daemon=True).start()
            return

        return self._send(404, {"error": "not found", "path": path})

    def log_message(self, fmt: str, *a) -> None:
        print(f"{NAME} {self.address_string()} {fmt % a}", flush=True)


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"{NAME} listening on :{PORT}", flush=True)
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()
