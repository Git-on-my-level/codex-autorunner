/** Agent-oriented commands return JSON with explicit next actions. */
import { readSync, openSync, closeSync } from "node:fs";
import { z } from "zod";
import { AcknowledgeRequest, CancelRequest, DecisionPacket, EnrichRequest, RaiseRequest, McpInputs, parseMcpArguments } from "./contract.ts";
import { clientFromEnvironment } from "./client.ts";
import { initialize, addClient } from "./setup.ts";
import { runMcp } from "./mcp.ts";
const value = (args: string[], flag: string) => { const i = args.indexOf(flag); return i < 0 ? undefined : args[i + 1]; };
function required(args: string[], flag: string): string { const result = value(args, flag); if (!result || result.startsWith("--")) throw new Error(`${flag} is required`); return result; }
function readInput(args: string[]): unknown {
  const file = value(args, "--file");
  const fd = !file || file === "-" ? 0 : openSync(file, "r");
  const buffer = Buffer.alloc(64 * 1024 + 1); let size = 0;
  try { for (;;) { const count = readSync(fd, buffer, size, buffer.length - size, null); if (!count) break; size += count; if (size === buffer.length) throw new Error("JSON input exceeds 64 KiB"); } }
  finally { if (fd !== 0) closeSync(fd); }
  const input = buffer.subarray(0, size).toString("utf8");
  return JSON.parse(input);
}
export const ATTENTION_HELP = `CAR: grounded decisions, not another agent harness.

Operator setup (never exposed as MCP tools):
  card init [--config /path/config.toml]
  card client add NAME --host HOST --url https://car.example --output connection.json [--config PATH]
  card serve [--config PATH]

Agent commands (CAR_CONNECTION_FILE, or CAR_URL + CAR_AGENT_TOKEN):
  card raise --key STABLE_KEY --file packet.json      Register once; follow context_requests
  card request list [--before CURSOR]                Only this client's requests
  card request get ID                                Inspect state; GET is not receipt
  card request context ID --revision N --file packet.json
  card wait ID [--timeout 60]                         Bounded poll; no automatic acknowledgement
  card request receive ID                            Persist answer locally, then acknowledge receipt
  card request ack ID --answer ANSWER_ID --outcome resolved [--note TEXT]
  card request cancel ID --revision N --reason TEXT
  card flush                                         Replay this identity's durable pending requests
  card relay                                         Outbound-only retry loop; no LLM
  card mcp                                           MCP stdio; stdout is protocol only
  card schema                                        Full request schema and machine-readable guidance

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
  if (command === "init") { print(initialize(value(args, "--config"))); return; }
  if (command === "client") {
    if (args[0] !== "add" || !args[1]) throw new Error("Use card client add NAME --host HOST --url ORIGIN --output FILE");
    print(addClient({ name: args[1], host: required(args, "--host"), url: required(args, "--url"), output: required(args, "--output"), configPath: value(args, "--config"), allowHttp: args.includes("--allow-http") })); return;
  }
  if (command === "schema") { print({ request: z.toJSONSchema(RaiseRequest), tools: Object.fromEntries(Object.entries(McpInputs).map(([name, schema]) => [name, z.toJSONSchema(schema)])), instructions: ATTENTION_HELP }); return; }
  const client = clientFromEnvironment();
  if (command === "mcp") { await runMcp(client, { raise: z.toJSONSchema(RaiseRequest), context: z.toJSONSchema(EnrichRequest), ack: z.toJSONSchema(AcknowledgeRequest), cancel: z.toJSONSchema(CancelRequest) }, parseMcpArguments); return; }
  if (command === "raise") { const packet = DecisionPacket.parse(readInput(args)); print(await client.raise(required(args, "--key"), packet)); return; }
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
  if (verb === "list") { print(await client.list(value(args, "--before"))); return; }
  if (!id || id.startsWith("--")) throw new Error("Use a request id returned by CAR");
  if (verb === "get") print(await client.get(id));
  else if (verb === "receive") print(await client.receive(id));
  else if (verb === "context") {
    const input = EnrichRequest.parse({ expected_revision: Number(required(args, "--revision")), packet: readInput(args) });
    print(await client.context(id, input.expected_revision, input.packet));
  } else if (verb === "ack") {
    const input = AcknowledgeRequest.parse({ answer_id: required(args, "--answer"), outcome: required(args, "--outcome"), ...(value(args, "--note") ? { note: value(args, "--note") } : {}) });
    if (input.outcome !== "resolved") throw new Error("Use card request receive ID to durably save the answer before acknowledging receipt");
    print(await client.acknowledge(id, input.answer_id, input.outcome, input.note));
  } else if (verb === "cancel") {
    const input = CancelRequest.parse({ expected_revision: Number(required(args, "--revision")), reason: required(args, "--reason") });
    print(await client.cancel(id, input.expected_revision, input.reason));
  } else throw new Error("Unknown request command; use card request --help");
}
