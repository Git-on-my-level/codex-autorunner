/** Agent-oriented commands return JSON with explicit next actions. */
import { readSync, openSync, closeSync } from "node:fs";
import { z } from "zod";
import { AcknowledgeRequest, CancelRequest, DecisionPacket, EnrichRequest, RaiseRequest, McpInputs, parseMcpArguments } from "./contract.ts";
import { clientFromEnvironment } from "./client.ts";
import { initialize, addClient } from "./setup.ts";
import { runMcp } from "./mcp.ts";
import { REQUEST_GUIDE } from "./guidance.ts";
const value = (args: string[], flag: string) => {
  const i = args.findIndex((arg) => arg === flag || arg.startsWith(`${flag}=`));
  if (i < 0) return undefined;
  return args[i]!.startsWith(`${flag}=`) ? args[i]!.slice(flag.length + 1) : args[i + 1];
};
type FlagSpec = Record<string, "value" | "boolean">;
function cliError(code: string, message: string): never { throw Object.assign(new Error(message), { code }); }
function validateArgs(args: string[], specs: FlagSpec, usage: string, positional: (value: string, index: number) => boolean = () => false): void {
  const seen = new Set<string>();
  let positionalIndex = 0;
  for (let index = 0; index < args.length; index++) {
    const arg = args[index]!;
    if (!arg.startsWith("--")) {
      if (arg.startsWith("-")) cliError("unknown_option", `Unknown option ${arg}. Usage: ${usage}`);
      if (!positional(arg, positionalIndex)) cliError("unexpected_argument", `Unexpected argument ${arg}. Usage: ${usage}`);
      positionalIndex++;
      continue;
    }
    const equals = arg.indexOf("=");
    const flag = equals < 0 ? arg : arg.slice(0, equals);
    const spec = specs[flag];
    if (!spec) cliError("unknown_option", `Unknown option ${flag}. Usage: ${usage}`);
    if (seen.has(flag)) cliError("duplicate_option", `${flag} may be supplied only once`);
    seen.add(flag);
    if (spec === "boolean") {
      if (equals >= 0) cliError("invalid_option", `${flag} does not take a value`);
      continue;
    }
    const inline = equals >= 0 ? arg.slice(equals + 1) : undefined;
    const next = args[index + 1];
    if (inline === "" || (inline === undefined && (!next || next.startsWith("--")))) cliError("missing_option_value", `${flag} requires a value`);
    if (inline === undefined) index++;
  }
}
function required(args: string[], flag: string): string {
  const result = value(args, flag);
  if (!result || result.startsWith("--")) cliError("missing_option_value", `${flag} requires a value`);
  return result;
}
function readInput(args: string[]): unknown {
  const file = value(args, "--file");
  if (file === undefined) cliError("missing_option_value", "--file is required for packet JSON; use --file - to read stdin explicitly");
  let fd = 0;
  if (file !== "-") {
    try { fd = openSync(file, "r"); }
    catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") cliError("input_file_missing", `Input file not found: ${file}`);
      throw error;
    }
  }
  const buffer = Buffer.alloc(64 * 1024 + 1); let size = 0;
  try { for (;;) { const count = readSync(fd, buffer, size, buffer.length - size, null); if (!count) break; size += count; if (size === buffer.length) throw new Error("JSON input exceeds 64 KiB"); } }
  finally { if (fd !== 0) closeSync(fd); }
  const input = buffer.subarray(0, size).toString("utf8");
  try { return JSON.parse(input); }
  catch { cliError("invalid_json", `Could not parse packet JSON from ${file === "-" ? "stdin" : file}`); }
}
function packetInput(args: string[]): DecisionPacket {
  const raw = readInput(args);
  if (raw && typeof raw === "object" && !Array.isArray(raw) &&
      ("idempotency_key" in raw || ("contract" in raw && (raw as { contract?: unknown }).contract === "car.request.v1"))) {
    cliError("invalid_packet", "--file must contain the packet body only (goal, blocker, question, etc.), not a request envelope; pass the stable idempotency key separately with --key");
  }
  try { return DecisionPacket.parse(raw); }
  catch { cliError("invalid_packet", "Packet JSON is invalid. Run card schema to inspect the packet shape; --file accepts the packet body only"); }
}
export const ATTENTION_HELP = `CAR: grounded decisions, not another agent harness.

Operator setup (never exposed as MCP tools):
  card init [--config /path/config.toml]
  card client add NAME --host HOST --url https://car.example --output connection.json [--config PATH]
  card serve [--config PATH]

Agent commands (CAR_CONNECTION_FILE, or CAR_URL + CAR_AGENT_TOKEN):
  card raise --key STABLE_KEY --file packet.json      Packet body only (goal, blocker, question); not a request envelope
  card request list [--before CURSOR]                Only this client's requests
  card request get ID                                Inspect state; GET is not receipt
  card request doctor                                Check this client without raising a request
  card request context ID --revision N --file packet.json  Replace preparation context (packet body only)
  card wait ID [--timeout 60]                         Poll for 0–3600 seconds; no automatic acknowledgement
  card request receive ID                            Persist answer locally, then acknowledge receipt
  card request ack ID --answer ANSWER_ID --outcome resolved [--note TEXT]
  card request cancel ID --revision N --reason TEXT
  card flush                                         Replay this identity's durable pending requests
  card relay                                         Outbound-only retry loop; no LLM
  card mcp                                           MCP stdio; stdout is protocol only
  card schema                                        Full request schema and machine-readable guidance

Options are strict: unknown or duplicate flags fail before any network call. Check the
process exit status before parsing JSON or retrying; a printed error is not success.

A decision packet needs goal, blocker and question. CAR asks the agent for missing
why_human, facts with sources, attempts, recommendation, and impact. Use
cannot_investigate to explain limits, never fabricate evidence. Urgent requests
surface immediately; normal preparation is bounded. A submitted request is NOT
approval. A recorded answer is NOT delivery. Receipt is NOT resolution.
`;
export const ATTENTION_COMMANDS = new Set(["init", "client", "raise", "request", "wait", "flush", "relay", "mcp", "schema"]);
export async function runAttentionCli(command: string, args: string[]): Promise<void> {
  const print = (result: unknown) => console.log(JSON.stringify(result, null, 2));
  if (args.includes("--help")) { console.log(ATTENTION_HELP); return; }
  if (command === "init") {
    validateArgs(args, { "--config": "value" }, "card init [--config PATH]");
    print(initialize(value(args, "--config"))); return;
  }
  if (command === "client") {
    validateArgs(args, { "--host": "value", "--url": "value", "--output": "value", "--config": "value", "--allow-http": "boolean" }, "card client add NAME --host HOST --url ORIGIN --output FILE [--config PATH]", (_arg, index) => index < 2);
    if (args[0] !== "add" || !args[1]) throw new Error("Use card client add NAME --host HOST --url ORIGIN --output FILE");
    print(addClient({ name: args[1], host: required(args, "--host"), url: required(args, "--url"), output: required(args, "--output"), configPath: value(args, "--config"), allowHttp: args.includes("--allow-http") })); return;
  }
  if (command === "schema") {
    validateArgs(args, {}, "card schema");
    print({ packet: z.toJSONSchema(DecisionPacket, { io: "input" }), cli_file: "raise --file and request context --file accept the packet schema only, not the request envelope. Pass the raise idempotency key separately with --key.", request: z.toJSONSchema(RaiseRequest, { io: "input" }), tools: Object.fromEntries(Object.entries(McpInputs).map(([name, schema]) => [name, z.toJSONSchema(schema, { io: "input" })])), instructions: ATTENTION_HELP, guide: REQUEST_GUIDE }); return;
  }
  if (command === "raise") validateArgs(args, { "--key": "value", "--file": "value" }, "card raise --key STABLE_KEY --file packet.json");
  if (command === "wait") validateArgs(args, { "--timeout": "value" }, "card wait ID [--timeout SECONDS]", (_arg, index) => index === 0);
  if (command === "flush" || command === "relay" || command === "mcp") validateArgs(args, {}, `card ${command}`);
  // Parse local raise inputs before opening the connection so a malformed packet
  // or missing key cannot be obscured by an unrelated credential error.
  const raiseInput = command === "raise" ? { key: required(args, "--key"), packet: packetInput(args) } : undefined;
  const client = clientFromEnvironment();
  if (command === "mcp") { await runMcp(client, { raise: z.toJSONSchema(RaiseRequest), context: z.toJSONSchema(EnrichRequest), ack: z.toJSONSchema(AcknowledgeRequest), cancel: z.toJSONSchema(CancelRequest) }, parseMcpArguments); return; }
  if (command === "raise") { print(await client.raise(raiseInput!.key, raiseInput!.packet)); return; }
  if (command === "flush") { print(await client.flush()); return; }
  if (command === "wait") { if (!args[0]) throw new Error("request id required"); print(await client.wait(args[0], Number(value(args, "--timeout") ?? "60"))); return; }
  if (command === "relay") {
    let stop = false; const shutdown = () => { stop = true; };
    process.once("SIGINT", shutdown); process.once("SIGTERM", shutdown);
    try { while (!stop) { const result = await client.flush(); if (result.accepted.length || result.pending.length || result.rejected.length) print(result); for (let n=0; n<10 && !stop; n++) await new Promise((resolve) => setTimeout(resolve, 1000)); } }
    finally { process.removeListener("SIGINT", shutdown); process.removeListener("SIGTERM", shutdown); }
    return;
  }
  const [verb, id] = args;
  if (verb === "list") { validateArgs(args.slice(1), { "--before": "value" }, "card request list [--before CURSOR]"); print(await client.list(value(args, "--before"))); return; }
  if (verb === "doctor") { validateArgs(args.slice(1), {}, "card request doctor"); const result = await client.doctor(); print(result); if (!result.ready) process.exitCode = 1; return; }
  if (!id || id.startsWith("--")) throw new Error("Use a request id returned by CAR");
  if (verb === "get") { validateArgs(args.slice(1), {}, "card request get ID", (_arg, index) => index === 0); print(await client.get(id)); }
  else if (verb === "receive") { validateArgs(args.slice(1), {}, "card request receive ID", (_arg, index) => index === 0); print(await client.receive(id)); }
  else if (verb === "context") {
    validateArgs(args.slice(1), { "--revision": "value", "--file": "value" }, "card request context ID --revision N --file packet.json", (_arg, index) => index === 0);
    const input = EnrichRequest.parse({ expected_revision: Number(required(args, "--revision")), packet: packetInput(args) });
    print(await client.context(id, input.expected_revision, input.packet));
  } else if (verb === "ack") {
    validateArgs(args.slice(1), { "--answer": "value", "--outcome": "value", "--note": "value" }, "card request ack ID --answer ANSWER_ID --outcome resolved [--note TEXT]", (_arg, index) => index === 0);
    const input = AcknowledgeRequest.parse({ answer_id: required(args, "--answer"), outcome: required(args, "--outcome"), ...(value(args, "--note") ? { note: value(args, "--note") } : {}) });
    if (input.outcome !== "resolved") throw new Error("Use card request receive ID to durably save the answer before acknowledging receipt");
    print(await client.acknowledge(id, input.answer_id, input.outcome, input.note));
  } else if (verb === "cancel") {
    validateArgs(args.slice(1), { "--revision": "value", "--reason": "value" }, "card request cancel ID --revision N --reason TEXT", (_arg, index) => index === 0);
    const input = CancelRequest.parse({ expected_revision: Number(required(args, "--revision")), reason: required(args, "--reason") });
    print(await client.cancel(id, input.expected_revision, input.reason));
  } else throw new Error("Unknown request command; use card request --help");
}
