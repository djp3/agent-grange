# agent-grange: the service

One grange runs on each developer's machine. It takes that machine's agent
status from **agent-farm**, gossips it to every teammate's grange over a
Tailscale tailnet run by headscale, and serves the whole team's merged picture
to **agent-frolic**. JSON only, nothing on disk, no history. The full design
is in [`docs/requirements.md`](../docs/requirements.md); the control server
runbook is [`docs/headscale.md`](../docs/headscale.md).

```
 this machine                                              a teammate's machine
 ┌──────────────────────────────────────────────┐          ┌───────────────────┐
 │ agent-farm ──PUT /v1/farm──▶ 127.0.0.1:7788   │          │                   │
 │ agent-frolic ◀─GET /v1/agents──┘              │          │                   │
 │   ┌──────────────┐  shares its  ┌─────────┐   │  tailnet │ ┌───────┐ ┌─────┐ │
 │   │  tailscale   │◀─ namespace ─┤ grange  │◀──┼─ :7789 ──┼▶│ ts    │ │grng │ │
 │   │  sidecar     │              │ python  │   │  gossip  │ └───────┘ └─────┘ │
 │   └──────────────┘              └─────────┘   │          │                   │
 └──────────────────────────────────────────────┘          └───────────────────┘
```

## Join the mesh

You need Docker (Docker Desktop for Mac or Docker Engine on Linux, R8.6) and
the three values from the team's password manager.

```bash
cd docker
cp .env.example .env      # then fill in GRANGE_ID, TS_AUTHKEY, HEADSCALE_URL
make up
```

`GRANGE_ID` is your id on every board: 1 to 56 lowercase letters, digits, or
hyphens, not starting or ending with a hyphen. `make up` builds the image,
checks `.env`, starts the sidecar, which joins the tailnet with the key (no
browser, no approval), then the grange, and waits until both are healthy. An
id outside that rule, or any other bad value, stops `make up` before the
sidecar joins, with the reason; fix `.env` and run `make up` again. Other granges
find you by the tag within 30 seconds; you see them within a few.

**After a reboot, or a restart of Docker, the grange does not come back by
itself** (R8.9). Run `make up` again.

## Make targets

```bash
make up            # build if needed, check .env, start both containers, wait for healthy
make down          # stop; a leaving record tells the mesh at once
make update        # git pull --ff-only, then make up: the fix when the image is stale
make logs          # follow the grange's log
make info          # the git summary the build would bake in
make ps            # both containers' status, and the check's exit code
make test          # unit tests and a short simulation, inside the built image
make test-host     # the same unit tests on your own Python, a fast loop
make test-sim      # the hour-long simulations at 100 granges, about 10 minutes
make test-plain    # five granges on a plain Docker network, scripted, several minutes
make test-headscale  # a local headscale, three sidecar and grange pairs, an untagged device
```

Use `make`, not `docker compose` directly: compose cannot run `git`, so the
Makefile is where the build's git metadata comes from, and an image built
without it refuses to start. `make test-plain` and `make test-headscale` drive
Docker from the host and need Python 3.9 or later there; everything else needs
only Docker.

## Read the board

The local API is on `127.0.0.1:${GRANGE_PORT}` (default 7788) and nowhere else.

```bash
curl -s http://127.0.0.1:7788/v1/agents | python3 -m json.tool
curl -s 'http://127.0.0.1:7788/v1/agents?after=42&wait=30'   # long-poll: answers when the cursor moves
curl -s http://127.0.0.1:7788/health                         # freshness, cursor, peers, rejected counts
curl -s http://127.0.0.1:7788/version                        # the commit this image was built from
```

`/v1/agents` is always the whole picture: this grange first, then the others by
id, each with its agents, `seen_age_s`, `farm_age_s`, and `farm_stale`. Pass
the `cursor` you last saw as `after` and the request waits, up to `wait`
seconds (default and maximum 30), until something changes. An `after` other
than the current cursor answers at once, which covers a grange restart.

## How farm and frolic connect

- **agent-farm** sends `PUT /v1/farm` with `{"agents": [{"owner", "name",
  "state"}, ...]}`: the complete list it chooses to report, on every change and
  at least every 30 seconds. Grange answers 204. Which sessions farm reports is
  farm's business; grange relays exactly `owner`, `name`, and `state` and
  nothing else (R4.4, R5.4). If farm goes quiet for 60 seconds, its last agents
  stay on every board with `farm_stale: true`.
- **agent-frolic** long-polls `GET /v1/agents`, compares cursors for equality
  only, and maps farm's raw states to what it draws.
- Both use only `127.0.0.1:${GRANGE_PORT}`. The gossip port is never published.

## Configuration

Everything is in `.env`, documented key by key in `.env.example`. An empty
value means the default, and the defaults live in `content/grange/config.py`.
Three keys must be equal on every grange in a mesh: `GRANGE_GOSSIP_PORT`,
`GRANGE_GROUP`, and `TS_TAG`. Timing keys are local: you may try a value in
your own `.env`; a team-wide change is a new default in the code.

### Working ahead of the tag

A build whose protocol or record schema differs from the tagged release must
run with a private `GRANGE_GROUP` in its `.env`, for example
`GRANGE_GROUP=proto-yourname` (R6.21). It then neither disturbs nor is disturbed
by the team mesh; mismatched exchanges are refused with 409 and counted in
`/health` under `rejected.group`, so a split mesh is visible.

## Files

```
Makefile               git metadata and every target above
docker-compose.yml     the tailscale sidecar and the grange in its namespace (R8.1)
compose.plain.yml      five granges on a plain Docker network, for make test-plain
compose.headscale.yml  a local headscale and sidecars, for make test-headscale
headscale/             that local headscale's config and test policies
Dockerfile             python:3.12-slim, nothing installed, runs as uid 10001
.env.example           every key, one line each
content/entrypoint.py  launch-time freshness check: start or refuse (exit 78)
content/freshness.py   the tag comparison, shared by the entrypoint and the server
content/healthcheck.py Docker HEALTHCHECK; prints the reason into the health log
content/server.py      wires the grange package together; startup as in requirements 6.1
content/grange/        config, model, state (the Store), ticker, gossip core, runtime,
                       local API, gossip endpoint, discovery
tests/                 unittest modules, replays of the worked examples, tests/sim/
tools/                 host-side: fake_farm.py, mock_github.py, scenario.py,
                       headscale_scenario.py, query_server.py
```

## Freshness check

Problem: each developer updates with `git pull` and then (ideally) `make up`.
If they skip either step they run stale code and nothing tells them. Worse,
two protocol versions would gossip with each other.

The team marks the current release with a moving git tag,
`agent-grange-latest`. Every container start compares itself to that tag, and
the running grange keeps comparing.

**Build time** (`Makefile` -> compose build args -> Dockerfile `ARG`/`ENV`):

| baked in     | from                                      |
|--------------|-------------------------------------------|
| `GIT_REPO`   | `remote.origin.url`, e.g. `owner/agent-grange` |
| `GIT_COMMIT` | `git rev-parse HEAD`                      |
| `GIT_BRANCH` | informational only                        |
| `GIT_DIRTY`  | 1 if this directory had uncommitted changes |
| `GIT_PUSHED_BASE` | nearest ancestor(s) of HEAD that exist on origin (`git rev-list --boundary HEAD --not --remotes=origin`); HEAD itself if nothing is unpushed |

The commit is also stored as the OCI label `org.opencontainers.image.revision`,
so `docker image inspect` shows it, and the grange returns it at `/version`.

**Launch time** (`entrypoint.py`, before the server starts):

1. Resolve the tag: `GET /repos/{repo}/git/ref/tags/{CHECK_TAG}`. An annotated
   tag is peeled to its commit with one more call.
2. If the tag's commit equals `GIT_COMMIT`: start.
3. Otherwise `GET /repos/{repo}/compare/{GIT_COMMIT}...{tag_commit}` and act on
   `status`:

| GitHub says                       | meaning                              | action            |
|-----------------------------------|--------------------------------------|-------------------|
| `ahead`                           | tag moved past this image            | **refuse**, exit 78 |
| `diverged`                        | tag has history this image lacks     | **refuse**, exit 78 |
| `behind`                          | image is newer than the tag          | start, warn       |
| 404 on compare                    | built commit not pushed yet          | judge by `GIT_PUSHED_BASE`, see below |
| 404 on tag                        | tag not created, or no token         | **refuse** (default) |
| network error / other HTTP error  | cannot reach GitHub                  | **refuse** (default) |

**Unpushed commits.** GitHub has never seen a local-only commit, so the compare
call 404s. The entrypoint then compares each recorded pushed ancestor with the
tag instead. If the tag's commit is an ancestor of one of them, the image
descends from the tag: start with a warning. If the tag has moved past the
ancestor, or diverged from it, the image lacks that history: refuse. If no
ancestor was recorded, or none is on GitHub: cannot verify.

The compose file sets `FRESHNESS_STRICT=1`, so the last two rows refuse. Run with
`FRESHNESS_STRICT=0 make up` to start with a loud warning instead (offline).

**While running.** The grange polls the tag every `RECHECK_SECONDS` (default
30, with 10% jitter so a team does not poll in lockstep). The poll is a
conditional GET: it sends the `ETag` from the last tag-ref response as
`If-None-Match`, and GitHub answers `304 Not Modified` when the tag has not
moved. A 304 is not counted against the API rate limit and triggers no further
calls, so the steady state costs nothing; only a real move runs the full
comparison. Only a confirmed STALE verdict changes state; a transient GitHub
error keeps the previous state. Once stale:

- every route, `/health` and `/v1/gossip` included, answers `503` with a JSON
  body naming the built commit, the tag's commit, and the fix, plus
  `Connection: close` and `Retry-After` (R5.16);
- the grange stops sending gossip, so its record ages out of every board, and
  boards go blank for that teammate until they update (R6.18);
- `healthcheck.py` sees the 503 and prints its summary, so after 3 misses Docker
  marks the container **unhealthy** with the reason in its health log;
- if the tag later points back at this commit, the grange recovers on its own.

The fix is always the same: `make update`. It pulls (fast-forward only) and
re-invokes `make up`, so the git metadata is recomputed after the pull, the
image is rebuilt from the new commit, and compose replaces the stale container.
Plain `make up` without pulling is a cache hit and changes nothing.
`SKIP_FRESHNESS_CHECK=1` skips the check entirely.

Private repos need `GITHUB_TOKEN` at run time. The Makefile borrows the `gh`
CLI's token if the variable is not already set. The token is passed as a
container environment variable; it is visible in `docker inspect` on your own
machine and is never baked into the image.

### Moving the tag

The tag is the team's "this is current" pointer, and it is moved by hand, never
by a workflow (decision 23):

```bash
git tag -f agent-grange-latest <commit>
git push -f origin agent-grange-latest
```

Every running grange built from an earlier commit refuses on its next poll,
which is the signal to run `make update`. Announce the move, and do it when the
team is not in the middle of a review.

## Fallback: finding the container by label

The port is fixed, so `curl` is the supported way in. `tools/query_server.py`
is kept as a fallback that finds the sidecar, which owns the published port,
through the Docker socket:

1. It opens the Docker Unix socket (`DOCKER_HOST`, then `/var/run/docker.sock`,
   then `~/.docker/run/docker.sock`).
2. `GET /containers/json?filters={"label":["agent-grange.service=tailscale"],"health":["healthy"]}`
   returns the sidecar once it carries our label **and** has passed its healthcheck.
3. The entry's `Ports` array holds `PrivatePort: 8080 -> PublicPort: <port>`.
   The script takes the IPv4 binding and connects to `127.0.0.1:<port>`.
4. If no healthy container exists yet it polls once a second for up to 60 s
   (`DISCOVERY_TIMEOUT` overrides), printing the container's current state.

```bash
python3 tools/query_server.py /health
```

## Things that would break it

- Publishing the gossip port, or binding the grange to all interfaces: gossip
  must stay on the tailnet (R3.4).
- Passing `--advertise-tags` to the sidecar: with the tag owned by the admin,
  headscale refuses the registration. The tag comes from the key.
- Any restart policy: a refusal would become a crash loop, and the grange is
  meant to stay down until `make up` (R8.9). Compose sets none.
- Building with plain `docker compose` instead of `make`: metadata is `unknown`.
- Different `GRANGE_GOSSIP_PORT`, `GRANGE_GROUP`, or `TS_TAG` on one teammate:
  that grange silently forms a mesh of one.
