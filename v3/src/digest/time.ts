/**
 * Local-time helpers shared by the scheduler and the Telegram surface.
 * The digest fires at a *local* wall-clock time, so day boundaries and
 * "tonight" are local too. Everything takes an explicit `now` — no ambient
 * clock reads, so a FakeClock drives all of it.
 */

const pad = (n: number): string => String(n).padStart(2, "0");

/** Local calendar day, YYYY-MM-DD. */
export function localDay(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Local wall clock, HH:MM. */
export function localHhMm(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function parseHhMm(value: string): { hours: number; minutes: number } {
  const m = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!m) return { hours: 8, minutes: 30 };
  return { hours: Math.min(23, Number(m[1])), minutes: Math.min(59, Number(m[2])) };
}

/** Today at HH:MM local. */
export function localAt(now: Date, hhmm: string): Date {
  const { hours, minutes } = parseHhMm(hhmm);
  const d = new Date(now);
  d.setHours(hours, minutes, 0, 0);
  return d;
}

/** The next occurrence of HH:MM local, strictly after `now`. */
export function nextLocalAt(now: Date, hhmm: string): Date {
  const today = localAt(now, hhmm);
  if (today.getTime() > now.getTime()) return today;
  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);
  return tomorrow;
}

/** Minutes offset before a HH:MM time, as a HH:MM string (for nightly jobs). */
export function shiftHhMm(hhmm: string, deltaMinutes: number): string {
  const { hours, minutes } = parseHhMm(hhmm);
  let total = (hours * 60 + minutes + deltaMinutes) % (24 * 60);
  if (total < 0) total += 24 * 60;
  return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
}

/** "2h", "30m", "1d", "90s" → milliseconds. Null when unparseable. */
export function parseDuration(value: string): number | null {
  const m = /^(\d+(?:\.\d+)?)\s*(s|m|h|d)$/i.exec(value.trim());
  if (!m) return null;
  const n = Number(m[1]);
  const unit = m[2]!.toLowerCase();
  const mult = unit === "s" ? 1000 : unit === "m" ? 60_000 : unit === "h" ? 3_600_000 : 86_400_000;
  return n * mult;
}

export function humanTime(d: Date): string {
  return `${localHhMm(d)}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Tue Aug 26" — the digest headline date. */
export function humanDay(d: Date): string {
  return `${WEEKDAYS[d.getDay()]} ${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

export function hoursAgoIso(now: Date, hours: number): string {
  return new Date(now.getTime() - hours * 3_600_000).toISOString();
}
