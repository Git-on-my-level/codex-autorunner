/** The agent-facing contract. Structure is enforced; factual truth is not inferred. */
import { z } from "zod";

const text = (max: number) => z.string().trim().min(1).max(max);
export const DecisionPacket = z.object({
  goal: text(2_000).describe("The outcome you are trying to achieve."),
  blocker: text(2_000).describe("Why you cannot proceed within existing authority."),
  question: text(500).describe("The specific judgment the human needs to make."),
  project: text(200).optional().describe("Display label only, not an authorization scope."),
  why_human: text(2_000).optional().describe("Why existing instructions or authority do not answer this."),
  attempts: z.array(text(1_000)).max(12).default([]),
  facts: z.array(z.object({ statement: text(2_000), source: text(1_000).optional() }).strict()).max(12).default([]),
  recommendation: z.object({ answer: text(2_000), rationale: text(2_000) }).strict().optional(),
  options: z.array(z.object({
    id: z.string().regex(/^[a-zA-Z0-9_-]{1,48}$/),
    label: text(120),
    answer: text(2_000),
    consequences: text(2_000),
  }).strict()).max(6).default([]),
  uncertainty: z.array(text(1_000)).max(8).default([]),
  cannot_investigate: text(2_000).optional().describe("Explain missing access/time; do not invent evidence or a recommendation."),
  impact: text(2_000).optional().describe("Work blocked, cost of waiting, and who else is affected."),
  urgency: z.enum(["normal", "urgent"]).default("normal"),
  deadline_at: z.iso.datetime({ offset: true }).optional(),
}).strict().refine((p) => new Set(p.options.map((o) => o.id)).size === p.options.length, {
  message: "Option ids must be unique", path: ["options"],
});
export type DecisionPacket = z.infer<typeof DecisionPacket>;
export const RaiseRequest = z.object({
  contract: z.literal("car.request.v1").default("car.request.v1"),
  idempotency_key: text(256),
  packet: DecisionPacket,
}).strict();
export const EnrichRequest = z.object({
  expected_revision: z.number().int().positive(), packet: DecisionPacket,
}).strict();
export const AcknowledgeRequest = z.object({
  answer_id: text(100),
  outcome: z.enum(["received", "resolved"]),
  note: text(2_000).optional(),
}).strict();
export const CancelRequest = z.object({
  expected_revision: z.number().int().positive(), reason: text(2_000),
}).strict();
export const HumanAnswer = z.object({
  expected_revision: z.number().int().positive(),
  option_id: text(48).optional(), text: text(8_000).optional(),
}).strict().refine((v) => Boolean(v.option_id) !== Boolean(v.text), "Provide one option_id or one text answer");

export interface ClientIdentity { workspaceId: string; clientId: string; host: string }
export type RequestState = "preparing" | "needs_you" | "answered" | "received" | "resolved" | "cancelled" | "expired";
export interface RequestRow {
  id: string; workspace_id: string; client_id: string; host: string;
  idempotency_key: string; initial_hash: string; packet_json: string;
  revision: number; state: RequestState; preparation_rounds: number;
  prepare_by: string; due_at: string | null;
  event_id: string | null; incident_id: string | null; escalation_id: string | null;
  created_at: string; updated_at: string; last_seen_at: string;
  closed_at: string | null; close_reason: string | null; reviewed_at: string | null; review_note: string | null;
}

/** Standard agent tools have one receipt path: receive persists locally first. */
const RequestId = z.string().regex(/^[a-zA-Z0-9_-]{1,100}$/);
export const McpInputs = {
  car_raise: RaiseRequest,
  car_get: z.object({ id: RequestId }).strict(),
  car_list: z.object({ before: text(1_000).optional() }).strict(),
  car_context: EnrichRequest.extend({ id: RequestId }),
  car_receive: z.object({ id: RequestId }).strict(),
  car_ack: AcknowledgeRequest.extend({ id: RequestId, outcome: z.literal("resolved") }),
  car_cancel: CancelRequest.extend({ id: RequestId }),
  car_flush: z.object({}).strict(),
  car_guide: z.object({}).strict(),
} as const;
export function parseMcpArguments(name: string, args: unknown): Record<string, unknown> {
  const schema = McpInputs[name as keyof typeof McpInputs];
  if (!schema) throw new Error("Unknown CAR tool");
  return schema.parse(args) as Record<string, unknown>;
}
