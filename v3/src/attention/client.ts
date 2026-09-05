/** Outbound-only client. No model, callback listener or host-management daemon required. */
import { createHash } from "node:crypto";
import { readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { REQUEST_STATES } from "./guidance.ts";
import { stableJson } from "../contract/ids.ts";
import type { DecisionPacket } from "./contract.ts";
import { privateDirectory, readPrivateJson, removeDurably, writePrivateJson } from "./files.ts";

export interface ClientOptions { url: string; token: string; spoolDir?: string; allowHttp?: boolean; timeoutMs?: number }
export interface RequestView {
  contract: "car.request.v1"; id: string; revision: number; state: string;
  answer: { id: string; payload: { text?: string; approval?: boolean }; eligible_for_receipt: boolean; delivery: string } | null;
  next_action: string; [key: string]: unknown;
}
export interface PendingReceipt { contract: "car.request.v1"; delivery: "accepted_locally"; idempotency_key: string; spool_file: string; next_action: string }
interface SpoolRecord { contract: "car.client-spool.v1"; audience: string; request: { contract: "car.request.v1"; idempotency_key: string; packet: DecisionPacket }; created_at: string }
const digest = (value: unknown) => createHash("sha256").update(stableJson(value)).digest("hex");
export class ClientError extends Error {
  constructor(readonly code: string, message: string, readonly retryable = false, readonly status?: number) { super(message); }
}

/** A server answer must be an unambiguous text-or-approval payload before receipt. */
function validReplyPayload(payload: unknown): payload is { text?: string; approval?: boolean } {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const keys = Object.keys(payload);
  if (keys.length !== 1 || (keys[0] !== "text" && keys[0] !== "approval")) return false;
  const value = payload as Record<string, unknown>;
  return keys[0] === "text"
    ? typeof value.text === "string" && value.text.trim().length > 0 && value.text.length <= 8_000
    : typeof value.approval === "boolean";
}

export function checkedServerUrl(value: string, allowHttp = false): string {
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/") throw new ClientError("invalid_url", "CAR_URL must be an origin, without credentials, path, query or fragment");
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && (loopback || allowHttp))) {
    throw new ClientError("insecure_transport", "Remote CAR requires HTTPS. For an explicitly trusted tailnet HTTP connection only, set CAR_ALLOW_HTTP=1.");
  }
  return url.origin;
}
function asView(value: unknown, expectedId?: string): RequestView {
  const v = value as Partial<RequestView> | null;
  const invalid = () => new ClientError("invalid_response", "CAR returned an inconsistent response; confirmation is unknown", true);
  if (!v || typeof v !== "object" || Array.isArray(v) || v.contract !== "car.request.v1" ||
    typeof v.id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(v.id) ||
    (expectedId !== undefined && v.id !== expectedId) || !Number.isSafeInteger(v.revision) || (v.revision ?? 0) < 1 ||
    !REQUEST_STATES.includes(v.state as typeof REQUEST_STATES[number]) || !Object.hasOwn(v, "answer")) throw invalid();
  if (v.answer !== null) {
    const a = v.answer;
    if (!a || typeof a !== "object" || typeof a.id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(a.id) ||
      !validReplyPayload(a.payload) ||
      typeof a.eligible_for_receipt !== "boolean" || typeof a.delivery !== "string" ||
      !["staged","delivered","pending","delivering","uncertain","failed","acknowledged","resolved","cancelled","expired"].includes(a.delivery) ||
      (a.eligible_for_receipt && !["answered", "received"].includes(v.state!)) || ["preparing","needs_you"].includes(v.state!)) throw invalid();
  } else if (["answered", "received"].includes(v.state!)) throw invalid();
  return value as RequestView;
}
const encoded = (id: string) => {
  if (typeof id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) throw new ClientError("invalid_id", "Use the request id returned by CAR");
  return encodeURIComponent(id);
};
export class AttentionClient {
  readonly url: string; readonly audience: string; readonly spoolDir: string;
  private readonly pendingDir: string; private readonly receiptDir: string; private readonly rejectedDir: string;
  constructor(private readonly options: ClientOptions) {
    if (!options.token || /[\r\n]/.test(options.token)) throw new ClientError("missing_token", "Configure CAR_AGENT_TOKEN or CAR_CONNECTION_FILE; never use the human sign-in credential");
    this.url = checkedServerUrl(options.url, options.allowHttp);
    // Bind retries to both the endpoint and credential identity. A changed URL
    // or another host's credential cannot silently inherit a pending request.
    this.audience = digest({ origin: this.url, credential: digest(options.token) });
    this.spoolDir = options.spoolDir ?? join(homedir(), ".car", "client");
    this.pendingDir = join(this.spoolDir, this.audience, "pending");
    this.receiptDir = join(this.spoolDir, this.audience, "answers");
    this.rejectedDir = join(this.spoolDir, this.audience, "rejected");
    privateDirectory(this.pendingDir); privateDirectory(this.receiptDir); privateDirectory(this.rejectedDir);
  }
  private async call(path: string, method = "GET", body?: unknown): Promise<unknown> {
    let response: Response;
    try {
      response = await fetch(`${this.url}/v1/attention${path}`, { method, redirect: "error",
        headers: { authorization: `Bearer ${this.options.token}`, ...(body === undefined ? {} : { "content-type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(this.options.timeoutMs ?? 10_000) });
    } catch { throw new ClientError("transport_unconfirmed", "CAR could not confirm the operation. Retry the same request; do not assume it failed remotely.", true); }
    const retryableStatus = response.status >= 500 || [408, 429].includes(response.status);
    let value: unknown;
    try {
      const reader = response.body?.getReader(); if (!reader) throw new Error("empty response");
      let size = 0; const chunks: Uint8Array[] = [];
      try { for (;;) { const part = await reader.read(); if (part.done) break; size += part.value.length; if (size > 512 * 1024) { await reader.cancel(); throw new Error("response too large"); } chunks.push(part.value); } }
      finally { reader.releaseLock(); }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      value = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      if (!response.ok) throw new ClientError("http_error", `CAR returned HTTP ${response.status} without a readable error body`, retryableStatus, response.status);
      throw new ClientError("invalid_response", "CAR returned an unreadable response; confirmation is unknown", true, response.status);
    }
    if (!response.ok) {
      const error = value as { error?: string; message?: string; issues?: unknown };
      throw new ClientError(error.error ?? "http_error", error.message ?? `CAR returned HTTP ${response.status}${error.issues ? `: ${JSON.stringify(error.issues).slice(0, 2000)}` : ""}`,
        retryableStatus, response.status);
    }
    return value;
  }
  /** Persist BEFORE sending. A failed process leaves the same stable request for a relay/flush. */
  async raise(key: string, packet: DecisionPacket): Promise<RequestView | PendingReceipt> {
    if (typeof key !== "string" || !key.trim() || key.length > 256) throw new ClientError("invalid_key", "Supply a stable idempotency key for this particular decision");
    key = key.trim();
    const request = { contract: "car.request.v1" as const, idempotency_key: key, packet };
    if (Buffer.byteLength(JSON.stringify(request)) > 64 * 1024) throw new ClientError("body_too_large", "Decision packet exceeds 64 KiB");
    const file = join(this.pendingDir, `${digest(key)}.json`);
    const record: SpoolRecord = { contract: "car.client-spool.v1", audience: this.audience, request, created_at: new Date().toISOString() };
    if (!writePrivateJson(file, record)) {
      const existing = this.spool(file);
      if (stableJson(existing.request) !== stableJson(request)) throw new ClientError("idempotency_conflict", "A pending request already uses this key with different content");
    }
    try {
      const result = asView(await this.call("/requests", "POST", request));
      removeDurably(file);
      return result;
    } catch (error) {
      if (!(error instanceof ClientError) || !error.retryable) {
        if (this.definitivelyRejected(error)) this.quarantine(file, record, error as ClientError);
        throw error;
      }
      return { contract: "car.request.v1", delivery: "accepted_locally", idempotency_key: key, spool_file: file,
        next_action: "No server acceptance or human notification is confirmed. Run card relay for automatic retries, or card flush. Keep this idempotency key." };
    }
  }
  private spool(path: string): SpoolRecord {
    const record = readPrivateJson(path) as SpoolRecord;
    if (record.contract !== "car.client-spool.v1" || record.audience !== this.audience || !record.request?.idempotency_key)
      throw new ClientError("invalid_spool", "Pending request does not belong to this server and credential identity");
    return record;
  }
  private definitivelyRejected(error: unknown): boolean {
    return error instanceof ClientError && [400, 404, 409, 413, 415, 422].includes(error.status ?? 0);
  }
  private quarantine(file: string, record: SpoolRecord, error: ClientError): void {
    // Preserve evidence, but never let a poison request starve the retry queue.
    const rejected = join(this.rejectedDir, `${digest({ request: record.request, code: error.code })}.json`);
    writePrivateJson(rejected, { ...record, rejection: { code: error.code, status: error.status, message: error.message } });
    removeDurably(file);
  }
  async flush(): Promise<{ accepted: RequestView[]; rejected: { file: string; error: string }[]; pending: { file: string; error: string; retryable: boolean }[]; remaining: number; other_audiences: number }> {
    const accepted: RequestView[] = []; const rejected: { file: string; error: string }[] = [];
    const pending: { file: string; error: string; retryable: boolean }[] = [];
    for (const name of readdirSync(this.pendingDir).filter((n) => /^[a-f0-9]{64}\.json$/.test(n)).slice(0, 100)) {
      const file = join(this.pendingDir, name); let record: SpoolRecord | undefined;
      try {
        record = this.spool(file);
        accepted.push(asView(await this.call("/requests", "POST", record.request)));
        removeDurably(file);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") continue; // Another relay completed this same intent.
        const message = error instanceof Error ? error.message : String(error);
        if (record && this.definitivelyRejected(error)) {
          this.quarantine(file, record, error as ClientError);
          rejected.push({ file, error: message });
        } else pending.push({ file, error: message, retryable: error instanceof ClientError && error.retryable });
        // A network/auth failure must not spend 100 timeout budgets per tick.
        if (error instanceof ClientError && (error.retryable || [401,403].includes(error.status ?? 0))) break;
      }
    }
    return { accepted, rejected, pending,
      remaining: readdirSync(this.pendingDir).filter((n) => /^[a-f0-9]{64}\.json$/.test(n)).length,
      other_audiences: readdirSync(this.spoolDir).filter((n) => /^[a-f0-9]{64}$/.test(n) && n !== this.audience).length };
  }
  async get(id: string): Promise<RequestView> { return asView(await this.call(`/requests/${encoded(id)}`), id); }
  list(before?: string): Promise<unknown> { return this.call(`/requests${before ? `?before=${encodeURIComponent(before)}` : ""}`); }
  async context(id: string, revision: number, packet: DecisionPacket): Promise<RequestView> {
    return asView(await this.call(`/requests/${encoded(id)}/context`, "POST", { expected_revision: revision, packet }), id);
  }
  async acknowledge(id: string, answerId: string, outcome: "received" | "resolved", note?: string): Promise<RequestView> {
    return asView(await this.call(`/requests/${encoded(id)}/ack`, "POST", { answer_id: answerId, outcome, ...(note ? { note } : {}) }), id);
  }
  async cancel(id: string, revision: number, reason: string): Promise<RequestView> {
    return asView(await this.call(`/requests/${encoded(id)}/cancel`, "POST", { expected_revision: revision, reason }), id);
  }
  /** A durable local receipt precedes the source acknowledgement. It does NOT execute an answer. */
  async receive(id: string): Promise<RequestView> {
    const view = await this.get(id);
    if (!view.answer || !view.answer.eligible_for_receipt || !["answered", "received"].includes(view.state)) return view;
    const receipt = join(this.receiptDir, `${digest({ request: id, answer: view.answer.id })}.json`);
    if (!writePrivateJson(receipt, view)) {
      const previous = asView(readPrivateJson(receipt), id);
      if (previous.answer?.id !== view.answer.id || stableJson(previous.answer.payload) !== stableJson(view.answer.payload))
        throw new ClientError("receipt_conflict", "The durable local receipt differs from the server answer. Check the source; do not acknowledge or apply it.");
    }
    // Core rechecks expiry/cancellation here. A stale local receipt is never permission.
    return this.acknowledge(id, view.answer.id, "received");
  }
  async wait(id: string, timeoutSeconds = 60): Promise<RequestView> {
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 0 || timeoutSeconds > 3600) throw new ClientError("invalid_timeout", "wait timeout must be between 0 and 3600 seconds");
    const end = Date.now() + timeoutSeconds * 1000;
    for (;;) {
      const view = await this.get(id);
      if (view.answer || ["resolved", "cancelled", "expired", "preparing"].includes(view.state) || Date.now() >= end) return view;
      await new Promise((resolve) => setTimeout(resolve, Math.min(2_000, Math.max(0, end - Date.now()))));
    }
  }
}
export function clientFromEnvironment(env: NodeJS.ProcessEnv = process.env): AttentionClient {
  const path = env.CAR_CONNECTION_FILE ?? join(homedir(), ".car", "agent.json");
  let profile: { url?: string; token?: string; allow_http?: boolean } = {};
  if (!env.CAR_AGENT_TOKEN || !env.CAR_URL) {
    try { profile = readPrivateJson(path) as typeof profile; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }
  return new AttentionClient({ url: env.CAR_URL ?? profile.url ?? "http://127.0.0.1:7171", token: env.CAR_AGENT_TOKEN ?? profile.token ?? "",
    allowHttp: env.CAR_ALLOW_HTTP === "1" || profile.allow_http === true, spoolDir: env.CAR_SPOOL_DIR });
}
