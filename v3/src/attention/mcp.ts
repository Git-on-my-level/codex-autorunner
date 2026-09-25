/** Minimal stdio MCP adapter over the same client. It owns no decisions or authority. */
import { REQUEST_GUIDE } from "./guidance.ts";
import { StringDecoder } from "node:string_decoder";
import type { AttentionClient } from "./client.ts";
import type { DecisionPacket } from "./contract.ts";

export interface ToolSchemas { raise: Record<string, unknown>; context: Record<string, unknown>; ack: Record<string, unknown>; cancel: Record<string, unknown> }
type RpcId = string | number;
const requestIdSchema = { type: "string", pattern: "^[a-zA-Z0-9_-]{1,100}$", description: "Use the durable request id returned by CAR." };
const objectSchema = (properties: Record<string, unknown>, required: string[]) => ({ type: "object", properties, required, additionalProperties: false });
const withId = (schema: Record<string, any>) => ({ ...schema, properties: { id: requestIdSchema, ...schema.properties }, required: [...new Set(["id", ...(schema.required ?? [])])] });
export type ParseArguments = (tool: string, args: unknown) => Record<string, unknown>;
export function createMcpHandler(client: AttentionClient, schemas: ToolSchemas, parseArguments: ParseArguments) {
  let initialized = false; let ready = false;
  const active = new Set<RpcId>(); const cancelled = new Set<RpcId>();
  const tools = [
    { name: "car_raise", description: "Raise one grounded human decision request. Supply a stable idempotency_key; use the returned context_requests before interrupting the human. Offline acceptance is explicitly local, never server acceptance.", inputSchema: schemas.raise },
    { name: "car_get", description: "Read your request and the exact next action. A GET is not receipt, approval, or resolution.", inputSchema: objectSchema({ id: requestIdSchema }, ["id"]), annotations: { readOnlyHint: true } },
    { name: "car_list", description: "List only this authenticated client's own requests.", inputSchema: objectSchema({ before: { type: "string" } }, []), annotations: { readOnlyHint: true } },
    { name: "car_context", description: "Add requested investigation to a preparing packet using expected_revision. Do not fabricate evidence. A surfaced packet is frozen; cancel and replace changed questions.", inputSchema: withId(schemas.context) },
    { name: "car_receive", description: "Fetch an answer, persist it on this host, then acknowledge receipt. This does not execute the answer or claim work is unblocked.", inputSchema: objectSchema({ id: requestIdSchema }, ["id"]) },
    { name: "car_ack", description: "Report resolved for the exact answer_id only after receive saved it and the actual blocker is gone. This is not a shortcut for receipt or standing permission.", inputSchema: { ...withId(schemas.ack), properties: { ...withId(schemas.ack).properties, outcome: { const: "resolved", type: "string" } } } },
    { name: "car_cancel", description: "Cancel your own obsolete request using its current revision and a reason. An old answer must not then be applied.", inputSchema: withId(schemas.cancel), annotations: { destructiveHint: true } },
    { name: "car_guide", description: "Read CAR's workflow and authority boundaries. Follow the structured guidance in each request rather than guessing the next action.", inputSchema: objectSchema({}, []), annotations: { readOnlyHint: true } },
    { name: "car_flush", description: "Retry this host's durable pending requests with their original identities. Inspect pending errors; do not substitute new request ids.", inputSchema: objectSchema({}, []) },
  ];
  return async (input: unknown): Promise<unknown | null> => {
    if (!input || typeof input !== "object" || Array.isArray(input)) return { jsonrpc: "2.0", id: null, error: { code: -32600, message: "Invalid request" } };
    const msg = input as { jsonrpc?: string; id?: unknown; method?: string; params?: Record<string, any> };
    const hasId = Object.prototype.hasOwnProperty.call(msg, "id");
    const id = (typeof msg.id === "string" || typeof msg.id === "number") ? msg.id as RpcId : null;
    const fail = (code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });
    if (msg.jsonrpc !== "2.0" || typeof msg.method !== "string" || (hasId && id === null)) return fail(-32600, "Invalid JSON-RPC request");
    if (!hasId) {
      if (msg.method === "notifications/initialized" && initialized) ready = true;
      if (msg.method === "notifications/cancelled") {
        const target = msg.params?.requestId;
        if ((typeof target === "string" || typeof target === "number") && active.has(target)) cancelled.add(target);
      }
      return null;
    }
    let result: unknown;
    if (msg.method === "initialize") {
      if (initialized) return fail(-32600, "Already initialized");
      if (typeof msg.params?.protocolVersion !== "string") return fail(-32602, "protocolVersion is required");
      initialized = true;
      // These stable tools require no newer optional capability. Negotiate a
      // supported version, rather than claiming untested future protocol support.
      const versions = ["2025-11-25", "2025-06-18"];
      result = { protocolVersion: versions.includes(msg.params.protocolVersion) ? msg.params.protocolVersion : versions[0],
        capabilities: { tools: {} }, serverInfo: { name: "car-attention", version: "3.0.0-alpha.2" },
        instructions: "Raise grounded decisions. Follow context_requests; use cannot_investigate rather than inventing evidence. Silence is never approval. Persist/receive answers before acting; acknowledge resolved only when unblocked. CAR does not own your work or grant new authority through memory." };
    } else if (msg.method === "ping") result = {};
    else if (!ready) return fail(-32002, "Initialize and send notifications/initialized first");
    else if (msg.method === "tools/list") result = { tools };
    else if (msg.method === "tools/call") {
      const name = msg.params?.name; const rawArgs = msg.params?.arguments ?? {};
      if (!tools.some((tool) => tool.name === name)) return fail(-32602, "Unknown tool");
      if (!rawArgs || typeof rawArgs !== "object" || Array.isArray(rawArgs)) return fail(-32602, "arguments must be an object");
      if (active.has(id!)) return fail(-32600, "Request id is already in flight");
      active.add(id!);
      try {
        // Required parser: malformed/offline packets must never receive local acceptance.
        const args = parseArguments(name, rawArgs) as Record<string, any>;
        let value: unknown;
        switch (name) {
          case "car_raise": value = await client.raise(args.idempotency_key, args.packet as DecisionPacket); break;
          case "car_get": value = await client.get(args.id); break;
          case "car_list": value = await client.list(args.before); break;
          case "car_context": value = await client.context(args.id, args.expected_revision, args.packet); break;
          case "car_receive": value = await client.receive(args.id); break;
          case "car_ack": value = await client.acknowledge(args.id, args.answer_id, args.outcome, args.note); break;
          case "car_cancel": value = await client.cancel(args.id, args.expected_revision, args.reason); break;
          case "car_guide": value = REQUEST_GUIDE; break;
          case "car_flush": value = await client.flush(); break;
        }
        result = { content: [{ type: "text", text: JSON.stringify(value) }], structuredContent: value, isError: false };
      } catch (error) {
        result = { content: [{ type: "text", text: JSON.stringify({ error: (error as { code?: string }).code ?? "client_error", message: (error instanceof Error ? error.message : String(error)).slice(0, 4_000) }) }], isError: true };
      } finally { active.delete(id!); }
      if (cancelled.delete(id!)) return null;
    } else return fail(-32601, "Method not found");
    return { jsonrpc: "2.0", id, result };
  };
}
/** Newline-framed stdio, bounded concurrency, cancellation without pretending to undo remote writes. */
export async function runMcp(client: AttentionClient, schemas: ToolSchemas, parseArguments: ParseArguments): Promise<void> {
  const handle = createMcpHandler(client, schemas, parseArguments);
  const decoder = new StringDecoder("utf8"); let buffer = "";
  let flushRun: Promise<unknown> | null = null;
  const inFlight = new Set<Promise<void>>();
  const write = (message: unknown) => { process.stdout.write(JSON.stringify(message) + "\n"); };
  const dispatch = (parsed: unknown) => {
    const request = parsed as { id?: unknown; method?: string } | null;
    const notification = request && !Object.hasOwn(request, "id") && typeof request.method === "string";
    if (inFlight.size >= 16 && !notification) {
      write({ jsonrpc: "2.0", id: request?.id ?? null, error: { code: -32000, message: "CAR has 16 requests in flight. Retry after reading the current request state." } });
      return;
    }
    const work = handle(parsed).then((response) => { if (response !== null) write(response); })
      .catch((error) => { process.stderr.write(`CAR MCP: ${String(error)}\n`); })
      .finally(() => { inFlight.delete(work); });
    inFlight.add(work);
  };
  const timer = setInterval(() => {
    if (flushRun) return;
    flushRun = client.flush().catch((e) => process.stderr.write(`CAR relay: ${String(e)}\n`)).finally(() => { flushRun = null; });
  }, 10_000);
  try {
    for await (const chunk of process.stdin) {
      buffer += decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end).trim(); buffer = buffer.slice(end + 1);
        if (Buffer.byteLength(line) > 256 * 1024) throw new Error("MCP message exceeds 256 KiB");
        if (!line) continue;
        let parsed: unknown;
        try { parsed = JSON.parse(line); }
        catch { write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }); continue; }
        dispatch(parsed);
      }
      if (Buffer.byteLength(buffer) > 256 * 1024) throw new Error("MCP message exceeds 256 KiB");
    }
    // A truncated frame is not a request. Never execute an unterminated write.
    if (buffer.trim()) process.stderr.write("CAR MCP: ignored incomplete frame at EOF\n");
  } finally {
    clearInterval(timer);
    await Promise.allSettled([...inFlight]);
    if (flushRun) await flushRun;
  }
}
