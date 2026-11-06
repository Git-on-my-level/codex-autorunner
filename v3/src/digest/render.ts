/**
 * Digest rendering — DESIGN §7 format. Pure: DigestData in, markdown out.
 *
 * Buttons are embedded as `⟦btn:…⟧` markers (see surfaces/telegram/render.ts):
 * the Telegram layer lifts them into an inline keyboard, and every other reader
 * (the `digests` table, /brief.md, the web archive) strips them to plain text.
 */
import { CB, btnMarker, encodeCallback } from "../surfaces/telegram/render.ts";
import { humanDay } from "./time.ts";
import type { DigestData } from "./build.ts";

export function renderDigest(data: DigestData, now: Date): string {
  const lines: string[] = [`☀️ CAR digest — ${humanDay(now)}`];
  if (data.escalateOnly) lines.push("🛑 ESCALATE-ONLY mode is on — CAR is not acting autonomously.");
  for (const day of data.missedDays) lines.push(`⚠️ No digest was produced for ${day} (CAR was not running).`);
  lines.push("");

  /* 🤖 handled autonomously */
  lines.push(`🤖 Handled (${data.handled.length})`);
  if (data.handled.length === 0) {
    lines.push("• nothing — CAR took no autonomous action.");
  } else {
    for (const item of data.handled) {
      lines.push(
        `• ${item.label} ${btnMarker("👍", encodeCallback(CB.digestUp, item.decisionId))}${btnMarker(
          "👎",
          encodeCallback(CB.digestDown, item.decisionId),
        )}`,
      );
    }
  }
  lines.push("");

  /* 🙋 david resolved */
  lines.push(`🙋 You resolved (${data.resolved.length})`);
  if (data.resolved.length === 0) lines.push("• nothing — no escalation needed you.");
  else for (const item of data.resolved) lines.push(`• ${item.label}`);
  lines.push("");

  /* ⚠️ stuck / silent */
  lines.push(`⚠️ Stuck / silent (${data.stuck.length})`);
  if (data.stuck.length === 0) {
    lines.push("• nothing overdue — every session is inside its expected cadence.");
  } else {
    for (const item of data.stuck) {
      const buttons = item.carSessionId
        ? `${btnMarker("🔍 probe", encodeCallback(CB.probe, item.carSessionId))}${btnMarker(
            "escalate",
            encodeCallback(CB.escalateNow, item.carSessionId),
          )}`
        : "";
      lines.push(`• ${item.label} ${buttons}`.trimEnd());
    }
  }
  lines.push("");

  /* 📥 held for the digest (quiet hours / muted sessions) */
  if (data.held.length) {
    lines.push(`📥 Held for this digest (${data.held.length})`);
    for (const item of data.held) lines.push(`• ${item.label}`);
    lines.push("");
  }

  /* 💸 spend */
  lines.push(
    `💸 Spend: triage ${usd(data.spend.triageUsd)} (${data.spend.triageCalls} runs) · agents ~${usd(
      data.spend.agentsUsd,
    )} (reported)`,
  );
  if (data.spend.byModel.length) {
    lines.push(`   ${data.spend.byModel.map((m) => `${m.model} ${usd(m.costUsd)} (${m.calls})`).join(" · ")}`);
  }
  lines.push("");

  /* 🧠 memory */
  const offerCount = data.memory.offers.length;
  lines.push(
    `🧠 Memory: ${data.memory.pending} pending learning${data.memory.pending === 1 ? "" : "s"} ${btnMarker(
      "review",
      encodeCallback(CB.memoryReview, "pending"),
    )} · ${offerCount} promotion offer${offerCount === 1 ? "" : "s"}`,
  );
  for (const offer of data.memory.offers) {
    // DESIGN §6: the promotion offer is always phrased as the same question,
    // whatever wording the memory module used to describe the rule.
    lines.push(
      `• Auto-handle "${offer.label}" from now on? ${btnMarker(
        "Yes",
        encodeCallback(CB.promoteYes, offer.memoryId),
      )}${btnMarker(
        "This repo only",
        encodeCallback(CB.promoteRepo, offer.memoryId),
      )}${btnMarker("Keep asking", encodeCallback(CB.promoteKeep, offer.memoryId))}`,
    );
  }

  if (isQuietDay(data)) {
    lines.push("");
    lines.push("🌙 Quiet day: no agent activity was recorded at all. That is a fact, not an outage —");
    lines.push("   this digest is sent every day precisely so silence is never ambiguous.");
  }
  return lines.join("\n");
}

export function isQuietDay(data: DigestData): boolean {
  return (
    data.handled.length === 0 &&
    data.resolved.length === 0 &&
    data.stuck.length === 0 &&
    data.held.length === 0 &&
    data.spend.triageCalls === 0
  );
}

function usd(value: number): string {
  return `$${(Math.round(value * 100) / 100).toFixed(2)}`;
}

/** A day the daemon missed entirely still gets an archive row saying so. */
export function renderMissedDay(day: string): string {
  return [
    `☀️ CAR digest — ${day}`,
    "",
    "⚠️ No digest was produced on this day: CAR was not running.",
    "This placeholder exists so the archive has no silent gaps.",
  ].join("\n");
}
