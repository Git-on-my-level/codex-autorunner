export const CONSTANTS = {
  UI: {
    TOAST_DURATION: 5000,
    POLLING_INTERVAL: 15000,
    LOG_SCROLL_THRESHOLD: 50,
    MAX_LOG_LINES_IN_DOM: 2000,
    MAX_LOG_LINES_IN_MEMORY: 10000,
    LOG_PAGE_SIZE: 500,
    DOC_CHAT_EVENT_LIMIT: 12,
    AUTO_REFRESH_INTERVAL: 30000,
    AUTO_REFRESH_USAGE_INTERVAL: 60000,
  },
  THEME: {
    XTERM: {
      background: '#0a0c12',
      foreground: '#e5ecff',
      cursor: '#6cf5d8',
      selectionBackground: 'rgba(108, 245, 216, 0.3)',
      black: '#000000',
      red: '#ff5566',
      green: '#6cf5d8',
      yellow: '#f1fa8c',
      blue: '#6ca8ff',
      magenta: '#bd93f9',
      cyan: '#8be9fd',
      white: '#e5ecff',
      brightBlack: '#6272a4',
      brightRed: '#ff6e6e',
      brightGreen: '#69ff94',
      brightYellow: '#ffffa5',
      brightBlue: '#d6acff',
      brightMagenta: '#ff92df',
      brightCyan: '#a4ffff',
      brightWhite: '#ffffff',
    }
  },
  PROMPTS: {
    VOICE_TRANSCRIPT_DISCLAIMER:
      "Note: transcribed from user voice. If confusing or possibly inaccurate and you cannot infer the intention please clarify before proceeding.",
  },
  API: {
    TERMINAL_ENDPOINT: "/api/terminal",
    TERMINAL_IMAGE_ENDPOINT: "/api/terminal/image",
  }
} as const;
