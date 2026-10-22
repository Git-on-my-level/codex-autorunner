import { describe, expect, test } from "bun:test";
import {
  CB,
  alwaysKeyboard,
  btnMarker,
  decodeCallback,
  editMarkup,
  encodeCallback,
  escalationKeyboard,
  extractButtons,
  renderDigestMessage,
  renderEscalation,
  renderReplyPrompt,
  renderResolution,
  renderSnoozed,
  severityMark,
  snoozeKeyboard,
  stripButtons,
  toReplyMarkup,
} from "../../src/surfaces/telegram/render.ts";

const base = {
  escalationId: "esc_01J000000000000000000000AA",
  incidentId: "inc_01J000000000000000000000BB",
  carSessionId: "sess_01J000000000000000000000CC",
  severity: "attention" as const,
  question: "Agent asks: force-push to fix/telemetry-cliff? Remote diverged.",
  contextLines: [
    "CAR probed: `git status` — remote has 2 CI-authored commits.",
    "Memory: you've denied force-push twice on this repo.",
  ],
  suggestedActionLabel: "DENY — tell agent to rebase instead.",
};

describe("renderEscalation", () => {
  test("renders the DESIGN §7 card verbatim", () => {
    const out = renderEscalation({ ...base, sessionLabel: "claude-code · omi-desktop @ mac-studio" });
    expect(out.text).toBe(
      [
        "🔴 needs you · claude-code · omi-desktop @ mac-studio",
        "Agent asks: force-push to fix/telemetry-cliff? Remote diverged.",
        "CAR probed: `git status` — remote has 2 CI-authored commits.",
        "Memory: you've denied force-push twice on this repo.",
        "Suggests: DENY — tell agent to rebase instead.",
      ].join("\n"),
    );
  });

  test("shows the five DESIGN §7 buttons in two rows", () => {
    const { inline_keyboard } = renderEscalation(base);
    expect(inline_keyboard.map((row) => row.map((b) => b.text))).toEqual([
      ["✅ Approve", "❌ Deny"],
      ["💬 Reply", "😴 ▾", "🧠 Always…"],
    ]);
    expect(inline_keyboard[0]![0]!.data).toBe(`ap:${base.escalationId}`);
    expect(inline_keyboard[1]![2]!.data).toBe(`al:${base.escalationId}`);
  });

  test("free-text questions drop approve/deny", () => {
    const { inline_keyboard } = renderEscalation({ ...base, allowApproveDeny: false });
    expect(inline_keyboard).toHaveLength(1);
    expect(inline_keyboard[0]!.map((b) => b.text)).toEqual(["💬 Reply", "😴 ▾", "🧠 Always…"]);
  });

  test("omits optional lines when absent", () => {
    const out = renderEscalation({
      ...base,
      contextLines: [],
      suggestedActionLabel: undefined,
      sessionLabel: undefined,
    });
    expect(out.text).toBe(`🔴 needs you\n${base.question}`);
  });

  test("severity drives the marker", () => {
    expect(severityMark("urgent")).toBe("🚨");
    expect(severityMark("attention")).toBe("🔴");
    expect(severityMark("notice")).toBe("🟡");
    expect(severityMark("info")).toBe("⚪");
    expect(renderEscalation({ ...base, severity: "urgent" }).text.startsWith("🚨 needs you")).toBe(true);
  });
});

describe("resolution + snooze cards", () => {
  test("resolution rewrites the header and clears the keyboard", () => {
    const card = renderEscalation({ ...base, sessionLabel: "claude-code · omi @ mac" }).text;
    const resolved = renderResolution(card, "❌ denied");
    expect(resolved.text.split("\n")[0]).toContain("🔴 handled ·");
    expect(resolved.text).toContain("— ❌ denied (david)");
    expect(resolved.inline_keyboard).toEqual([]);
  });

  test("snooze card names the wake time", () => {
    const card = renderEscalation(base).text;
    expect(renderSnoozed(card, "tonight").text).toContain("😴 snoozed until tonight");
  });

  test("reply prompt asks Telegram for a force reply", () => {
    const spec = renderReplyPrompt("codex · omi @ mac");
    expect(spec.force_reply).toBe(true);
    expect(spec.text).toContain("codex · omi @ mac");
  });
});

describe("callback codec", () => {
  test("round-trips every op", () => {
    for (const op of Object.values(CB)) {
      const data = encodeCallback(op, "esc_01J000000000000000000000AA");
      expect(decodeCallback(data)).toEqual({ op, id: "esc_01J000000000000000000000AA" });
    }
  });

  test("stays inside Telegram's 64-byte callback_data limit", () => {
    const longest = [...Object.values(CB)].sort((a, b) => b.length - a.length)[0]!;
    const data = encodeCallback(longest, "esc_01J000000000000000000000AA");
    expect(new TextEncoder().encode(data).length).toBeLessThanOrEqual(64);
  });

  test("rejects junk", () => {
    expect(decodeCallback("")).toBeNull();
    expect(decodeCallback("nope:esc_1")).toBeNull();
    expect(decodeCallback("ap:")).toBeNull();
    expect(decodeCallback(":esc_1")).toBeNull();
  });

  test("submenu keyboards carry the escalation id", () => {
    expect(snoozeKeyboard("esc_x").flat().map((b) => b.data)).toEqual([
      "sz1:esc_x",
      "szn:esc_x",
      "szd:esc_x",
      "bk:esc_x",
    ]);
    expect(alwaysKeyboard("esc_x").flat().map((b) => b.data)).toEqual([
      "ala:esc_x",
      "alr:esc_x",
      "alk:esc_x",
      "bk:esc_x",
    ]);
    expect(escalationKeyboard("esc_x", false)).toHaveLength(1);
  });
});

describe("digest button markers", () => {
  const md = `🤖 Handled (1)\n• approved dep bump ${btnMarker("👍", "du:dec_1")}${btnMarker("👎", "dd:dec_1")}`;

  test("extracts buttons and strips markers from the text", () => {
    expect(extractButtons(md)).toEqual([
      { text: "👍", data: "du:dec_1" },
      { text: "👎", data: "dd:dec_1" },
    ]);
    expect(stripButtons(md)).toBe("🤖 Handled (1)\n• approved dep bump");
  });

  test("renderDigestMessage lays buttons out per row", () => {
    const spec = renderDigestMessage(md, 2);
    expect(spec.text).toBe("🤖 Handled (1)\n• approved dep bump");
    expect(spec.inline_keyboard).toEqual([
      [
        { text: "👍", data: "du:dec_1" },
        { text: "👎", data: "dd:dec_1" },
      ],
    ]);
  });

  test("a digest with no buttons gets no keyboard", () => {
    const spec = renderDigestMessage("☀️ CAR digest — Tue Aug 26\nnothing happened");
    expect(spec.inline_keyboard).toBeUndefined();
  });
});

describe("Telegram reply markup", () => {
  test("an inline keyboard becomes callback_data buttons", () => {
    const spec = { text: "x", inline_keyboard: escalationKeyboard("esc_1") };
    expect(toReplyMarkup(spec)).toEqual({
      inline_keyboard: [
        [
          { text: "✅ Approve", callback_data: "ap:esc_1" },
          { text: "❌ Deny", callback_data: "dn:esc_1" },
        ],
        [
          { text: "💬 Reply", callback_data: "rp:esc_1" },
          { text: "😴 ▾", callback_data: "sz:esc_1" },
          { text: "🧠 Always…", callback_data: "al:esc_1" },
        ],
      ],
    });
  });

  test("a force-reply spec asks for the reply composer", () => {
    expect(toReplyMarkup({ text: "x", force_reply: true })).toEqual({ force_reply: true });
    expect(toReplyMarkup({ text: "x" })).toBeUndefined();
    // An inline keyboard wins: force_reply is only for the bare prompt.
    expect(toReplyMarkup({ text: "x", force_reply: true, inline_keyboard: [[{ text: "a", data: "ap:1" }]] }))
      .toEqual({ inline_keyboard: [[{ text: "a", callback_data: "ap:1" }]] });
  });

  test("an edit ALWAYS sends a markup, so a resolved card loses its buttons", () => {
    // Omitting reply_markup on editMessageText leaves the old keyboard live —
    // a resolved escalation would still offer a tappable Approve.
    expect(editMarkup({ text: "resolved" })).toEqual({ inline_keyboard: [] });
    expect(editMarkup({ text: "resolved", inline_keyboard: [] })).toEqual({ inline_keyboard: [] });
    expect(editMarkup({ text: "menu", inline_keyboard: snoozeKeyboard("esc_1") }).inline_keyboard).toHaveLength(2);
  });

  test("force_reply never reaches an edit", () => {
    expect(editMarkup({ text: "x", force_reply: true })).toEqual({ inline_keyboard: [] });
  });
});
