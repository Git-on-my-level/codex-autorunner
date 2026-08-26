# Multica integration

Multica autopilots/cards talk to CAR in both directions: a webhook feeds card
lifecycle/attention events into CAR's ingest, and CAR's `multica-api` reply-back
adapter comments/approves on the card via Multica's self-hosted REST API so replies
and approvals reach the autopilot (DESIGN.md §2, §8).

## 1. Point Multica's webhook at CAR

Configure the webhook target on your self-hosted Multica instance to POST card events
to:

```
http://127.0.0.1:7171/v1/ingest/multica
```

If Multica runs on a different host than CAR (i.e. not `127.0.0.1`), CAR requires a
bearer token for non-localhost ingest (DESIGN.md §2, "Auth"). Set a token for the
`multica` source in `~/.car/config.toml`:

```toml
[http.ingest_tokens]
multica = "same-secret-multica-sends-as-a-bearer-token"
```

...and configure Multica's webhook to send `Authorization: Bearer <that secret>`.

## 2. Ingest side

CAR's `/v1/ingest/multica` normalizer maps a Multica card-event payload onto
`car.event.v1`:
- `session.vendor = "multica"`, `session.native_id` ← the card/autopilot id.
- `idempotency_key` includes the card id + event sequence/timestamp so Multica's
  at-least-once webhook delivery is safe to retry.
- Card opened/started → `session.started`; card closed/completed → `session.ended`.
- Card asks a question or needs approval → `attention.question` /
  `attention.permission`, `requires_response: true`,
  `response_channel: {"kind": "multica-api", "hint": {"card_id": "..."}}`.
- Autopilot silent past its expected cadence surfaces separately, via CAR's own
  silence watchdog (DESIGN.md §4) rather than anything Multica sends — that's what
  catches a stuck autopilot Multica itself doesn't know is stuck.

## 3. Reply-back: env vars for `CAR_MULTICA_URL` / `CAR_MULTICA_TOKEN`

The `multica-api` adapter (DESIGN.md §8) is how CAR answers *back* into a card — a ✅
Approve tap or a typed reply in Telegram becomes a comment/approve call against
Multica's REST API. Set these in the same environment `card serve` runs under (see
`../ops/README.md` for launchd/systemd `EnvironmentVariables`):

```bash
export CAR_MULTICA_URL="https://multica.internal.example.com"
export CAR_MULTICA_TOKEN="mtc_xxx..."   # Multica API token with comment/approve scope
```

- `CAR_MULTICA_URL` — base URL of your self-hosted Multica instance's REST API.
- `CAR_MULTICA_TOKEN` — bearer token used when CAR calls back into Multica (comment on
  a card, approve/deny a pending request). Scope it to comment/approve only if
  Multica's token model supports scoping — CAR does not need broader access.

Like every reply-back adapter, `multica-api` is capability-probed at runtime (not
assumed from docs — DESIGN.md §2) and every delivery attempt is audited
(`audit` table, verb `reply.*`); a failed delivery re-escalates rather than silently
dropping (DESIGN.md non-negotiable #6).

## 4. If you don't have `CAR_MULTICA_URL`/`CAR_MULTICA_TOKEN` set

Ingest still works — events flow in and show up in the inbox/digest — but any reply
CAR would send back through `multica-api` instead falls through to the universal file
fallback (`~/.car/replies/<car_session_id>/reply-NNNN.md` — see
[`replies-file-contract.md`](./replies-file-contract.md)), and CAR says so honestly in
the Telegram thread ("reply staged, not delivered — no Multica API configured")
rather than claiming a delivery that didn't happen (DESIGN.md non-negotiable #6, §8).
