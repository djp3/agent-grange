# agent-grange

The communication plane between **agent-farm**, which watches each developer's
Claude Code sessions, and **agent-frolic**, the team's board of pull requests.
Each developer runs one grange. It takes that machine's agent status from farm,
gossips it to every teammate's grange over a Tailscale tailnet run by headscale,
and serves the whole team's merged picture to frolic.

- To join: [docker/README.md](docker/README.md#join-the-mesh). In short, copy
  `docker/.env.example` to `docker/.env`, fill in three values, and run
  `make up` in `docker/`.
- What it does and why: [docs/requirements.md](docs/requirements.md).
- How it was built, phase by phase: [docs/plan.md](docs/plan.md).
- For whoever runs the headscale server: [docs/headscale.md](docs/headscale.md).

A grange holds no history and stores nothing on disk. It refuses to run when its
image is older than the `agent-grange-latest` tag, so the whole team speaks one
protocol version; `make update` is the fix.
