#!/usr/bin/env python3
"""web — the public entrypoint of the trio, and the thing that PROVES east-west works.

`/call/api` makes a real HTTP request to the `api` sibling and reports what came back. That call
is the whole point of this service: when Fleet spills a group across nodes, `api` may live on a
different machine, and the request then travels

    web container -> the group network gateway (fleetd's egress proxy)
      -> WireGuard overlay -> the peer's multiplexed ingress (:7301, app named in a handshake)
        -> the api container

Every hop in that chain was broken in production at some point on 2026-07-20 and none of it was
caught by unit tests, because a co-located group never uses it. If the group is co-located the same
call resolves through docker DNS and never leaves the node — which is exactly why a test must
assert `via` (below) and not merely that the call returned 200.

The response includes `api.hostname` (which replica answered) and `api_saw_client` (the address api
saw us arrive from). The second is the one that distinguishes a co-located call from a cross-node
one: hostnames always differ, because web and api are separate containers either way.
"""

import http.server
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("PORT", "8080"))
NAME = os.environ.get("SERVICE_NAME", "web")
# `api` is the compose service name. Fleet registers it as a docker network alias inside the
# group's own network, and rewrites /etc/hosts to the egress proxy when the sibling is remote.
API_URL = os.environ.get("API_URL", "http://api:9090")

_started = time.time()
_ballast: list[bytearray] = []
_ballast_lock = threading.Lock()
_requests = 0
_requests_lock = threading.Lock()


def _held_mb() -> int:
    with _ballast_lock:
        return sum(len(b) for b in _ballast) // (1024 * 1024)


def _identity() -> dict:
    return {
        "service": NAME,
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
            return self._send(200, {"ok": True, **_identity()})

        if path in ("/", "/info"):
            return self._send(200, _identity())

        # THE east-west assertion. Returns the sibling's identity so the caller can see which node
        # answered, and how long the round trip took (a cross-node hop is visibly slower than a
        # docker-DNS one, which is a useful smell but NOT proof — assert on hostname, not latency).
        if path == "/call/api":
            t0 = time.time()
            try:
                with urllib.request.urlopen(f"{API_URL}/info", timeout=8) as r:
                    body = json.loads(r.read().decode())
                return self._send(200, {
                    "ok": True,
                    "ms": round((time.time() - t0) * 1000, 1),
                    "caller": _identity(),
                    "api": body,
                    # What api saw as our address. NOT a hostname comparison: web and api are
                    # always separate containers, so comparing hostnames reports "different" even
                    # when co-located and proves nothing. The client_ip api reports is the real
                    # evidence — a container IP on its own subnet means the call went straight
                    # through docker DNS; the node's own address means it came via fleetd's
                    # egress -> overlay -> ingress chain.
                    "api_saw_client": body.get("client_ip", ""),
                })
            except (urllib.error.URLError, OSError, ValueError) as e:
                # A failure here is the interesting case: east-west is down. Return 502 so a probe
                # comparing STATUS CODES (not curl exit codes) sees it.
                return self._send(502, {
                    "ok": False,
                    "ms": round((time.time() - t0) * 1000, 1),
                    "error": str(e),
                    "target": API_URL,
                    "caller": _identity(),
                })

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
    print(f"{NAME} listening on :{PORT} (api at {API_URL})", flush=True)
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()
