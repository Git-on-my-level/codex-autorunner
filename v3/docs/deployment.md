# First installation: local, tailnet or isolated hosted

V3 is pre-release and has no existing deployment to migrate. Use an **empty, private
v3 state directory**. CAR rejects unknown/nonempty databases without converting or
deleting them. A previous development snapshot is not a supported deployed schema;
retain anything valuable separately and initialize another empty directory.

## Local setup

From `v3/`, with Bun installed:

```sh
bun install --frozen-lockfile
bun run src/cli.ts init
bun run src/cli.ts serve
```

`init` creates private configuration, a server credentials file and an agent
connection file under `~/.car/`. It refuses to overwrite existing files. For a
separate test workspace, use `init --config /absolute/private-directory/config.toml`
and give `serve` that same `--config` path.

Open `http://127.0.0.1:7171/ui`. Sign in using `CAR_WEB_TOKEN` from the private
credentials file on the server. Give a calling agent **only** its `agent.json`.
Never give an agent `credentials.json`: it contains the human token. The default
client profile is `~/.car/agent.json`; override with `CAR_CONNECTION_FILE`.

No model credential or Telegram bot is required. Complete requests reach the human
without a model. Incomplete requests are returned to the source for bounded context
gathering. The optional preparation reviewer is off by default. See the authoritative
config schema in `src/config/config.ts` before enabling additional capabilities.

## Additional hosts

Run one authoritative server on an always-on host; other machines use outbound HTTP.
They do not need inbound listeners or a triage agent of their own.

```sh
bun run src/cli.ts client add mini \
  --host mac-mini \
  --url https://car.example \
  --output /private/mini-agent.json
```

Restart the server after changing configuration, transfer only the new connection
file to that host, keep it mode 0600, and set `CAR_CONNECTION_FILE`. Use a different
client name/credential for every host. `CAR_URL` and `CAR_AGENT_TOKEN` can explicitly
override a profile. `CAR_SPOOL_DIR` selects its private local spool.

Prefer HTTPS on remote links. A trusted tailnet may explicitly opt into HTTP with
`client add ... --allow-http` or `CAR_ALLOW_HTTP=1`; never make this the default for
arbitrary remote servers. A tailnet is transport, not a replacement for client and
human authentication. Do not accept identity from forwarded headers or packet fields.

For a reverse proxy, configure `[http].public_origin` to the exact externally served
HTTPS origin and preserve it in browser requests. Bind/firewall the internal server
so it is not inadvertently public. Serve CAR at the origin root: client profiles
accept an origin, not a path-prefix URL. Cookies and same-origin writes are checked
against configured origin, not client-supplied forwarded-header claims.

## Disconnections and delivery

`raise` writes a private local spool record before sending. `accepted_locally` means
**no server acceptance or human notification is confirmed**. Keep the original key.
Run `card relay` on the client host for retry, or leave the stdio MCP process alive
(it includes a relay). A powered-off host cannot flush its own queue.

`flush` reports `accepted`, `rejected`, `pending`, and remaining work. Known invalid
requests are preserved in the private `rejected/` directory, not retried forever.
Auth failures pause a batch; repair the connection. A different endpoint or token
uses a different spool audience and cannot silently replay another identity's work.
Credential rotation is therefore explicit: preserve old evidence and reconcile it,
not an automatic claim that pending work moved safely.

`receive` persists an answer on the calling host, then acknowledges exact receipt.
It does not execute the answer. Report `resolved` only when the blocker is gone.
Cancellation/expiry may invalidate a cached answer. Poll current state before use;
CAR cannot undo work already executed outside it.

## Hosted boundary and operational scope

The same API supports an isolated hosted instance with a separate state directory,
credentials and daemon owner for each workspace. This is **not** a shared-database
multi-tenant SaaS implementation. Do not pool tenant work in one database on the
strength of the `workspace_id` field; native events, grants and operations assume a
single workspace. SSO, billing, team roles, HA and federation require separate design.

Use a service supervisor for server/relay restart, access controls for state/backups,
and a SQLite-consistent backup procedure after valuable state exists. The one-daemon
lease is not automatic failover across replicated stores. Keep real credentials,
state, receipts, spools and generated browser evidence out of git. Live tailnet,
proxy, provider, Telegram and external deadman behavior require deployment testing;
see `../VALIDATION.md` for the actual validation of this package.
