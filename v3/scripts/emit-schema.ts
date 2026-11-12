#!/usr/bin/env bun
/**
 * Print the JSON Schema for car.event.v1, derived from the zod contract.
 *
 * The daemon serves the same document at GET /v1/schema; this script exists so
 * an adapter author can generate types without a running daemon:
 *
 *   bun run emit-schema > car.event.v1.schema.json
 */
import { eventJsonSchema } from "../src/ingest/schema.ts";

console.log(JSON.stringify(eventJsonSchema(), null, 2));
