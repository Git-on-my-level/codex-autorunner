import type { ChatRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';

export function progressElapsedWithLiveWall(value: ChatRunProgress, nowMs: number): number {
  const base = value.elapsedSeconds ?? 0;
  if (value.status !== 'running' || !value.startedAt) return base;
  const startedMs = Date.parse(value.startedAt);
  if (!Number.isFinite(startedMs)) return base;
  const wallElapsed = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  return Math.max(base, wallElapsed);
}

export function progressWithLiveElapsed(value: ChatRunProgress | null, nowMs: number): ChatRunProgress | null {
  if (!value) return value;
  const elapsedSeconds = progressElapsedWithLiveWall(value, nowMs);
  return elapsedSeconds === value.elapsedSeconds ? value : { ...value, elapsedSeconds };
}

export function mergeChatProgressUpdate(
  previousProgress: ChatRunProgress | null,
  nextProgress: ChatRunProgress,
  nowMs: number
): ChatRunProgress {
  if (!previousProgress || previousProgress.id !== nextProgress.id) return nextProgress;
  const incomingElapsed = nextProgress.elapsedSeconds ?? 0;
  const mergedElapsed = Math.max(progressElapsedWithLiveWall(previousProgress, nowMs), incomingElapsed);
  const seen = new Map<string, number>();
  const events: SurfaceArtifact[] = [];
  for (const ev of [...previousProgress.events, ...nextProgress.events]) {
    const key = canonicalProgressEventKey(ev);
    if (!key) continue;
    const existingIndex = seen.get(key);
    if (existingIndex !== undefined) {
      events[existingIndex] = { ...events[existingIndex], ...ev, raw: { ...events[existingIndex].raw, ...ev.raw } };
      continue;
    }
    seen.set(key, events.length);
    events.push(ev);
  }
  return {
    ...nextProgress,
    startedAt: nextProgress.startedAt ?? previousProgress.startedAt,
    elapsedSeconds: mergedElapsed,
    events
  };
}

function canonicalProgressEventKey(event: SurfaceArtifact): string {
  const raw = event.raw && typeof event.raw === 'object' && !Array.isArray(event.raw) ? event.raw : {};
  const progressItem = raw.progress_item && typeof raw.progress_item === 'object' && !Array.isArray(raw.progress_item)
    ? raw.progress_item as Record<string, unknown>
    : {};
  return String(progressItem.item_id ?? raw.progress_item_id ?? event.id ?? '');
}
