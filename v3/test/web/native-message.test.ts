import { describe, expect, test } from "bun:test";
import { jsx } from "hono/jsx/jsx-runtime";
import { renderToString } from "hono/jsx/dom/server";
import { NativeCard, type NativeDecision } from "../../src/surfaces/web/decision_views.tsx";

function native(overrides: Partial<NativeDecision> = {}): NativeDecision {
  return {
    event_type: "attention.question", id: "esc_native", incident_id: "inc_native", question: "Proceed?",
    severity: "attention", state: "answered", body: "A response is required.", title: "Proceed?",
    source_host: "mac", created_at: "2026-08-26T12:00:00.000Z", obligation_state: "open",
    reply_state: null, last_error: null, snooze_until: null, reply_id: null, reply_revision: undefined,
    reply_payload_json: null, ...overrides,
  };
}

function render(row: NativeDecision, canWrite = false): string {
  return renderToString(jsx(NativeCard, { row, canWrite }));
}

describe("native decision reader", () => {
  test("renders exact text replies while escaping source-controlled markup", () => {
    const html = render(native({ reply_state: "staged", reply_payload_json: JSON.stringify({ text: "Keep <script>alert(1)</script>" }) }));
    expect(html).toContain("Your reply");
    expect(html).toContain("Keep &lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).not.toContain("<script>alert(1)</script>");
  });

  test("renders both approval outcomes as exact answers", () => {
    expect(render(native({ reply_state: "delivered", reply_payload_json: JSON.stringify({ approval: true }) }))).toContain("Approved");
    expect(render(native({ reply_state: "delivered", reply_payload_json: JSON.stringify({ approval: false }) }))).toContain("Denied");
  });

  test("terminal obligation status wins over delivered reply evidence and hides actions", () => {
    const html = render(native({ obligation_state: "expired", reply_state: "delivered", reply_id: "reply_native", reply_revision: 2, reply_payload_json: JSON.stringify({ text: "Proceed" }) }), true);
    expect(html).toContain("Deadline missed · not approved");
    expect(html).toContain("Delivery record: Delivered · awaiting clearance");
    expect(html).not.toContain("Check delivery at the source");
    expect(html).not.toContain("<form");
  });

  test("read-only pending native questions do not expose mutation forms", () => {
    const html = render(native({ state: "pending", reply_state: null }), false);
    expect(html).toContain("Proceed?");
    expect(html).not.toContain("<form");
  });
});
