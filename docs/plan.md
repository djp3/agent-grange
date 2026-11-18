# agent-grange v1 execution plan

Companion to `requirements.md`; requirement numbers below refer to it.
Rewritten 2026-11-18 against the requirements as they stood during Don's
manual review, and revised the same day after a third review pass. Not
approved: phase 0 starts only on Don's word. Where this plan and the
requirements disagree, the requirements win and the plan is corrected.

Nine phases, 0 to 8, each ending in something that runs and is checked.
Phases 0 to 4 need no tailnet and no control server, so they are built and
tested on one machine with Docker. Phase 5 brings in a local headscale and the
sidecar. Phase 6 automates that. Phase 7 is documentation and developer
experience. Phase 8 is rollout and is the only phase that needs the team's
control server.

| phase | delivers | size | depends on |
|---|---|---|---|
| 0 | rename to `agent-grange`, fixed local port, package layout, test runner in the image | S | Don's go-ahead for two tag pushes |
| 1 | configuration, model, and state, pure Python, unit tested | M | 0 |
| 2 | local API, ticker, health | M | 1 |
| 3 | gossip, plus the 100-grange simulation | L | 2 |
| 4 | plain-network harness and automated scenarios | M | 3 |
| 5 | local headscale stack, spike on both runtimes, sidecar integration, runbook | L | 4, a Linux host |
| 6 | headscale test mode automated | S | 5 |
| 7 | README, CI, final check of `.env.example` | S | 6 |
| 8 | team headscale, two-machine smoke test, farm and frolic handoff, move the tag | S | 7, team server |

S is a sitting, M a day, L two to three days, as rough relative sizes.

## Ground rules

- Standard library only in the image (R8.2). Tests use `unittest` and run
  inside the built image with the tests bind-mounted (R8.8), so they see the
  interpreter the image ships and the repo has zero Python dependencies. A
  host run is an optional fast loop, not the reference.
- Configuration is environment only (section 7). Defaults for the grange keys
  live in one place, `config.py`. Compose passes each key through from `.env`
  and an empty value means "use the default" (section 7), so the compose file
  does not repeat a default. There are two exceptions, where compose itself
  consumes the value: `GRANGE_PORT` for the port publish, and `TS_TAG` for the
  tag the sidecar advertises. The freshness keys keep the compose defaults
  they have today (R8.5).
- Every module takes a clock object with a monotonic reading for ages and a
  wall reading for incarnations, so tests and the simulation run in simulated
  time.
- The freshness check, the Makefile's git metadata, and the `make update` flow
  are kept as they are, with one exception: the tag is renamed in phase 0
  (R3.9, R8.5).
- Work lands on `main` as plain commits once its tests pass. No pull requests
  and no merge commits (decision 22). When the work is done in a separate
  worktree, as the implementer session's is, it is committed on that
  worktree's branch, which starts from the approved commit on `main`. At the
  end of each phase, on Don's word, `main` is fast-forwarded to that branch
  from the main checkout with `git merge --ff-only`; a worktree cannot move a
  branch that is checked out elsewhere. If `main` has moved in the meantime,
  the branch is rebased onto it first. `main` is pushed to origin only when
  Don says so. The tag moves only in phase 8, by hand (decision 23).
- No organization name anywhere in the repo, the documents, or the code
  (R3.9).
- Nothing before phase 8 depends on the team's control server (R9.0).

## Phase 0: rename, fixed port, layout, test runner

**Rename (R3.9), in this order, because a strict start refuses until its tag
exists:**

1. `make down`, so no container is left running under the old project name.
2. Create `agent-grange-latest` on origin at the commit `agentic-grange-latest`
   points to. This is a push to the public repo; Don confirms it in the
   session doing the work before it runs.
3. Rename every occurrence in tracked files outside `docs/`. Today that is ten
   places in five files:

   | file | what changes |
   |---|---|
   | `docker/Makefile` | the `CHECK_TAG` default |
   | `docker/docker-compose.yml` | the project `name`; the `CHECK_TAG` default |
   | `docker/content/freshness.py` | the `User-Agent` string; the example in the empty-`CHECK_TAG` error message |
   | `README.md` | the tag name |
   | `docker/README.md` | the tag name in four places, two of them the tag-move commands |

   The compose service `web` becomes `grange` in the same commit (R8.1, R8.3).
   The label is already `agent-grange.service`. Commit.
4. `make up` must start, reporting AHEAD of `agent-grange-latest`. The new tag
   stays at the commit it was copied from until phase 8 and `main` is already
   past it, so FRESH is not reachable here; AHEAD does not block a start.
5. Delete `agentic-grange-latest` from origin and locally. Also a push; same
   confirmation.

The rename is done when `git grep -i agentic` finds nothing in tracked files
outside the rename notes in `docs/`.

**Fixed port (R3.3, decision 9).** Compose publishes
`127.0.0.1:${GRANGE_PORT:-7788}` to the container's port 8080 in place of the
Docker-assigned port. `make query` is dropped in the same commit, because
`curl http://127.0.0.1:7788/health` now replaces it (R8.3).

**Layout.** Reshape `docker/content/` into a package so the server stops being
one file, and give tests and tools a home.

```
.github/workflows/ci.yml     phase 7
docker/
  Makefile  docker-compose.yml  Dockerfile  .dockerignore
  .env.example               phase 1: every key from section 7
  compose.plain.yml          phase 4: five granges, plain Docker network
  compose.headscale.yml      phase 5: local headscale plus sidecars
  content/
    entrypoint.py  freshness.py  healthcheck.py   unchanged apart from the rename
    server.py                                     thin: wires the pieces below
    grange/
      __init__.py
      config.py      env parsing, defaults, validation
      model.py       Agent, Record, versions, size limits
      state.py       Store: merge, expiry, forget map, cursor, farm_stale, wait
      ticker.py      time-driven transitions: farm_stale flip, expiry, resume gap
      api_local.py   /v1/farm, /v1/agents, /health, /version
      api_gossip.py  /v1/gossip handler and the HTTP exchange client
      gossip.py      the core: live set, marks, neighbours, probe, what is sent to whom
      runtime.py     the shell: timers, worker pool, HTTP transport, signal handler
      discovery.py   static peers; later the Tailscale local API
  tests/             unittest modules, one per grange module, plus the two example replays
  tests/sim/         phase 3: the 100-grange simulation
  tools/             run on the host: Python 3.9 or later, standard library only (R8.8)
    fake_farm.py     scripted pusher for the harness
    mock_github.py   ETag-aware stand-in for the tag and compare endpoints
    scenario.py      drives and times the phase 4 scenarios
    query_server.py  moved here; label discovery kept as a fallback (R8.3)
docs/
  requirements.md  plan.md  headscale.md
```

**Housekeeping in the same phase.**

- `.gitignore` gains `!.env.example`; today's `.env.*` rule would ignore it.
- The Dockerfile copies the whole `content/` tree and `.dockerignore` allows
  `content/**/*.py`. `.env` stays outside the build context, so no secret can
  be baked into the image (section 7).
- The Makefile gains `test` (builds the image, then runs `unittest` discovery
  inside it over a bind-mounted `docker/tests`, bypassing the entrypoint),
  `test-sim`, `test-host` (the same discovery on the host interpreter,
  optional), `test-plain`, and `test-headscale`. `make logs` follows the
  `grange` service. `build` and `ps` stay.
- The compose file carries no restart policy, in this phase or later (R8.9).

Done when the rename criterion holds, `make test` runs an empty suite inside
the image, `make up` starts the current server reporting AHEAD of the new tag,
and `curl` reaches it on `127.0.0.1:7788` and on no other host address.

## Phase 1: configuration, model, state

Pure Python, no sockets. This is where the correctness lives, so it gets the
most tests.

- `config.py`: every key in section 7 with its type and default. An empty
  value means the default. A missing `GRANGE_ID`, or one outside the
  character and length rule of section 7, refuses to start with a one-line
  reason and exit code 78, as the freshness refusal does (startup step 2).
  `GRANGE_PEERS` parses to a list of `host:port`. The caps of R4.3 and R5.17
  and the resume gap of R6.22 are constants here, not keys.
- `.env.example`: created now with every key from section 7 and a one-line
  comment each, since `make up` needs a `.env` from phase 2 on (R8.4).
- `model.py`: `Agent(owner, name, state)` with `state` kept as an opaque
  string (R4.1); `Record` with `proto, group, grange, incarnation, counter,
  rev, farm_age_s, farm_stale, leaving, agents`; `content_version()` returning
  `(incarnation, rev)` and `liveness_version()` returning `(incarnation,
  counter)`; `validate()` enforcing the R4.3 limits and types, reporting a
  wrong type so the API can answer 400, and dropping unknown fields, so
  nothing but the three agent fields can be relayed (R4.4, R9.10, R9.11); a
  `DigestEntry` of `[incarnation, counter, rev, farm_age_s]`; JSON in and out.
- `state.py`: `Store(clock, cfg)` with
  - `apply_body(record) -> changed` implementing the content half of R4.5 and
    R6.5 to R6.7, with the forget map keyed by liveness version, readmission
    by body only, and a `leaving` body removing the peer only when R4.5
    accepts it;
  - `apply_digest(entries)` implementing the liveness half of R4.5 and R5.18:
    same incarnation as the stored body, or nothing happens;
  - the local record: incarnation chosen at construction from the wall
    reading (R6.17), `bump()` for the heartbeat (counter only),
    `set_agents(list) -> changed` (rev and counter; a reordered list is not a
    change, a push that clears `farm_stale` is, R5.3), `farm_age_s`,
    `farm_stale` with the never-pushed rule from R6.3, `leave()`;
  - `correct_incarnation(seen)` implementing R4.8: strictly greater versions
    only, one correction, then the duplicate flag and no further correction
    while it is set;
  - `expire()` implementing R6.4 and moving versions to the forget map;
  - `cursor` and `wait_for(after, timeout)` on a `threading.Condition`,
    advancing only on the R5.6 events, waiting only when `after` equals the
    cursor (R5.8);
  - `picture()` producing the R5.5 body, local grange first and the others
    by id, with `seen_age_s` and aged `farm_age_s` (R5.9);
  - `digest()`, `bodies_newer_than(digest)`, and `bodies_missing_from(digest)`
    for the repair exchange (R5.10), every outgoing body stamped with the
    Store's current liveness for that grange (4.2) and the list trimmed to the
    R5.17 caps.

Tests (R10.1): configuration defaults, empty meaning default, and the
refusals for a missing or invalid `GRANGE_ID`; content ordering and ties;
liveness refresh from a digest with no body; a digest entry for an unknown id
creates nothing; a digest entry with a greater incarnation than the stored
body refreshes nothing, and after every operation the stored body and
liveness share one incarnation; restart with higher incarnation replaces
without expiry; incarnation correction after a backward clock step; an equal
version for the own id is ignored as an echo; two Stores sharing an id settle
after a bounded number of corrections, the loser keeps `duplicate_id` and the
winner's flag never sets; a seen age equal to `EXPIRE_SECONDS` is kept and
anything greater expires (R6.4); a stale copy from a lagging peer is refused
after removal, a digest entry never readmits, and a relayed body carrying the
relayer's later counter does; an accepted `leaving` body removes at once and
one from an older incarnation removes nothing; heartbeat does not move the
cursor, an agent change does, a peer's first body does, a peer expiring does,
`farm_stale` flipping does; `after` equal to the cursor waits and any other
value returns; `farm_stale` true when farm never pushed and the grace has
passed; a push with an identical list while stale is a content change, and a
push that only reorders the list is not; oversize input is rejected, outgoing
bodies are trimmed to the caps, and the record cap counts the own record; the
picture lists the local grange first and the others by id; same-named agents
are both kept; a state string never seen before is accepted; an extra field
on an agent entry is dropped.

**Replay of the worked example (4.3).** One test module walks the ten steps
of section 4.3 with three Stores and a fake clock, exchanging bodies and
digests by direct calls. It asserts the versions the text gives and, at each
step, whether each cursor moved; it does not assert by how much (R5.6). The
example checks the code and the code checks the example: a number the replay
contradicts is corrected in the requirements, not papered over in the test.

## Phase 2: local API and ticker

`api_local.py` on `ThreadingHTTPServer`, reusing the existing freshness 503
wrapper so a stale image refuses these routes too (R5.16).

- `PUT /v1/farm`: parse, validate, `set_agents`, 204; 400 on malformed, 413
  over limits (R5.1 to R5.3). The declared length is checked against the
  1 MiB cap of R5.1 before the body is read, so no request can make the
  server read without bound (R9.10).
- `GET /v1/agents`: `after` and `wait`, which defaults to 30 and is capped
  there; immediate answer unless `after` equals the cursor; 400 for a value
  that is not a non-negative number (R5.7, R5.8).
- `GET /health`: the existing freshness block plus the R5.14 fields.
  `cursor`, `incarnation`, and `duplicate_id` are live from this phase.
  `peers_discovered`, `peers_live`, and the `rejected` counts are present and
  zero until phase 3 fills them, `rejected.identity` until phase 5.
  `last_gossip_age_s` is `null` until the first exchange. `GET /version` is
  unchanged (R5.15).
- `ticker.py`: a one-second thread that runs `expire()` and re-evaluates the
  local `farm_stale`, so time-driven transitions move the cursor before the
  gossip scheduler exists. It records the monotonic time of each run and
  signals a resume when the gap since the previous run is longer than 5
  seconds (R6.22); phase 3 acts on the signal.
- Binding: two listeners with one handler, on loopback and on the container's
  bridge address found from the default route; never all interfaces (startup
  step 4). Today's server binds every interface.
- Signals: the entrypoint replaces itself with the server, which therefore
  runs as process 1 and ignores SIGTERM unless it installs a handler. A
  handler that exits promptly goes in now; phase 3 extends it with the
  `leaving` push.
- Compose: the grange keys are passed through from `.env`; the healthcheck
  `start_period` is raised to 75 seconds now, so phase 5's tailnet wait does
  not surprise anyone (R8.1). Until phase 5.2 an empty `GRANGE_PEERS` means a
  lone grange with no peers.

Tests: in-process server on an ephemeral port; a push wakes a waiting
long-poll within a few milliseconds; a long-poll with nothing happening
returns at `wait`; `after` ahead of the cursor returns at once; a fake-clock
ticker flips `farm_stale` and moves the cursor; a fake-clock gap raises the
resume signal; limits produce 413 and malformed input 400, a bad `after` or
`wait` included; the healthcheck script still passes against it. By hand:
`make up` with only `GRANGE_ID` in `.env` starts, a `curl` push shows up in
`GET /v1/agents`, and `make down` returns within two seconds.

## Phase 3: gossip and the design-size simulation

- `api_gossip.py`: `POST /v1/gossip` handler implementing R5.10's two uses:
  push (bodies in, 204 out) and repair (digest in; own digest plus newer and
  missing bodies out). Reject on `proto` or `group` mismatch with 409, log
  once per peer, and count it (R5.11, R6.21); refuse malformed bodies with 400
  (R9.10); enforce the R5.17 caps with 413 and count those, and ids beyond the
  record cap, as overflow. The listener binds only to the gossip address and
  refuses any source outside the allowed set with 403: in plain mode the
  bridge address and its network (R6.9), in tailnet mode the tailnet address
  and tailnet sources (R5.12, added in 5.2). The HTTP client side has
  `push(peer, bodies)` and `repair(peer)`, the latter doing the round trip
  and then the follow-up push of what the peer lacks; anything but a 2xx
  answer is a failure (R6.20).
- `gossip.py` is a core with no threads and no sockets. It is driven by the
  clock object, reaches peers only through a transport interface, and holds
  every decision: the live set, the failed marks, the neighbours, the probe,
  which peer to repair with, and what is sent to whom. The runtime and the
  simulation use this core unchanged, so the simulation exercises the real
  behaviour and not a copy of it (R10.5). The core covers:
  - heartbeat: `bump()` every `HEARTBEAT_SECONDS` (R6.2), nothing sent;
  - resume: on the ticker's signal, `bump()` and one repair at once, whose
    follow-up push readmits this grange at the peer (R6.22);
  - repair: every `REPAIR_SECONDS`, one repair with a peer drawn uniformly
    from the live addresses that are not neighbours, or from the whole live
    set when all of them are (R6.13, R6.10);
  - event-driven push: the Store signals when the local record's content
    changed, when its incarnation was corrected (R4.8), or when a body with
    new content arrived; the record goes to all neighbours at once, excluding
    the sender for forwarded bodies (R6.11, R6.12). No scheduler tick is
    involved, since per-hop latency multiplies;
  - own address: the tailnet IPv4 with the gossip port in tailnet mode; in
    plain mode the container's hostname with the gossip port, which the
    harness makes equal to what the other granges list in `GRANGE_PEERS`
    (R6.9);
  - order: addresses compare as `host:port` strings, byte by byte (R6.10);
  - neighbours: successor in that order plus the `FANOUT - 1` live addresses
    with the lowest `hashlib.sha256` of own id joined to address, recomputed
    on live-set change (R6.10);
  - live set: addresses bound to an unexpired record, plus addresses with no
    unexpired record that discovery hints online, minus addresses marked
    failed. A request that does not get a 2xx answer marks the address; the
    mark clears on a later success, on an accepted request from that address,
    or when discovery next reports it online (R6.10, R6.20);
  - probe: every `DISCOVERY_SECONDS`, and on every repair tick while the live
    set is empty, one repair with a random discovered address outside the
    live set, so split halves and a lone returner find the mesh again (R6.10);
  - address to id binding from `from` on the most recent successful exchange
    in either direction, with a change logged once (R6.19);
  - startup: discovery, then up to `FANOUT` parallel repairs bounded to 5
    seconds, whose follow-up pushes deliver this grange's first record, then
    serve (startup steps 5 and 6, R6.1);
  - leaving: a `leaving` body to all neighbours (R6.16);
  - stale image: when the freshness state flips to stale the core stops
    sending, and the handler already returns 503 (R5.16, R6.18).
- `runtime.py` is the thin shell around the core: the timers, the worker pool
  of `2 x FANOUT`, the HTTP transport with its 1-second connect and 3-second
  exchange timeouts (R6.20), and the SIGTERM handler, which asks the core to
  leave, waits at most two seconds for the pushes, and exits (R6.16).
- `discovery.py`: in this phase only the static `GRANGE_PEERS` provider, in
  which every listed address counts as online (R6.9), behind the interface
  the Tailscale provider will implement in phase 5.
- `/health` now reports `peers_discovered`, `peers_live`,
  `last_gossip_age_s`, and the `proto`, `group`, and `overflow` counts.

**Simulation (R10.5), `tests/sim/`.** A pure-Python mesh of 100 granges with
10 agents each. Every grange is a Store plus the `gossip.py` core, both used
unchanged and wired through the real request and response construction to a
fake transport that adds a per-hop latency parameter, can cut links, and
counts bytes; a fake clock; and a fake discovery whose online hint follows
whether a grange is up, with a configurable lag. Runs, each over one
simulated hour: all alive; half
the discovered peers dead; two halves partitioned past `EXPIRE_SECONDS` and
rejoined; a grange silent past `EXPIRE_SECONDS` returning, and a fresh start;
two granges configured with one id. Assertions as R10.5 states them: the
5-second convergence (R6.14), no false expiry, the R6.15 budget, rejoin within
`DISCOVERY_SECONDS` plus 15 seconds, return and fresh start within 10 seconds,
and a bounded number of duplicate-id corrections. These runs are `make
test-sim`; their wall time is measured in this phase, not guessed. `make test`
runs only a short all-alive run, a few simulated minutes, as a smoke test. No
assertion is weakened to fit a time limit.

Unit tests: convergence of a small simulated mesh after one round of pushes;
a local change reaches every neighbour's handler within one second over
loopback (R6.11); repair heals a dropped push; forward-once does not loop; 409
on group mismatch; a repair against a peer with newer bodies fetches them and
pushes back what it lacks; leaving propagates; a dead peer is removed from the
live set and from neighbours; a failed address re-enters the live set when
discovery hints it online; two halves that expired each other reconnect
through the hint and, with hints withheld, through the probe; a resume signal
readmits a paused grange within one repair; one peer joining or leaving
changes at most one neighbour; a 409 or 503 answer marks the address failed
and an accepted request from it clears the mark; a changed `from` rebinds the
address; the first record leaves by the startup repairs' follow-up push.

**Replay of the discovery examples (6.4).** One test module walks the
examples of section 6.4 with thirteen simulated granges at the addresses the
text uses: Carol joining, address binding, Don's successor and ranked
neighbours, repair preference, Frank's laptop closing, the split and rejoin,
the cut-off grange, and static peers. The untagged device of example 8 needs
a tailnet and is covered by phase 6. As with 4.3, a claim the replay
contradicts is corrected in the requirements.

## Phase 4: plain-network harness

`compose.plain.yml` starts five granges, `grange-a` to `grange-e`, on one
Docker network. Each service's hostname is its name, each lists the other
four in `GRANGE_PEERS`, and each API is published on its own localhost port
away from 7788, so a real grange on the same machine is not disturbed.
`SKIP_FRESHNESS_CHECK=1` is set except in the stale scenario. Timings are the
defaults, so a full run takes several minutes.

`tools/fake_farm.py` pushes a scripted agent list to one grange with
heartbeats, a change at a chosen moment, or silence on command.
`tools/mock_github.py` serves the tag and compare endpoints with ETags and
lets the driver move the tag. `tools/scenario.py` runs on the host, drives
Docker, polls the five boards, and asserts the R10.2 list:

| scenario | pass condition |
|---|---|
| change on one grange | visible on all five `/v1/agents` within 5 s |
| kill one grange | gone from all boards within `EXPIRE_SECONDS` plus 15 s |
| kill without a `leaving` body, serving again within 20 s of the kill | new incarnation on all boards within 5 s of serving; never absent from a board in between |
| fake farm goes silent | `farm_stale` true within `FARM_STALE_SECONDS` plus 5 s, agents still listed |
| clean stop | gone from all boards within 3 s |
| pause past `EXPIRE_SECONDS`, then unpause | removed, then readmitted within 5 s of unpause |
| stale image | with the mock GitHub, gossip returns 503 and the grange ages out |
| change with `FANOUT=2` | same 5 s bound, with forwarding over real sockets |

`make test-plain` brings the stack up, runs the scenarios, prints a table,
tears down, and exits non-zero on any failure. It makes no claim about
R6.15; that is the simulation's job.

## Phase 5: local headscale, spike, sidecar

Nothing here touches the team's control server, which does not exist yet.
This phase needs a Linux host with Docker Engine as well as the Mac (R8.6).

**5.0 The local headscale stack.** `compose.headscale.yml` in its first form:
a pinned headscale container (0.28 or later, R9.6) with a config enabling the
embedded relay, a policy file declaring `tag:grange` and the single ACL rule
(R9.1, R9.2), an init step that creates a reusable tagged pre-auth key (R9.3)
and writes it where the sidecars read it, and two Tailscale client sidecars
that register with it. This is the spike's test bed and later phase 6's.

**5.1 The spike, one day across both runtimes (R8.6).** Against the local
stack, by hand, on Docker Desktop for Mac and on Docker Engine on Linux,
confirm and write down:

- registration with the tag, and that both sidecars see each other's tags in
  the daemon's status;
- `NET_ADMIN` and `/dev/net/tun` work under each runtime;
- the daemon socket can be shared to a second container, and which local API
  paths and headers answer status and whois (the open item in the
  requirements);
- whois from one node for the other's address returns its tags;
- a non-root user in the second container can call status and whois on the
  shared socket (the mount being read-only does not limit a socket), so the
  grange can run non-root (R8.7);
- whether tagged nodes' keys expire, and what the pre-auth key's own expiry
  is;
- expiring the pre-auth key leaves joined nodes working (R9.4), and deleting
  a node on the server cuts it off from its peers (R9.5);
- binding a listener to the IPv4 tailnet address inside the shared namespace;
- that a port published on the sidecar service reaches a listener in the
  shared namespace from the host's loopback, and that the gossip port is not
  reachable from the host or the bridge network (R3.4);
- how long after a sidecar stops the daemon's online hint for it turns
  offline, which the live-set rules tolerate but the examples in 6.4 describe;
- whether the monotonic clock shows a gap after `docker pause`, after the
  Mac sleeps, and after a Linux laptop suspends, so Don knows where the
  resume rule of R6.22 fires. This is information for him, not a pending
  change;
- with the headscale container stopped: whether two joined granges keep
  gossiping, and whether the daemon still answers status and whois, before
  and after a sidecar restart. No requirement covers a control server outage;
  this is recorded so Don can decide whether one is needed.

Findings go into `docs/headscale.md`, which in this phase also becomes the
admin runbook from section 9.1: policy snippet, create key, expire key, delete
node, what to tell a new teammate. The runbook is delivered here so Don has
it before he stands up the team server (R9.0).

**5.2 Sidecar integration.**

- `docker-compose.yml` becomes two services per R8.1: `tailscale` with the
  pinned image, capabilities, state and socket volumes, hostname
  `grange-${GRANGE_ID}`, login server and tag from `.env`, a healthcheck, the
  label `agent-grange.service=tailscale`, and the `127.0.0.1:${GRANGE_PORT}`
  publish; `grange` with `network_mode: service:tailscale`, `depends_on`
  healthy, the socket mounted, its label, running as the user the spike
  settled on, with the 75-second start period. Neither service has a restart
  policy (R8.9).
- `discovery.py` gains the Tailscale provider and it becomes the default when
  `GRANGE_PEERS` is empty: peers from the daemon's status filtered by
  `TS_TAG`, with IPv4 addresses and the online hint, refreshed every
  `DISCOVERY_SECONDS` (R6.8); `whois(addr)` returning the device's tags; the
  sidecar hostname `grange-<id>` kept as a hint, with a disagreement against
  the `from` field logged once (R6.19).
- Startup: when `GRANGE_PEERS` is empty, wait up to 60 seconds for a tailnet
  IPv4 address, then refuse (step 3); bind gossip to it and only it (step 4).
  Plain mode does neither (R6.9).
- `api_gossip.py`: source must be a tailnet address, and `whois` must report
  the tag, or the exchange is refused with 403, logged once per peer, and
  counted as `rejected.identity` (R5.12, R5.13).
- `healthcheck.py` keeps hitting loopback, which works inside the shared
  namespace.
- `tools/query_server.py` updated to the sidecar's label, since the sidecar
  owns the published port now.

Checked by: two granges on the local stack discover each other by tag and
gossip, on both runtimes; from the sidecar turning healthy to the grange
serving takes under 10 seconds (R6.1); `make up` with a filled `.env` joins
with no browser and no approval (R8.3, R8.4).

## Phase 6: headscale test mode automated

`compose.headscale.yml` grows to three sidecar and grange pairs plus a fourth
sidecar without the tag (R10.3). `make test-headscale` runs:

- the change scenario from phase 4 against this stack with the 5-second
  bound, which proves registration, tag discovery, and gossip over the
  tailnet;
- the two negative cases: with the ACL in place the untagged device cannot
  connect at all; with a test-only policy that opens the ACL, the whois check
  alone rejects it and `rejected.identity` counts it (R5.13);
- a bind check: the gossip port answers on the tailnet address, refuses a
  connection to the bridge address from another container, and is not
  reachable from the host at all (R3.4, R5.12).

## Phase 7: documentation and developer experience

- `README.md` and `docker/README.md` rewritten for the two-container system:
  what it is, how to join (R8.4), how to read the board with `curl`, how farm
  and frolic connect, the freshness check as it stands, that the grange does
  not come back by itself after a reboot or a Docker restart and needs
  `make up` (R8.9), and the note from R6.21 about using a private
  `GRANGE_GROUP` while working ahead of the tag.
- `.env.example` checked key by key against section 7 and `config.py`.
- GitHub Actions: `make test`, `make test-sim`, and `make test-plain` on every
  push (R8.8). All three need only Docker, so CI and a laptop run the same
  thing, and none of them contacts the real freshness endpoint. If the hosted
  runner allows the tun device, `make test-headscale` is added as well, which
  would keep the Linux runtime covered on every push; it is not required.
- The tag is moved by hand (decision 23). No workflow touches it.

## Phase 8: rollout

Prerequisite outside this repo: the team's headscale server, stood up by Don
from `docs/headscale.md`. No date yet (R9.0).

1. Smoke test across two real machines, one per supported runtime where
   available, against the team's headscale (R10.4). It includes one real
   sleep and wake on each machine past `EXPIRE_SECONDS`, timing how long the
   grange takes to reappear on the other board (R6.22).
2. Hand farm its contract: `PUT /v1/farm` body, a push on every change and at
   least every 30 s, the grange port in farm's config; what farm reports is
   farm's decision (R5.4). Hand frolic its contract: the long-poll and
   response shape, the cursor rule, the `farm_stale` and `seen_age_s` fields,
   and the 503 body a stale grange returns. Both stay in this phase
   (decision 20).
3. Move `agent-grange-latest` to the release commit, by hand. Every running
   earlier grange refuses on its next poll, which is the intended signal to run
   `make update` (R6.18).
4. Watch the first week for expiry flapping on sleep and wake and for real
   traffic against R6.15. One teammate can try a different `EXPIRE_SECONDS`
   in their own `.env`. A team-wide change is a new default in `config.py`,
   which is a release and a tag move (section 7).

## External dependencies

| who | needs to do | by phase |
|---|---|---|
| Don | approve the two tag pushes in phase 0, in the session doing the work; say when `main` is fast-forwarded and when it is pushed | 0 onward |
| Don | provide a Linux host with Docker Engine for the spike and the smoke test | 5 |
| Don | stand up the team headscale server from the runbook; no date yet | 8 |
| headscale admin | define the tag and ACL, issue a key | 8 |
| agent-farm | push to `PUT /v1/farm` on change and every 30 s; grange port setting | 8 |
| agent-frolic | long-poll `GET /v1/agents`, map states, draw `farm_stale` and ages | 8 |

## Where each requirement is built and checked

| requirements | built in phase | checked by |
|---|---|---|
| R3.1, R6.14, R6.15: design size, propagation, traffic | 3 | `make test-sim`; real laptops in phase 8 |
| R3.2, R8.1: two containers | 5.2 | phase 5 check, `make test-headscale` |
| R3.3, R8.3: fixed localhost port, Makefile targets | 0 | phase 0 done criterion |
| R3.4, R5.12: gossip only on the tailnet | 3, 5.2 | spike, phase 6 bind check |
| R3.5, R9.0 to R9.6: control server | 5 | spike, runbook |
| R3.6, R5.1 to R5.4: farm push | 2 | phase 2 tests, fake farm in phase 4 |
| R3.7, R5.5 to R5.9: frolic long-poll | 1, 2 | phase 1 and 2 tests |
| R3.8, R5.10, R5.11, R5.17, R5.18, R6.11 to R6.13: gossip | 3 | phase 3 tests, phase 4 scenarios |
| R3.9: names | 0 | the `git grep` criterion |
| R4.1 to R4.4: agent | 1 | phase 1 tests |
| R4.5 to R4.9 and the example in 4.3: record, merge, incarnation | 1; 3 for the push after a correction | phase 1 tests, replay of 4.3 |
| R5.13: peer identity | 5.2 | phase 6 negative cases |
| R5.14 to R5.16, R6.18: health, version, stale image | 2, 3 | phase 2 tests, stale scenario |
| 6.1 and R6.1: startup | 2, 3, 5.2 | phase 5 timing check |
| R6.2, R6.3, R6.22: this grange's liveness | 1, 2, 3 | unit tests, pause scenario, phase 8 sleep check |
| R6.4 to R6.7: peer liveness | 1 | unit tests; kill, restart, and clean-stop scenarios |
| R6.8 to R6.10, R6.19, R6.20 and the examples in 6.4: discovery, topology | 3, 5.2 | unit tests, replay of 6.4, simulation |
| R6.16, R6.17: shutdown, restart | 2, 3 | clean-stop and restart scenarios |
| R6.21: private group for protocol work | 3 | 409 test; README note in phase 7 |
| section 7: configuration | 1 | configuration tests, `.env.example` check in phase 7 |
| R8.2: standard library only | all | the image installs nothing |
| R8.4: joining | 1, 5.2 | phase 5 check |
| R8.5: freshness unchanged | 0 | AHEAD start in phase 0, stale scenario |
| R8.6, R8.7: runtimes, non-root | 5.1 | spike on both runtimes |
| R8.8: test runner | 0, 3, 7 | CI runs the three targets |
| R8.9: no automatic restart | 0, 5.2 | the compose file has no restart policy; README note in phase 7 |
| R9.7 to R9.13: security, privacy | 1, 2, 3, 5.2 | validation tests, negative cases |
| R10.1 to R10.5 | 1 to 8 | they are the checks above |

## Risks and how the plan handles them

- **Resume detection may not fire on a real laptop (R6.22).** The requirement
  looks for a gap in the monotonic clock only, by Don's decision (decision
  33). `docker pause` produces one, but on Linux the monotonic clock does not
  advance while the machine is suspended, and a suspended Docker Desktop
  virtual machine may behave the same way. If so, a laptop that wakes falls
  back to readmission by its next heartbeat and repair, up to about 13
  seconds, and only the harness sees the fast path. That is accepted. The
  spike measures the clock on each runtime and phase 8 times a real sleep and
  wake, so the actual delay is known before rollout.
- **Tailscale local API details** (headers, endpoint shapes, socket sharing,
  non-root access): the phase 5.1 spike settles them against the local stack
  before any code depends on them.
- **Runtime differences** between Docker Desktop for Mac and Docker Engine on
  Linux, above all the tun device and published-port behaviour: same spike,
  both runtimes.
- **Headscale version drift** in CLI syntax: pin the image in phase 5 and name
  the version in the runbook.
- **Control server outage**: no requirement says what must keep working when
  headscale is down. The spike records what does, so the question is settled
  with evidence before rollout.
- **Closed laptops** dominating the discovered set: neighbours and repair peers
  come from the live set with short timeouts (R6.10, R6.20); the simulation
  runs with half the peers dead.
- **Clock steps and duplicate ids**: incarnation correction heals a backward
  step in one exchange while any peer still holds the old record. After a
  clean stop, or a gap longer than `EXPIRE_SECONDS`, no peer holds it and the
  grange is absent from other boards for up to `FORGET_SECONDS`, by choice
  (R4.8, decision 29). Two granges sharing an id correct a bounded number of
  times and then flag `duplicate_id` instead of flooding the mesh.
- **Examples drifting from behaviour**: sections 4.3 and 6.4 state concrete
  numbers. The two replay tests keep them true.
- **Long-poll threads**: frolic opens one or two connections; the threaded
  server is fine. If a second local consumer appears, revisit.
- **Split mesh**: two groups that expired each other keep each other in the
  live set through the online hint, and the probe reaches addresses the hint
  misses, so a healed partition converges within `DISCOVERY_SECONDS` plus 15
  seconds (R6.10, R10.5).
- **Stale image during rollout**: moving the tag takes every grange down until
  updated, by design. Announce the move; do it when the team is not mid-review.
- **No team control server yet**: nothing before phase 8 needs it, and the
  runbook ships in phase 5 so it can be stood up in parallel with phases 6
  and 7.

## Order of first sessions

1. Phase 0 and phase 1 together: rename, fixed port, layout, configuration,
   model, state, tests green.
2. Phase 2: local API and ticker, tests green, `make up` still works end to end.
3. Phase 3 and phase 4 together: gossip with the simulation and the harness,
   since those are how gossip gets exercised.
4. Phase 5.0 and 5.1, the local stack and the spike, then 5.2.
5. Phase 6, then phase 7, then phase 8 with Don present for the smoke test and
   the tag move.
