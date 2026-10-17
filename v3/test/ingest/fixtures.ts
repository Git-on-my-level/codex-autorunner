/**
 * Golden-fixture loader.
 *
 * A fixture is a self-describing JSON file: the vendor payload plus the exact
 * canonical projection it must normalize to. Keeping the expectation next to the
 * input is the point — a normalizer change that alters the wire meaning of a
 * source has to edit the golden file, in the diff, on purpose.
 */
import { readdirSync } from "node:fs";
import { join } from "node:path";
import type { CarEvent } from "../../src/contract/events.ts";
import { sessionKey } from "../../src/contract/events.ts";

export const FIXTURE_ROOT = join(import.meta.dir, "..", "fixtures");

/** Fixed identity used by fixtures so golden session keys never embed a real hostname. */
export const FIXTURE_HOST = "test-host";
/** Matches FakeClock's default instant, so hash+minute-bucket keys are stable. */
export const FIXTURE_NOW = new Date("2026-08-26T12:00:00Z");

export interface ExpectedEvent {
  type: string;
  severity: string;
  requires_response: boolean;
  idempotency_key: string;
  session_key: string | null;
  response_channel: unknown;
  ts?: string;
  source?: { vendor: string; host: string; adapter: string };
  title?: string;
  body?: string;
  payload?: Record<string, unknown>;
  expires_at?: string;
}

export interface Fixture {
  file: string;
  name: string;
  note?: string;
  input: unknown;
  expect: ExpectedEvent[];
  expect_park?: boolean;
}

export function loadFixtures(source: string): Fixture[] {
  const dir = join(FIXTURE_ROOT, source);
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .sort();
  if (files.length === 0) throw new Error(`no fixtures in ${dir}`);
  return files.map((file) => {
    const parsed = JSON.parse(
      require("node:fs").readFileSync(join(dir, file), "utf8") as string,
    ) as Omit<Fixture, "file">;
    return { ...parsed, file };
  });
}

/** Project a canonical event onto the fields a golden fixture asserts. */
export function project(event: CarEvent, expected: ExpectedEvent): Record<string, unknown> {
  const actual: Record<string, unknown> = {
    type: event.type,
    severity: event.severity,
    requires_response: event.requires_response,
    idempotency_key: event.idempotency_key,
    session_key: event.session ? sessionKey(event.session) : null,
    response_channel: event.response_channel,
  };
  // Optional assertions are opt-in per fixture: only compare what it declares.
  if (expected.ts !== undefined) actual["ts"] = event.ts;
  if (expected.source !== undefined) actual["source"] = event.source;
  if (expected.title !== undefined) actual["title"] = event.title;
  if (expected.body !== undefined) actual["body"] = event.body;
  if (expected.payload !== undefined) actual["payload"] = event.payload;
  if (expected.expires_at !== undefined) actual["expires_at"] = event.expires_at;
  return actual;
}

/** The subset of the fixture that `project` produced a counterpart for. */
export function expectation(expected: ExpectedEvent): Record<string, unknown> {
  const { ...rest } = expected;
  return rest as Record<string, unknown>;
}
