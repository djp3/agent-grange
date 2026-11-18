# agent-grange v1 execution plan

Companion to `requirements.md` (requirement numbers below refer to it). Eight
phases, each ending in something that runs and is checked. Phases 1 to 4 need no
tailnet and no headscale, so they can be built and tested entirely on one laptop
with Docker. Phases 5 and 6 bring in the sidecar and the control server. Phase 7
is documentation and developer experience, phase 8 is rollout.

| phase | delivers | size | depends on |
|---|---|---|---|
| 0 | repo layout, package skeleton, test runner | S | |
| 1 | model and state, pure Python, unit tested | M | 0 |
| 2 | local API: farm push, frolic long-poll, health | M | 1 |
| 3 | gossip: exchange, push, repair, forward-once, leaving | L | 2 |
| 4 | plain-network harness and automated scenarios | M | 3 |
| 5 | Tailscale sidecar, tag discovery, whois check | M | 4, spike 5.0 |
| 6 | local headscale mode | M | 5 |
| 7 | runbook, env example, README, CI | S | 6 |
| 8 | two-laptop smoke test, move the tag, farm and frolic integration | S | 7 |

S is a sitting, M a day, L two to three days, as rough relative sizes.

## Ground rules

- Standard library only in the image (R8.2). Tests use `unittest` so the
  repo has zero Python dependencies anywhere.
- Every timing knob is an environment variable with the default from section 7
  of the requirements, and every module takes a clock function so tests run in
  simulated time.
- The freshness check, Makefile git metadata, and `make update` flow are kept
  as they are. New work plugs in beside them.
- Nothing is committed to `main` without its tests passing; the tag moves only
  in phase 8.

## Phase 0: layout

Reshape `docker/content/` into a package so the server stops being one file,
and give tests a home.

```
docker/
  Makefile  docker-compose.yml  Dockerfile  .dockerignore  .env.example
  compose.plain.yml          phase 4: several granges, plain Docker network
  compose.headscale.yml      phase 6: local headscale plus sidecars
  content/
    entrypoint.py  freshness.py  healthcheck.py   unchanged
    server.py                                     thin: wires the pieces below
    grange/
      __init__.py
      config.py      env parsing, defaults, validation
      model.py       Agent, Record, version ordering, size limits
      state.py       Store: merge, expiry, forget map, cursor, farm_stale, wait
      api_local.py   /v1/farm, /v1/agents, /health, /version
      api_gossip.py  /v1/gossip handler and the exchange client
      gossip.py      heartbeat, repair tick, push on change, neighbours, leaving
      discovery.py   static peers; later the Tailscale local API
  tests/             unittest modules, one per grange module
  tools/
    fake_farm.py     scripted pusher for the harness
    scenario.py      drives and times the phase 4 scenarios
    query_server.py  moved here; label discovery kept as a fallback tool
docs/
  requirements.md  plan.md  headscale.md
```

Changes: Dockerfile copies the whole `content/` tree, `.dockerignore` allows
`content/**/*.py`, Makefile gains `test`, `test-plain`, `test-headscale`.
Done when `make test` runs an empty suite in the image and `make up` still
starts the current server unchanged.

## Phase 1: model and state

Pure Python, no sockets. This is where the correctness lives, so it gets the
most tests.

- `model.py`: `Agent(owner, name, state)`, `Record(proto, group, grange,
  incarnation, counter, farm_age_s, leaving, agents)`, `version()` returning
  the `(incarnation, counter)` tuple, `validate()` enforcing R4.3 limits and
  types, JSON in and out.
- `state.py`: `Store(clock, cfg)` with
  - `apply(record) -> changed` implementing R4.5, R6.5, R6.6, R6.7;
  - `local()` building this grange's record: incarnation chosen at
    construction, `bump()` for heartbeat, `set_agents(list) -> changed`,
    `farm_age_s` and `farm_stale` from the last push time;
  - `expire()` implementing R6.4 and moving expired versions to the forget map;
  - `cursor` and `wait_for(after, timeout)` on a `threading.Condition`,
    advancing only on the R5.6 events;
  - `picture()` producing the R5.5 response body with `seen_age_s`;
  - `digest()` and `newer_than(digest)` for the gossip exchange.

Tests (R10.1): ordering and ties; restart with higher incarnation replaces
without expiry; expiry at exactly `EXPIRE_SECONDS`; a stale copy from a
lagging peer is refused after removal and a higher one is admitted; `leaving`
removes at once; heartbeat does not move the cursor, an agent change does, a
peer appearing or expiring does, `farm_stale` flipping does; oversize input is
rejected; same-named agents are both kept.

## Phase 2: local API

`api_local.py` on `ThreadingHTTPServer`, reusing the existing freshness 503
wrapper so a stale image refuses these routes too.

- `PUT /v1/farm`: parse, validate, `set_agents`, 204; 400 on malformed, 413
  over limits (R5.1 to R5.3).
- `GET /v1/agents`: `after` and `wait` (cap 30), immediate answer when the
  cursor has moved or `after` is ahead of it (R5.7, R5.8), otherwise block on
  `wait_for`.
- `GET /health`: existing freshness block plus peer count, cursor, last gossip
  age (R5.14). `GET /version` unchanged.
- Binding: loopback plus the container's bridge address, found from the
  default route interface; never all interfaces (startup step 4).

Tests: in-process server on an ephemeral port; a push wakes a waiting
long-poll within a few milliseconds; a long-poll with nothing happening
returns at `wait`; a stale cursor returns at once; limits produce 413; the
healthcheck script still passes against it.

## Phase 3: gossip

- `api_gossip.py`: `POST /v1/gossip` handler: reject on `proto` or `group`
  mismatch with 409 (R5.11); reject sources outside the allowed address set
  (R5.12; in plain mode the set is the Docker network); merge `records`;
  reply with `newer_than(digest)` (R5.10). The client side `exchange(peer)`
  sends our digest and the records we believe the peer lacks, merges the
  reply, and reports which records were new.
- `gossip.py`: one scheduler thread and a small worker pool.
  - heartbeat: `bump()` every `HEARTBEAT_SECONDS` (R6.2);
  - repair: every `REPAIR_SECONDS`, `exchange` with one random known peer
    (R6.13); also runs `expire()`;
  - push on change: the Store signals when the local record changed or a
    record arrived that was new; the scheduler pushes that record to all
    neighbours within a second, once (R6.11, R6.12);
  - neighbours: sorted peer addresses, the `FANOUT` entries after our own
    address in ring order (R6.10), recomputed on peer list change;
  - leaving: SIGTERM handler sends a `leaving` record to neighbours, waits at
    most two seconds, exits (R6.16);
  - stale image: when the freshness state flips to stale the scheduler stops
    sending, and the handler already returns 503 (R5.16).
- `discovery.py`: in this phase only the static `GRANGE_PEERS` provider
  (R6.9), with the interface the Tailscale provider will implement in phase 5.

Tests: a simulated mesh of several Stores wired through `exchange` with a fake
transport and a fake clock: convergence after one round of pushes, repair
heals a dropped push, forward-once does not loop, 409 on group mismatch,
leaving propagates. Network tests wait for phase 4.

## Phase 4: plain-network harness

`compose.plain.yml` starts five granges on one Docker network with
`GRANGE_PEERS`, `SKIP_FRESHNESS_CHECK=1`, and each API published on its own
localhost port. `tools/fake_farm.py` pushes a scripted agent list to one
grange with heartbeats, a change at a chosen moment, or silence on command.
`tools/scenario.py` runs the R10.2 list and asserts the timings:

| scenario | pass condition |
|---|---|
| change on one grange | visible on all five `/v1/agents` within 5 s |
| kill one grange | gone from all boards within `EXPIRE_SECONDS` plus 5 s |
| restart it | new incarnation replaces the old record before expiry |
| fake farm goes silent | `farm_stale` true within `FARM_STALE_SECONDS` plus 5 s, agents still listed |
| clean stop | gone from all boards within 3 s |
| stale image | with the mock GitHub from the freshness work, gossip returns 503 and the grange ages out |

`make test-plain` brings the stack up, runs the scenarios, prints a table,
tears down, and exits non-zero on any failure. Traffic is measured here
against R6.15 by reading container network counters.

## Phase 5: Tailscale sidecar

**5.0 spike, half a day, before anything else in this phase.** On one laptop,
hand-run the Tailscale client container against the team's headscale with a
pre-auth key and confirm: registration with the tag; `NET_ADMIN` and
`/dev/net/tun` work under Docker Desktop; the daemon socket can be shared to a
second container; the local API answers the status and whois queries we need,
and which headers it requires. Write the findings into `docs/headscale.md`.
This closes the last open item in the requirements and de-risks the phase.

Then:

- `docker-compose.yml` becomes two services per R8.1: `tailscale` with the
  pinned image, capabilities, state and socket volumes, hostname
  `grange-${GRANGE_ID}`, login server and tag from `.env`, a healthcheck, and
  the `127.0.0.1:${GRANGE_PORT}` publish; `grange` with
  `network_mode: service:tailscale`, `depends_on` healthy, socket mounted
  read-only.
- `discovery.py` gains the Tailscale provider: peers from the daemon's status
  filtered by `TS_TAG` and online state, refreshed every `DISCOVERY_SECONDS`
  (R6.8); `whois(addr)` returning the device's tags.
- Startup: wait up to 60 s for a tailnet address (step 3); bind gossip to it
  and only it (step 4).
- `api_gossip.py`: source must be a tailnet address, and `whois` must report
  the tag, or the exchange is rejected and logged once per peer (R5.13).
- `healthcheck.py` keeps hitting loopback, which still works inside the shared
  namespace.

Checked by: two laptops, or one laptop plus phase 6. `make up` with a filled
`.env` joins, discovers, and gossips with no browser and no approval (R8.3,
R8.4).

## Phase 6: local headscale mode

`compose.headscale.yml`: a pinned headscale container with a config enabling
the embedded relay, a policy file declaring `tag:grange` and the single ACL
rule (R9.1, R9.2), and an init step that creates a reusable tagged pre-auth
key and writes it where the sidecars read it. Three sidecar and grange pairs
register against it on the Docker network, discover each other by tag, and
gossip. A fourth sidecar without the tag proves the ACL and whois rejection.
`make test-headscale` runs the same scenario driver against this stack with
the propagation assertion kept at 5 s.

## Phase 7: documentation and developer experience

- `docs/headscale.md`: the admin runbook from section 9.1 with exact
  commands for headscale 0.28 or later: policy snippet, create key, expire
  key, delete node, what to tell a new teammate.
- `.env.example` with every key from section 7 and a one-line comment each.
- `README.md` rewritten for the two-container system: what it is, how to
  join, how to read the board with curl, how farm and frolic connect, the
  freshness check as it stands.
- GitHub Actions: `make test` on every push and pull request; `make
  test-plain` as well, since it needs only Docker.
- Optional, your call: a workflow that moves `agentic-grange-latest` on merge
  to `main`, which the README already describes as an alternative to moving it
  by hand.

## Phase 8: rollout

1. Smoke test across two real laptops against the team's headscale (R10.4).
2. Hand farm its contract: `PUT /v1/farm` body, heartbeat at most every 30 s,
   whitelist and port in farm's config. Hand frolic its contract: the long-poll
   and response shape, the cursor rule, the `farm_stale` and `seen_age_s`
   fields.
3. Move `agentic-grange-latest` to the release commit. Every running demo
   grange refuses on its next poll, which is the intended signal to run
   `make update`.
4. Watch the first week for expiry flapping on sleep and wake, and tune
   `EXPIRE_SECONDS` if needed. It is an `.env` change, not a release.

## External dependencies

| who | needs to do | by phase |
|---|---|---|
| headscale admin | run headscale 0.28+, define the tag and ACL, issue a key | 5.0 |
| agent-farm | push to `PUT /v1/farm` on change and every 30 s; whitelist and port settings | 8 |
| agent-frolic | long-poll `GET /v1/agents`, map states, draw `farm_stale` and ages | 8 |

## Risks and how the plan handles them

- **Tailscale local API details** (headers, endpoint shapes, socket sharing):
  the phase 5.0 spike settles them before any code depends on them.
- **Docker Desktop and the tun device**: same spike.
- **Headscale version drift** in CLI syntax: pin the image in phase 6 and name
  the version in the runbook.
- **Long-poll threads**: frolic opens one or two connections; the threaded
  server is fine. If a second local consumer appears, revisit.
- **Sleep and wake**: a laptop that sleeps past `EXPIRE_SECONDS` is removed and
  readmitted on wake because its counter is higher than the remembered one.
  Phase 4 cannot simulate this; phase 8 watches for it.
- **Stale image during rollout**: moving the tag takes every grange down until
  updated, by design. Announce the move; do it when the team is not mid-review.

## Order of first sessions

1. Phase 0 and phase 1 together: layout, model, state, tests green.
2. Phase 2: local API, tests green, `make up` still works end to end.
3. Phase 3 and phase 4 together: gossip with the harness, since the harness is
   how gossip gets exercised for real.
4. Phase 5.0 spike, then phase 5.
5. Phase 6, then phase 7, then phase 8 with you present for the smoke test and
   the tag move.
