/** Pure preparation policy: bounded friction for agents, never a blocker lost to silence. */
import type { DecisionPacket } from "./contract.ts";
export interface ContextRequest { field: string; instruction: string }
export function assessPacket(packet: DecisionPacket): ContextRequest[] {
  const requests: ContextRequest[] = [];
  if (!packet.why_human) requests.push({ field: "why_human", instruction: "Check existing instructions/decisions. Explain the specific authority or judgment gap." });
  if (!packet.attempts.length && !packet.cannot_investigate) requests.push({ field: "attempts", instruction: "Describe what you checked or tried. Explain unavailable access in cannot_investigate instead of guessing." });
  if (!packet.facts.length && !packet.cannot_investigate) requests.push({ field: "facts", instruction: "Attach the facts and source references the human needs to decide. Separate observations from uncertainty." });
  if (!packet.recommendation && !packet.cannot_investigate) requests.push({ field: "recommendation", instruction: "Recommend an answer with a rationale, or explain why a recommendation is not justified in cannot_investigate." });
  if (!packet.impact) requests.push({ field: "impact", instruction: "State what is blocked and the consequence of waiting. Unknown is an acceptable explicit answer." });
  return requests;
}
export function decisionContext(packet: DecisionPacket): string[] {
  return [
    ...(packet.recommendation ? [`Recommendation: ${packet.recommendation.answer}`, `Why: ${packet.recommendation.rationale}`] : ["No grounded recommendation supplied."]),
    ...(packet.why_human ? [`Why you: ${packet.why_human}`] : []),
    ...(packet.impact ? [`Blocked work: ${packet.impact}`] : []),
    ...packet.facts.slice(0, 3).map((f) => `${f.statement}${f.source ? ` [${f.source}]` : " [source not supplied]"}`),
    ...(packet.uncertainty.length ? [`Uncertain: ${packet.uncertainty.join("; ")}`] : []),
    ...(packet.cannot_investigate ? [`Investigation limit: ${packet.cannot_investigate}`] : []),
  ];
}
