/**
 * Body decoding for POST /v1/events: one JSON object, a JSON array, or an
 * NDJSON batch.
 *
 * The scaffold sniffed a batch with `body.startsWith("{") && body.includes("\n")`,
 * which mis-splits any pretty-printed single object. Order is inverted here:
 * whole-body JSON is tried first (unambiguous), and NDJSON is the fallback.
 * That makes `curl -d @event.json` work regardless of formatting.
 */

/** Hard caps so a runaway producer cannot exhaust the daemon. */
export const MAX_BODY_BYTES = 8 * 1024 * 1024;
export const MAX_BATCH_ITEMS = 1000;

export type DecodedBody =
  | { mode: "single"; items: unknown[] }
  | { mode: "batch"; items: (unknown | BatchLineError)[] };

export class BatchLineError {
  constructor(
    readonly index: number,
    readonly detail: string,
  ) {}
}

export class BodyError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "BodyError";
  }
}

export function decodeBody(raw: string, contentType: string | undefined): DecodedBody {
  if (raw.length > MAX_BODY_BYTES) {
    throw new BodyError("body_too_large", `body exceeds ${MAX_BODY_BYTES} bytes`);
  }

  // Strip a UTF-8 BOM; some Windows/PowerShell producers emit one.
  const text = raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw;
  if (text.trim().length === 0) throw new BodyError("empty_body", "request body is empty");

  const ndjsonDeclared = /ndjson|jsonlines|jsonl/i.test(contentType ?? "");

  if (!ndjsonDeclared) {
    const whole = tryParse(text);
    if (whole.ok) {
      if (Array.isArray(whole.value)) return { mode: "batch", items: capped(whole.value) };
      return { mode: "single", items: [whole.value] };
    }
  }

  return { mode: "batch", items: capped(parseNdjson(text)) };
}

function parseNdjson(text: string): (unknown | BatchLineError)[] {
  const out: (unknown | BatchLineError)[] = [];
  const lines = text.split("\n");
  let index = 0;
  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, "").trim();
    if (line.length === 0) continue;
    const parsed = tryParse(line);
    out.push(parsed.ok ? parsed.value : new BatchLineError(index, parsed.detail));
    index++;
  }
  if (out.length === 0) throw new BodyError("empty_body", "no JSON lines found in body");
  return out;
}

function capped(items: unknown[]): unknown[] {
  if (items.length > MAX_BATCH_ITEMS) {
    throw new BodyError("batch_too_large", `batch exceeds ${MAX_BATCH_ITEMS} items`);
  }
  return items;
}

function tryParse(text: string): { ok: true; value: unknown } | { ok: false; detail: string } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, detail: err instanceof Error ? err.message : String(err) };
  }
}
