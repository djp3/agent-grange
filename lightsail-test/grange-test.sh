#!/usr/bin/env bash
# One throwaway grange on AWS Lightsail, for checking that a local grange can
# reach a remote one. Usage and background: README.md in this directory.
#
# Settings come from the environment. OP_ITEM is required; every other one has
# a default. AWS account and region come from the AWS CLI in the usual way
# (AWS_PROFILE, AWS_REGION).
set -euo pipefail
cd "$(dirname "$0")"

STACK="${STACK:-grange-test}"              # CloudFormation stack name
INSTANCE="${INSTANCE:-grange-test}"        # Lightsail instance name
GRANGE_ID="${GRANGE_ID:-lightsail-test}"   # the remote grange's id on every board
GIT_REF="${GIT_REF:-develop}"              # branch, tag or commit the instance checks out
GIT_REPO="${GIT_REPO:-}"                   # HTTPS clone URL; default: this checkout's origin
BUNDLE="${BUNDLE:-micro_3_0}"              # Lightsail plan: 1 GB, 2 vCPUs
OP_ITEM="${OP_ITEM:-}"                     # required: 1Password item with fields "preauthkey" and "URL"
OWNER="${OWNER:-$(id -un)}"                # value of the AWS Owner tag; default: your local username
GRANGE_GROUP="${GRANGE_GROUP:-}"           # set only if the local grange sets one
SSH_CIDR="${SSH_CIDR:-}"                   # who may reach SSH; default: this machine's public address
LOCAL_PORT="${GRANGE_PORT:-7788}"          # the local grange's API port
REPO_DIR=/opt/agent-grange                 # where the instance clones the repository

say() { printf '%s\n' "$*" >&2; }
die() { say "grange-test: $*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

aws_identity() {
  need aws
  need jq
  local account region
  account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || die "no working AWS credentials; set AWS_PROFILE to a profile that has them"
  region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
  [ -n "$region" ] || die "no AWS region; set AWS_REGION"
  say "AWS account $account, region $region"
}

# remote [-t] [command]: run a command on the instance over SSH; stdin is
# passed through. Lightsail issues a key and certificate that last about a
# minute, together with the instance's host keys, so no key is kept on this
# machine and the host is verified before anything is sent to it.
remote() {
  local tty_flag="" access="$TMP/access.json" ip user strict=yes
  if [ "${1:-}" = "-t" ]; then
    tty_flag="-t"
    shift
  fi
  (
    umask 077
    aws lightsail get-instance-access-details --instance-name "$INSTANCE" \
      --protocol ssh --output json >"$access"
    jq -r '.accessDetails.privateKey' "$access" >"$TMP/key"
    jq -r '.accessDetails.certKey' "$access" >"$TMP/key-cert.pub"
  ) || return 1
  ip="$(jq -r '.accessDetails.ipAddress' "$access")"
  user="$(jq -r '.accessDetails.username' "$access")"
  jq -r --arg ip "$ip" '.accessDetails.hostKeys[]? | "\($ip) \(.algorithm) \(.publicKey)"' \
    "$access" >"$TMP/known_hosts"
  if [ ! -s "$TMP/known_hosts" ]; then
    if [ "${HOSTKEY_TOFU:-0}" != 1 ]; then
      say "Lightsail has not reported the instance's host keys yet"
      return 3
    fi
    strict=accept-new
  fi
  # shellcheck disable=SC2086  # tty_flag is empty or one word
  ssh $tty_flag -i "$TMP/key" -o CertificateFile="$TMP/key-cert.pub" \
    -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o UserKnownHostsFile="$TMP/known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o StrictHostKeyChecking="$strict" -o ConnectTimeout=10 -o LogLevel=ERROR \
    "$user@$ip" "$@"
}

# The join key and the control server's address, from 1Password into shell
# variables only. Read before anything is created, so a locked vault fails early.
read_secrets() {
  [ -n "$OP_ITEM" ] \
    || die "set OP_ITEM to the 1Password item with fields preauthkey and URL, e.g. OP_ITEM='op://<vault>/<item>'"
  need op
  KEY="$(op read "$OP_ITEM/preauthkey")" || die "could not read $OP_ITEM/preauthkey from 1Password"
  URL="$(op read "$OP_ITEM/URL")" || die "could not read $OP_ITEM/URL from 1Password"
  [ -n "$KEY" ] || die "$OP_ITEM/preauthkey is empty"
  [ -n "$URL" ] || die "$OP_ITEM/URL is empty"
  case "$URL" in
    http://* | https://*) ;;
    *) URL="https://$URL" ;;
  esac
}

repo_url() {
  local url="$GIT_REPO"
  [ -n "$url" ] || url="$(git config --get remote.origin.url 2>/dev/null || true)"
  case "$url" in
    git@github.com:*) url="https://github.com/${url#git@github.com:}" ;;
  esac
  [ -n "$url" ] || die "no origin remote found; set GIT_REPO to the HTTPS clone URL"
  printf '%s' "$url"
}

deploy() {
  local cidr="$SSH_CIDR" ip repo
  repo="$(repo_url)"
  if [ -z "$cidr" ]; then
    need curl
    ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com)" \
      || die "could not find this machine's public address; set SSH_CIDR, for example SSH_CIDR=203.0.113.7/32"
    cidr="$ip/32"
  fi
  say "stack $STACK: instance $INSTANCE ($BUNDLE), $repo at $GIT_REF, SSH allowed from $cidr"
  aws cloudformation deploy --stack-name "$STACK" --template-file template.yaml \
    --no-fail-on-empty-changeset \
    --parameter-overrides "InstanceName=$INSTANCE" "BundleId=$BUNDLE" \
    "GitRepo=$repo" "GitRef=$GIT_REF" "SshCidr=$cidr" "Owner=$OWNER" \
    --tags "Owner=$OWNER" Project=grange-test
}

wait_ready() {
  local deadline=$((SECONDS + 900))
  say "waiting for the instance to boot, install Docker and clone the repository (a few minutes)"
  until remote 'test -f /opt/grange-test/ready' 2>"$TMP/wait.err"; do
    if remote 'test -f /opt/grange-test/failed' 2>/dev/null; then
      remote 'sudo tail -n 40 /var/log/grange-test-bootstrap.log' || true
      die "the first-boot script failed; the end of its log is above"
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      cat "$TMP/wait.err" >&2
      die "the instance was not ready after 15 minutes"
    fi
    sleep 10
  done
}

# Write docker/.env on the instance and start the grange. The key goes down
# the SSH connection on stdin: never on a command line, never through AWS.
join() {
  say "delivering .env over SSH and starting the grange as $GRANGE_ID"
  {
    printf 'GRANGE_ID=%s\n' "$GRANGE_ID"
    printf 'TS_AUTHKEY=%s\n' "$KEY"
    printf 'HEADSCALE_URL=%s\n' "$URL"
    # No GitHub token on this machine, and the unauthenticated API allows 60
    # requests an hour, so the tag is polled every two minutes, not every 30 s.
    printf 'RECHECK_SECONDS=120\n'
    if [ -n "$GRANGE_GROUP" ]; then printf 'GRANGE_GROUP=%s\n' "$GRANGE_GROUP"; fi
  } | remote "sudo sh -c 'umask 077; cat > $REPO_DIR/docker/.env' && sudo make -C $REPO_DIR/docker up"
}

cmd_up() {
  aws_identity
  read_secrets
  deploy
  wait_ready
  join
  say ""
  say "The remote grange is up as $GRANGE_ID. Next: make check"
}

cmd_down() {
  aws_identity
  remote "sudo make -C $REPO_DIR/docker down" 2>/dev/null \
    || say "the grange was not stopped cleanly; boards drop its record after about a minute"
  say "deleting stack $STACK"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  say "deleted. The device grange-$GRANGE_ID stays listed on the headscale server until"
  say "its admin removes it: docs/headscale.md, \"Remove a teammate's grange\"."
}

cmd_status() {
  aws_identity
  local stack instance
  stack="$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null)" || stack="not created"
  say "stack     $stack"
  instance="$(aws lightsail get-instance --instance-name "$INSTANCE" --output json 2>/dev/null)" || {
    say "instance  none"
    return 0
  }
  jq -r '.instance | "instance  \(.state.name)  \(.publicIpAddress // "no address")  \(.location.availabilityZone)  \(.bundleId)",
         "tags      \([.tags[]? | "\(.key)=\(.value)"] | join("  "))"' <<<"$instance" >&2
  aws lightsail get-instance-port-states --instance-name "$INSTANCE" --output json |
    jq -r '.portStates[] | "firewall  \(.protocol) \(.fromPort)-\(.toPort) \(.state)  from \((.cidrs + .ipv6Cidrs + .cidrListAliases) | join(" "))"' >&2
  say "containers and health on the instance:"
  remote "sudo docker ps --format '{{.Names}}  {{.Status}}'; curl -fsS --max-time 5 http://127.0.0.1:7788/health" || true
}

# The test itself: each board should list the other grange.
cmd_check() {
  need curl
  need jq
  local here there local_id failed=0
  here="$(curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/v1/agents")" \
    || die "no grange answers on 127.0.0.1:$LOCAL_PORT; start the local one with make up in ../docker"
  local_id="$(jq -r '.grange' <<<"$here")"
  there="$(remote 'curl -fsS --max-time 5 http://127.0.0.1:7788/v1/agents')" \
    || die "the remote grange does not answer; look at: make status, make logs"
  lists() { jq -e --arg id "$2" '.granges | any(.grange == $id)' <<<"$1" >/dev/null; }
  line() { jq -r --arg id "$2" '.granges[] | select(.grange == $id) | "seen \(.seen_age_s) s ago, \(.agents | length) agent(s)"' <<<"$1"; }
  if lists "$here" "$GRANGE_ID"; then
    say "ok       local board ($local_id) lists $GRANGE_ID: $(line "$here" "$GRANGE_ID")"
  else
    say "MISSING  local board ($local_id) does not list $GRANGE_ID"
    failed=1
  fi
  if lists "$there" "$local_id"; then
    say "ok       remote board ($GRANGE_ID) lists $local_id: $(line "$there" "$local_id")"
  else
    say "MISSING  remote board ($GRANGE_ID) does not list $local_id"
    failed=1
  fi
  if [ "$failed" = 1 ]; then
    say "local health:  $(curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/health" | jq -c '{peers_discovered, peers_live, last_gossip_age_s, rejected}')"
    say "remote health: $(remote 'curl -fsS --max-time 5 http://127.0.0.1:7788/health' | jq -c '{peers_discovered, peers_live, last_gossip_age_s, rejected}')"
    say "Discovery can take 30 seconds after a grange starts; try again before digging."
  fi
  return "$failed"
}

# Push one agent to the remote grange, as agent-farm would. It should show up
# on the local board within a second: content crossing the tailnet.
cmd_agent() {
  need jq
  local name="${1:-lightsail smoke test}" state="${2:-busy}"
  jq -cn --arg o "$GRANGE_ID" --arg n "$name" --arg s "$state" \
    '{agents: [{owner: $o, name: $n, state: $s}]}' |
    remote "curl -fsS -o /dev/null -w 'PUT /v1/farm answered %{http_code}\n' -X PUT -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:7788/v1/farm"
  say "now look for it here: curl -s http://127.0.0.1:$LOCAL_PORT/v1/agents"
}

cmd_logs() {
  remote "echo '--- first boot'; sudo tail -n 15 /var/log/grange-test-bootstrap.log; \
echo '--- sidecar'; sudo docker logs --tail 30 agent-grange-tailscale-1 2>&1; \
echo '--- grange'; sudo docker logs --tail 60 agent-grange-grange-1 2>&1"
}

cmd_update() { remote "sudo make -C $REPO_DIR/docker update"; }
# ssh: a shell on the instance; ssh <command>: run one command there.
cmd_ssh() {
  if [ "$#" -eq 0 ]; then remote -t; else remote "$*"; fi
}

case "${1:-}" in
  up | down | status | check | agent | logs | update | ssh)
    cmd="$1"
    shift
    "cmd_$cmd" "$@"
    ;;
  *)
    die "usage: $0 up | check | agent [name] [state] | status | logs | update | ssh [command] | down"
    ;;
esac
