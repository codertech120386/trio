#!/usr/bin/env python3
"""worker — a PORTLESS background service. No HTTP listener at all.

This is not filler. Portless services have their own failure modes in Fleet and each one was a real
bug:

  - fleetd's readiness check used `containerAddr`, which reports "not running" for anything without
    a port — so a portless service could never pass readiness and blue-green discarded every new
    version of it (#53/#59). Workers sat frozen on old code for a day.
  - the cross-VM ingress proxy bound `net.Listen(":0")` for a portless app: an arbitrary ephemeral
    port no peer endpoint points at. Observed live as `ingress up for …-worker on :0`.
  - the edge can never hold a tunnel session for a portless app, so "absent from /connected" is its
    normal permanent state, not a fault. Treating that as unhealthy made whole groups unable to
    report Live.

So a trio without a portless member is not a realistic test of a compose group.

It has no health check either, which is correct: Fleet only probes over HTTP, and drift-home
therefore treats a probe-less app as ineligible for migration. That is deliberate — see the trio
README.

Progress goes to stdout so `fleet logs` shows it doing something, and so a test can assert it is
alive without a port to poll.
"""

import os
import socket
import sys
import time

NAME = os.environ.get("SERVICE_NAME", "worker")
INTERVAL_S = float(os.environ.get("TICK_INTERVAL_S", "5"))

# Optional ballast, set once at startup: a portless service still consumes memory, and the
# autoscaler's MEMORY signal is a group-average across the tier — so a heavy worker can pull the
# whole group toward a scale-out. Set WORKER_HOLD_MB to exercise that.
HOLD_MB = int(os.environ.get("WORKER_HOLD_MB", "0"))


def main() -> None:
    host = socket.gethostname()
    ballast = []
    if HOLD_MB > 0:
        block = bytearray(HOLD_MB * 1024 * 1024)
        for i in range(0, len(block), 4096):  # touch each page so the RSS is real
            block[i] = 1
        ballast.append(block)
        print(f"{NAME} holding {HOLD_MB} MB", flush=True)

    print(f"{NAME} started on {host} (no listening port — this is intentional)", flush=True)
    tick = 0
    while True:
        tick += 1
        # One line per tick, with the hostname: a test can read `fleet logs` and tell WHICH node
        # the worker is running on without being able to curl it.
        print(f"{NAME} tick={tick} host={host} held_mb={HOLD_MB}", flush=True)
        sys.stdout.flush()
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
