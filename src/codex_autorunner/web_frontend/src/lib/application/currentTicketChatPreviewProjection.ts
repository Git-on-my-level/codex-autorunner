import {
  openChatTranscriptEventSource,
  type ChatTranscriptStreamEvent,
  type StreamSubscription,
  type TranscriptStreamOptions
} from '$lib/api/streaming';

export type CurrentTicketChatPreviewRole = 'user' | 'assistant' | 'intermediate';
export type CurrentTicketChatPreviewStreamState = 'idle' | 'connecting' | 'connected' | 'interrupted';

export type CurrentTicketChatPreviewState = {
  targetChatId: string | null;
  latestText: string;
  latestRole: CurrentTicketChatPreviewRole | null;
  streamState: CurrentTicketChatPreviewStreamState;
};

export type CurrentTicketChatPreviewProjectionDeps = {
  onStateChange?: (state: CurrentTicketChatPreviewState) => void;
  openStream?: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
};

const IDLE_STATE: CurrentTicketChatPreviewState = {
  targetChatId: null,
  latestText: '',
  latestRole: null,
  streamState: 'idle'
};

export class CurrentTicketChatPreviewProjection {
  private readonly onStateChange: ((state: CurrentTicketChatPreviewState) => void) | undefined;
  private readonly openStream: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
  private state: CurrentTicketChatPreviewState = { ...IDLE_STATE };
  private subscription: StreamSubscription | null = null;

  constructor(deps: CurrentTicketChatPreviewProjectionDeps = {}) {
    this.onStateChange = deps.onStateChange;
    this.openStream = deps.openStream ?? openChatTranscriptEventSource;
  }

  snapshot(): CurrentTicketChatPreviewState {
    return { ...this.state };
  }

  activate(chatId: string | null | undefined): void {
    const nextChatId = chatId || null;
    if (!nextChatId) {
      this.destroy();
      return;
    }
    if (this.state.targetChatId === nextChatId) return;

    this.closeStream();
    this.setState({
      targetChatId: nextChatId,
      latestText: '',
      latestRole: null,
      streamState: 'connecting'
    });
    this.subscription = this.openStream(nextChatId, {
      onStatus: (status) => this.handleStreamStatus(nextChatId, status),
      onEvent: (event) => this.handleStreamEvent(nextChatId, event),
      onError: () => this.handleStreamError(nextChatId)
    });
  }

  destroy(): void {
    this.closeStream();
    this.setState({ ...IDLE_STATE });
  }

  private handleStreamStatus(
    chatId: string,
    status: 'connecting' | 'connected' | 'interrupted' | 'closed'
  ): void {
    if (this.state.targetChatId !== chatId) return;
    if (status === 'connecting' && this.state.streamState !== 'connected') {
      this.setState({ streamState: 'connecting' });
    } else if (status === 'connected') {
      this.setState({ streamState: 'connected' });
    } else if (status === 'interrupted') {
      this.setState({ streamState: 'interrupted' });
    }
  }

  private handleStreamEvent(chatId: string, event: ChatTranscriptStreamEvent): void {
    if (this.state.targetChatId !== chatId) return;
    this.setState({ streamState: 'connected' });
    if (event.kind !== 'transcript_snapshot' && event.kind !== 'transcript_append') return;

    for (const row of transcriptRows(event.payload).reverse()) {
      const preview = rowPreview(row);
      if (!preview) continue;
      this.setState({
        latestText: preview.text,
        latestRole: preview.role
      });
      return;
    }
  }

  private handleStreamError(chatId: string): void {
    if (this.state.targetChatId !== chatId) return;
    this.setState({ streamState: 'interrupted' });
  }

  private closeStream(): void {
    const subscription = this.subscription;
    this.subscription = null;
    subscription?.close();
  }

  private setState(next: Partial<CurrentTicketChatPreviewState>): void {
    const merged = { ...this.state, ...next };
    if (
      merged.targetChatId === this.state.targetChatId &&
      merged.latestText === this.state.latestText &&
      merged.latestRole === this.state.latestRole &&
      merged.streamState === this.state.streamState
    ) {
      return;
    }
    this.state = merged;
    this.onStateChange?.(this.snapshot());
  }
}

export function createCurrentTicketChatPreviewProjection(
  deps: CurrentTicketChatPreviewProjectionDeps = {}
): CurrentTicketChatPreviewProjection {
  return new CurrentTicketChatPreviewProjection(deps);
}

function transcriptRows(payload: Record<string, unknown>): Record<string, unknown>[] {
  const rows = payload.rows;
  return Array.isArray(rows)
    ? rows.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object' && !Array.isArray(row))
    : [];
}

function rowPreview(row: Record<string, unknown>): { text: string; role: CurrentTicketChatPreviewRole } | null {
  const kind = String(row.kind ?? '');
  if (kind === 'message') {
    const message = row.message;
    if (!message || typeof message !== 'object' || Array.isArray(message)) return null;
    const text = String((message as Record<string, unknown>).text ?? '').trim();
    if (!text) return null;
    const role = String((message as Record<string, unknown>).role ?? '') === 'user' ? 'user' : 'assistant';
    return { text, role };
  }
  if (
    kind === 'intermediate' ||
    kind === 'tool_group' ||
    kind === 'approval' ||
    kind === 'lifecycle' ||
    kind === 'context_compaction'
  ) {
    const text = String(row.text ?? row.summary ?? row.title ?? '').trim();
    return text ? { text, role: 'intermediate' } : null;
  }
  return null;
}
