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
