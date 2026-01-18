import { TerminalManager } from "./terminalManager.js";

let terminalManager: TerminalManager | null = null;

export function getTerminalManager(): TerminalManager | null {
  return terminalManager;
}

export function initTerminal(): void {
  if (terminalManager) {
    return;
  }
  terminalManager = new TerminalManager();
  terminalManager.init();
}
