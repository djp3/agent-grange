# lightsail-test: one remote grange for a contact test

This brings up one throwaway grange on an AWS Lightsail instance, so you can
check that the grange on your own machine and a remote one find each other
and gossip over the tailnet. The remote grange joins the same headscale
server as your local one, with the join key from 1Password.

Nothing in `docker/` depends on this folder.

## What you need

- AWS CLI v2 with credentials that may use CloudFormation and Lightsail. The
  account and region are whatever `AWS_PROFILE` and `AWS_REGION` select.
- The 1Password CLI (`op`), signed in, and an item with two fields:
  `preauthkey`, the reusable tagged join key, and `URL`, the headscale
  server. Name the item in `OP_ITEM`.
- `jq`, `curl`, `ssh`, and `make`.
- Your local grange running: `make up` in `../docker`.

## Use

```bash
cd lightsail-test
export AWS_PROFILE=<your profile>
export OP_ITEM='op://<vault>/<item>'
make up       # create the instance and start the grange; about five minutes
make check    # does each board list the other grange?
make agent    # push one test agent on the remote grange ...
curl -s http://127.0.0.1:7788/v1/agents    # ... and see it arrive here
make down     # stop the grange and delete everything
```

`make check` is the test. It reads your local board and the remote board and
passes when each lists the other grange. Give discovery 30 seconds after
`make up` before you read a failure as real.

Other targets: `make status` (stack, instance, tags, firewall, remote
health), `make logs` (first boot, sidecar and grange logs), `make ssh` (a
shell on the instance), `make update` (`make update` on the instance, for
when the tag has moved past its commit). To run one command on the
instance: `./grange-test.sh ssh '<command>'`.

## What `make up` does

1. Reads the join key and the server address from 1Password. This comes
   first, so a locked vault stops the run before anything is created.
2. Deploys `template.yaml` as the CloudFormation stack `grange-test`: one
   Lightsail instance (Ubuntu 24.04, 1 GB), tagged
   `Owner=<your local username>` and `Project=grange-test`, with SSH as its
   only open port. SSH is open to your current public address and to the
   Lightsail console's SSH client.
3. Waits while the instance's first boot installs Docker Engine from
   Docker's apt repository and clones this repository at `develop`.
4. Writes `docker/.env` on the instance over SSH and runs `make up` there:
   the same services as on a laptop: the one-shot configuration check, then
   the sidecar and the grange.

Running `make up` again is safe. It updates the firewall to your current
address, rewrites the `.env`, and starts the grange if it is down, for
example after the instance rebooted (the grange has no restart policy).

## Where the key goes

The join key is read from 1Password into a shell variable and sent down the
SSH connection on standard input. It is not a CloudFormation parameter, it is
not in the instance's launch script, and it is never on a command line. It
ends up in one place: `/opt/agent-grange/docker/.env` on the instance,
readable by root only, and it goes away with the instance.

SSH uses a key and certificate that Lightsail issues for about a minute at a
time, and the host keys Lightsail reports for the instance, so no SSH key is
stored on your machine and the instance is verified before the join key is
sent. If Lightsail never reports host keys, `HOSTKEY_TOFU=1 make up` accepts
the instance's key on first use.

## Settings

From the environment or on the `make` command line. `OP_ITEM` is required;
the rest are optional.

| name | default | meaning |
|---|---|---|
| `GRANGE_ID` | `lightsail-test` | the remote grange's id on every board |
| `GIT_REF` | `develop` | branch, tag or commit the instance checks out |
| `GIT_REPO` | this checkout's `origin` | HTTPS clone URL; must be public |
| `OP_ITEM` | none: required | the 1Password item, as `op://<vault>/<item>` |
| `OWNER` | your local username | value of the `Owner` tag on the stack and the instance |
| `GRANGE_GROUP` | empty | set it only if your local `.env` sets one |
| `SSH_CIDR` | your public address, `/32` | who may reach SSH |
| `BUNDLE` | `micro_3_0` | Lightsail plan |
| `STACK`, `INSTANCE` | `grange-test` | stack and instance names |
| `GRANGE_PORT` | `7788` | your local grange's port, for `make check` |

`GIT_REF` and `BUNDLE` take effect only when the instance is created. To
change them, `make down` and then `make up`.

## Cost and cleanup

The 1 GB plan is 7 dollars a month, billed by the hour, so a test of an
hour or two costs a few cents. `make down` stops the grange,
which tells the mesh it is leaving, and deletes the stack and the instance.

One thing outlives `make down`: the device `grange-lightsail-test` stays
listed on the headscale server until its admin deletes it. See "Remove a
teammate's grange" in `docs/headscale.md`. Every fresh `make up` registers a
new device there.

## When something is wrong

- **Every Lightsail call is denied.** Credentials can be limited to one
  region. Set `AWS_REGION` to a region where yours allow Lightsail.
- **`make up` fails while creating the stack.** `make down` clears a
  half-created stack; then try again.
- **`make up` stops at "waiting for the instance".** `make logs` shows the
  first-boot log. On the instance it is `/var/log/grange-test-bootstrap.log`.
- **The grange does not become healthy.** `make logs`. A refused key shows in
  the sidecar log; a stale image shows in the grange log with the fix.
- **`make check` says MISSING.** It prints both `/health` summaries. Zero
  `peers_discovered` on both sides means the two are not on one tailnet or
  do not carry the same tag. A count under `rejected.group` means the two
  `GRANGE_GROUP` values differ.
- **SSH stops working after you change networks.** `make up` again, which
  updates the firewall to your new address.

The remote grange shows `farm_stale: true` on every board. That is expected:
no agent-farm runs on the instance. It polls the release tag every two
minutes, not every 30 seconds, because it has no GitHub token and the
unauthenticated API allows 60 requests an hour.
