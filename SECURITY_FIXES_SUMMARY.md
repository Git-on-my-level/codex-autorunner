# Security Vulnerability Fixes Summary

This document summarizes the fixes for process management and memory leak vulnerabilities.

## 1. Fixed Orphaned Subprocesses (runner_process.py:22-29)

**Issue:** `spawn_detached()` returned Popen object but caller didn't store it. No cleanup mechanism for spawned processes.

**Changes:**
- Added `_process_registry: Set[subprocess.Popen]` to track all spawned processes
- Modified `spawn_detached()` to register spawned processes in the registry
- Added `cleanup_processes()` function that:
  - Iterates through registered processes
  - Calls `terminate()` first, waits up to 5 seconds
  - If process doesn't terminate, calls `kill()` as fallback
  - Clears the registry after cleanup
- Registered `cleanup_processes()` with `atexit` to ensure cleanup on program exit

**File:** `src/codex_autorunner/core/runner_process.py:1-47`

## 2. Fixed WebSocket Memory Leak (terminalManager.ts:2140-2159)

**Issue:** `_teardownSocket()` set `this.socket = null` but didn't clear `reconnectTimer`. If teardown called while reconnect pending, timer remained active.

**Changes:**
- Modified `_teardownSocket()` to clear `reconnectTimer` before cleaning up socket
- Added check and cleanup of `reconnectTimer` at the start of the method

**File:** `src/codex_autorunner/static_src/terminalManager.ts:2140-2161`

## 3. Reconnection Logic Already Fixed (terminalManager.ts:2314-2319)

**Issue:** Reconnection logic had no timeout or retry limit (mentioned in issue).

**Status:** Already implemented correctly in the codebase:
- Max retry limit: 3 attempts (line 2577)
- Exponential backoff: `Math.min(1000 * Math.pow(2, this.reconnectAttempts), 8000)` with 8 second cap (line 2578)
- "Disconnected (max retries reached)" status message (line 2586)

**No changes needed.**

## 4. Fixed xterm Data Handler Leak (terminalManager.ts:2007-2019)

**Issue:** `this.inputDisposable = this.term.onData(...)` never cleaned up.

**Changes:**
- Modified `disconnect()` method to dispose `inputDisposable`
- Added disposal of `inputDisposable` with null check
- Ensures proper cleanup when disconnecting

**File:** `src/codex_autorunner/static_src/terminalManager.ts:2606-2616`

## 5. Fixed Event Listener and MediaRecorder Leaks (voice.ts:232-244, 491-507)

**Issue:** Multiple event listeners added but cleanup not exposed. MediaRecorder and AudioContext resources not properly cleaned up.

**Changes:**
- Added `cleanup` method to `VoiceInputAPI` interface
- Stored references to all event listener functions:
  - `startHandler`, `endHandler`, `pointerLeaveHandler`, `pointerCancelHandler`, `clickHandler`
- Created `cleanup()` function that:
  - Removes all event listeners from the button
  - Calls `cleanupRecorder(state)` to clean up MediaRecorder, AudioContext, AnalyserNode, and animation frame
  - Calls `cleanupStream(state)` to clean up MediaStream
- Added `cleanup` to the return object of `initVoiceInput()`
- Updated `VoiceController` interface in terminalManager.ts to include optional `cleanup()` method
- Modified `disconnect()` method in terminalManager.ts to call `cleanup()` on all voice controllers

**Files:**
- `src/codex_autorunner/static_src/voice.ts:95-102` (interface update)
- `src/codex_autorunner/static_src/voice.ts:219-261` (listener storage and cleanup function)
- `src/codex_autorunner/static_src/voice.ts:563-569` (return object update)
- `src/codex_autorunner/static_src/terminalManager.ts:196-199` (VoiceController interface update)
- `src/codex_autorunner/static_src/terminalManager.ts:2610-2614` (disconnect method update)

## Testing

- Python code passes ruff checks: `All checks passed!`
- TypeScript changes follow existing code patterns
- All cleanup functions properly dispose resources and remove event listeners
- Process registry ensures spawned processes are cleaned up on shutdown

## Summary of Files Modified

1. `src/codex_autorunner/core/runner_process.py` - Process registry and cleanup
2. `src/codex_autorunner/static_src/terminalManager.ts` - Socket teardown, inputDisposable disposal, voice controller cleanup
3. `src/codex_autorunner/static_src/voice.ts` - Event listener cleanup and cleanup API exposure
