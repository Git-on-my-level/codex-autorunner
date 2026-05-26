import type { ReadModelStreamStatus } from '$lib/data/readModelStream';

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'interrupted' | 'offline';

type StreamInput = ReadModelStreamStatus | 'idle';

const state = $state<{ status: ConnectionStatus }>({ status: 'idle' });

let listenersAttached = false;
function attachBrowserListeners(): void {
  if (listenersAttached || typeof window === 'undefined') return;
  listenersAttached = true;
  window.addEventListener('online', () => {
    if (state.status === 'offline') state.status = 'idle';
  });
  window.addEventListener('offline', () => {
    state.status = 'offline';
  });
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    state.status = 'offline';
  }
}

function project(input: StreamInput): ConnectionStatus {
  if (input === 'closed') return 'idle';
  return input;
}

export const connectionStore = {
  get status(): ConnectionStatus {
    attachBrowserListeners();
    return state.status;
  },
  setStreamStatus(next: StreamInput): void {
    attachBrowserListeners();
    if (state.status === 'offline') return;
    state.status = project(next);
  },
  reset(): void {
    if (state.status === 'offline') return;
    state.status = 'idle';
  }
};
