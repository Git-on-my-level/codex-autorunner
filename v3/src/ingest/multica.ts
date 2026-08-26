/**
 * Multica issue/card webhook normalizer.
 *
 * Multica publishes no public webhook schema, so this adapter is deliberately
 * shape-tolerant: it looks for an issue/card object under several common keys
 * and reads ids/titles/urls across the usual aliases. Anything it cannot
 * classify becomes a `note` rather than being dropped — per DESIGN §11 the
 * generic webhook already covers Multica, so this adapter's job is enrichment
 * (a real response channel + issue ref), never gatekeeping.
 */
import type { CarEvent, EventType, Severity } from "../contract/events.ts";
import {
  buildEvent,
  isRecord,
  isoTs,
  minuteBucket,
  NormalizeError,
  obj,
  shortHash,
  str,
  type NormalizeContext,
} from "./normalize.ts";

export const MULTICA_ADAPTER = "multica-webhook";

/** Actions that put the ball in a human's court. */
const NEEDS_RESPONSE = new Set([
  "question",
  "asked",
  "question_asked",
  "assigned",
  "review_requested",
  "approval_requested",
  "approval.requested",
  "blocked",
  "needs_input",
  "needs_response",
  "mention",
  "mentioned",
  "comment_mention",
]);

/** Actions that signal a failure the operator should look at. */
const ERROR_ACTIONS = new Set(["failed", "errored", "error", "crashed", "autopilot_failed"]);

export function normalizeMultica(raw: unknown, ctx: NormalizeContext): CarEvent {
  if (!isRecord(raw)) throw new NormalizeError("multica payload must be an object");

  const issue = obj(raw, "issue", "card", "ticket", "object", "data", "resource") ?? raw;

  const issueRef =
    str(issue, "ref", "key", "slug", "identifier", "number", "id") ??
    str(raw, "issue_ref", "issue_key", "issue_id", "card_id");
  if (!issueRef) {
    throw new NormalizeError("multica payload is missing an issue reference", {
      keys: Object.keys(raw).slice(0, 20),
    });
  }

  const action = (
    str(raw, "action", "event", "event_type", "type", "kind") ??
    str(issue, "action", "state", "status") ??
    "updated"
  ).toLowerCase();

  const workspace = str(raw, "workspace", "workspace_id", "project") ?? str(issue, "workspace", "project");
  const url = str(issue, "url", "html_url", "web_url", "link") ?? str(raw, "url", "html_url");
  const issueTitle = str(issue, "title", "subject", "name", "summary") ?? issueRef;
  const state = str(issue, "state", "status");
  const actor = str(raw, "actor", "sender", "user", "author") ?? str(obj(raw, "actor", "sender", "user"), "name", "login", "id");
  const comment = obj(raw, "comment") ?? obj(issue, "comment");
  const commentBody = str(comment, "body", "text", "message");
  const issueBody = str(issue, "body", "description", "text", "content");

  const mapped = classify(action, issue);
  const ts = isoTs(
    raw["ts"] ?? raw["timestamp"] ?? raw["created_at"] ?? raw["occurred_at"] ?? issue["updated_at"],
    ctx.now,
  );
  const host = str(raw, "host") ?? ctx.host;

  const deliveryId = str(raw, "delivery_id", "delivery", "webhook_id", "event_id");
  const commentId = str(comment, "id");

  return buildEvent({
    idempotency_key: deliveryId
      ? `multica:${deliveryId}`
      : `multica:${issueRef}:${action}:${shortHash(commentId ?? "", issueBody ?? "", commentBody ?? "", minuteBucket(ctx.now))}`,
    ts,
    vendor: "multica",
    adapter: MULTICA_ADAPTER,
    host,
    session: {
      vendor: "multica",
      native_id: issueRef,
      host,
      title: issueTitle,
      ...(str(issue, "repo", "repository") ? { repo: str(issue, "repo", "repository")! } : {}),
    },
    type: mapped.type,
    severity: mapped.severity,
    requires_response: mapped.requiresResponse,
    response_channel: {
      kind: "multica-api",
      hint: {
        issue: issueRef,
        ...(workspace ? { workspace } : {}),
        ...(url ? { url } : {}),
        ...(commentId ? { comment_id: commentId } : {}),
        action,
      },
    },
    title: `${issueTitle} (${action})`,
    body: [commentBody, issueBody, url].filter(Boolean).join("\n\n"),
    payload: {
      multica: {
        action,
        issue_ref: issueRef,
        ...(workspace ? { workspace } : {}),
        ...(state ? { state } : {}),
        ...(url ? { url } : {}),
        ...(actor ? { actor } : {}),
      },
      raw: raw,
    },
  });
}

function classify(
  action: string,
  issue: Record<string, unknown>,
): { type: EventType; severity: Severity; requiresResponse: boolean } {
  if (ERROR_ACTIONS.has(action)) {
    return { type: "attention.error", severity: "attention", requiresResponse: true };
  }
  if (NEEDS_RESPONSE.has(action)) {
    return { type: "attention.question", severity: "attention", requiresResponse: true };
  }
  if (action === "closed" || action === "resolved" || action === "completed") {
    return { type: "attention.cleared", severity: "info", requiresResponse: false };
  }
  // A comment that ends in a question mark is still a question to answer.
  if (action.startsWith("comment")) {
    const body = str(obj(issue, "comment") ?? {}, "body", "text") ?? "";
    const asking = body.trimEnd().endsWith("?");
    return asking
      ? { type: "attention.question", severity: "attention", requiresResponse: true }
      : { type: "note", severity: "notice", requiresResponse: false };
  }
  return { type: "note", severity: "notice", requiresResponse: false };
}
