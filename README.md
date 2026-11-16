# agent-grange
A communication substrate for a team of agent-farms to communicate their status

## Docker service

`docker/` holds the containerized service, a host-side discovery client, and a
build-time freshness check against the `agentic-grange-latest` tag. Start with
`make up` in that directory. See [docker/README.md](docker/README.md).
