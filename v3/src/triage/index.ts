/**
 * WS-B owns src/triage/: rules pass, coalescer, incident lineage, the bounded
 * LLM tool loop, and the safety rails around all of it.
 *
 * Shape of a tick (DESIGN §5):
 *   1. claim pending events (leased, crash-safe)
 *   2. rules pass — pure, $0; resolves the boring majority and handles the two
 *      cases that must never reach a model (urgent pages, CAR's own events)
 *   3. coalesce the remainder per session for `coalesce_seconds`
 *   4. release each batch as ONE incident and run the bounded LLM loop
 *
 * Everything writes an audit row; every decision and action is reconstructable.
 */
import type { Store, EventRow } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type {
  ActionBus,
  ChannelPort,
  LlmRunner,
  MemoryHit,
  MemoryReader,
  MemoryWriter,
  PolicyPort,
  TriagePort,
} from "../ports.ts";
import { decisionId as mintDecisionId } from "../contract/ids.ts";
import { policySummary } from "../policy/index.ts";
import { dedupeClassFor, actionDedupeHash } from "./dedupe.ts";
import { classifyEvent, severityRank, type RuleOutcome } from "./rules.ts";
import { TriageRepo, type Disposition, type IncidentRow } from "./repo.ts";
import {
  TERMINAL_TOOLS,
  TOOL_SPECS,
  executeTool,
  isKnownTool,
  type TerminalCall,
  type ToolContext,
} from "./tools.ts";

export { TRIVIAL_TYPES, classifyEvent } from "./rules.ts";
export { dedupeClassFor, actionDedupeHash } from "./dedupe.ts";
export { TOOL_SPECS, TERMINAL_TOOLS } from "./tools.ts";
export { TriageRepo } from "./repo.ts";

/** Events claimed per tick. */
const CLAIM_LIMIT = 50;
/** Events read back out of `coalescing` per tick. */
const COALESCE_READ_LIMIT = 500;
/** Hard cap on a single coalesced batch. */
const MAX_BATCH_EVENTS = 50;
/** Body characters per event in the prompt. */
const PROMPT_BODY_CHARS = 1200;
/** Token budget handed to the memory reader. */
const MEMORY_TOKEN_BUDGET = 2500;

export interface TriageDeps {
  policy: PolicyPort;
  actions: ActionBus;
  channel: ChannelPort;
  memoryReader: MemoryReader;
  memoryWriter: MemoryWriter;
  /**
   * Optional LLM seam. Tests inject ScriptedLlm here; production leaves it unset
   * and the real ai-SDK runner is built lazily from `config.providers.triage`.
   */
  llm?: LlmRunner;
}

export interface TriageEngine extends TriagePort {
  /** Swap the LLM seam after construction (tests, provider hot-swap). */
  setLlmRunner(runner: LlmRunner): void;
}

interface SessionCard {
  car_session_id: string;
  vendor: string;
  host: string;
  title: string | null;
  cwd: string | null;
  repo: string | null;
  state: string;
}

interface Batch {
  carSessionId: string | null;
  events: EventRow[];
  newestMs: number;
}

const SYSTEM_PROMPT = `You are CAR's triage brain. CAR sits between David and every coding agent he runs
(Claude Code, Codex, Cursor, hermes, omp, agentctl, Multica, CI, cron). Your job is to unblock agents
yourself when that is clearly safe, and to escalate to David when it is not.

Rules:
- You MUST finish by calling exactly ONE terminal tool: resolve, keep_informed, escalate, or defer.
- Only the tools offered exist. Never invent a tool name.
- Policy is enforced by the executor, not by you. A blocked action comes back as {"blocked": "..."};
  when that happens, escalate rather than trying variations.
- When in doubt, escalate. A needless page costs David seconds; a wrong autonomous action costs trust.
- Never take a mutating action that memory or policy has not clearly sanctioned.
- Prefer probing (read-only) before acting. Keep summaries to one line, concrete and specific.`;

export function createTriage(store: Store, config: CarConfig, deps: TriageDeps): TriageEngine {
  const repo = new TriageRepo(store);
  let llm: LlmRunner | null = deps.llm ?? null;

  /** Lazy so importing triage never constructs a provider client or needs keys. */
  async function llmRunner(): Promise<LlmRunner> {
    if (llm) return llm;
    const { createLlmRunner } = await import("./llm.ts");
    llm = createLlmRunner(config.providers.triage);
    return llm;
  }

  /* ------------------------------------------------------------- read helpers */

  function sessionCard(carSessionId: string | null): SessionCard | null {
    if (!carSessionId) return null;
    return (
      (store.db
        .query(
          "SELECT car_session_id, vendor, host, title, cwd, repo, state FROM sessions WHERE car_session_id = ?",
        )
        .get(carSessionId) as SessionCard | null) ?? null
    );
  }

  function grantedRulesFor(row: EventRow): MemoryHit[] {
    if (!row.type.startsWith("attention.")) return [];
    const card = sessionCard(row.car_session_id);
    try {
      return deps.memoryReader.grantedRules({
        vendor: row.source_vendor,
        repo: card?.repo ?? undefined,
        eventType: row.type,
        dedupeClass: dedupeClassFor(row),
      });
    } catch {
      return [];
    }
  }

  function readCoalescing(): EventRow[] {
    return store.db
      .query(
        `SELECT * FROM events WHERE triage_state = 'coalescing'
          ORDER BY received_at ASC LIMIT ?`,
      )
      .all(COALESCE_READ_LIMIT) as EventRow[];
  }

  /** One batch per session; sessionless events are their own batch. */
  function groupBatches(rows: EventRow[]): Batch[] {
    const groups = new Map<string, Batch>();
    for (const row of rows) {
      const key = row.car_session_id ?? `evt:${row.id}`;
      let batch = groups.get(key);
      if (!batch) {
        batch = { carSessionId: row.car_session_id, events: [], newestMs: 0 };
        groups.set(key, batch);
      }
      if (batch.events.length >= MAX_BATCH_EVENTS) continue;
      batch.events.push(row);
      const ms = Date.parse(row.received_at);
      if (Number.isFinite(ms) && ms > batch.newestMs) batch.newestMs = ms;
    }
    return [...groups.values()];
  }

  /** The event a batch is *about*: highest severity, newest wins ties. */
  function primaryEvent(events: EventRow[]): EventRow {
    let best = events[0]!;
    for (const row of events) {
      if (severityRank(row.severity) >= severityRank(best.severity)) best = row;
    }
    return best;
  }

  function questionFor(row: EventRow): string {
    const title = row.title.trim();
    if (title) return title;
    const body = row.body.trim().slice(0, 240);
    return body || `${row.type} (${row.severity}) needs a decision`;
  }

  function contextLines(events: EventRow[], card: SessionCard | null, extra: string[]): string[] {
    const lines: string[] = [];
    if (card) {
      lines.push(
        `${card.vendor} · ${card.title ?? card.cwd ?? card.car_session_id} @ ${card.host}${card.repo ? ` · ${card.repo}` : ""}`,
      );
    }
    for (const row of events.slice(-5)) {
      lines.push(`${row.ts} ${row.type}/${row.severity}: ${questionFor(row).slice(0, 160)}`);
    }
    lines.push(...extra);
    return lines;
  }

  /* -------------------------------------------------------- incident lineage */

  function incidentForBatch(batch: Batch, dedupeClass: string): IncidentRow {
    const primary = primaryEvent(batch.events);
    const existing = repo.findLineageIncident(batch.carSessionId, dedupeClass);
    if (existing) return existing.state === "open" ? existing : repo.reopenIncident(existing.id);
    return repo.openIncident({
      carSessionId: batch.carSessionId,
      openedByEvent: primary.id,
      dedupeClass,
    });
  }

  /* ------------------------------------------------------------- escalations */

  function escalateBatch(input: {
    incident: IncidentRow;
    events: EventRow[];
    decidedBy: "rules" | "llm";
    rationale: string;
    severity?: string;
    question?: string;
    suggestedAction?: Record<string, unknown> | null;
    suggestedActionLabel?: string;
    decisionId?: string;
    model?: string | null;
    tokensIn?: number;
    tokensOut?: number;
    costUsd?: number;
  }): void {
    const primary = primaryEvent(input.events);
    const card = sessionCard(input.incident.car_session_id);
    const question = input.question ?? questionFor(primary);
    const severity = input.severity ?? (primary.severity === "urgent" ? "urgent" : "attention");

    const decisionIdValue = repo.recordDecision({
      id: input.decisionId,
      incidentId: input.incident.id,
      decidedBy: input.decidedBy,
      disposition: "escalate",
      rationale: input.rationale,
      actionArgs: input.suggestedAction ?? null,
      model: input.model ?? null,
      tokensIn: input.tokensIn,
      tokensOut: input.tokensOut,
      costUsd: input.costUsd,
    });

    const escalationIdValue = repo.createEscalation({
      incidentId: input.incident.id,
      severity,
      question,
      suggestedAction: input.suggestedAction ?? null,
    });

    deps.channel.sendEscalation({
      escalationId: escalationIdValue,
      incidentId: input.incident.id,
      carSessionId: input.incident.car_session_id,
      severity: severity as never,
      question,
      contextLines: contextLines(input.events, card, [input.rationale]),
      suggestedActionLabel: input.suggestedActionLabel,
    });

    repo.setIncidentState(input.incident.id, "escalated", { summary: question });
    repo.attachEvents(
      input.events.map((e) => e.id),
      input.incident.id,
      "escalated",
    );
    store.audit(input.decidedBy, "triage.escalated", "incident", input.incident.id, {
      decision_id: decisionIdValue,
      escalation_id: escalationIdValue,
      severity,
      rationale: input.rationale,
    });
  }

  /* -------------------------------------------------------------- rules pass */

  /** Returns the number of incidents this outcome opened/processed. */
  async function applyRuleOutcome(row: EventRow, outcome: RuleOutcome): Promise<number> {
    switch (outcome.kind) {
      case "resolved": {
        store.setEventTriageState(row.id, "rules_resolved");
        store.audit("rules", "triage.rules_resolved", "event", row.id, { reason: outcome.reason });
        return 0;
      }
      case "expired": {
        store.setEventTriageState(row.id, "expired");
        store.audit("rules", "triage.expired", "event", row.id, { reason: outcome.reason });
        return 0;
      }
      case "self_event": {
        // Attach to the originating incident; NEVER open fresh LLM triage.
        const origin = row.car_session_id ? repo.findOpenIncidentForSession(row.car_session_id) : null;
        store.setEventTriageState(row.id, "rules_resolved", origin?.id);
        store.audit("rules", "triage.self_event_suppressed", "event", row.id, {
          reason: outcome.reason,
          attached_incident: origin?.id ?? null,
        });
        return 0;
      }
      case "escalate": {
        // An urgent event CAR itself emitted still pages David, but it belongs on
        // the incident that caused it rather than opening a parallel story.
        const origin =
          row.actor === "car" && row.car_session_id
            ? repo.findOpenIncidentForSession(row.car_session_id)
            : null;
        const dedupeClass = dedupeClassFor(row);
        const incident =
          origin ??
          incidentForBatch({ carSessionId: row.car_session_id, events: [row], newestMs: 0 }, dedupeClass);
        escalateBatch({
          incident,
          events: [row],
          decidedBy: "rules",
          rationale: outcome.reason,
          severity: "urgent",
        });
        return 1;
      }
      case "granted":
        return executeGrantedRule(row, outcome.rule, outcome.reason);
      case "llm":
        // Stay in `coalescing`; the coalescer releases the batch when it settles.
        return 0;
    }
  }

  /**
   * David granted autonomy for exactly this shape of event. Execute it
   * deterministically ($0, decided_by=rules) — but the safety rails still apply:
   * escalate-only mode, dedupe, rate limits and the breaker all outrank a grant.
   */
  async function executeGrantedRule(row: EventRow, rule: MemoryHit, reason: string): Promise<number> {
    const dedupeClass = dedupeClassFor(row);
    const batch: Batch = { carSessionId: row.car_session_id, events: [row], newestMs: 0 };
    const incident = incidentForBatch(batch, dedupeClass);

    const content = rule.content ?? {};
    const actionClass =
      typeof content.action_class === "string" && content.action_class
        ? content.action_class
        : row.type === "attention.permission"
          ? "approve_permission"
          : "reply";
    const args = ((content.args_template as Record<string, unknown> | undefined) ?? {}) as Record<string, unknown>;
    const dedupeHash = actionDedupeHash(actionClass, { ...args, event: row.id });

    const blocked = grantBlockReason(actionClass, args, dedupeHash, row.car_session_id);
    if (blocked) {
      repo.recordAction({
        decisionId: "pending",
        actionClass,
        args,
        policyVerdict: "blocked",
        dedupeHash,
        state: "failed",
        result: { blocked },
      });
      escalateBatch({
        incident,
        events: [row],
        decidedBy: "rules",
        rationale: `granted rule ${rule.id} blocked: ${blocked}`,
      });
      return 1;
    }

    const decisionIdValue = mintDecisionId();
    const ctx = toolContext(incident, [row], decisionIdValue, { granted: true });
    const disposition: Disposition = "auto_resolve";
    let ok = false;
    let result: unknown = null;

    if (actionClass === "approve_permission" || actionClass === "deny_permission") {
      const res = await executeTool(ctx, actionClass === "approve_permission" ? "approve_permission" : "deny_permission", {
        event_id: row.id,
        reason: `granted rule ${rule.id}`,
      });
      ok = res.ok;
      result = res.output;
    } else if (actionClass === "reply") {
      const text =
        typeof content.text === "string" ? content.text : typeof args.text === "string" ? args.text : "";
      const res = await executeTool(ctx, "reply_to_agent", { text });
      ok = res.ok;
      result = res.output;
    } else {
      const templateId = actionClass.startsWith("exec.") ? actionClass.slice(5) : actionClass;
      const res = await executeTool(ctx, "run_action", { template_id: templateId, args });
      ok = res.ok;
      result = res.output;
    }

    repo.recordDecision({
      id: decisionIdValue,
      incidentId: incident.id,
      decidedBy: "rules",
      disposition,
      rationale: `${reason} (autonomy=granted, executed without LLM)`,
      actionClass,
      actionArgs: args,
    });

    if (!ok) {
      escalateBatch({
        incident,
        events: [row],
        decidedBy: "rules",
        rationale: `granted rule ${rule.id} failed to execute: ${JSON.stringify(result).slice(0, 200)}`,
      });
      return 1;
    }

    repo.setIncidentState(incident.id, "resolved", { summary: `auto: ${actionClass} (granted rule)` });
    repo.attachEvents([row.id], incident.id, "rules_resolved");
    store.audit("rules", "triage.granted_rule_executed", "incident", incident.id, {
      memory_id: rule.id,
      action_class: actionClass,
      decision_id: decisionIdValue,
    });
    notifyFirstGrantUse(rule, actionClass, row.car_session_id);
    return 1;
  }

  /** Safety rails that outrank an autonomy grant. Null when clear. */
  function grantBlockReason(
    actionClass: string,
    args: Record<string, unknown>,
    dedupeHash: string,
    carSessionId: string | null,
  ): string | null {
    if (deps.policy.escalateOnly()) return "escalate-only mode is active";
    // NB: class *enablement* is deliberately not required here — DESIGN §5 keeps
    // approve_permission disabled globally precisely so it can be granted
    // per-rule via memory promotion. An explicit `forbid` still stops us.
    if (deps.policy.check(actionClass, args) === "forbid") return `policy forbids ${actionClass}`;
    const gateFn = deps.policy.gate as (c: string, h: string, s?: string | null) => string | null;
    return gateFn.call(deps.policy, actionClass, dedupeHash, carSessionId);
  }

  /** DESIGN §6: first autonomous use after a grant sends a one-time notify. */
  function notifyFirstGrantUse(rule: MemoryHit, actionClass: string, carSessionId: string | null): void {
    const key = `granted_rule_first_use:${rule.id}`;
    if (store.kvGet<boolean>(key) === true) return;
    store.kvSet(key, true);
    deps.channel.sendNotify(
      `🤖 First autonomous use of a granted rule: ${actionClass} (memory ${rule.id}). 👎 to revoke.`,
      carSessionId ?? undefined,
    );
    store.audit("rules", "memory.first_grant_use", "memory", rule.id, { action_class: actionClass });
  }

  /* ---------------------------------------------------------------- LLM loop */

  function toolContext(
    incident: IncidentRow,
    events: EventRow[],
    decisionIdValue: string,
    opts: { granted?: boolean } = {},
  ): ToolContext {
    return {
      granted: opts.granted,
      store,
      config,
      repo,
      policy: deps.policy,
      actions: deps.actions,
      memoryReader: deps.memoryReader,
      memoryWriter: deps.memoryWriter,
      incidentId: incident.id,
      decisionId: decisionIdValue,
      carSessionId: incident.car_session_id,
      events,
    };
  }

  function buildPrompt(input: {
    incident: IncidentRow;
    events: EventRow[];
    dedupeClass: string;
    runNo: number;
  }): string {
    const card = sessionCard(input.incident.car_session_id);
    const primary = primaryEvent(input.events);
    let memory: { charter: string; hits: MemoryHit[] } = { charter: "", hits: [] };
    try {
      memory = deps.memoryReader.assembleContext({
        vendor: primary.source_vendor,
        repo: card?.repo ?? undefined,
        eventType: primary.type,
        dedupeClass: input.dedupeClass,
        carSessionId: input.incident.car_session_id ?? undefined,
        freeText: `${primary.title} ${primary.body}`.slice(0, 500),
        tokenBudget: MEMORY_TOKEN_BUDGET,
      });
    } catch {
      /* memory is best-effort context, never a hard dependency */
    }

    const sections: string[] = [];
    sections.push(
      `## Policy\n${policySummary(deps.policy)}\n\nActions you request are checked against this before they run.`,
    );
    if (memory.charter.trim()) sections.push(`## Charter (David's standing instructions)\n${memory.charter.trim()}`);
    if (memory.hits.length > 0) {
      const rendered = memory.hits
        .map(
          (h) =>
            `- [${h.tier}/${h.kind} conf=${h.confidence.toFixed(2)} autonomy=${h.autonomy}${h.status !== "active" ? ` status=${h.status}` : ""}] ${JSON.stringify(h.content)}`,
        )
        .join("\n");
      sections.push(`## Memory\n${rendered}`);
    }
    sections.push(
      card
        ? `## Session\nvendor=${card.vendor} host=${card.host} state=${card.state}\ntitle=${card.title ?? "(none)"}\ncwd=${card.cwd ?? "(none)"} repo=${card.repo ?? "(none)"}\ncar_session_id=${card.car_session_id}`
        : "## Session\n(sessionless event — cron/CI source)",
    );
    sections.push(
      `## Incident\nid=${input.incident.id} dedupe_class=${input.dedupeClass}\nLLM run ${input.runNo} of ${config.triage.max_llm_runs_per_incident} allowed for this lineage; beyond that CAR escalates unconditionally.\nTool budget: ${config.triage.max_tool_calls} non-terminal calls.`,
    );
    const eventsText = input.events
      .map((row, i) => {
        const channel = row.response_channel_json ? ` response_channel=${row.response_channel_json}` : "";
        return [
          `### Event ${i + 1}/${input.events.length} — ${row.type} (${row.severity})`,
          `id=${row.id} ts=${row.ts} source=${row.source_vendor}/${row.source_adapter}@${row.source_host}`,
          `requires_response=${row.requires_response === 1}${channel}`,
          `title: ${row.title}`,
          row.body ? `body: ${row.body.slice(0, PROMPT_BODY_CHARS)}` : "",
        ]
          .filter(Boolean)
          .join("\n");
      })
      .join("\n\n");
    sections.push(`## Events (coalesced batch of ${input.events.length})\n${eventsText}`);
    sections.push("Decide. Finish with exactly one terminal tool call.");
    return sections.join("\n\n");
  }

  async function runLlmTriage(incident: IncidentRow, batch: Batch, dedupeClass: string, runNo: number): Promise<void> {
    const decisionIdValue = mintDecisionId();
    const ctx = toolContext(incident, batch.events, decisionIdValue);
    const messages: { role: "user" | "assistant" | "tool"; content: string }[] = [
      { role: "user", content: buildPrompt({ incident, events: batch.events, dedupeClass, runNo }) },
    ];

    let toolCallCount = 0;
    let terminal: TerminalCall | null = null;
    let violation: string | null = null;
    let tokensIn = 0;
    let tokensOut = 0;
    let costUsd = 0;
    let model: string | null = null;

    const runner = await llmRunner();
    const maxTurns = config.triage.max_tool_calls + 2;

    for (let turnNo = 0; turnNo < maxTurns; turnNo++) {
      let turn;
      try {
        turn = await runner.turn({ system: SYSTEM_PROMPT, messages, tools: TOOL_SPECS });
      } catch (err) {
        violation = `llm turn failed: ${String(err)}`;
        break;
      }

      tokensIn += turn.tokensIn;
      tokensOut += turn.tokensOut;
      costUsd += turn.costUsd;
      model = turn.model;
      const slash = turn.model.indexOf("/");
      store.recordSpend(
        slash > 0 ? turn.model.slice(0, slash) : "unknown",
        slash > 0 ? turn.model.slice(slash + 1) : turn.model,
        turn.tokensIn,
        turn.tokensOut,
        turn.costUsd,
      );

      if (tokensIn + tokensOut > config.triage.run_token_cap) {
        violation = `exceeded run_token_cap=${config.triage.run_token_cap} (${tokensIn + tokensOut} tokens)`;
        break;
      }

      if (turn.toolCalls.length === 0) {
        violation = "model returned no tool call";
        break;
      }

      const unknown = turn.toolCalls.find((c) => !isKnownTool(c.tool));
      if (unknown) {
        violation = `unknown tool "${unknown.tool}" (closed toolset)`;
        break;
      }

      const terminals = turn.toolCalls.filter((c) => TERMINAL_TOOLS.has(c.tool));
      if (terminals.length > 1) {
        violation = `${terminals.length} terminal tools in one turn; exactly one is required`;
        break;
      }

      let budgetBlown = false;
      for (const call of turn.toolCalls) {
        if (TERMINAL_TOOLS.has(call.tool)) continue;
        toolCallCount++;
        if (toolCallCount > config.triage.max_tool_calls) {
          violation = `exceeded max_tool_calls=${config.triage.max_tool_calls}`;
          budgetBlown = true;
          break;
        }
        const res = await executeTool(ctx, call.tool, call.args);
        store.audit("llm", "triage.tool_call", "incident", incident.id, {
          tool: call.tool,
          ok: res.ok,
          decision_id: decisionIdValue,
        });
        messages.push({ role: "assistant", content: `tool_call ${call.tool} ${JSON.stringify(call.args)}` });
        messages.push({ role: "tool", content: JSON.stringify(res.output).slice(0, 4000) });
      }
      if (budgetBlown) break;

      const chosen = terminals[0];
      if (chosen) {
        terminal = { tool: chosen.tool as TerminalCall["tool"], args: chosen.args };
        break;
      }
    }

    if (!terminal && !violation) violation = "run ended without a terminal tool";

    const spendMeta = { model, tokensIn, tokensOut, costUsd };

    if (violation || !terminal) {
      // Terminal-tool contract violated → forced escalate. Fail loud, not silent.
      store.audit("llm", "triage.terminal_violation", "incident", incident.id, {
        violation,
        tool_calls: toolCallCount,
        decision_id: decisionIdValue,
      });
      escalateBatch({
        incident,
        events: batch.events,
        decidedBy: "llm",
        rationale: `LLM triage did not complete cleanly (${violation}); escalating for safety`,
        decisionId: decisionIdValue,
        ...spendMeta,
      });
      return;
    }

    await applyTerminal(incident, batch, terminal, decisionIdValue, spendMeta);
  }

  async function applyTerminal(
    incident: IncidentRow,
    batch: Batch,
    terminal: TerminalCall,
    decisionIdValue: string,
    spend: { model: string | null; tokensIn: number; tokensOut: number; costUsd: number },
  ): Promise<void> {
    const args = terminal.args ?? {};
    const eventIds = batch.events.map((e) => e.id);

    switch (terminal.tool) {
      case "resolve":
      case "keep_informed": {
        const summary = String(args.summary ?? "").slice(0, 500) || `${terminal.tool} (no summary given)`;
        const disposition: Disposition = terminal.tool === "resolve" ? "auto_resolve" : "keep_informed";
        repo.recordDecision({
          id: decisionIdValue,
          incidentId: incident.id,
          decidedBy: "llm",
          disposition,
          rationale: summary,
          model: spend.model,
          tokensIn: spend.tokensIn,
          tokensOut: spend.tokensOut,
          costUsd: spend.costUsd,
        });
        repo.setIncidentState(incident.id, "resolved", { summary });
        repo.attachEvents(eventIds, incident.id, "llm_resolved");
        store.audit("llm", `triage.${terminal.tool}`, "incident", incident.id, {
          decision_id: decisionIdValue,
          summary,
        });
        return;
      }
      case "escalate": {
        const severity = typeof args.severity === "string" ? args.severity : "attention";
        const question = String(args.question ?? "").slice(0, 800) || questionFor(primaryEvent(batch.events));
        const suggested = (args.suggested_action as Record<string, unknown> | undefined) ?? null;
        escalateBatch({
          incident,
          events: batch.events,
          decidedBy: "llm",
          rationale: typeof args.rationale === "string" ? args.rationale : "LLM triage escalated",
          severity,
          question,
          suggestedAction: suggested,
          suggestedActionLabel: suggested && typeof suggested.label === "string" ? suggested.label : undefined,
          decisionId: decisionIdValue,
          ...spend,
        });
        return;
      }
      case "defer": {
        const until = typeof args.until === "string" ? args.until : null;
        const reason = String(args.reason ?? "deferred by triage").slice(0, 500);
        repo.recordDecision({
          id: decisionIdValue,
          incidentId: incident.id,
          decidedBy: "llm",
          disposition: "defer",
          rationale: reason,
          actionArgs: { until },
          model: spend.model,
          tokensIn: spend.tokensIn,
          tokensOut: spend.tokensOut,
          costUsd: spend.costUsd,
        });
        repo.setIncidentState(incident.id, "snoozed", { summary: reason, snoozeUntil: until });
        repo.attachEvents(eventIds, incident.id, "llm_resolved");
        store.audit("llm", "triage.defer", "incident", incident.id, {
          decision_id: decisionIdValue,
          until,
          reason,
        });
        return;
      }
    }
  }

  /* ------------------------------------------------------------- batch release */

  async function triageBatch(batch: Batch): Promise<number> {
    const primary = primaryEvent(batch.events);
    const dedupeClass = dedupeClassFor(primary);
    const incident = incidentForBatch(batch, dedupeClass);

    // Breaker or budget: escalate without spending a token.
    if (deps.policy.escalateOnly()) {
      escalateBatch({
        incident,
        events: batch.events,
        decidedBy: "rules",
        rationale: "escalate-only mode active (circuit breaker or daily budget); LLM triage skipped",
      });
      return 1;
    }

    // DESIGN §5(f): max LLM runs per incident lineage, then escalate unconditionally.
    if (incident.llm_runs >= config.triage.max_llm_runs_per_incident) {
      escalateBatch({
        incident,
        events: batch.events,
        decidedBy: "rules",
        rationale: `dedupe class ${dedupeClass} exhausted its ${config.triage.max_llm_runs_per_incident} LLM triage runs; escalating unconditionally`,
      });
      return 1;
    }

    const runNo = repo.incrementLlmRuns(incident.id);
    store.audit("triage", "triage.batch_released", "incident", incident.id, {
      events: batch.events.length,
      dedupe_class: dedupeClass,
      llm_run: runNo,
    });
    await runLlmTriage(incident, batch, dedupeClass, runNo);
    return 1;
  }

  /* -------------------------------------------------------------------- tick */

  return {
    setLlmRunner(runner: LlmRunner): void {
      llm = runner;
    },

    async tick(): Promise<number> {
      let incidents = 0;

      // 1 + 2: claim and run the pure rules pass.
      const claimed = store.claimPendingEvents(CLAIM_LIMIT, config.triage.lease_seconds);
      for (const row of claimed) {
        const outcome = classifyEvent(row, {
          now: store.clock.now(),
          grantedRules: grantedRulesFor(row),
        });
        incidents += await applyRuleOutcome(row, outcome);
      }

      // 3 + 4: release settled batches, one incident each.
      const holdMs = config.triage.coalesce_seconds * 1000;
      const nowMs = store.clock.now().getTime();
      for (const batch of groupBatches(readCoalescing())) {
        if (batch.events.length === 0) continue;
        if (nowMs - batch.newestMs < holdMs) continue;
        incidents += await triageBatch(batch);
      }

      return incidents;
    },
  };
}
