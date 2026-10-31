/**
 * GET /v1/schema — the JSON Schema for car.event.v1.
 *
 * Derived at runtime from the frozen zod contract via zod v4's `z.toJSONSchema`
 * (verified against the installed zod 4.4.3), so the served schema can never
 * drift from what ingest actually validates. No hand-maintained literal.
 *
 * `io: "input"` is deliberate: this describes what a *producer* may POST, so
 * fields with contract defaults (severity, session, payload, …) are optional.
 */
import { z } from "zod";
import { CarEvent, CONTRACT_VERSION } from "../contract/events.ts";

export const SCHEMA_ID = "https://car.invalid/schemas/car.event.v1.json";

let cached: Record<string, unknown> | null = null;

export function eventJsonSchema(): Record<string, unknown> {
  if (cached) return cached;
  const derived = z.toJSONSchema(CarEvent, {
    target: "draft-2020-12",
    io: "input",
    unrepresentable: "any",
  }) as Record<string, unknown>;
  cached = {
    ...derived,
    $id: SCHEMA_ID,
    title: CONTRACT_VERSION,
    description:
      "CAR v3 wire contract. POST a single object or an NDJSON/array batch to /v1/events, " +
      "or a vendor payload to /v1/ingest/{agentctl,claude,multica}.",
  };
  return cached;
}

/** Test seam: drop the memoized schema. */
export function resetSchemaCache(): void {
  cached = null;
}
