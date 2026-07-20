# fleet-scenario-trio

A three-service docker-compose group for exercising Fleet's group placement, per-service
autoscaling, and — the part that keeps breaking — **cross-node east-west networking**.

```
web (public, :8080)  ──/call/api──▶  api (internal, :9090)
worker (PORTLESS)                     background ticker
```

Covers **scenario 3** (compose group, steady traffic), **scenario 4** (per-service scaling, which
necessarily spills the group across nodes), and is the building block for **5 and 6** (deploy it
several times under one tenant).

## Why each piece is shaped this way

**`web` calls `api` for real.** A group whose services never talk to each other tests nothing about
east-west. When Fleet spills a group across nodes the call travels:

```
web container -> group network gateway (fleetd egress proxy)
  -> WireGuard overlay -> peer's multiplexed ingress (:7301, app named in a handshake)
    -> api container
```

Every hop in that chain was broken in production at some point and none of it was caught by unit
tests — because a **co-located** group never uses it, and co-located is the default.

**`worker` has no port and no health check.** Both deliberate. Portless services have their own
failure modes in Fleet, each of which was a real bug: readiness checks that reported "not running"
for anything portless, an ingress proxy binding `:0`, and a healthy-but-unroutable state being read
as a fault. A trio without a portless member is not a realistic compose group.

**`deploy.resources.limits.memory` is set on every service.** Fleet maps it to a tier. Omit it and
you get the default (standard, 1 GB) per service — on a small donor that is the difference between
the group placing and sitting in "warming up" indefinitely.

## Reading the east-west result

`GET /call/api` returns:

```jsonc
{
  "ok": true,
  "ms": 27.4,
  "caller": { "hostname": "cdeddd789479", ... },   // which web replica called
  "api":    { "hostname": "8c5243676da2", ... },   // which api replica answered
  "api_saw_client": "172.20.0.4"                   // ← the one that matters
}
```

**Do not compare hostnames to decide whether the call crossed a node.** web and api are separate
containers either way, so hostnames *always* differ and prove nothing. (I wrote that check first;
it reports "different node" 100% of the time, including on a single laptop.)

`api_saw_client` is the real evidence — the address `api` observed the call arriving from:

| value | meaning |
|---|---|
| a container IP on api's own subnet (`172.x.0.y`) | direct docker DNS — **co-located**, never left the node |
| the node's own address | arrived via fleetd's egress → overlay → ingress chain — **crossed nodes** |

A cross-node hop is also visibly slower, but latency is a smell, not proof. Assert on the address.

## Endpoints

`web` and `api` share these; `worker` has none.

| endpoint | purpose |
|---|---|
| `GET /healthz` | liveness — **required**, drift-home refuses apps without a health check |
| `GET /` `/info` | hostname, pid, uptime, held MB, request count (api also reports `client_ip`) |
| `GET /mem?mb=N` | hold N MB, pages touched so RSS really rises — **the autoscale lever** |
| `GET /mem/release` | free it |
| `GET /slow?ms=N` | hold the connection, building queue depth without burning CPU |
| `GET /crash` | `exit(1)` after replying — prove the container comes back |
| `GET /call/api` | *(web only)* the east-west call |

## The thing that will waste your afternoon

**CPU is not an autoscale signal.** Fleet scales on **memory** (percentage of the service's tier)
and on **request rate** (only when `target_rps` is set on that service). It never looks at CPU. A
load test that pegs the CPU scales nothing, and "autoscaling is broken" would be the wrong
conclusion. Drive `/mem`, or drive requests with `target_rps` set.

Each service is its own app in Fleet with its own autoscaler state, so `web` can scale to 4 while
`api` stays at 2 — that is scenario 4.

## Scenario 3 — steady traffic, co-located

Deploy as-is. Assert:

- the public URL returns `200`
- `/call/api` returns `200` with `ok: true`
- `api_saw_client` is a **container IP** (group is co-located, so nothing crossed a node)
- all three services report healthy; `worker` logs ticks

## Scenario 4 — per-service scaling, group spills across nodes

Scale only `web` (`replicas: 2+`, or drive `/mem` past its tier). Fleet places at most one replica
of an app per node, so web's second replica lands on a **different node** while `api` stays put —
the group is now spilled, and east-west is live.

Assert:

- `/call/api` **still returns 200** from every web replica
- at least one call reports `api_saw_client` as a **node address**, not a container IP — this is
  the proof the overlay carried it
- no `bind: address already in use` and no `not a group peer` in the agents' logs

## Port collision variant

`docker-compose.collision.yml` puts `web` and `api` **both on 8080**:

```sh
docker compose -f docker-compose.yml -f docker-compose.collision.yml up --build
```

Two services of one group sharing a port used to break cross-node east-west silently — the ingress
proxy bound the app's own port on the host, and the second service lost the race. Fleet now
multiplexes east-west onto one port per node, so this is structurally impossible; the variant is
the regression test. Deploy it, spill the group, assert `/call/api` still returns 200.

## Running locally

```sh
docker compose up --build            # or WEB_PORT=18080 if 8080 is taken
curl localhost:8080/healthz
curl localhost:8080/call/api         # ok:true, api_saw_client = a container IP
docker compose logs worker           # ticks
docker compose down
```

`WEB_PORT` only affects local runs. Fleet ignores the host side entirely — containers there have
their own IPs and publish nothing.
