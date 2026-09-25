/**
 * Daemon-owned newline-delimited JSON-RPC ACP transport for Hermes.
 *
 * This is deliberately a small transport/lifecycle adapter, not a second
 * Hermes client. It owns one long-lived `hermes acp` process and exposes only
 * public ACP request/response/notification data to the provider layer. In
 * particular, it never reads HERMES_HOME, transcript files, lock files, or
 * any other Hermes-private state.
 */
import type { PublicProviderResponse } from "./invocation.ts";
import type { HermesAcpHandshake, HermesAcpLifecycle, HermesPublicRequest } from "./hermes.ts";

export type AcpStdioErrorCode =
  | "spawn_failed"
  | "not_initialized"
  | "deadline_exceeded"
  | "cancelled"
  | "protocol_error"
  | "remote_error"
  | "process_exit"
  | "closed";

/** Transport errors retain process evidence without embedding private state. */
export class AcpStdioError extends Error {
  readonly name = "AcpStdioError";
  constructor(
    readonly code: AcpStdioErrorCode,
    message: string,
    readonly details: { exitCode?: number | null; stderrTail?: string[]; method?: string; remoteCode?: number; remoteData?: unknown } = {},
  ) {
    super(message);
  }
}

/** Durable CAR-owned binding for a public ACP session id. */
export interface AcpSessionBindings {
  get(providerInstance: string, configFingerprint: string, continuityKey: string): string | null | Promise<string | null>;
  set(providerInstance: string, configFingerprint: string, continuityKey: string, sessionId: string): void | Promise<void>;
  delete(providerInstance: string, configFingerprint: string, continuityKey: string): void | Promise<void>;
}

interface JsonRpcMessage {
  id?: string | number;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code?: number; message?: string; data?: unknown };
}

interface PendingRequest {
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: unknown) => void;
  timer?: ReturnType<typeof setTimeout>;
}

interface ActiveRequest {
  request: HermesPublicRequest;
  sessionId: string;
  invocationId: string;
  nextSeq: number;
  events: Record<string, unknown>[];
}

export interface AcpStdioLifecycleOptions {
  /** Exact Hermes executable or profile-alias wrapper. */
  executable?: string;
  /** Public Hermes CLI profile selector; activates isolated profile state. */
  profile?: string;
  cwd?: string;
  env?: Record<string, string | undefined>;
  requestTimeoutMs?: number;
  stderrTailLines?: number;
  /** Optional durable CAR-owned continuity binding. */
  sessionBindings?: AcpSessionBindings;
}

type AcpProcess = ReturnType<typeof Bun.spawn>;

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function isoNow(): string {
  return new Date().toISOString();
}

/**
 * A persistent ACP stdio lifecycle. One instance is intended to be owned by
 * one configured provider scope; the composition root controls that scope.
 */
export class AcpStdioLifecycle implements HermesAcpLifecycle {
  readonly executable: string;
  readonly argv: string[];
  readonly cwd?: string;

  private readonly env?: Record<string, string | undefined>;
  private readonly requestTimeoutMs: number;
  private readonly stderrLimit: number;
  private readonly sessionBindings?: AcpSessionBindings;
  private process: AcpProcess | null = null;
  private startPromise: Promise<void> | null = null;
  private readerPromise: Promise<void> | null = null;
  private stderrPromise: Promise<void> | null = null;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly sessions = new Map<string, string>();
  private readonly sessionLookups = new Map<string, Promise<string>>();
  private readonly active = new Map<string, ActiveRequest>();
  private readonly activeBySession = new Map<string, string>();
  private readonly stderrTail: string[] = [];
  private nextRpcId = 0;
  private handshake: HermesAcpHandshake | null = null;
  private initialized = false;
  private closed = false;
  private closing = false;
  private processExit: Promise<number> | null = null;
  private providerIdentity: { providerInstance: string; configFingerprint: string } | null = null;

  constructor(options: AcpStdioLifecycleOptions = {}) {
    this.executable = options.executable ?? "hermes";
    this.argv = options.profile
      ? [this.executable, "-p", options.profile, "acp"]
      : [this.executable, "acp"];
    this.cwd = options.cwd;
    this.env = options.env;
    this.requestTimeoutMs = Math.max(100, options.requestTimeoutMs ?? 60_000);
    this.stderrLimit = Math.max(1, options.stderrTailLines ?? 20);
    this.sessionBindings = options.sessionBindings;
  }

  getStderrTail(): string[] {
    return [...this.stderrTail];
  }

  getProcessId(): number | null {
    const pid = this.process?.pid;
    return typeof pid === "number" ? pid : null;
  }

  async initialize(input: {
    provider_instance: string;
    profile: string;
    state_root: string;
    config_fingerprint: string;
  }): Promise<HermesAcpHandshake> {
    if (this.closed) throw new AcpStdioError("closed", "ACP lifecycle is closed", { stderrTail: this.getStderrTail() });
    if (this.initialized && this.handshake) return this.handshake;
    this.providerIdentity = {
      providerInstance: input.provider_instance,
      configFingerprint: input.config_fingerprint,
    };
    await this.ensureStarted();
    const result = objectValue(await this.request("initialize", {
      protocolVersion: 1,
      clientInfo: { name: "car-v3", version: "3.0.0-alpha" },
      // Identity is metadata for the provider; Hermes owns its own account and
      // state. CAR never turns this into a Hermes-home path or reads that path.
      carProvider: {
        providerInstance: input.provider_instance,
        profile: input.profile,
        configFingerprint: input.config_fingerprint,
      },
    }));
    const protocolVersion = String(result.protocolVersion ?? result.protocol_version ?? "");
    if (protocolVersion !== "1") {
      throw new AcpStdioError("protocol_error", `unsupported ACP protocol version ${protocolVersion || "<missing>"}`, { method: "initialize", stderrTail: this.getStderrTail() });
    }
    this.handshake = {
      protocol_version: protocolVersion,
      server_name: textValue(objectValue(result.serverInfo ?? result.agentInfo).name) ?? undefined,
      server_version: textValue(objectValue(result.serverInfo ?? result.agentInfo).version) ?? undefined,
      capabilities: this.capabilitiesFrom(result.capabilities ?? result.agentCapabilities),
    };
    await this.notify("initialized", {});
    this.initialized = true;
    return this.handshake;
  }

  async invoke(input: HermesPublicRequest, signal?: AbortSignal): Promise<PublicProviderResponse<unknown>> {
    if (!this.initialized || !this.handshake) throw new AcpStdioError("not_initialized", "ACP lifecycle has not been initialized", { method: "session/prompt", stderrTail: this.getStderrTail() });
    if (this.closed) throw new AcpStdioError("closed", "ACP lifecycle is closed", { method: "session/prompt", stderrTail: this.getStderrTail() });
    const sessionId = await this.sessionFor(input.continuity_key, input.payload);
    const existing = this.activeBySession.get(sessionId);
    if (existing) throw new AcpStdioError("remote_error", `ACP session already has an active CAR request: ${existing}`, { method: "session/prompt", stderrTail: this.getStderrTail() });
    const active: ActiveRequest = {
      request: input,
      sessionId,
      invocationId: `acp_${input.request_id}`,
      nextSeq: 0,
      events: [],
    };
    this.active.set(input.request_id, active);
    this.activeBySession.set(sessionId, input.request_id);
    this.pushEvent(active, "started", { session_id: sessionId });
    let rejectAbort: ((error: AcpStdioError) => void) | undefined;
    const abortPromise = new Promise<never>((_, reject) => {
      rejectAbort = reject;
    });
    const abort = () => {
      void this.cancel(input.request_id);
      rejectAbort?.(new AcpStdioError("cancelled", `ACP request ${input.request_id} was cancelled`, { method: "session/prompt", stderrTail: this.getStderrTail() }));
    };
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
    try {
      const promptRequest = this.request("session/prompt", {
        sessionId,
        messageId: input.request_id,
        prompt: [
          {
            type: "text",
            // Keep the prompt a single structured JSON document. The strict
            // response instruction prevents CAR from having to reinterpret prose.
            text: this.structuredPrompt(input),
          },
        ],
      }, this.remainingMs(input.deadline_at));
      const result = await Promise.race([promptRequest, abortPromise]);
      const structured = this.extractStructuredResult(result, input.request_id);
      const status = this.statusFromResult(result);
      this.pushEvent(active, "terminal_result", {
        outcome: status,
        public_protocol: "session/prompt",
      });
      return { result: structured, events: active.events };
    } catch (error) {
      if (error instanceof AcpStdioError && error.code === "deadline_exceeded") {
        try {
          await this.cancel(input.request_id);
        } catch {
          // Preserve the deadline as the terminal transport fact.
        }
      }
      if (error instanceof AcpStdioError && (error.code === "deadline_exceeded" || error.code === "cancelled")) {
        this.pushEvent(active, "terminal_result", { outcome: error.code === "cancelled" ? "cancelled" : "timed_out", public_protocol: "session/prompt" });
      }
      throw error;
    } finally {
      if (signal) signal.removeEventListener("abort", abort);
      this.active.delete(input.request_id);
      this.activeBySession.delete(sessionId);
    }
  }

  async cancel(requestId: string): Promise<void> {
    const active = this.active.get(requestId);
    if (!active) return;
    try {
      await this.notify("session/cancel", { sessionId: active.sessionId });
    } catch (error) {
      // The original timeout/process error is more useful than a cancellation
      // write failure. The next request will observe the process state.
      if (!(error instanceof AcpStdioError && error.code === "closed")) throw error;
    }
  }

  async close(): Promise<void> {
    if (this.closed || this.closing) return;
    this.closing = true;
    const process = this.process;
    try {
      if (process && this.initialized && !this.isExited(process)) {
        try {
          await this.request("shutdown", {}, 2_000);
        } catch {
          // Shutdown is best effort; close still terminates this owned process.
        }
        try {
          await this.notify("exit", {});
        } catch {
          // The pipe may already have closed after shutdown.
        }
      }
      if (process) {
        try {
          const stdin = process.stdin;
          if (stdin && typeof stdin !== "number") stdin.end();
        } catch {
          // Already closed.
        }
        await this.waitForExit(process, 2_000);
        if (!this.isExited(process)) {
          process.kill();
          await this.waitForExit(process, 2_000);
        }
      }
    } finally {
      this.closed = true;
      this.closing = false;
      this.rejectPending(new AcpStdioError("closed", "ACP lifecycle closed", { stderrTail: this.getStderrTail() }));
      this.process = null;
      this.initialized = false;
      this.handshake = null;
      this.sessions.clear();
      this.sessionLookups.clear();
      this.active.clear();
      this.activeBySession.clear();
    }
  }

  private async ensureStarted(): Promise<void> {
    if (this.process) return this.startPromise ?? Promise.resolve();
    if (this.startPromise) return this.startPromise;
    this.startPromise = (async () => {
      try {
        const env = this.env ? { ...process.env, ...this.env } : undefined;
        this.process = Bun.spawn(this.argv, {
          cwd: this.cwd,
          env,
          stdin: "pipe",
          stdout: "pipe",
          stderr: "pipe",
        });
        this.processExit = this.process.exited;
        this.readerPromise = this.readStdout(this.process);
        this.stderrPromise = this.readStderr(this.process);
        void this.watchExit(this.process, this.processExit);
      } catch (cause) {
        throw new AcpStdioError("spawn_failed", `could not start ${this.argv.join(" ")}`, { stderrTail: this.getStderrTail() });
      }
    })();
    try {
      await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  private async request(method: string, params: Record<string, unknown>, timeoutMs = this.requestTimeoutMs): Promise<unknown> {
    if (!this.process || this.closed) throw new AcpStdioError("closed", "ACP process is not running", { method, stderrTail: this.getStderrTail() });
    const id = String(++this.nextRpcId);
    const result = new Promise<unknown>((resolve, reject) => {
      const pending: PendingRequest = { method, resolve, reject };
      pending.timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new AcpStdioError("deadline_exceeded", `ACP request ${method} timed out`, { method, stderrTail: this.getStderrTail() }));
      }, Math.max(1, timeoutMs));
      this.pending.set(id, pending);
    });
    try {
      await this.write({ id, method, params });
    } catch (error) {
      const pending = this.pending.get(id);
      this.pending.delete(id);
      if (pending?.timer) clearTimeout(pending.timer);
      throw error;
    }
    return result;
  }

  private async notify(method: string, params: Record<string, unknown>): Promise<void> {
    if (!this.process || this.closed) throw new AcpStdioError("closed", "ACP process is not running", { method, stderrTail: this.getStderrTail() });
    await this.write({ method, params });
  }

  private async write(message: JsonRpcMessage): Promise<void> {
    const process = this.process;
    if (!process) throw new AcpStdioError("closed", "ACP process is not running", { stderrTail: this.getStderrTail() });
    try {
      const stdin = process.stdin;
      if (!stdin || typeof stdin === "number") throw new Error("ACP stdin is not a writable pipe");
      stdin.write(`${JSON.stringify(message)}\n`);
      await stdin.flush();
    } catch (cause) {
      throw new AcpStdioError("process_exit", "ACP stdin is unavailable", { stderrTail: this.getStderrTail() });
    }
  }

  private async readStdout(process: AcpProcess): Promise<void> {
    const stream = process.stdout;
    if (!stream || typeof stream === "number") return;
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let remainder = "";
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        remainder += decoder.decode(chunk.value, { stream: true });
        const lines = remainder.split("\n");
        remainder = lines.pop() ?? "";
        for (const line of lines) {
          if (line.trim()) this.dispatch(line.trim());
        }
      }
      remainder += decoder.decode();
      if (remainder.trim()) this.dispatch(remainder.trim());
    } catch (cause) {
      this.rejectPending(new AcpStdioError("protocol_error", `ACP stdout reader failed: ${String(cause)}`, { stderrTail: this.getStderrTail() }));
    } finally {
      reader.releaseLock();
    }
  }

  private async readStderr(process: AcpProcess): Promise<void> {
    const stream = process.stderr;
    if (!stream || typeof stream === "number") return;
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let remainder = "";
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        remainder += decoder.decode(chunk.value, { stream: true });
        const lines = remainder.split("\n");
        remainder = lines.pop() ?? "";
        for (const line of lines) this.noteStderr(line);
      }
      remainder += decoder.decode();
      if (remainder) this.noteStderr(remainder);
    } finally {
      reader.releaseLock();
    }
  }

  private dispatch(line: string): void {
    let message: JsonRpcMessage;
    try {
      const parsed = JSON.parse(line) as unknown;
      if (!parsed || typeof parsed !== "object") throw new Error("message is not an object");
      message = parsed as JsonRpcMessage;
    } catch (cause) {
      this.rejectPending(new AcpStdioError("protocol_error", `ACP emitted malformed JSON: ${line.slice(0, 256)}`, { stderrTail: this.getStderrTail() }));
      return;
    }
    const id = message.id === undefined ? null : String(message.id);
    if (id && ("result" in message || "error" in message)) {
      const pending = this.pending.get(id);
      if (!pending) return;
      this.pending.delete(id);
      if (pending.timer) clearTimeout(pending.timer);
      if (message.error) {
        pending.reject(new AcpStdioError("remote_error", message.error.message ?? `ACP ${pending.method} failed`, {
          method: pending.method,
          stderrTail: this.getStderrTail(),
          ...(typeof message.error.code === "number" ? { remoteCode: message.error.code } : {}),
          remoteData: message.error.data,
        }));
      } else pending.resolve(message.result);
      return;
    }
    if (message.method) {
      this.recordNotification(message.method, objectValue(message.params));
      // ACP permission requests are server requests. CAR has not supplied an
      // approval callback at this transport boundary, so fail closed rather
      // than allowing Hermes to wait indefinitely.
      if (id) {
        void this.write({ id: message.id, error: { code: -32601, message: "CAR approval callback is not configured" } });
      }
    }
  }

  private recordNotification(method: string, params: Record<string, unknown>): void {
    const sessionId = textValue(params.sessionId ?? params.session_id);
    const requestId = sessionId ? this.activeBySession.get(sessionId) : undefined;
    if (!requestId) return;
    const active = this.active.get(requestId);
    if (!active) return;
    if (method === "session/request_permission") this.pushEvent(active, "progress_snapshot", { method, params });
    else if (method === "session/update") {
      const update = objectValue(params.update);
      const text = this.textFromUpdate(update);
      this.pushEvent(active, text ? "progress_delta" : "heartbeat", { method, params, ...(text ? { text } : {}) });
    } else if (method.includes("started")) this.pushEvent(active, "progress_snapshot", { method, params });
    else this.pushEvent(active, "heartbeat", { method, params });
  }

  private pushEvent(active: ActiveRequest, type: "started" | "heartbeat" | "progress_delta" | "progress_snapshot" | "artifact" | "final_answer" | "terminal_result" | "recovery_state", payload: Record<string, unknown>): void {
    active.events.push({
      contract: "car.provider-event.v1",
      invocation_id: active.invocationId,
      request_id: active.request.request_id,
      provider_id: active.request.provider_instance.startsWith("hermes") ? "hermes" : "hermes",
      provider_instance: active.request.provider_instance,
      type,
      seq: active.nextSeq++,
      ts: isoNow(),
      payload,
    });
  }

  private async sessionFor(continuityKey: string, payload: HermesPublicRequest["payload"]): Promise<string> {
    const existingLookup = this.sessionLookups.get(continuityKey);
    if (existingLookup) return existingLookup;
    const lookup = this.resolveSession(continuityKey, payload);
    this.sessionLookups.set(continuityKey, lookup);
    try {
      return await lookup;
    } finally {
      this.sessionLookups.delete(continuityKey);
    }
  }

  private async resolveSession(continuityKey: string, payload: HermesPublicRequest["payload"]): Promise<string> {
    const identity = this.providerIdentity ?? {
      providerInstance: payload.provider.provider_instance,
      configFingerprint: payload.provider.config_fingerprint,
    };
    let current = this.sessions.get(continuityKey) ?? null;
    if (!current && this.sessionBindings) {
      current = await this.sessionBindings.get(identity.providerInstance, identity.configFingerprint, continuityKey);
      if (current) this.sessions.set(continuityKey, current);
    }
    if (current) {
      try {
        const loaded = await this.request("session/load", { sessionId: current, cwd: this.cwd ?? process.cwd(), mcpServers: [] });
        const loadedId = textValue(objectValue(loaded).sessionId ?? objectValue(loaded).session_id ?? objectValue(loaded).id);
        if (loadedId || loaded !== null) {
          const resolved = loadedId ?? current;
          if (resolved !== current) {
            this.sessions.set(continuityKey, resolved);
            await this.sessionBindings?.set(identity.providerInstance, identity.configFingerprint, continuityKey, resolved);
          }
          return resolved;
        }
      } catch (error) {
        if (!this.isMissingSession(error)) throw error;
        // Only an explicit public missing-session response authorizes a fresh
        // session. Transport/protocol failures never silently reset continuity.
        this.sessions.delete(continuityKey);
        await this.sessionBindings?.delete(identity.providerInstance, identity.configFingerprint, continuityKey);
      }
    }
    const created = await this.request("session/new", {
      cwd: this.cwd ?? process.cwd(),
      mcpServers: [],
      title: `${payload.provider.provider_instance}:${continuityKey}`,
    });
    const createdId = textValue(objectValue(created).sessionId ?? objectValue(created).session_id ?? objectValue(created).id);
    if (!createdId) throw new AcpStdioError("protocol_error", "ACP session/new did not return a session identifier", { method: "session/new", stderrTail: this.getStderrTail() });
    this.sessions.set(continuityKey, createdId);
    await this.sessionBindings?.set(identity.providerInstance, identity.configFingerprint, continuityKey, createdId);
    return createdId;
  }

  private structuredPrompt(input: HermesPublicRequest): string {
    return JSON.stringify({
      car_contract: "car.provider-request.v1",
      response_contract: input.capability === "operator" ? "car.operator.v1" : input.capability === "policy" ? "car.policy.v1" : "car.memory.v1",
      request_id: input.request_id,
      capability: input.capability,
      deadline_at: input.deadline_at,
      payload: input.payload,
      instructions: "Return exactly one JSON object conforming to response_contract. Do not return markdown, prose, or a second object.",
    });
  }

  private extractStructuredResult(result: unknown, requestId: string): unknown {
    const object = objectValue(result);
    const candidates: unknown[] = [object.structuredContent, object.structured, object.result, object.output, object.finalOutput, object.final_output, result];
    for (const candidate of candidates) {
      if (candidate && typeof candidate === "object" && !Array.isArray(candidate) && "contract" in candidate) return candidate;
      if (typeof candidate === "string") {
        try {
          const parsed = JSON.parse(candidate) as unknown;
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
        } catch {
          // Try the next public ACP result field; prose is not a valid result.
        }
      }
    }
    throw new AcpStdioError("protocol_error", `ACP session/prompt returned no structured JSON result for ${requestId}`, { method: "session/prompt", stderrTail: this.getStderrTail() });
  }

  private statusFromResult(result: unknown): "succeeded" | "failed" | "cancelled" | "unknown" {
    const value = String(objectValue(result).status ?? objectValue(result).stopReason ?? objectValue(result).stop_reason ?? "").toLowerCase();
    if (["cancelled", "canceled", "interrupted"].includes(value)) return "cancelled";
    if (["failed", "error", "errored"].includes(value)) return "failed";
    if (["completed", "complete", "success", "succeeded", "end_turn", "end-turn"].includes(value) || value === "") return "succeeded";
    return "unknown";
  }

  private textFromUpdate(update: Record<string, unknown>): string | null {
    const content = objectValue(update.content ?? update.message ?? update.delta);
    return textValue(content.text ?? update.text ?? update.delta);
  }

  private capabilitiesFrom(value: unknown): string[] {
    if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
    if (value && typeof value === "object") return Object.keys(value);
    return [];
  }

  private isMissingSession(error: unknown): boolean {
    if (!(error instanceof AcpStdioError) || error.code !== "remote_error") return false;
    if (error.details.remoteCode === -32004) return true;
    const publicMessage = `${error.message} ${JSON.stringify(error.details.remoteData ?? "")}`.toLowerCase();
    return publicMessage.includes("session not found") || publicMessage.includes("missing session");
  }

  private remainingMs(deadlineAt: string): number {
    const deadline = Date.parse(deadlineAt);
    if (!Number.isFinite(deadline)) throw new AcpStdioError("protocol_error", "deadline_at must be an ISO timestamp", { method: "session/prompt", stderrTail: this.getStderrTail() });
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new AcpStdioError("deadline_exceeded", "ACP request deadline has passed", { method: "session/prompt", stderrTail: this.getStderrTail() });
    return remaining;
  }

  private async watchExit(process: AcpProcess, exited: Promise<number>): Promise<void> {
    const code = await exited;
    if (this.process !== process || this.closing || this.closed) return;
    const error = new AcpStdioError("process_exit", `Hermes ACP exited with code ${code}`, { exitCode: code, stderrTail: this.getStderrTail() });
    this.rejectPending(error);
    this.process = null;
    this.initialized = false;
    this.handshake = null;
  }

  private rejectPending(error: AcpStdioError): void {
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(error);
    }
  }

  private noteStderr(line: string): void {
    const clean = line.trim();
    if (!clean) return;
    this.stderrTail.push(clean.slice(0, 1024));
    while (this.stderrTail.length > this.stderrLimit) this.stderrTail.shift();
  }

  private isExited(process: AcpProcess): boolean {
    return process.exitCode !== null;
  }

  private async waitForExit(process: AcpProcess, timeoutMs: number): Promise<void> {
    await Promise.race([process.exited, new Promise((resolve) => setTimeout(resolve, timeoutMs))]);
  }
}

export function createAcpStdioLifecycle(options: AcpStdioLifecycleOptions = {}): AcpStdioLifecycle {
  return new AcpStdioLifecycle(options);
}
