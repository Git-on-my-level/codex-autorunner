import { TerminalManager } from "./terminalManager.js";

// Singleton instance of the terminal manager
let terminalManager = null;

/**
 * Initialize the terminal panel.
 * Creates a TerminalManager instance and initializes all terminal functionality.
 */
export function initTerminal() {
  if (terminalManager) {
    // Already initialized
    return;
  }
  terminalManager = new TerminalManager();
  terminalManager.init();
}
