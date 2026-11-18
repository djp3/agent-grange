# agent-grange v1 requirements

Status: currently under review by Don Patterson
Execution plan: `docs/plan.md`, rewritten 2026-11-18 against this document.

## 1. Purpose

agent-grange ("grange") is the communication plane between two existing
programs:

- **agent-farm** ("farm") runs on each developer's machine and watches that
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
- Standing up the team's control server. That is outside this repo (R9.0).

## 2. Terms

| term | meaning |
|---|---|
| grange | one instance of this service; one per user, on that user's machine |
| grange id | a short name set in that machine's `.env`; stable across restarts |
| owner | the human a session belongs to, as reported by farm |
| agent | one Claude Code session as farm sees it |
| record | everything one grange publishes about itself; the body plus its versions |
| incarnation | a number chosen once per grange start; separates restarts |
| rev | the content version of a record; moves only when its content changes |
| counter | the liveness version of a record; moves on every heartbeat and every content change |
| digest | one line per grange: its id and the versions the sender holds for it |
| discovered peer | a device carrying the grange tag in the sidecar's peer list |
| live peer | a discovered peer in the live set of R6.10: unexpired record, or online hint and no failed mark |
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
- **R3.9** Names. The kill-switch tag is `agent-grange-latest`, the compose
  project is `agent-grange`, the container label is
  `agent-grange.service=grange`. The current `agentic-grange` names are renamed
  in plan phase 0. The new tag is created on GitHub before the default changes,
  because a strict start refuses until its tag exists. No organization name
  appears anywhere in the repo, the documents, or the implementation.

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
  entries. Grange does not deduplicate. `owner` stays on every agent entry, as
  farm reports it; it is not lifted to the record.
- **R4.3** Every string field is limited to 256 bytes; a record carries at most
  200 agents. Pushes that exceed either limit are rejected with 413.
- **R4.4** No other field about a session leaves the machine: no transcripts,
  prompts, tokens, paths, hostnames, or timestamps from farm. If farm sends
  any other field on an agent entry, grange drops it, does not reject the
  push, and never relays it.

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
  "rev": 12,
  "farm_age_s": 7.2,
  "farm_stale": false,
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
| `incarnation` | grange | chosen at start from the local clock in milliseconds, corrected per R4.8 |
| `counter` | grange | liveness version: increases on every heartbeat and every content change |
| `rev` | grange | content version: increases only when `agents`, `farm_stale`, or `leaving` change |
| `farm_age_s` | grange | seconds since farm last pushed; `null` until the first push |
| `farm_stale` | grange | true when farm has been silent too long (R6.3); owned by the origin |
| `leaving` | grange | true on the final record sent during clean shutdown |
| `agents` | farm, relayed by grange | the sessions farm reports, full list, replaces the previous list |

The content fields are `agents`, `farm_stale`, and `leaving`; `rev` versions
them. `counter` and `farm_age_s` are liveness fields: every sender writes its
own stored liveness for that grange into a body it sends, `counter` as last
refreshed (R4.5) and `farm_age_s` aged by the time the sender has held it. A
digest entry carries the same two values. So a relayed body may carry a later
`counter` than the origin put in it, and a body is never older, in liveness,
than its sender's knowledge.

- **R4.5** Merge rule, two parts.
  *Content:* a body replaces the stored body for the same grange id when its
  `(incarnation, rev)` is greater. When its incarnation is greater than the
  stored one, or no body is stored, the stored liveness is taken from the body
  as well.
  *Liveness:* the stored `counter` and `farm_age_s` are refreshed, and that
  peer's seen age resets to zero, from any body or digest entry whose
  `incarnation` equals the stored body's and whose `counter` is greater. A
  digest entry with any other incarnation refreshes nothing (R5.18).
  Invariant: the stored liveness and the stored body always share one
  incarnation. Anything else is ignored. No clocks are compared across
  machines.
- **R4.6** Because a body is a full replacement, an agent disappears when farm
  stops listing it. There are no per-agent tombstones.
- **R4.7** `incarnation`, `counter`, and `rev` are compared only against the
  same grange's earlier values. Wall-clock values in a record are never used
  for ordering.
- **R4.8** Incarnation correction. If a grange receives, for its own id, a
  version whose `(incarnation, counter)` is strictly greater than its own, in a
  body or a digest entry, it adopts `incarnation = max(now_ms, seen_incarnation
  + 1)`, restarts `counter` and `rev` at 1, and pushes its record. A version
  equal to its own is its own echo in a response digest and a lower one is lag;
  neither triggers. This heals a restart after a backward clock step within
  one repair exchange, as long as some peer still holds the old record. Once
  every peer has expired that record nothing lists it in a digest, the forget
  map refuses the new record silently (R6.6), and the grange is absent from
  other boards until `FORGET_SECONDS` have passed since the expiry; its own
  board keeps working. Damping: a second trigger within `FORGET_SECONDS` of a
  correction means two granges share an id. The grange then logs the address
  the version arrived from, which may be a relay and not the duplicate itself,
  sets `duplicate_id: true` in `/health`, does not correct again while the
  flag is set, and keeps serving its local API. The flag clears after
  `FORGET_SECONDS` with no further foreign greater version. Two granges
  sharing an id therefore correct a bounded number of times, two in the common
  case, and settle with the one holding the greater incarnation visible to the
  mesh and the other flagged.
- **R4.9** Within one incarnation `counter` and `rev` start at 1 and never
  decrease. Equal `(incarnation, rev)` implies identical content fields, so a
  body's content need never be sent twice to the same peer; only its liveness
  fields can differ between copies (4.2).

### 4.3 Worked example

Three teammates, Don (`djp3`), Alice, and Bob, each run one grange. Don's is
the origin throughout; the others hold copies. Times are Don's clock, used only
for narration: nothing below compares clocks across machines.

1. **Start.** At 09:00:00 Don's grange starts and reads its clock in
   milliseconds, 1763974800000, written I1 below. That is its incarnation for
   as long as this process lives. `counter` and `rev` are 1, the agent list is
   empty, `farm_age_s` is `null`. Its startup repairs with Alice and Bob come
   back with digests that do not list `djp3`, so each repair's follow-up push
   hands over his record. Alice and Bob now hold "djp3: I1, counter 1, rev
   1".

2. **First farm push.** At 09:00:05 farm sends two sessions. The list changed,
   so this is a content change: `rev` becomes 2 and `counter` becomes 2. The
   body is pushed to Don's neighbours within a second and forwarded once. On
   Alice's board the two agents appear and frolic's cursor moves.

3. **Heartbeats.** Every 10 seconds `counter` goes up by one and nothing is
   sent. The new value rides in the digests of the repair exchanges that run
   every 3 seconds, so Alice's stored counter for Don climbs, 3, 4, 5, and
   each time her "seen age" for Don resets to zero. `rev` stays at 2, the body
   is never resent, and the cursor does not move. By 09:01:00 the counter is 8.

4. **A state change.** At 09:01:12 one agent goes from `busy` to `waiting`.
   The 09:01:10 heartbeat had just made the counter 9, so farm's push of the
   new list gives `rev` 3, `counter` 10, body pushed at once. Every board
   shows the change within a few seconds. At 09:01:30 farm's heartbeat
   repeats the same list: grange answers 204 and resets `farm_age_s`, but
   neither `rev` nor `counter` moves and nothing is sent.

5. **A copy that is behind.** Bob was briefly unreachable and still holds
   rev 2 with counter 9. At 09:02:00 he repairs with Alice. His digest says
   "djp3: I1, counter 9, rev 2"; Alice holds counter 14, rev 3, so she
   refreshes nothing from it, and her response carries the rev 3 body stamped
   with her own stored counter, 14. Bob takes the content because rev 3 beats
   rev 2, and the liveness because 14 beats 9. Later a slow neighbour offers
   Alice a copy of rev 2 with counter 7. Both parts are behind what she
   holds, so it is ignored entirely.

6. **Farm goes quiet.** At 09:05:00 Don's farm crashes. The grange keeps
   heartbeating, so Don stays live everywhere. At 09:06:00, sixty seconds
   after the last push, `farm_stale` flips to true. That is a content change:
   `rev` 4, counter up, body pushed. Boards keep showing Don's two agents, now
   flagged stale. At 09:08:00 farm restarts and sends the same two sessions.
   The list is identical, but the push clears `farm_stale`, so that too is a
   content change: `rev` 5, pushed.

7. **Update and restart.** At 09:10:03 Don runs `make update`. On SIGTERM the
   grange sends a final body with `leaving` true, `rev` 6 and `counter` 66:
   the starting 1, sixty heartbeats, and five content changes, which were the
   first push, the state change, the stale flag set and cleared, and this
   one. Peers remove Don at once and remember "I1, counter 66" for ten
   minutes. The new container starts at 09:10:20 with a fresh incarnation I2,
   which is larger because the clock moved on, and `counter` and `rev` back
   at 1; farm's first push makes `rev` 2. Alice receives "I2, counter 2, rev
   2". Although counter 2 is far below the 66 she remembers, I2 is greater
   than I1, so this is a new life of the same grange: the body is accepted,
   and its liveness is taken whole.
   If Bob, lagging, later re-sends the old I1 body, it is refused because I1
   is below the I2 record everyone now holds. If a digest entry "I2, counter
   3" reaches someone who still holds an I1 body, it refreshes nothing: that
   peer waits for the I2 body, which the next repair response delivers.

8. **Laptop sleeps.** At 12:30 Don closes the lid with the counter at 1300.
   The process is frozen, so no heartbeat moves and no repair answers. By
   12:31 every peer's seen age for Don passes 60 seconds; they drop him from
   their boards, the cursor moves, and each remembers "I2, counter 1300". At
   13:15 the lid opens. What happens next depends on whether the stopwatch
   the grange measures time with, the monotonic clock, kept running while
   the machine slept. The phase 5 spike measures which it is on each kind of
   laptop.

   *The stopwatch ran on*, as it does under `docker pause`. The ticker sees
   a 45-minute gap between two of its runs, bumps the counter, and runs a
   repair immediately (R6.22). That repair's follow-up push carries Don's
   body with a counter above the remembered 1300, so the peer readmits him
   and forwards the body; Don is back on every board within a few seconds.
   On his own board every teammate's seen age is now 45 minutes, so the wake
   first expires everyone and the repair response then refills the picture.
   Farm looks 45 minutes silent as well, so `farm_stale` flips to true until
   farm's first push after the wake clears it. Both flips are content
   changes that every board sees.

   *The stopwatch stood still*, which is likely on a real laptop. No gap is
   seen and nothing special happens. A repair in the first seconds offers
   Don's body with counter 1300, which does not beat the remembered 1300 and
   is refused. The next heartbeat, at most 10 seconds away, makes it 1301,
   and the next repair delivers that, so Don is back on every board within
   about 13 seconds of his network returning. His own board keeps the
   picture from before the sleep: the first repair corrects the teammates
   who are still running, and teammates who left while he slept drop off
   within 60 seconds. Farm does not look silent, so `farm_stale` does not
   flip.

9. **Clock was wrong.** Suppose Don's clock was an hour ahead when a grange
   started, giving it an incarnation I3 that is too large, and NTP then fixes
   the clock. Thirty seconds later the container crashes, with no chance to
   send a `leaving` body, and Don starts it again. The new process picks I4
   from the corrected clock, so I4 is less than I3. Peers still hold "I3,
   counter 5", because a crash leaves the old record in place until it
   expires. Don's startup repair comes back with a digest listing his own id
   as "I3, counter 5", which is greater than his own "I4, counter 1". Had a
   copy of the I4 body reached a peer first, it would have been ignored as an
   older incarnation. He adopts I3 plus 1, resets `counter` and `rev` to 1,
   and pushes; everyone accepts. Healed in one exchange. This works only
   while some peer still holds the I3 record. Had the restart come after
   every peer had expired it, or been a clean `make update`, whose `leaving`
   body makes peers drop the record at once, nothing would list I3 in a
   digest: Don's new record would be refused silently until ten minutes had
   passed since the removal, then accepted as a fresh start.

10. **Two granges with one id.** Bob copies Don's `.env` by mistake and starts
    a second `djp3` with incarnation I5, greater than Don's current one. Don
    sees I5 in a digest and corrects: with both clocks roughly right, his
    current clock reading is already later than I5, so that becomes his new
    incarnation. Bob sees that and corrects in turn, to his own current
    reading, later still. Don sees that too, his second trigger within ten
    minutes: he logs the address the version came from, which may be a
    relaying peer and not Bob's machine, sets `duplicate_id` in `/health`,
    and stops correcting. Bob never sees a greater version again, so his flag
    never sets. The mesh shows Bob's agents under `djp3`; Don's own board
    still works, and his health page says why the team cannot see him. When
    Bob fixes his `.env` and restarts, peers remember Bob's last version for
    ten minutes, after which Don's record is accepted again.

What frolic notices in all of this, on Alice's machine: her cursor moves at
step 1, when Don's grange first appears, and at steps 2, 4, 6, 7, 8, 9, and
10, each time Don's content changes or he enters or leaves her picture. It
never moves for the heartbeats in step 3. In step 5 it is Bob's cursor that
moves, when he catches up to rev 3; Alice's does not, since she learns
nothing new. In step 8 Don's own cursor moves as well. If the stopwatch ran
on, it moves when everyone on his board expires and again when the repair
refills it. If the stopwatch stood still, it moves only for what really
changed while he slept.

## 5. Interfaces

All bodies are JSON. All timing fields are relative ages in seconds computed by
the grange that serves the response, never absolute timestamps from another
machine.

### 5.1 Farm to grange, localhost

`PUT /v1/farm`

```json
{"agents": [{"owner": "djp3", "name": "proj-86-pr-98 fix tests", "state": "busy"}]}
```

- **R5.1** The body is the complete list of sessions farm chooses to report.
  Grange replaces its agent list with it. An empty list is valid and means
  farm has nothing to report. The body is at most 1 MiB; a longer one is
  refused with 413 before it is read, and a malformed one with 400 (R9.10).
- **R5.2** Farm sends on every change and at least every 30 seconds as a
  heartbeat, even when nothing changed.
- **R5.3** Grange answers 204 and records the push time. If the list differs
  from the previous one, or `farm_stale` was true, that is a content change:
  `rev` and `counter` increase and the record is pushed at once (R6.11). Two
  lists are the same when they hold the same entries the same number of
  times, in any order, so a push that only reorders the list changes nothing
  and the stored order is kept. A push that clears `farm_stale` is a content
  change even when the list is identical (R6.3). Otherwise nothing else
  happens; liveness is the heartbeat's job (R6.2).
- **R5.4** Grange applies no filter of its own. Which sessions farm reports,
  and by what rule, is farm's concern and lives in farm's configuration; grange
  never needs to know the rule, now or when farm's filtering changes.

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
  expired (section 6.3), including the local one. The local grange is listed
  first and the others follow in order of grange id.
- **R5.6** `cursor` is a local integer that increases, by at least one,
  whenever one or more of these events occur: the local agent list changes;
  the local `farm_stale` flips; a peer body is accepted with a new
  `(incarnation, rev)`, including the first body from a peer; a peer expires,
  leaves, or is readmitted. Several events at once may move it by one or by
  more, so clients compare cursors for equality only. Heartbeats, digest
  liveness refreshes, and the ticking of age fields do not move it.
- **R5.7** With `after`, grange answers immediately if the cursor differs from
  it, otherwise holds the request up to `wait` seconds and then answers with
  the current picture either way. `wait` defaults to 30, which is also its
  maximum: a larger value is treated as 30, and `wait=0` answers immediately.
  Without `after`, or with `after=0`, it answers immediately. An `after` that
  is not a non-negative integer, or a `wait` that is not a non-negative
  number, is refused with 400.
- **R5.8** After a grange restart the cursor starts again from 1. `after` less
  than the cursor means something changed; `after` greater than the cursor
  means the grange restarted; both are answered immediately. Only `after` equal
  to the cursor waits. Clients may use `incarnation` to notice the restart,
  with one caution: an incarnation correction (R4.8) also changes
  `incarnation`, with no restart and no cursor reset, so a changed
  `incarnation` alone does not prove a restart.
- **R5.9** `seen_age_s` is how long since this grange last refreshed that
  peer's liveness (R4.5), by body or digest. `farm_age_s` is the last relayed
  value plus the time since it was received. `farm_stale` is the origin's flag
  (R6.3), relayed unchanged.

### 5.3 Grange to grange, tailnet

`POST /v1/gossip`. One endpoint, one body schema, two uses.

```json
{
  "proto": 1,
  "group": "default",
  "from": "djp3",
  "digest": {
    "djp3":  [1763323200123, 418, 12, 7.2],
    "alice": [1763300000000,  77,  3, 20.5]
  },
  "records": [ { "...": "full grange records as in 4.2" } ]
}
```

A digest entry is `[incarnation, counter, rev, farm_age_s]` for one grange id,
as the sender holds it at that moment.

- **R5.10** Two uses.
  *Push:* `records` carries one or more bodies and `digest` is omitted. The
  receiver merges the bodies under R4.5 and answers 204 with no body. Used by
  R6.11 and R6.12.
  *Repair:* `records` is empty and `digest` is the sender's full digest. The
  receiver merges the digest for liveness under R4.5 and answers 200 with the
  same schema: its own full digest, plus the bodies of every grange whose
  `(incarnation, rev)` it holds greater than the requester's entry or that the
  requester's digest lacks. The requester merges the response and then pushes
  to that peer the bodies it holds that are greater than, or missing from, the
  response digest. A repair is one round trip plus at most one push. Bodies
  in either direction carry the sender's stored liveness (4.2) and are trimmed
  to the caps (R5.17). Used by R6.13 and by startup.
- **R5.11** `proto` or `group` mismatch is rejected with 409, logged once per
  peer, and counted in `/health`.
- **R5.12** The gossip endpoint is bound only to the tailnet IPv4 address and
  answers only connections whose source is a tailnet address. Any other
  source is refused with 403.
- **R5.13** Before merging, grange asks the sidecar's local API who the source
  address is. The exchange is accepted only if that device carries the grange
  tag; otherwise it is refused with 403. Failure to answer is treated as
  rejection, logged once per peer, and counted in `/health`. The headscale ACL
  is the mandatory floor; this check is the belt on top.
- **R5.17** Caps. A request or response body is at most 1 MiB; a message
  carries at most 200 bodies and at most 1000 digest entries; a grange holds at
  most 1000 records, its own included, so its digest always fits, and ignores
  further ids, counting them in `/health`. Senders trim to the caps: as many
  bodies as fit go in this exchange and the rest follow in later ones.
  Over-limit messages, which only a violator sends, are rejected with 413 and
  logged once per peer.
- **R5.18** Bodies are accepted only under R4.5. A digest entry refreshes
  liveness only for an id whose body is already stored, and only when the
  entry's incarnation equals the stored body's. An entry for an unknown id, or
  with a different incarnation, refreshes nothing; the body arrives by the
  repair response or the follow-up push. Digest entries never readmit an
  expired peer (R6.6).

### 5.4 Health and version

- **R5.14** `GET /health` returns 200 while the image is current with: the
  freshness block as today; `cursor`; `incarnation`; `peers_discovered`,
  `peers_live`; `last_gossip_age_s`; `rejected` counts for `proto`, `group`,
  `identity`, and `overflow`; `duplicate_id`. `last_gossip_age_s` is the time
  since the last successful gossip exchange in either direction, and `null`
  until the first. `rejected.proto` and `rejected.group` count incoming
  messages refused under R5.11; `rejected.identity` counts those refused
  under R5.12 or R5.13; `rejected.overflow` counts messages refused with 413
  and ids ignored beyond the record cap (R5.17). It returns 503 while the
  image is stale, as the existing freshness design specifies.
- **R5.15** `GET /version` returns the baked git metadata.
- **R5.16** While stale, every route including `/v1/gossip` returns 503 and
  the scheduler stops sending, so a stale grange drops out of the mesh and its
  record ages out at peers.

## 6. Behaviour

### 6.1 Startup

1. Freshness check against the kill-switch tag, exactly as built. A stale or
   unverifiable image refuses to start.
2. Read configuration (section 7). A missing or invalid `GRANGE_ID` refuses to
   start.
3. Wait for the sidecar to report a tailnet IPv4 address, up to 60 seconds,
   then refuse to start. Plain mode skips this step (R6.9).
4. Bind the local API to loopback and the container's bridge address. Bind the
   gossip listener to the tailnet address. Never bind either to all interfaces.
5. Run discovery once, then repair exchanges with up to `FANOUT` peers in
   parallel, bounded to 5 seconds in total. Peers that do not answer are simply
   not live yet. There is no separate first push: the follow-up push of these
   repairs delivers this grange's first record (R5.10), and forward-once
   spreads it (R6.12).
6. Serve. The picture fills in over the next repair ticks.
- **R6.1** Steps 3 to 6 complete within 10 seconds of the sidecar being ready
  under normal conditions. The compose healthcheck's start period covers the
  60-second tailnet wait plus this (R8.1).

### 6.2 Heartbeat and liveness of this grange

- **R6.2** Grange increases its own `counter` every `HEARTBEAT_SECONDS`
  (default 10) regardless of whether farm has pushed. The heartbeat travels
  only in digests, through repair exchanges. It is never pushed and never
  forwarded.
- **R6.3** `farm_stale` is owned by the origin. It is true when no farm push
  has arrived within `FARM_STALE_SECONDS` (default 60), measured from the last
  push, or from grange start if none has arrived yet. `farm_age_s` is `null`
  until the first push. A flip is a content change: `rev` increases and the
  record is pushed. The last known agents keep being published, so boards show
  them flagged rather than removed. A dead farm is distinguishable from a
  closed laptop.
- **R6.22** Resume. The ticker records the monotonic time of each run. When a
  run finds a gap longer than 5 seconds, the process was paused or the machine
  slept: grange increases its `counter` and runs one repair exchange at once
  instead of waiting for the next tick. That repair's follow-up push carries
  its own body with the new counter, which readmits it at the peer (R6.6) and
  spreads by forward-once (R6.12). Without this, readmission after a pause
  would wait for the next heartbeat and the next repair tick.

### 6.3 Liveness of peers

- **R6.4** A peer whose seen age (time since its last liveness refresh, R4.5)
  exceeds `EXPIRE_SECONDS` (default 60) is removed from the picture and the
  cursor moves.
- **R6.5** A body with `leaving: true` that is accepted under R4.5 removes the
  peer immediately. A `leaving` body that R4.5 ignores, such as one from an
  older incarnation, removes nothing.
- **R6.6** Flapping guard: the `(incarnation, counter)` of a removed peer is
  remembered for `FORGET_SECONDS` (default 600). A body for that peer is
  readmitted only if its `(incarnation, counter)` is greater than the
  remembered one, so a lagging neighbour cannot resurrect it. Digest entries
  never readmit (R5.18). Because a body on the wire carries its sender's
  current liveness (4.2), a grange that returns after expiry is readmitted by
  whichever peer first receives any copy of its body with a later counter, not
  only by direct contact.
- **R6.7** A peer that restarts arrives with a greater incarnation and replaces
  its old record under R4.5 without waiting for expiry.

### 6.4 Peer discovery and topology

- **R6.8** Discovered set: grange reads the sidecar's peer list through the
  local API and keeps every device carrying `TS_TAG` (default `tag:grange`)
  with a tailnet IPv4 address, together with the daemon's online hint. The list
  is refreshed every `DISCOVERY_SECONDS` (default 30). New teammates appear
  with no configuration change anywhere.
- **R6.9** Plain mode, for testing only. When `GRANGE_PEERS` is set (a list
  of `host:port` for the other granges, never including this one), no tailnet
  is involved. The list replaces tag discovery entirely and every listed
  address counts as online. Startup does not wait for a sidecar (step 3). The
  gossip listener binds to the container's bridge address and answers only
  sources on that network, in place of R5.12. The identity check of R5.13 is
  not run. This grange's own address is its container hostname with the
  gossip port, which must equal what the other granges list. An incoming
  request is matched to the listed peer whose host resolves to its source
  address (R6.19). A mesh that carries real status never runs in plain mode.
- **R6.10** Live set and neighbours. The live set is built from discovered
  addresses. An address is live when either (a) it is bound (R6.19) to an id
  whose record is unexpired, or (b) no unexpired record is held for it and the
  daemon's online hint says it is online. Case (b) applies to every address
  at all times, not only before the first record is held, so two groups that
  expired each other still find each other. An address marked failed under
  R6.20 is outside the live set in either case. Until discovery has answered,
  the live set is empty. Probe: every `DISCOVERY_SECONDS`, and on every repair
  tick while the live set is empty, grange makes one repair attempt to a
  random discovered address outside the live set, whatever its hint and
  failed mark say. Neighbours are drawn from the live set: the successor of
  this grange's own address in sorted order, which keeps the ring connected,
  plus the `FANOUT - 1` (default 4) other live addresses with the lowest
  `hashlib.sha256` digest of this grange's id joined to the address, so the
  choice is stable, differs per grange, and one peer joining or leaving
  changes at most one neighbour. Sorted order means comparing addresses as
  `host:port` strings, byte by byte; every grange uses this same order, which
  is what makes the successor edges form one ring. Python's built-in `hash()`
  is salted per process and must not be used. Neighbours are recomputed only
  when the live set changes. The repair peer is drawn uniformly from the live
  addresses that are not neighbours, or from the whole live set when every
  live address is a neighbour.
- **R6.19** Address and id. An address is bound to a grange id by the `from`
  field of the most recent successful exchange with it, in either direction:
  a request this grange sent there, or an accepted request whose source is
  that address. A binding that changes is logged once. In tag mode the
  sidecar hostname `grange-<id>` is a hint only; a disagreement is logged
  once. Topology (sorting, successor) uses addresses; records and digests use
  ids.
- **R6.20** Timeouts and failed addresses. Connecting to a peer times out
  after `CONNECT_TIMEOUT_SECONDS` (default 1) and a whole exchange after
  `EXCHANGE_TIMEOUT_SECONDS` (default 3). Exchanges run on a worker pool of
  `2 x FANOUT`. A gossip request, push or repair, that does not get a 2xx
  answer has failed, whether it timed out, was refused, or was answered with
  an error such as 403, 409, 413, or 503. A failed request marks the address
  failed; the mark clears on a later successful request to it, on an accepted
  request from it, or when the next discovery refresh reports it online. A
  failed address is outside the live set (R6.10), so closed laptops do not
  fill the pool or become neighbours.

**Examples.** A team of twelve, each with one grange. Tailnet addresses are
`100.64.0.1` to `100.64.0.12`; Don is `100.64.0.5` and Alice `100.64.0.6`.
Defaults apply: `FANOUT` 5, discovery every 30 seconds, repair every 3
seconds, expiry at 60 seconds.

1. **A new teammate joins.** Carol clones the repo, fills in `GRANGE_ID`,
   the pre-auth key, and the headscale address, and runs `make up`. Her
   sidecar registers with headscale under `tag:grange` as `grange-carol` and
   gets `100.64.0.13`. Within 30 seconds every other grange's discovery
   refresh lists a new tagged device, online. Nobody edits anything. Her
   address has no record yet, so the online hint alone makes it live
   everywhere (case b). Her own grange discovers the twelve at startup,
   repairs with five of them in parallel, and has the full board within a
   few seconds. Neighbours recompute: Carol becomes the successor of
   `100.64.0.12`, and on any grange whose ranking puts her in its top four
   she displaces exactly one existing neighbour; the rest keep theirs. The
   team is now thirteen.

2. **Binding an address to a name.** Don's grange first talks to
   `100.64.0.13` in a repair; the reply says `from: carol`, so that address
   is Carol for topology and `carol` is the key for her record. The latest
   successful exchange with an address always decides, in either direction.
   The sidecar hostname `grange-carol` agrees. If Bob had set `GRANGE_ID=bob`
   but renamed his sidecar `grange-robert`, grange would log the disagreement
   once and go by `bob`, which is what his records say.

3. **How Don's neighbours are chosen.** Sorted by address, Don's successor is
   Alice at `100.64.0.6`; she is always a neighbour while she is live, and
   every grange's successor edge together makes one ring through the whole
   live set, so nobody is unreachable. For the other four, Don's grange
   computes the SHA-256 of `djp3` joined to each other live address and takes
   the four smallest. Alice ranks the same addresses with `alice` in front,
   so her four are different, and the mesh does not collapse onto the same
   few machines. The choice is stable: tomorrow, with the same twelve peers
   live, Don has the same five. If Frank's laptop leaves the live set and he
   was one of Don's four, the fifth-ranked address moves up and the other
   three stay; if he was Don's successor, the next address in order takes
   over.

4. **Who gets the repairs.** Every 3 seconds Don's grange picks one peer at
   random from the seven live ones that are not his neighbours. His
   pushes already reach the five neighbours, so repairs are how his
   heartbeats, and anything a push missed, reach the rest of the team.

5. **A laptop closes.** At 17:30 Frank shuts his lid. Don's next push to
   him waits one second for a connection, gives up, and marks the address
   failed; it drops out of Don's live set, his neighbours recompute without
   it, and repairs stop choosing it. Headscale still reports Frank online for
   a few minutes, so at Don's next discovery refresh the mark clears, Frank
   re-enters the live set, the next attempt fails again, and he is marked
   again: at most one wasted second per 30 seconds. After 60 seconds without
   a heartbeat Frank's record expires and boards drop him. Once headscale
   notices his sidecar is gone, the hint turns offline and his address leaves
   the live set until he returns. The probe still tries one outside address
   every 30 seconds, so a hint that is simply wrong cannot hide a reachable
   peer for good.

6. **The team splits in two.** Frank's laptop is still closed, so twelve
   granges are running. A relay outage leaves the six in the office able to
   reach each other but not the six at home, and the reverse, while both
   halves can still reach headscale. After 60 seconds each half has
   expired the other; every board shows six granges. The hints still say the
   far half is online, so far addresses stay in each live set, fail, get
   marked, and re-enter at each refresh. When the path heals, the next
   attempt to a far address simply works: one repair response brings all six
   missing bodies, and forward-once pushes carry them around the half within
   a second or two. Both halves have the full picture within 30 plus 15
   seconds at worst. If one half had also lost headscale, so its hints went
   stale and offline, the probe to a random outside address would find the
   far half instead.

7. **Cut off, then back.** Don's grange keeps running but his network drops
   for ten minutes. Every attempt fails and marks its address, and after 60
   seconds everyone on his board expires. Each discovery refresh clears the
   marks on addresses still hinted online, so there is usually something left
   in the live set to try; if the live set does empty, the probe takes over
   and tries a random discovered address on every repair tick. Either way one
   attempt goes out every 3 seconds. The first one to reach a running peer
   after the network returns succeeds, the response carries the bodies of the
   eleven others still running, and his board refills within a few seconds.
   The lid-closed case is different and is step 8 of the example in 4.3:
   nothing runs while the machine sleeps, so on wake the addresses are
   unmarked and still hinted online, and the first repair brings the board
   up to date at once.

8. **A device that is not a grange.** A teammate's second laptop is joined to
   the same headscale without `tag:grange`. No grange ever discovers it: it
   is not in the tagged peer list. If something on it tried to connect to a
   gossip port anyway, the headscale ACL would refuse the connection, and if
   the ACL were misconfigured, the identity check (R5.13) would reject the
   exchange because the device carries no grange tag.

9. **Local testing without a tailnet.** In the plain-network harness there is
   no sidecar and no hint. `GRANGE_PEERS` lists the other containers'
   `host:port`, and every listed address counts as online. The same rules
   then apply: a killed container's address fails and is marked, re-enters
   at the next refresh, fails again, and its record expires after 60 seconds.

### 6.5 Propagation

- **R6.11** On a content change to its own record, grange pushes the body to
  all neighbours within one second, driven by the event, not by a scheduler
  tick. An incarnation correction (R4.8) is pushed the same way.
- **R6.12** On accepting a body with a new `(incarnation, rev)` from any
  source, grange pushes it once to all neighbours except the one it came from,
  again event-driven. Bodies it already held are not forwarded. Digests are
  never forwarded.
- **R6.13** Every `REPAIR_SECONDS` (default 3) grange runs one repair exchange
  with one random live peer (R5.10). This is how heartbeats and `farm_age_s`
  spread.
- **R6.14** Target: a state change on one laptop is visible on every teammate's
  board within 5 seconds at 100 granges. With a successor plus four
  pseudo-random neighbours and forward-once, a change crosses the mesh in about
  three to four hops; the repair exchange covers anything missed within a few
  ticks. The simulation in R10.5 verifies this against a per-hop latency
  parameter.
- **R6.15** Traffic budget: under 10 KB per second per grange, sent plus
  received, averaged over a minute, with 100 granges of 10 agents each and no
  content changes. Model: a 100-entry digest is about 5 KB, a grange initiates
  one repair every 3 seconds and is the target of about one more, so about
  6 KB per second idle. Verified by the simulation in R10.5; measured on real
  laptops in rollout, not in the five-grange harness.

### 6.6 Shutdown and restart

- **R6.16** On SIGTERM grange sends a `leaving` body to all neighbours, waits
  at most 2 seconds for the pushes, and exits.
- **R6.17** On start, a new incarnation is chosen. Nothing is read from disk.

### 6.7 Stale image

- **R6.18** When the freshness poll finds the tag has moved, every route returns
  503 including gossip, the scheduler stops sending, the Docker healthcheck
  fails, and the operator fixes it with `make update`. The team-wide
  consequence, that boards go blank until each teammate updates, is accepted
  because it guarantees one protocol version across the mesh.

### 6.8 Protocol work in progress

- **R6.21** A build whose protocol or record schema differs from the tagged
  release runs with a private `GRANGE_GROUP`, so it neither disturbs nor is
  disturbed by the team mesh. Mismatches are counted in `/health` either way,
  so a split mesh is visible.

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
| `GRANGE_PEERS` | empty | static `host:port` list of the other granges; disables tag discovery when set |
| `HEARTBEAT_SECONDS` | `10` | own counter increase interval |
| `FARM_STALE_SECONDS` | `60` | farm silence before `farm_stale` |
| `EXPIRE_SECONDS` | `60` | peer silence before removal |
| `FORGET_SECONDS` | `600` | how long a removed peer's version is remembered |
| `REPAIR_SECONDS` | `3` | periodic exchange interval |
| `DISCOVERY_SECONDS` | `30` | peer list refresh interval |
| `FANOUT` | `5` | neighbours, including the ring successor |
| `CONNECT_TIMEOUT_SECONDS` | `1` | per-peer connect timeout |
| `EXCHANGE_TIMEOUT_SECONDS` | `3` | per-exchange total timeout |
| `CHECK_TAG` | `agent-grange-latest` after the phase 0 rename | kill-switch tag |
| `FRESHNESS_STRICT`, `RECHECK_SECONDS`, `SKIP_FRESHNESS_CHECK`, `GITHUB_TOKEN` | as today | freshness check |

An empty value means the default. `GRANGE_ID` is 1 to 56 characters from
lowercase letters, digits, and hyphens, and neither starts nor ends with a
hyphen, so that `grange-<GRANGE_ID>` is a valid hostname (R8.1).

Three keys must be equal on every grange in a mesh: `GRANGE_GOSSIP_PORT`,
because peers are dialled on the local value and the ACL names it,
`GRANGE_GROUP`, and `TS_TAG`. The timing keys are local to each grange, but a
grange's `HEARTBEAT_SECONDS` must stay well under every peer's
`EXPIRE_SECONDS`. One teammate can try a timing value in their own `.env`; a
team-wide change is a new default in the code, which is a release and a tag
move.

The caps in R4.3 and R5.17 and the resume gap in R6.22 are constants in code,
not configuration.

Farm-side configuration, in farm's own settings and outside this repo: the
grange port. What farm reports is farm's concern (R5.4).

## 8. Deployment

- **R8.1** `docker-compose.yml` defines two services in project `agent-grange`.
  `tailscale` runs the official Tailscale client image, pinned, with `NET_ADMIN`
  and `/dev/net/tun`, a named volume for its state so the device identity
  survives restarts, a hostname of `grange-<GRANGE_ID>`, the login server and
  tag from `.env`, a healthcheck, the label `agent-grange.service=tailscale`,
  and publishes `127.0.0.1:${GRANGE_PORT}` to the grange API port. `grange`
  uses `network_mode: service:tailscale`, starts after the sidecar is healthy,
  carries the label `agent-grange.service=grange`, mounts the sidecar's daemon
  socket, and has a healthcheck start period of at least 75 seconds to cover
  the tailnet wait.
- **R8.2** The grange image stays `python:3.12-slim` with the standard library
  only. Any dependency needs a stated reason.
- **R8.3** Makefile targets: `make up`, `make down`, `make update`, `make logs`
  (follows the grange service), `make info`, `make test`, `make test-sim`,
  `make test-host`, `make test-plain`, `make test-headscale` (R8.8). `make
  query` is retired; with a fixed port, `curl`
  against `127.0.0.1:${GRANGE_PORT}` replaces it. `tools/query_server.py` is
  kept as a fallback that discovers the container by label through the Docker
  socket; it is not part of the supported flow. `make up` on a fresh laptop
  with a filled-in `.env` joins the mesh with no browser login and no approval
  click.
- **R8.4** A developer who has never joined needs only: clone, copy
  `.env.example` to `.env`, fill in `GRANGE_ID`, `TS_AUTHKEY`, `HEADSCALE_URL`,
  run `make up`.
- **R8.5** Build-time git metadata and the freshness check are unchanged,
  except for the tag name in R3.9.
- **R8.6** Supported runtimes for v1 are Docker Desktop for Mac and Docker
  Engine on Linux. Both are verified in the phase 5 spike. Nothing else is
  supported.
- **R8.7** The grange container runs as a non-root user if the shared daemon
  socket accepts status and whois calls from it; the spike decides. A read-only
  mount does not restrict what a socket allows, so the user, not the mount, is
  the control.
- **R8.8** `make test` runs the unit tests and the short simulation inside
  the built image with `docker/tests` bind-mounted, so tests run on the
  interpreter the image ships whatever the host has installed; the Makefile
  needs Docker anyway. `make test-sim` runs the hour-long simulations (R10.5)
  the same way. `make test-host` runs the unit tests on the host interpreter
  as an optional fast loop with no claim about version parity. CI runs
  `make test`, `make test-sim`, and `make test-plain`. The harness drivers in
  `tools/` run on the host, because they drive Docker, so `make test-plain`
  and `make test-headscale` need a host Python 3.9 or later; the drivers use
  the standard library only.
- **R8.9** No automatic restart. Compose sets no restart policy on either
  service. After a reboot or a restart of Docker the grange stays down until
  the developer runs `make up`.

## 9. Control server, security, privacy

### 9.1 What headscale must provide

- **R9.0** As of 2026-11-18 no team headscale server exists. Standing one up
  is a work item outside this repo. Don will do it; no date is set. It is
  needed only by the phase 8 smoke test (R10.4). Nothing before phase 8 may
  depend on it; all earlier testing uses the headscale container in this
  repo. The runbook (`docs/headscale.md`) is delivered in phase 5 so he has
  it when he stands the server up.
- **R9.1** The tag `tag:grange` is defined in the policy, owned by the admin.
- **R9.2** One ACL rule: sources with `tag:grange` may reach destinations with
  `tag:grange` on `GRANGE_GOSSIP_PORT`. Nothing else is granted to or from the
  tag.
- **R9.3** Join key: a reusable pre-auth key carrying `tag:grange`, created by
  the admin and distributed through the team password manager.
- **R9.4** Rotation: create a new key, let the old one expire or expire it.
  Devices already joined are unaffected; only new laptops need the new key.
- **R9.5** Removal: delete the device on the server. Its grange leaves the mesh
  at once and its record ages out of boards within about `EXPIRE_SECONDS`.
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
  field is used (R4.3, R5.1, R5.17). A body that is not valid JSON, or whose
  known fields have the wrong type, is refused with 400. Unknown fields are
  dropped, not rejected.

### 9.3 Privacy

- **R9.11** Only `owner`, `name`, `state` ever leave a machine (R4.4).
- **R9.12** Farm decides which sessions leave a machine before it pushes;
  grange publishes what farm gives it and nothing else (R5.4).
- **R9.13** Every grange holds the whole team's status. A team that must not
  see another team's agents needs its own tag, ACL rule, and group name; v1
  does not provide that.

## 10. Testing

- **R10.1** Unit tests, no network: merge rule for content and liveness
  (R4.5), including that a digest entry with a greater incarnation refreshes
  nothing and that the stored body and liveness always share one incarnation;
  incarnation correction, an equal version ignored as an echo, and duplicate
  id damping, with two Stores sharing an id settling after a bounded number
  of corrections (R4.8); cursor semantics (R5.6 to R5.8), including that
  `after` equal to the cursor is the only case that waits; expiry and
  flapping guard (R6.4 to R6.7), including readmission by a relayed body that
  carries the relayer's later counter and never by a digest entry;
  `farm_stale` transitions including the never-pushed case and the push that
  clears it with an identical list (R5.3, R6.3); request validation, caps,
  and trimming (R4.3, R5.17); repair request and response construction with
  sender liveness in bodies (R5.10, 4.2); live set, failed marks, and
  neighbour selection (R6.10, R6.20); the resume gap (R6.22).
- **R10.2** Plain-network mode (R6.9): a compose file that starts five granges
  on an ordinary Docker network with `GRANGE_PEERS`, a fake farm that pushes
  scripted changes, and the mock GitHub server from the freshness work, which
  lives in `tools/`. The drivers run on the host (R8.8). Automated scenarios:
  - a change on one grange is visible from every other grange's `/v1/agents`
    within 5 seconds;
  - a killed grange disappears from all boards within `EXPIRE_SECONDS` plus 15,
    since its last counter can take several repair rounds to reach the last
    peer and each peer's expiry clock starts when it learns that counter;
  - a grange killed without a `leaving` body and serving again within 20
    seconds of the kill has its new incarnation's record on every board
    within 5 seconds of serving, and is never absent from a board in between
    (R6.7). Twenty seconds keeps the old record clear of expiry: at the kill a
    peer's last refresh can be `HEARTBEAT_SECONDS` plus 15 seconds old, and
    the new record needs up to 5 more, which totals 50 of the 60 seconds;
  - a grange whose fake farm stops pushing shows `farm_stale` within
    `FARM_STALE_SECONDS` plus 5 and keeps its agents;
  - a `leaving` body removes a grange within 3 seconds;
  - a grange paused with `docker pause` past `EXPIRE_SECONDS` disappears, and
    on unpause is readmitted within 5 seconds;
  - a grange whose image is stale returns 503 on gossip and ages out;
  - the same change scenario with `FANOUT=2`, so multi-hop forwarding runs over
    real sockets.
  The harness makes no claim about R6.15: with five granges every grange is
  everyone's neighbour.
- **R10.3** Headscale mode: a compose file with a local headscale container,
  three sidecar and grange pairs registering with a pre-auth key and
  discovering each other by tag, and a fourth sidecar without the tag. Proves
  registration, tag discovery, the whois check, and the ACL. Two negative
  cases: with the ACL in place the untagged device cannot connect at all; with
  the ACL opened for the test, the whois check alone rejects it.
- **R10.4** A documented smoke test across two real machines, one on each
  supported runtime where available, against the team's headscale before the
  tag is first moved.
- **R10.5** Simulation at design size: pure Python, a fake clock, a fake
  transport with a per-hop latency parameter that can also cut links, and a
  fake discovery whose online hint follows whether a grange is up with a
  configurable lag; 100 granges of 10 agents. Runs, each over one hour of
  simulated time:
  - all alive: a change converges to every grange within 5 seconds of
    simulated time; no false expiry; bytes per grange per second, sent plus
    received, within R6.15;
  - half of the discovered peers dead: the same three assertions over the
    live half;
  - partition and rejoin: two halves cut off from each other past
    `EXPIRE_SECONDS`, then rejoined, converge to the full picture within
    `DISCOVERY_SECONDS` plus 15 seconds;
  - return and fresh start: a grange silent past `EXPIRE_SECONDS` regains the
    full picture within 10 seconds of returning and is back on every other
    board within 10 seconds; a freshly started grange has the full picture
    within 10 seconds;
  - duplicate id: two granges configured with one id cause a bounded number
    of corrections and floods, and the mesh settles on one of them (R4.8).
  This is the test that exercises forward-once, neighbour choice, failed
  addresses, the probe, and the traffic budget at scale. The hour-long runs
  are `make test-sim`; `make test` runs a short all-alive run, a few simulated
  minutes, as a smoke test (R8.8). No assertion is weakened to fit a time
  limit.

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
| 12 | privacy filter | farm filters before pushing; grange has no filter | all sessions; grange filters |
| 13 | headscale in repo | separate; local test container only | full deployment in repo |
| 14 | farm silent | keep last agents, flag stale | empty list after timeout; rewrite state to unknown |
| 15 | groups | single group, name reserved in protocol | multiple groups in v1 |
| 16 | local testing | plain-network mode and headscale mode | one or the other |
| 17 | visibility | JSON only | `make status`; HTML page |
| 18 | mesh secret | none; tailnet identity | shared token; key list via gossip; key id via kill-switch |
| 19 | version split (review D1) | `rev` for content, `counter` for liveness, both in the digest | one counter for both |
| 20 | contract handoff (D2) | phase 8, as planned | earlier handoff |
| 21 | team control server (D3; owner settled 2026-11-18) | outside this repo; needed only by phase 8; Don stands it up, date open | deployment in repo |
| 22 | how work lands (D4; landing rule spelled out 2026-11-18) | plain commits on `main` once tests pass; no pull requests, no merge commits; from a worktree branch, `main` is fast-forwarded at the end of each phase on Don's word and pushed only on his word | pull requests |
| 23 | moving the tag (D5) | by hand | GitHub Actions on merge |
| 24 | runtimes (D6) | Docker Desktop for Mac and Docker Engine on Linux | Mac only |
| 25 | names (D7) | `agent-grange` for tag, project, label; renamed in phase 0 | keep `agentic-grange` |
| 26 | owner placement (D8) | on every agent entry | lifted to the record |
| 27 | label and organization names | `agent-grange.service`; no organization name anywhere in the repo | keep the prefixed label |
| 28 | test runner (review pass 2) | the built image with tests bind-mounted; host run optional | host interpreter |
| 29 | incarnation healing after expiry (pass 2; confirmed by Don 2026-11-18) | claim narrowed: heals while a peer still holds the old record, otherwise absent until `FORGET_SECONDS` pass | list remembered versions in digests, which needs a marker so they are not merged as live |
| 30 | restart after a reboot (review pass 3, Don) | no automatic restart; `make up` brings the grange back | restart policy on the containers |
| 31 | comparing farm pushes (pass 3; confirmed by Don 2026-11-18) | the same entries in any order are the same list | exact ordered comparison, with farm required to keep a stable order |
| 32 | long-poll `wait` (pass 3; confirmed by Don 2026-11-18) | defaults to 30 seconds, its maximum | no default; answer immediately when `wait` is absent |
| 33 | resume detection, R6.22 (Don, 2026-11-18) | kept; it looks for a jump in the monotonic clock only, and ages follow that clock whatever it does | strike it; also watch the wall clock |
| 34 | `GRANGE_ID` rule (pass 3; confirmed by Don 2026-11-18) | 1 to 56 characters from lowercase letters, digits, and hyphens, no leading or trailing hyphen | any string, refusing only a missing id |

## 12. Open items

- When Don stands up the team headscale server. He owns it; no date is set.
  Needed before phase 8.
- Don's go-ahead, in the session doing the work, for the two pushes in the
  phase 0 rename: creating `agent-grange-latest` on origin and later deleting
  `agentic-grange-latest`.
- The exact local API paths and headers of the Tailscale daemon used for
  discovery and whois, settled by the phase 5 spike against the pinned client
  version.

## 13. Revision history

- **2026-11-16** Accepted after the design interview. Farm-side filtering,
  default values, and farm heartbeat confirmed the same day.
- **2026-11-17** Revised after review. Content version `rev` split from the
  liveness `counter`; digest entry defined; push and repair uses of the gossip
  endpoint defined (R4.5, R4.9, R5.3, R5.6, R5.9, R5.10, R6.2, R6.12, R6.13).
  `farm_stale` made an origin-owned field, with the never-pushed case defined
  (R6.3). Long-poll wait condition corrected to `after` equal to the cursor
  (R5.8). Incarnation correction and duplicate id detection added (R4.8).
  Neighbours changed from consecutive ring entries to successor plus
  pseudo-random picks from the live set; live set, address-to-id binding, and
  timeouts defined (R6.10, R6.19, R6.20). Forwarding made event-driven (R6.11,
  R6.12). Startup exchange bounded (6.1, R6.1). Traffic budget stated as sent
  plus received and moved from the harness to the simulation (R6.15, R10.5).
  Gossip caps and `/health` counters added (R5.17, R5.14). Private group for
  protocol work (R6.21). Names changed to `agent-grange`, label to
  `agent-grange.service`, organization names removed (R3.9). Team control
  server status recorded (R9.0). Runtimes, non-root, host-run tests, retired
  `make query` (R8.6 to R8.8, R8.3). Pause and unpause, `FANOUT=2`, mock GitHub,
  and the ACL-open whois case added to tests (R10.2, R10.3). Owner placement
  closed (R4.2).
- **2026-11-18** Second review pass, fixes approved by Don. Incarnation
  correction triggers only on a strictly greater version, corrects once, then
  flags `duplicate_id` and stops; its healing claim narrowed to the case where
  a peer still holds the old record (R4.8, decision 29). Live set redefined so
  the online hint applies per address at all times, failed addresses are
  outside it, and a probe reaches addresses outside it (R6.10, R6.20, terms).
  Liveness refresh restricted to the stored body's incarnation, with the
  invariant stated (R4.5, R5.18). Bodies on the wire carry the sender's stored
  liveness; digest entries never readmit (4.2, R4.9, R5.10, R6.6). Resume
  detection added (R6.22, open item). Tests run inside the built image (R8.3,
  R8.8, decision 28). Simulation split into `make test-sim` hour-long runs
  with partition, return, fresh start, and duplicate id, plus a short run in
  `make test` (R10.5). Kill bound `EXPIRE_SECONDS` plus 15 and removal "about
  `EXPIRE_SECONDS`" (R10.2, R9.5). Clearing `farm_stale` is a content change
  even with an identical list (R5.3). Neighbour choice by `hashlib` ranking
  (R6.10). Senders trim to the caps (R5.17). Unit test list extended (R10.1).
- **2026-11-18, Don's manual pass.** Worked examples added for incarnation,
  `rev`, and `counter` (4.3) and for peer discovery and topology (6.4).
  Farm's filtering mechanism removed from grange's requirements, since grange
  need not know it (R5.1, R5.4, R9.12, section 7, decision 12). The 4.3
  example corrected after a trace against the rules: the counter in steps 4
  and 7, a crash instead of a clean restart in step 9, and the cursor note at
  the end.
- **2026-11-18, third review pass.** Points from the review of commit
  8083d15. Don confirmed R6.22 as written, watching the monotonic clock only
  (decision 33), and its open item is closed; step 8 of 4.3 now covers both
  a clock that runs on through sleep and one that stands still.
  Plain mode defined in one place (R6.9, 6.1 step 3). Sorted order and the
  choice of repair peer defined (R6.10). Binding by the most recent exchange
  in either direction (R6.19). A failed request defined (R6.20). First record
  delivered by the startup repairs (6.1 step 5). Farm push: reordering is not
  a change, 1 MiB cap, unknown fields dropped, 400 for malformed input (R5.1,
  R5.3, R4.4, R9.10, decision 31). Long-poll: default and cap for `wait`, 400
  for bad values, cursor moves by at least one, caution on `incarnation`
  (R5.6 to R5.8, decision 32). Board order (R5.5). `leaving` only when
  accepted (R6.5). Health fields and 403 defined (R5.12 to R5.14). Record cap
  includes the own record (R5.17). A correction is pushed like a content
  change (R6.11); the logged address may be a relay (R4.8). `GRANGE_ID` rule,
  empty means default, keys that must match across a mesh (section 7, 6.1
  step 2). Host Python for the harness drivers (R8.8, R10.2). No automatic
  restart (R8.9, decision 30). Restart scenario given a bound (R10.2).
  Landing rule spelled out (decision 22). Examples corrected to match (4.3
  steps 1 and 7 to 10; 6.4 examples 1 to 4, 6, 7).
- **2026-11-18, after the third pass.** Don confirmed the three choices made
  in that pass: farm pushes compared without regard to order (decision 31),
  the 30-second default `wait` (decision 32), and the `GRANGE_ID` rule
  (decision 34). He also took on standing up the team headscale server, with
  no date yet (R9.0, decision 21, open items), and confirmed the narrowed
  healing claim of R4.8 (decision 29). No decision in the log is left
  unconfirmed.
