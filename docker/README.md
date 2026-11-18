# agent-grange service

A container runs a small Python JSON server. Compose publishes it on a fixed
localhost port, `127.0.0.1:${GRANGE_PORT}` (default 7788), and nowhere else, so
farm, frolic, and `curl` reach it at a known address.

The container also refuses to start if the commit it was built from is older
than the commit the `agent-grange-latest` tag points to on GitHub, and while
running it re-checks periodically: if the tag moves past it, every route answers
503 and Docker marks it unhealthy (see "Freshness check" below).

```
Makefile             captures git metadata and drives compose: make up / update / down / logs
docker-compose.yml   publishes 127.0.0.1:${GRANGE_PORT:-7788} -> 8080 + label + healthcheck
Dockerfile           python:3.12-slim; bakes GIT_* build args into ENV and OCI labels
content/freshness.py   the tag comparison; returns a verdict, shared by entrypoint and server
content/entrypoint.py  launch-time check: start or refuse (exit 78), then exec's the server
content/server.py      runs inside the container on 0.0.0.0:8080; re-checks on a timer;
                       /, /health, /version; all routes 503 once stale
content/healthcheck.py Docker HEALTHCHECK; prints the 503 reason into the health log
content/grange/        the grange package (filled in from plan phase 1 on)
tests/               unittest modules; `make test` runs them inside the built image
tools/query_server.py  runs on the host, stdlib only, fallback discovery by label
.dockerignore        only content/**/*.py enters the build context
```

## Run it

```bash
make up            # = docker compose up -d --build --wait, with git metadata
make update        # = git pull --ff-only && make up: the fix when a container says it is stale
make logs          # follow the grange service's output
make test          # unit tests inside the built image (make test-host: on the host)
make down
curl http://127.0.0.1:7788/health
```

Use `make`, not `docker compose` directly. Compose cannot run `git`, so the
Makefile is where the commit, branch, repo and dirty flag get captured and
exported as build args. An image built with plain `docker compose build` has
`unknown` metadata and will refuse to start.

## Fallback: finding the container by label

The port is fixed, so `curl` is the supported way in. `query_server.py` is
kept as a fallback, `python3 tools/query_server.py`, that finds the container
through the Docker socket instead:

1. `query_server.py` opens the Docker Unix socket (`DOCKER_HOST`, then
   `/var/run/docker.sock`, then `~/.docker/run/docker.sock`).
2. `GET /containers/json?filters={"label":["agent-grange.service=tailscale"],"health":["healthy"]}`
   returns the sidecar, which owns the published port, once it carries our label **and** has passed its healthcheck.
3. The entry's `Ports` array holds `PrivatePort: 8080 -> PublicPort: <port>`.
   The script takes the IPv4 binding and connects to `127.0.0.1:<port>`.
4. If no healthy container exists yet it polls once a second for up to 60 s
   (`DISCOVERY_TIMEOUT` overrides), printing the container's current state.

If more than one healthy container carries the label, the client takes the
first one Docker lists (most recently created). Run one copy per machine, or
add a second label/filter to tell them apart.

## Freshness check

Problem: each developer updates with `git pull` and then (ideally) `make up`.
If they skip either step they run stale code and nothing tells them.

The team marks the current blessed commit with a moving git tag,
`agent-grange-latest`. Every container start compares itself to that tag.

**Build time** (`Makefile` -> compose build args -> Dockerfile `ARG`/`ENV`):

| baked in     | from                                      |
|--------------|-------------------------------------------|
| `GIT_REPO`   | `remote.origin.url`, e.g. `djp3/agent-grange`       |
| `GIT_COMMIT` | `git rev-parse HEAD`                      |
| `GIT_BRANCH` | informational only                        |
| `GIT_DIRTY`  | 1 if this directory had uncommitted changes |
| `GIT_PUSHED_BASE` | nearest ancestor(s) of HEAD that exist on origin (`git rev-list --boundary HEAD --not --remotes=origin`); HEAD itself if nothing is unpushed |

The commit is also stored as the OCI label `org.opencontainers.image.revision`,
so `docker image inspect` shows it, and the server returns it at `/version`.

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
`FRESHNESS_STRICT=0 make up` to start with a loud warning instead (offline, or
before the tag exists).

**While running.** A container that is already up when the tag moves would never
know, so `server.py` polls the tag every `RECHECK_SECONDS` (default 30, with 10%
jitter so a team does not poll in lockstep). The poll is a conditional GET: it
sends the `ETag` from the last tag-ref response as `If-None-Match`, and GitHub
answers `304 Not Modified` when the tag has not moved. A 304 is not counted
against the API rate limit and triggers no further calls, so the steady state
costs nothing; only a real move runs the full comparison. Only a confirmed STALE
verdict changes state; a transient GitHub error keeps the previous state. Once
stale:

- every route, `/health` included, answers `503` with a JSON body naming the
  built commit, the tag's commit, and the fix, plus `Connection: close` and
  `Retry-After`;
- `healthcheck.py` sees the 503 and prints its summary, so after 3 misses Docker
  marks the container **unhealthy** with the reason in its health log;
- `query_server.py` finds the container, sees it is unhealthy, prints the reason
  from the health log and exits 1 immediately instead of waiting;
- if the tag later points back at this commit, the server recovers on its own.

Rate-limit note: while the tag does not exist yet, every poll is a 404, which
*is* counted. At 30 s that is 120/hour, fine with a token (5000/hour) but over
the 60/hour unauthenticated limit. Create the tag, or set a token.

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

The tag is the team's "this is current" pointer. Advance it when a commit is
ready for everyone:

```bash
git tag -f agent-grange-latest <commit>
git push -f origin agent-grange-latest
```

A moving tag needs force-push, so either restrict who may push it (GitHub tag
protection rules) or have a GitHub Actions job move it on merge to `main`.
Until the tag exists every start refuses with "tag not found". Use
`FRESHNESS_STRICT=0 make up` in the meantime.

### Try the paths yourself

```bash
make up                                                  # tag missing -> refuses (strict default)
FRESHNESS_STRICT=0 make up                               # -> warns, starts
CHECK_TAG=v0.5.9 make up GIT_COMMIT=$(git rev-parse 'v0.5.9^{commit}')   # identical -> ok
CHECK_TAG=v0.5.9 make up GIT_COMMIT=$(git rev-parse 'v0.5.9~2')          # 2 behind -> refuses
CHECK_TAG=v0.5.9 make up                                 # unpushed HEAD on top of the tag -> warns, starts
CHECK_TAG=v0.5.9 make up GIT_PUSHED_BASE=$(git rev-parse 'v0.5.9~2')   # unpushed, ancestor predates tag -> refuses
docker compose up -d --build --wait                      # no Makefile -> unknown metadata -> refuses
SKIP_FRESHNESS_CHECK=1 docker compose up -d --wait       # bypass -> starts
make down
```

## Things that would break it

- Server bound to `127.0.0.1` inside the container instead of `0.0.0.0`.
- Label in `docker-compose.yml` not matching `LABEL` in `query_server.py`.
- Removing the healthcheck (the `health` filter would then never match; drop the
  filter too if you do this).
- Any restart policy: a refusal would become a crash loop, and the grange is
  meant to stay down until `make up` (R8.9). Compose sets none.
- Building with plain `docker compose` instead of `make`: metadata is `unknown`.
