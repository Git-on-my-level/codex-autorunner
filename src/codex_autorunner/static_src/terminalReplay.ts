const textEncoder = new TextEncoder();
const ALT_SCREEN_ENTER = "\x1b[?1049h";
const ALT_SCREEN_ENTER_BYTES = textEncoder.encode(ALT_SCREEN_ENTER);
const ALT_SCREEN_ENTER_SEQUENCES = [
  ALT_SCREEN_ENTER,
  "\x1b[?47h",
  "\x1b[?1047h",
];
const ALT_SCREEN_ENTER_MAX_LEN = ALT_SCREEN_ENTER_SEQUENCES.reduce(
  (max, seq) => Math.max(max, seq.length),
  0
);

export interface ReplayFlush {
  hasReplay: boolean;
  chunks: Uint8Array[];
  prelude: Uint8Array | null;
  shouldApplyPrelude: boolean;
  hasAltScreenEnter: boolean;
}

export interface ReplayState {
  awaitingReplayEnd: boolean;
  replayBuffer: Uint8Array[] | null;
  replayPrelude: Uint8Array | null;
  pendingReplayPrelude: Uint8Array | null;
  clearTranscriptOnFirstLiveData: boolean;
}

export function createReplayState(): ReplayState {
  return {
    awaitingReplayEnd: false,
    replayBuffer: null,
    replayPrelude: null,
    pendingReplayPrelude: null,
    clearTranscriptOnFirstLiveData: false,
  };
}

export function resetReplayState(state: ReplayState): void {
  state.awaitingReplayEnd = false;
  state.replayBuffer = null;
  state.replayPrelude = null;
  state.pendingReplayPrelude = null;
  state.clearTranscriptOnFirstLiveData = false;
}

export function initReplayForConnect(
  state: ReplayState,
  shouldAwaitReplay: boolean,
  transcriptResetForConnect: boolean
): void {
  state.awaitingReplayEnd = shouldAwaitReplay;
  state.replayBuffer = shouldAwaitReplay ? [] : null;
  state.replayPrelude = null;
  state.pendingReplayPrelude = null;
  state.clearTranscriptOnFirstLiveData = false;
  if (!shouldAwaitReplay && !transcriptResetForConnect) {
    state.clearTranscriptOnFirstLiveData = true;
  }
}

export function isAltScreenEnterChunk(chunk: Uint8Array): boolean {
  if (!chunk || chunk.length !== ALT_SCREEN_ENTER_BYTES.length) return false;
  for (let idx = 0; idx < ALT_SCREEN_ENTER_BYTES.length; idx++) {
    if (chunk[idx] !== ALT_SCREEN_ENTER_BYTES[idx]) return false;
  }
  return true;
}

export function replayHasAltScreenEnter(chunks: Uint8Array[]): boolean {
  if (!Array.isArray(chunks) || chunks.length === 0) return false;
  const decoder = new TextDecoder();
  const maxTail = Math.max(ALT_SCREEN_ENTER_MAX_LEN - 1, 0);
  let tail = "";
  for (const chunk of chunks) {
    const text = decoder.decode(chunk, { stream: true });
    if (!text) continue;
    const combined = tail + text;
    for (const seq of ALT_SCREEN_ENTER_SEQUENCES) {
      if (combined.includes(seq)) return true;
    }
    tail = maxTail ? combined.slice(-maxTail) : "";
  }
  if (!tail) return false;
  return ALT_SCREEN_ENTER_SEQUENCES.some((seq) => tail.includes(seq));
}

export function bufferReplayChunk(
  state: ReplayState,
  chunk: Uint8Array
): void {
  if (!state.awaitingReplayEnd) return;
  const replayEmpty =
    Array.isArray(state.replayBuffer) && state.replayBuffer.length === 0;
  if (!state.replayPrelude && replayEmpty && isAltScreenEnterChunk(chunk)) {
    state.replayPrelude = chunk;
    return;
  }
  state.replayBuffer?.push(chunk);
}

export function handleReplayEnd(
  state: ReplayState,
  transcriptResetForConnect: boolean,
  _altScrollbackLength: number
): ReplayFlush | null {
  if (!state.awaitingReplayEnd) return null;

  const buffered = Array.isArray(state.replayBuffer) ? state.replayBuffer : [];
  const prelude = state.replayPrelude;
  const hasReplay = buffered.length > 0;
  const hasAlt = hasReplay && replayHasAltScreenEnter(buffered);
  const shouldApplyPrelude = Boolean(prelude && !hasAlt);

  state.awaitingReplayEnd = false;
  state.replayBuffer = null;
  state.replayPrelude = null;

  if (hasReplay) {
    return {
      hasReplay: true,
      chunks: buffered,
      prelude,
      shouldApplyPrelude,
      hasAltScreenEnter: hasAlt,
    };
  }

  state.clearTranscriptOnFirstLiveData = !transcriptResetForConnect;
  state.pendingReplayPrelude = shouldApplyPrelude ? prelude : null;
  return {
    hasReplay: false,
    chunks: [],
    prelude,
    shouldApplyPrelude,
    hasAltScreenEnter: hasAlt,
  };
}

export function consumeLiveReset(state: ReplayState): {
  shouldReset: boolean;
  hadPrelude: boolean;
  prelude: Uint8Array | null;
} {
  if (!state.clearTranscriptOnFirstLiveData) {
    return { shouldReset: false, hadPrelude: false, prelude: null };
  }
  state.clearTranscriptOnFirstLiveData = false;
  const hadPrelude = Boolean(state.pendingReplayPrelude);
  const prelude = state.pendingReplayPrelude;
  state.pendingReplayPrelude = null;
  return { shouldReset: true, hadPrelude, prelude };
}
