export interface XtermTerminal {
  open(parent: HTMLElement): void;
  dispose(): void;
  onData(cb: (data: string) => void): { dispose(): void };
  onBinary(cb: (data: string) => void): { dispose(): void };
  onResize(cb: (size: { cols: number; rows: number }) => void): { dispose(): void };
  onScroll(cb: (newPos: number) => void): { dispose(): void };
  onRender(cb: (event: { start: number; end: number }) => void): { dispose(): void };
  write(data: string | Uint8Array, callback?: () => void): void;
  resize(cols: number, rows: number): void;
  focus(): void;
  scrollToBottom(): void;
  scrollLines(amount: number): void;
  reset(): void;
  clear(): void;
  buffer: {
    active: {
      type: string;
      baseY: number;
      viewportY: number;
      length: number;
      cursorY: number;
      lines: {
        get(index: number): { translateToString(trimRight?: boolean): string } | undefined;
        length: number;
      };
      getLine(y: number): { translateToString(trimRight?: boolean): string } | undefined;
      cols: number;
    };
    alternate: {
      type: string;
      baseY: number;
      viewportY: number;
    };
  };
  element: HTMLElement | null;
  textarea: HTMLTextAreaElement | null;
  cols: number;
  rows: number;
  options: XtermTerminalOptions;
  modes: {
    mouseTrackingMode: string;
  };
  _core: unknown;
  loadAddon(addon: { dispose(): void }): void;
}

export interface XtermTerminalOptions {
  scrollback?: number;
  fontSize?: number;
  scrollSensitivity?: number;
  fastScrollSensitivity?: number;
  fastScrollModifier?: string;
  [key: string]: unknown;
}

export interface XtermFitAddon {
  fit(): void;
  dispose(): void;
}

export interface TranscriptCell {
  t: string;
  c: string;
  s?: string;
}

export interface TranscriptAnsiState {
  mode: string;
  oscEsc: boolean;
  csiParams: string;
  fg: string | null;
  bg: string | null;
  fgRgb: string | null;
  bgRgb: string | null;
  bold: boolean;
  className: string;
  style: string;
}

export interface TextHook {
  id: string;
  apply: (context: { text: string; manager: unknown }) => { text?: string; stop?: boolean } | string | null | Promise<{ text?: string; stop?: boolean } | string | null>;
}

export interface PendingTextInput {
  id: string;
  payload: string;
  originalText: string;
  sentAt: number;
  lastRetryAt: number | null;
  sendEnter: boolean;
  chunkSize: number;
  chunkIndex: number;
  chunkIds: string[] | null;
  inFlightId: string | null;
  totalBytes: number | null;
}

export interface VoiceController {
  start(): void;
  stop(): void;
  cleanup?(): void;
}
