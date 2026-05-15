import { describe, expect, it } from 'vitest';
import type { PmaRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import type { PmaCard } from '$lib/viewModels/pmaChat';
import { ReadModelEntityStore, type ReadModelEntityState } from './readModelStore';

const runProfile = process.env.RUN_WEB_STORE_PROFILE === '1';
const describeProfile = runProfile ? describe : describe.skip;
const now = '2026-05-15T12:00:00Z';

describeProfile('manual read model store profile', () => {
  it('measures active-chat update cost with many cached transcripts', () => {
    const store = new ReadModelEntityStore();
    const chatCount = numberFromEnv('WEB_STORE_PROFILE_CHATS', 160);
    const cardsPerChat = numberFromEnv('WEB_STORE_PROFILE_CARDS_PER_CHAT', 120);
    const iterations = numberFromEnv('WEB_STORE_PROFILE_ITERATIONS', 80);

    for (let chatIndex = 0; chatIndex < chatCount; chatIndex += 1) {
      store.replacePmaTranscript(`chat-${chatIndex}`, cardsForChat(`chat-${chatIndex}`, cardsPerChat));
    }

    const legacySamples: number[] = [];
    const seededState = store.snapshot();
    for (let i = 0; i < iterations; i += 1) {
      const start = performance.now();
      legacyDeepCloneState(seededState);
      legacySamples.push(performance.now() - start);
    }

    const samples: number[] = [];
    for (let i = 0; i < iterations; i += 1) {
      const start = performance.now();
      store.setPmaProgress('chat-0', progress(i));
      samples.push(performance.now() - start);
    }

    samples.sort((left, right) => left - right);
    legacySamples.sort((left, right) => left - right);
    const p50 = percentile(samples, 0.5);
    const p95 = percentile(samples, 0.95);
    const legacyP95 = percentile(legacySamples, 0.95);
    const payload = {
      chatCount,
      cardsPerChat,
      iterations,
      transcriptCards: chatCount * cardsPerChat,
      p50Ms: round(p50),
      p95Ms: round(p95),
      maxMs: round(samples[samples.length - 1] ?? 0),
      legacyDeepCloneP95Ms: round(legacyP95),
      p95Improvement: legacyP95 > 0 ? round(legacyP95 / Math.max(p95, 0.001)) : null
    };
    console.info(`WEB_STORE_PROFILE ${JSON.stringify(payload)}`);
    expect(p95).toBeLessThan(8);
  }, 30_000);
});

function numberFromEnv(name: string, fallback: number): number {
  const parsed = Number.parseInt(process.env[name] ?? '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function legacyDeepCloneState(state: ReadModelEntityState): ReadModelEntityState {
  return {
    ...state,
    cursors: { ...state.cursors },
    chats: { ...state.chats },
    chatOrder: [...state.chatOrder],
    chatGroups: { ...state.chatGroups },
    chatGroupOrder: [...state.chatGroupOrder],
    chatCounters: { ...state.chatCounters },
    chatDetails: legacyCloneRecord(state.chatDetails),
    timelines: legacyCloneRecord(state.timelines),
    pmaTranscripts: legacyCloneRecord(state.pmaTranscripts),
    pmaProgress: { ...state.pmaProgress },
    pmaQueues: legacyCloneRecord(state.pmaQueues),
    pmaArtifacts: legacyCloneRecord(state.pmaArtifacts),
    readMarkers: { ...state.readMarkers },
    artifacts: { ...state.artifacts },
    repos: { ...state.repos },
    repoOrder: [...state.repoOrder],
    worktrees: { ...state.worktrees },
    worktreeOrder: [...state.worktreeOrder],
    runtime: { ...state.runtime },
    tickets: { ...state.tickets },
    ticketSummaries: { ...state.ticketSummaries },
    ticketOrderByOwner: legacyCloneRecord(state.ticketOrderByOwner),
    ticketSiblings: legacyCloneRecord(state.ticketSiblings),
    runs: { ...state.runs },
    pmaRuns: { ...state.pmaRuns },
    pmaRunOrderByOwner: legacyCloneRecord(state.pmaRunOrderByOwner),
    repoDetails: legacyCloneRecord(state.repoDetails),
    worktreeDetails: legacyCloneRecord(state.worktreeDetails),
    agents: { ...state.agents },
    models: { ...state.models },
    optimistic: { ...state.optimistic },
    versions: {
      chat: { ...state.versions.chat },
      chatGroup: { ...state.versions.chatGroup },
      timeline: { ...state.versions.timeline },
      repo: { ...state.versions.repo },
      worktree: { ...state.versions.worktree },
      ticket: { ...state.versions.ticket },
      run: { ...state.versions.run },
      artifact: { ...state.versions.artifact },
      agent: { ...state.versions.agent },
      model: { ...state.versions.model }
    }
  };
}

function legacyCloneRecord<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).map(([key, value]) => [key, structuredClone(value)]));
}

function percentile(samples: number[], p: number): number {
  if (!samples.length) return 0;
  const index = Math.min(samples.length - 1, Math.max(0, Math.ceil(samples.length * p) - 1));
  return samples[index] ?? 0;
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function cardsForChat(chatId: string, count: number): PmaCard[] {
  const cards: PmaCard[] = [];
  for (let index = 0; index < count; index += 1) {
    cards.push({
      kind: 'message',
      id: `${chatId}:turn:${index}:assistant`,
      turnId: `${index}`,
      orderKey: String(index).padStart(8, '0'),
      timestamp: now,
      message: {
        id: `${chatId}:message:${index}`,
        chatId,
        role: 'assistant',
        text: `Synthetic transcript row ${index} `.repeat(20),
        createdAt: now,
        status: null,
        artifacts: [],
        raw: {}
      }
    });
  }
  return cards;
}

function progress(index: number): PmaRunProgress {
  const events: SurfaceArtifact[] = [];
  for (let i = 0; i < 8; i += 1) {
    events.push({
      id: `event-${index}-${i}`,
      kind: 'progress',
      title: `Event ${i}`,
      summary: null,
      url: null,
      createdAt: now,
      raw: {}
    });
  }
  return {
    id: `turn-${index}`,
    chatId: 'chat-0',
    status: 'running',
    workStatus: 'running',
    operatorStatus: null,
    streamShouldClose: false,
    streamCloseReason: null,
    terminal: false,
    phase: null,
    guidance: null,
    queueDepth: 0,
    elapsedSeconds: index,
    startedAt: now,
    idleSeconds: null,
    lastEventId: index,
    lastEventAt: now,
    progressPercent: null,
    events,
    raw: {}
  };
}
