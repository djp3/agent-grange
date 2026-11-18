# agent-grange v1 requirements

Status: accepted 2026-11-16. Decisions below were made by Don Patterson on
2026-11-16, in a design discussion relayed from the agent-frolic session and an
interview in the agent-grange session. The whitelist semantics (R5.4), the
default values in section 7, and farm's heartbeat cadence (R5.2) were confirmed
the same day. Execution plan: `docs/plan.md`.

## 1. Purpose

agent-grange ("grange") is the communication plane between two existing
programs:

- **agent-farm** ("farm") runs on each developer's Mac and watches that
  machine's Claude Code sessions.
- **agent-frolic** ("frolic") is a board of open GitHub pull requests. It wants
  to show which agents are working on which pull request, under which human.

Grange carries each machine's agent status to every teammate's board. It holds
no history, makes no decisions about pull requests, and stores nothing on disk.

### Non-goals for v1

- Matching agents to pull requests. Frolic does that from the agent name.
- Normalizing states. Grange passes farm's vocabulary through unchanged.
- Persistence. State is disposable and rebuilt within one heartbeat.
- More than one team on the same mesh. The protocol reserves a group name;
  v1 ships one group.
- A user interface. Grange serves JSON only. Observability is a later, separate
  app.
- Carrying repo, branch, or folder per agent. Planned for v2.

## 2. Terms

| term | meaning |
|---|---|
| grange | one instance of this service; one per user, on that user's machine |
| grange id | a short name set in that machine's `.env`; stable across restarts |
| owner | the human a session belongs to, as reported by farm |
| agent | one Claude Code session as farm sees it |
| record | everything one grange publishes about itself, versioned |
| mesh | all granges that gossip with each other |
| sidecar | the Tailscale client container that gives a grange its tailnet address |
| control server | the headscale server the sidecars register with |
| tag | the tailnet tag that marks a device as a grange, `tag:grange` |

## 3. System shape

```
  laptop A                                           laptop B
 ┌───────────────────────────────────────┐         ┌────────────────────────────┐
 │ agent-farm ──push──▶ 127.0.0.1:7788   │         │                            │
 │ agent-frolic ◀─long-poll─┐            │         │                            │
 │                          │            │         │                            │
 │  docker compose project  ▼            │         │  docker compose project    │
 │  ┌─────────────┐  ┌──────────────┐    │ tailnet │  ┌─────────┐ ┌─────────┐   │
 │  │  tailscale  │◀─┤    grange    │◀───┼─gossip──┼─▶│tailscale│ │ grange  │   │
 │  │  sidecar    │  │  (python)    │    │  :7789  │  │ sidecar │ │         │   │
 │  └──────┬──────┘  └──────────────┘    │         │  └────┬────┘ └─────────┘   │
 └─────────┼─────────────────────────────┘         └───────┼────────────────────┘
           │              headscale (operated separately)  │
           └──────────────────────┬────────────────────────┘
                                  ▼
```

- **R3.1** One grange per user, running on that user's machine. The design size
  is 100 granges. Typical load is up to about 10 agents per grange.
- **R3.2** A grange is one docker compose project with two containers: a
  Tailscale client sidecar and the grange itself. The grange container shares
  the sidecar's network namespace.
- **R3.3** Farm and frolic reach grange only on localhost, on a fixed port that
  compose publishes to `127.0.0.1` and that is set in `.env`.
- **R3.4** Granges reach each other only over the tailnet, through the sidecar.
  The gossip port is never published to the host and never reachable from the
  LAN.
- **R3.5** The control server is headscale, operated outside this repo. This
  repo documents what it must provide (section 9) and ships a headscale
  container for local testing only.
- **R3.6** Farm pushes to its local grange. Grange never polls farm.
- **R3.7** Frolic long-polls its local grange and always receives the whole
  merged picture, never a difference.
- **R3.8** Granges gossip with peers to spread the full status. There is no
  central hub.

## 4. Data model

### 4.1 Agent

The v1 unit of status is a triple reported by farm:

| field | type | meaning |
|---|---|---|
| `owner` | string | GitHub login of the human who operates the session |
| `name` | string | the session name as farm sees it |
| `state` | string | farm's raw state, passed through unchanged |

- **R4.1** Grange treats `state` as an opaque string. Farm's vocabulary today
  is `busy`, `waiting`, `idle`, `shell`, `dead`, `unknown`; grange must not
  reject a value it has not seen. Mapping to what frolic draws (working,
  at-prompt, waiting) is frolic's job.
- **R4.2** Agents are a list, not a map. Two sessions with the same name are two
  entries. Grange does not deduplicate.
- **R4.3** Every string field is limited to 256 bytes; a record carries at most
  200 agents. Pushes that exceed either limit are rejected with 413.
- **R4.4** No other field about a session leaves the machine: no transcripts,
  prompts, tokens, paths, hostnames, or timestamps from farm.

### 4.2 Grange record

Everything one grange publishes about itself. One record per grange, keyed by
grange id. Only the owning grange ever writes it.

```json
{
  "proto": 1,
  "group": "default",
  "grange": "djp3",
  "incarnation": 1763323200123,
  "counter": 418,
  "farm_age_s": 7.2,
  "leaving": false,
  "agents": [
    {"owner": "djp3", "name": "proj-86-pr-98 fix tests", "state": "busy"},
    {"owner": "djp3", "name": "proj-91-pr-102 review",   "state": "waiting"}
  ]
}
```

| field | set by | meaning |
|---|---|---|
| `proto` | grange | protocol version; exchanges with a different value are rejected |
| `group` | grange, from `.env` | mesh name; v1 ships `default` |
| `grange` | grange, from `.env` | the grange id |
| `incarnation` | grange | chosen once at start from the local clock in milliseconds |
| `counter` | grange | increases on every change and every heartbeat; never decreases within an incarnation |
| `farm_age_s` | grange | seconds since farm last pushed, or `null` if it never has |
| `leaving` | grange | true on the final record sent during clean shutdown |
| `agents` | farm, relayed by grange | the whitelisted sessions, full list, replaces the previous list |

- **R4.5** Merge rule: a record replaces the stored record for the same grange
  id when its `(incarnation, counter)` is lexicographically greater. Anything
  else is ignored. No clocks are compared across machines.
- **R4.6** Because a record is a full replacement, an agent disappears when farm
  stops listing it. There are no per-agent tombstones.
- **R4.7** `incarnation` and `counter` are compared only against the same
  grange's earlier values. Wall-clock values in a record are never used for
  ordering.

## 5. Interfaces

All bodies are JSON. All timing fields are relative ages in seconds computed by
the grange that serves the response, never absolute timestamps from another
machine.

### 5.1 Farm to grange, localhost

`PUT /v1/farm`

```json
{"agents": [{"owner": "djp3", "name": "proj-86-pr-98 fix tests", "state": "busy"}]}
```

- **R5.1** The body is the complete list of whitelisted sessions. Grange
  replaces its agent list with it. An empty list is valid and means no
  team-relevant sessions.
- **R5.2** Farm sends on every change and at least every 30 seconds as a
  heartbeat, even when nothing changed.
- **R5.3** Grange answers 204. It bumps its counter and gossips immediately only
  if the list differs from the previous one.
- **R5.4** The whitelist lives in farm's configuration, alongside the grange
  port: a list of regular expressions; a session is sent only if its name
  matches at least one. Grange documents this rule and relies on it, but does
  not enforce it. Confirmed: farm applies the whitelist and sends only
  matching sessions; grange never sees the others.

### 5.2 Frolic from grange, localhost

`GET /v1/agents?after=<cursor>&wait=<seconds>`

```json
{
  "cursor": 9124,
  "grange": "djp3",
  "incarnation": 1763323200123,
  "granges": [
    {
      "grange": "djp3",
      "seen_age_s": 0.0,
      "farm_age_s": 7.2,
      "farm_stale": false,
      "agents": [{"owner": "djp3", "name": "proj-86-pr-98 fix tests", "state": "busy"}]
    },
    {
      "grange": "alice",
      "seen_age_s": 2.4,
      "farm_age_s": 95.0,
      "farm_stale": true,
      "agents": [{"owner": "alice", "name": "proj-12-pr-7 docs", "state": "idle"}]
    }
  ]
}
```

- **R5.5** The response is always the whole merged picture: every grange not yet
  expired (section 6.3), including the local one.
- **R5.6** `cursor` is a local integer that increases whenever the picture
  changes: an agent list changes, a grange appears or expires, or a
  `farm_stale` flag flips. Heartbeats and the ticking of age fields do not
  change it.
- **R5.7** With `after`, grange answers immediately if the cursor has moved past
  it, otherwise holds the request up to `wait` seconds (maximum 30) and then
  answers with the current picture either way. Without `after`, or with
  `after=0`, it answers immediately.
- **R5.8** After a grange restart the cursor starts again from 1. A client whose
  `after` is greater than or equal to the current cursor is answered
  immediately, so a stale cursor never blocks. Clients may use `incarnation` to
  notice the restart.
- **R5.9** `seen_age_s` is how long since this grange last received a higher
  counter for that peer. `farm_age_s` is relayed from the peer's record.
  `farm_stale` is true when `farm_age_s` exceeds the configured threshold.

### 5.3 Grange to grange, tailnet

`POST /v1/gossip`

Request and response have the same shape: the sender's view of versions and
any records it believes the other side lacks.

```json
{
  "proto": 1,
  "group": "default",
  "from": "djp3",
  "digest": {"djp3": [1763323200123, 418], "alice": [1763300000000, 77]},
  "records": [ { "...": "full grange records newer than the receiver's digest" } ]
}
```

- **R5.10** The receiver merges `records` under R4.5, then replies with every
  record it holds whose version is greater than the sender's digest entry, or
  that the sender's digest lacks.
- **R5.11** `proto` or `group` mismatch is rejected with 409 and logged once per
  peer.
- **R5.12** The gossip endpoint is bound only to the tailnet address and
  answers only connections whose source is a tailnet address.
- **R5.13** Before merging, grange asks the sidecar's local API who the source
  address is. The exchange is accepted only if that device carries the grange
  tag. Failure to answer is treated as rejection. The headscale ACL is the
  mandatory floor; this check is the belt on top.

### 5.4 Health and version

- **R5.14** `GET /health` returns 200 with freshness and mesh summary (peer
  count, cursor, last gossip age) while the image is current, and 503 while it
  is stale, as the existing freshness design specifies.
- **R5.15** `GET /version` returns the baked git metadata.
- **R5.16** While stale, every route including `/v1/gossip` returns 503, so a
  stale grange drops out of the mesh and its record ages out at peers.

## 6. Behaviour

### 6.1 Startup

1. Freshness check against the `agentic-grange-latest` tag, exactly as built.
   A stale or unverifiable image refuses to start.
2. Read configuration (section 7). Missing `GRANGE_ID` refuses to start.
3. Wait for the sidecar to report a tailnet address, up to 60 seconds, then
   refuse to start.
4. Bind the local API to loopback and the container's bridge address. Bind the
   gossip listener to the tailnet address. Never bind either to all interfaces.
5. Discover peers (6.4), run one repair exchange with each known peer, then
   start serving.
- **R6.1** Steps 3 to 5 complete within 10 seconds of the sidecar being ready
  under normal conditions.

### 6.2 Heartbeat and liveness of this grange

- **R6.2** Grange bumps its own counter every `HEARTBEAT_SECONDS` (default 10)
  regardless of whether farm has pushed. The heartbeat rides the repair
  exchange; it is not pushed to neighbours as a change.
- **R6.3** `farm_age_s` is recomputed on every outgoing record. When it exceeds
  `FARM_STALE_SECONDS` (default 60) the local `farm_stale` flag flips, the
  cursor moves, and the last known agents keep being published. Boards show
  them flagged, not removed. A dead farm is distinguishable from a closed
  laptop.

### 6.3 Liveness of peers

- **R6.4** A peer whose counter has not advanced for `EXPIRE_SECONDS` (default
  60) is removed from the picture and the cursor moves.
- **R6.5** A record with `leaving: true` removes the peer immediately.
- **R6.6** Flapping guard: the version of a removed peer is remembered for
  `FORGET_SECONDS` (default 600). A record for that peer is readmitted only if
  its version is greater than the remembered one, so a lagging neighbour cannot
  resurrect it.
- **R6.7** A peer that restarts arrives with a greater incarnation and replaces
  its old record under R4.5 without waiting for expiry.

### 6.4 Peer discovery and topology

- **R6.8** Peers are discovered by tag: grange reads the sidecar's peer list
  through the local API and keeps every device carrying `TS_TAG` (default
  `tag:grange`) with a tailnet address. The list is refreshed every
  `DISCOVERY_SECONDS` (default 30). New teammates appear with no configuration
  change anywhere.
- **R6.9** For local testing without a tailnet, `GRANGE_PEERS` (a list of
  `host:port`) replaces tag discovery entirely.
- **R6.10** Each grange keeps up to `FANOUT` (default 5) steady neighbours,
  chosen deterministically from the sorted list of peer ids so the graph is
  connected and stable, plus one random peer per repair tick. Neighbours are
  recomputed when the peer list changes.

### 6.5 Propagation

- **R6.11** On a change to its own record (agent list, `farm_stale`, leaving),
  grange pushes the record to all neighbours within one second.
- **R6.12** A grange that receives a record it did not already have forwards it
  to its neighbours once. Records it already had are not forwarded.
- **R6.13** Every `REPAIR_SECONDS` (default 3) grange runs one exchange with one
  random known peer.
- **R6.14** Target: a state change on one laptop is visible on every teammate's
  board within 5 seconds at 100 granges. With a fanout of 5 and forward-once, a
  change crosses the mesh in about three hops; the repair exchange covers
  anything missed within a few seconds.
- **R6.15** Traffic budget: under 10 KB per second per grange averaged over a
  minute at design size, with the mesh otherwise idle.

### 6.6 Shutdown and restart

- **R6.16** On SIGTERM grange sends a `leaving` record to all neighbours, waits
  at most 2 seconds for the pushes, and exits.
- **R6.17** On start, a new incarnation is chosen. Nothing is read from disk.

### 6.7 Stale image

- **R6.18** When the freshness poll finds the tag has moved, every route returns
  503 including gossip, the Docker healthcheck fails, and the operator fixes it
  with `make update`. This is unchanged from the current design; the team-wide
  consequence, that boards go blank until each teammate updates, is accepted
  because it guarantees one protocol version across the mesh.

## 7. Configuration

All configuration is environment, read from a git-ignored `.env` next to the
compose file. An `.env.example` documents every key. Secrets are never baked
into the image and never committed.

| key | default | meaning |
|---|---|---|
| `GRANGE_ID` | required | this grange's id; also the sidecar's hostname suffix |
| `GRANGE_PORT` | `7788` | localhost port farm and frolic use |
| `GRANGE_GOSSIP_PORT` | `7789` | tailnet port for `/v1/gossip` |
| `GRANGE_GROUP` | `default` | mesh name carried in every record |
| `TS_AUTHKEY` | required for tailnet mode | reusable tagged pre-auth key from headscale |
| `HEADSCALE_URL` | required for tailnet mode | login server for the sidecar |
| `TS_TAG` | `tag:grange` | tag that marks peers |
| `GRANGE_PEERS` | empty | static `host:port` list; disables tag discovery when set |
| `HEARTBEAT_SECONDS` | `10` | own counter bump interval |
| `FARM_STALE_SECONDS` | `60` | farm silence before `farm_stale` |
| `EXPIRE_SECONDS` | `60` | peer silence before removal |
| `FORGET_SECONDS` | `600` | how long a removed peer's version is remembered |
| `REPAIR_SECONDS` | `3` | periodic exchange interval |
| `DISCOVERY_SECONDS` | `30` | peer list refresh interval |
| `FANOUT` | `5` | steady neighbours |
| `CHECK_TAG`, `FRESHNESS_STRICT`, `RECHECK_SECONDS`, `SKIP_FRESHNESS_CHECK`, `GITHUB_TOKEN` | as today | freshness check |

Farm-side configuration, in farm's own settings and outside this repo: the
grange port, and the whitelist of regular expressions (R5.4).

## 8. Deployment

- **R8.1** `docker-compose.yml` defines two services. `tailscale` runs the
  official Tailscale client image with `NET_ADMIN` and `/dev/net/tun`, a named
  volume for its state so the device identity survives restarts, a hostname of
  `grange-<GRANGE_ID>`, the login server and tag from `.env`, and publishes
  `127.0.0.1:${GRANGE_PORT}` to the grange API port. `grange` uses
  `network_mode: service:tailscale`, starts after the sidecar is healthy, and
  mounts the sidecar's daemon socket read-only for discovery and identity
  checks.
- **R8.2** The grange image stays `python:3.12-slim` with the standard library
  only. Any dependency needs a stated reason.
- **R8.3** The existing Makefile targets keep working: `make up`, `make down`,
  `make update`, `make logs`, `make info`. `make up` on a fresh laptop with a
  filled-in `.env` joins the mesh with no browser login and no approval click.
- **R8.4** A developer who has never joined needs only: clone, copy
  `.env.example` to `.env`, fill in `GRANGE_ID`, `TS_AUTHKEY`, `HEADSCALE_URL`,
  run `make up`.
- **R8.5** Build-time git metadata and the freshness check are unchanged.

## 9. Control server, security, privacy

### 9.1 What headscale must provide

Operated separately. This repo's `docs/headscale.md` is the runbook.

- **R9.1** The tag `tag:grange` is defined in the policy, owned by the admin.
- **R9.2** One ACL rule: sources with `tag:grange` may reach destinations with
  `tag:grange` on `GRANGE_GOSSIP_PORT`. Nothing else is granted to or from the
  tag.
- **R9.3** Join key: a reusable pre-auth key carrying `tag:grange`, created by
  the admin and distributed through the team password manager.
- **R9.4** Rotation: create a new key, let the old one expire or expire it.
  Devices already joined are unaffected; only new laptops need the new key.
- **R9.5** Removal: delete the device on the server. Its grange leaves the mesh
  at once and its record ages out of boards within `EXPIRE_SECONDS`.
- **R9.6** Headscale 0.28 or later, where tags are tied to the key rather than a
  user.

### 9.2 Security

- **R9.7** There is no application-level mesh secret. Peer identity comes from
  the tailnet: the ACL and the whois check (R5.13). Nothing in the repo or the
  image authenticates a peer.
- **R9.8** The kill-switch (freshness check) governs code and protocol versions
  only. It never carries or implies a secret.
- **R9.9** The local API has no authentication. Anyone with a process on the
  developer's machine can push status or read the board. Accepted for
  single-user laptops; recorded as a known limitation.
- **R9.10** Every incoming body is size-limited and schema-checked before any
  field is used.

### 9.3 Privacy

- **R9.11** Only `owner`, `name`, `state` ever leave a machine (R4.4).
- **R9.12** Only sessions matching farm's whitelist leave a machine (R5.4).
- **R9.13** Every grange holds the whole team's status. A team that must not
  see another team's agents needs its own tag, ACL rule, and group name; v1
  does not provide that.

## 10. Testing

- **R10.1** Unit tests, no network: merge rule (R4.5), cursor semantics
  (R5.6 to R5.8), expiry and flapping guard (R6.4 to R6.7), `farm_stale`
  transitions, request validation limits.
- **R10.2** Plain-network mode: a compose file that starts several granges on
  an ordinary Docker network with `GRANGE_PEERS` and a fake farm that pushes
  scripted changes. Automated scenarios:
  - a change on one grange is visible from every other grange's `/v1/agents`
    within 5 seconds;
  - a killed grange disappears from all boards within `EXPIRE_SECONDS`;
  - a restarted grange replaces its old record immediately;
  - a grange whose fake farm stops pushing shows `farm_stale` within
    `FARM_STALE_SECONDS` and keeps its agents;
  - a `leaving` record removes a grange immediately;
  - a grange whose image is stale returns 503 on gossip and ages out.
- **R10.3** Headscale mode: a compose file with a local headscale container,
  two or more sidecars registering with a pre-auth key, and their granges
  discovering each other by tag. Proves registration, tag discovery, the whois
  check, and the ACL on one laptop.
- **R10.4** A documented smoke test across two real laptops against the team's
  headscale before the tag is first moved.

## 11. Decision log

| # | decision | chosen | alternatives considered |
|---|---|---|---|
| 1 | status unit | owner, name, state triple from farm | name:state only; richer record with repo and branch (v2) |
| 2 | state vocabulary | farm's raw states, unchanged | grange normalizes; free-form |
| 3 | language and transport | Python, HTTP and JSON gossip | Python with a framework; Go with memberlist |
| 4 | stale image | refuse everything | serve local board only; serve with flag |
| 5 | grange id | configured in `.env` | tailnet hostname; random per start |
| 6 | control server | headscale, operated separately | Tailscale hosted (paid plan, tagged device limits) |
| 7 | join | reusable tagged pre-auth key in `.env` | interactive login; OAuth client (Tailscale hosted only) |
| 8 | peer discovery | by tag | static seeds; both |
| 9 | local port | fixed, in `.env` | Docker-assigned with label discovery |
| 10 | propagation target | under 5 seconds | under 30 seconds; minutes |
| 11 | peer expiry | about 60 seconds | 15 seconds; 2 to 3 minutes |
| 12 | privacy filter | whitelist regexes in farm's config | all sessions; grange filters |
| 13 | headscale in repo | separate; local test container only | full deployment in repo |
| 14 | farm silent | keep last agents, flag stale | empty list after timeout; rewrite state to unknown |
| 15 | groups | single group, name reserved in protocol | multiple groups in v1 |
| 16 | local testing | plain-network mode and headscale mode | one or the other |
| 17 | visibility | JSON only | `make status`; HTML page |
| 18 | mesh secret | none; tailnet identity | shared token; key list via gossip; key id via kill-switch |

## 12. Open items

- Whether frolic wants `owner` repeated per agent (as farm sends it) or lifted
  to the grange record when every agent has the same owner. v1 keeps it per
  agent, since farm reports it there.
- The exact local API paths of the Tailscale daemon used for discovery and
  whois are to be confirmed against the client version pinned in compose.
