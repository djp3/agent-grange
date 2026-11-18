# Headscale for agent-grange: runbook and spike findings

This repo does not run the team's control server (R3.5, R9.0). This page is
what whoever stands it up needs: what headscale must provide, the exact
commands, and what the phase 5 spike found when the grange sidecar was run
against a local headscale. Requirement numbers refer to `requirements.md`.

Versions tested: headscale **0.29.7** (R9.6 asks for 0.28 or later) and the
Tailscale client image **tailscale/tailscale:v1.104.7**, both pinned in the
compose files. Headscale 0.29.7 rejects clients older than v1.80.

## 1. Runbook for the admin

Commands below run on the headscale server. In a container, prefix them with
`docker exec <headscale container>`.

### Server settings

- TLS on `server_url` with a certificate the clients trust. Clients need no
  extra configuration when the certificate is publicly trusted.
- The embedded relay on, with the server's public addresses, and STUN on
  udp/3478 open:

  ```yaml
  derp:
    server:
      enabled: true
      region_id: 999
      stun_listen_addr: "0.0.0.0:3478"
      ipv4: <public IPv4>
      ipv6: <public IPv6, if any>
  ```

- Tagged nodes do not expire: the spike saw `Expiration: N/A` on every
  tagged node, so no per-node key renewal is needed.

### The tag and the one rule (R9.1, R9.2)

The policy file, HuJSON, at the path in `policy.path`:

```hujson
{
  "tagOwners": {
    "tag:grange": [],
  },
  "grants": [
    {
      "src": ["tag:grange"],
      "dst": ["tag:grange"],
      "ip": ["tcp:7789"],
    },
  ],
}
```

`tag:grange` has no user owners: only the admin can hand it out, through a
pre-auth key. The port must equal `GRANGE_GOSSIP_PORT` (default 7789). Merge
this into an existing policy rather than replacing it. Then:

```console
headscale policy check -f /etc/headscale/policy.hujson
kill -HUP <headscale pid>          # or: systemctl reload headscale
```

### Create the join key (R9.3)

```console
headscale preauthkeys create --reusable --tags tag:grange --expiration 90d
```

It prints the key once. Put it in the team password manager. `--expiration`
takes `90d` or `2160h` alike; without it the key lasts one hour. Do not add a
`--user`: since 0.28 the tag belongs to the key, and the node is owned by the
special user `tagged-devices`.

### Rotate it (R9.4)

```console
headscale preauthkeys create --reusable --tags tag:grange --expiration 90d
headscale preauthkeys list
headscale preauthkeys expire --id <ID of the old key>
```

Replace the key in the password manager. Granges that already joined keep
working, across restarts of their sidecar, because the device identity lives
in the sidecar's state volume. A new laptop with the old key is refused with
`authkey expired`.

### Remove a teammate's grange (R9.5)

```console
headscale nodes list
headscale nodes delete --identifier <ID>
```

The device is cut off at once: in the spike its peers dropped it within two
seconds, and its sidecar logged out and exited. Its record then leaves every
board within about `EXPIRE_SECONDS`. **Also rotate the key** if that person
keeps a copy: anyone with a valid key in their `.env` can rejoin with
`make up`.

### What to tell a new teammate (R8.4)

1. Clone the repo.
2. `cp docker/.env.example docker/.env`.
3. Fill in `GRANGE_ID` (lowercase letters, digits, hyphens), `TS_AUTHKEY` (from
   the password manager), and `HEADSCALE_URL`.
4. `cd docker && make up`.

No browser login and no approval click. The sidecar registers as
`grange-<GRANGE_ID>` with `tag:grange`, and other granges find it within
`DISCOVERY_SECONDS`.

## 2. How the grange uses the sidecar

These are the facts the code relies on (`docker/content/grange/discovery.py`),
as the spike confirmed them against tailscale v1.104.7.

- **LocalAPI.** HTTP over the daemon's Unix socket, shared to the grange
  container as a volume at `/var/run/tailscale/tailscaled.sock`.
  - `GET /localapi/v0/status`: `Self` and `Peer`, each with `HostName`,
    `TailscaleIPs`, `Online`, and `Tags`.
  - `GET /localapi/v0/whois?addr=<IP>` or `?addr=<IP>:<port>`: `Node.Tags`
    of the device at that address; 404 `no match for IP:port` for an
    unknown address.
  - The `Host` header must be `local-tailscaled.sock`. A request carrying an
    `Origin` or `Referer` header is refused with 403.
- **Permissions.** The socket is mode 0666. Status and whois need only read
  access, and a non-root caller (uid 65534 in the spike) gets both, so the
  grange runs as uid 10001 (R8.7). Mounting the socket read-only does not
  restrict it; the user is the control.
- **Tags come from the key.** With `tag:grange` owned by the admin alone,
  headscale refuses a node that also passes `--advertise-tags=tag:grange`
  (`requested tags [tag:grange] are invalid or not permitted`). The sidecar
  therefore passes only `--login-server`, and the key supplies the tag.
- **Kernel networking.** `NET_ADMIN` and `/dev/net/tun` with
  `TS_USERSPACE=false` give a `tailscale0` interface with the tailnet IPv4.
  The grange binds its gossip listener to that address and only that one.
- **Health.** `TS_ENABLE_HEALTH_CHECK=true` with
  `TS_LOCAL_ADDR_PORT=127.0.0.1:9002` serves `/healthz`, which turns 200
  once the node has a tailnet address; compose waits on it before starting
  the grange. Bound to loopback, so it is not exposed on the tailnet.
- **DNS.** `TS_ACCEPT_DNS=false`: the grange uses addresses only, and the
  shared namespace keeps Docker's resolver.

## 3. Spike findings

Run by hand on 2026-11-18 against the local stack in
`docker/compose.headscale.yml`: headscale with the embedded relay behind a
throwaway CA, the policy above, and Tailscale sidecars on one Docker network.

| check (plan 5.1) | Docker Desktop for Mac (Docker 29.9) | Docker Engine on Linux |
|---|---|---|
| registration with the tag; peers see each other's tags in status | yes | not yet run |
| `NET_ADMIN` and `/dev/net/tun` | yes, `tailscale0` up with the tailnet IPv4 | not yet run |
| socket shared to a second container; status and whois paths and headers | yes, as in section 2 | not yet run |
| whois for the other node returns its tags | yes, `["tag:grange"]` | not yet run |
| non-root caller can use status and whois | yes, uid 65534 | not yet run |
| do tagged node keys expire | no: `Expiration N/A`, no `KeyExpiry` | not yet run |
| pre-auth key expiry | the default is 1 hour; `--expiration` sets it | (server-side, same) |
| expiring the key leaves joined nodes working | yes, also across a sidecar restart; new devices refused | not yet run |
| deleting a node cuts it off | yes, within 2 s; its sidecar exits | not yet run |
| binding to the tailnet IPv4 in the shared namespace | yes | not yet run |
| published port reaches the grange from host loopback | yes | not yet run |
| gossip port unreachable from host and bridge network | yes: refused on the bridge address, no route to the tailnet address | not yet run |
| ACL allows only the gossip port between tagged nodes | yes: 7789 connects, 8000 times out | not yet run |
| online hint after a sidecar stops or is killed | offline after 12 s; back 2 to 3 s after a restart | not yet run |
| online hint after a sidecar is paused | still online after 30 minutes; see below | not yet run |
| monotonic clock across `docker pause` | gap shown: 12.1 s, same as the wall clock | not yet run |
| monotonic clock across a machine sleep or suspend | not measured; timed in the phase 8 smoke test | not yet run |
| headscale stopped: joined granges keep gossiping | yes, changes still arrive at once | not yet run |
| headscale stopped: daemon answers status and whois | yes, and still reports peers online | not yet run |
| headscale stopped: a sidecar restarts | it gets no network map and stays unhealthy; see below | not yet run |

Sidecar integration (plan 5.2), on the same stack and the same runtime:

- three granges, each in its sidecar's namespace, discovered each other by tag
  and gossiped over the tailnet; the untagged device was not discovered, and
  `rejected.identity` stayed 0;
- a grange served 0.36 s after starting with its sidecar already healthy
  (R6.1 asks for under 10 s);
- the real `docker-compose.yml`, joined to the lab network by a throwaway
  override, went from `make up` with a filled `.env` to both containers
  healthy in 7 s, with no browser and no approval (R8.3, R8.4).

The Linux column is deferred by Don's decision of 2026-11-18. The checks
above are to be repeated on Docker Engine on Linux before the smoke test, and
`make test-headscale` covers most of them automatically.

### Points for Don

- **The tag is not requested by the sidecar.** R8.1 says the sidecar takes
  "the login server and tag from .env". With the policy locked down as R9.1
  wants, asking for the tag makes headscale refuse the registration, so the
  tag comes only through the pre-auth key in `.env`. `TS_TAG` is still the
  grange's discovery filter. The wording of R8.1, and the plan's note that
  compose consumes `TS_TAG`, could say so.
- **A frozen machine stays "online" for a long time.** A sidecar under
  `docker pause` was still reported online after 30 minutes, the length of
  the measurement. `docker pause` freezes the process but leaves the kernel
  answering on its open connection to headscale, so this is the worst case
  rather than a closed laptop, which drops off the network; the hint on a real
  sleep is to be watched in the phase 8 smoke test. The live-set rules allow
  for a hint that lags: failed marks keep the address out between discovery
  refreshes, as 6.4 example 5 describes, at a cost of at most one failed
  attempt per refresh, and expiry removes the record after 60 seconds either
  way.
- **A control server outage.** No requirement covers it. What the spike saw:
  running granges are unaffected; a sidecar that restarts during the outage
  cannot get a network map until headscale returns; its grange waits up to
  60 seconds at startup step 3 and then refuses, and with no restart policy
  it stays down until `make up`. If headscale came back within the 60
  seconds, the grange started normally six seconds later.
