/**
 * ULID generation (Crockford base32, 48-bit time + 80-bit randomness),
 * monotonic within a process. No dependency; FROZEN with the contract.
 */
const ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

let lastTime = -1;
let lastRandom: number[] = [];

function encodeTime(time: number): string {
  let out = "";
  for (let i = 9; i >= 0; i--) {
    out = ENCODING[time % 32] + out;
    time = Math.floor(time / 32);
  }
  return out;
}

export function ulid(now: number = Date.now()): string {
  let random: number[];
  if (now === lastTime) {
    // increment previous randomness for monotonicity
    random = [...lastRandom];
    for (let i = random.length - 1; i >= 0; i--) {
      const value = random[i]!;
      if (value < 31) {
        random[i] = value + 1;
        break;
      }
      random[i] = 0;
    }
  } else {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    random = Array.from(bytes.slice(0, 16), (b) => b % 32);
  }
  lastTime = now;
  lastRandom = random;
  return encodeTime(now) + random.map((v) => ENCODING[v]).join("");
}

export function eventId(): string {
  return `evt_${ulid()}`;
}
export function sessionId(): string {
  return `sess_${ulid()}`;
}
export function incidentId(): string {
  return `inc_${ulid()}`;
}
export function escalationId(): string {
  return `esc_${ulid()}`;
}
export function decisionId(): string {
  return `dec_${ulid()}`;
}
export function actionId(): string {
  return `act_${ulid()}`;
}
export function memoryId(): string {
  return `mem_${ulid()}`;
}
export function outcomeId(): string {
  return `out_${ulid()}`;
}

/** Stable identities for v3 durable capability and effect records. */
export function intentId(prefix = "intent"): string {
  return `${prefix}_${ulid()}`;
}
export function effectId(): string {
  return `eff_${ulid()}`;
}
export function providerInvocationId(): string {
  return `pinv_${ulid()}`;
}
export function interactionId(): string {
  return `interaction_${ulid()}`;
}
export function humanFactId(): string {
  return `fact_${ulid()}`;
}
export function grantId(): string {
  return `grant_${ulid()}`;
}

/**
 * Deterministic JSON used as the payload of idempotency reservations. This is
 * deliberately kept in the contract layer so every producer hashes the same
 * semantic object rather than relying on insertion order.
 */
export function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`)
    .join(",")}}`;
}

export function payloadSha256(value: unknown): string {
  return new Bun.CryptoHasher("sha256").update(stableJson(value)).digest("hex");
}
