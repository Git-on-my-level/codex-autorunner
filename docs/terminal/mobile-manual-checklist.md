# Terminal Mobile Manual Checklist

- **Touch scroll**: on iOS Safari + Android Chrome, 1-finger swipe inside the terminal scrolls xterm scrollback (and doesn't “fight” the page scroll).
- **Jump to bottom**: scroll up and verify the `↓ Latest` button appears; tap it and confirm it jumps to the bottom and hides again.
- **Text composer**: enable Terminal “Text input”, type a draft, and confirm the composer sticks to the bottom while you scroll and stays above the on-screen keyboard.
- **Draft persistence**: type text, reload the page, and confirm the draft restores from local storage.
- **Flaky send**: with the terminal connected, toggle network off/on and click Send; confirm the draft isn't cleared until the UI receives a send acknowledgement, and it retries after reconnect.
- **Voice transcript editing**: on a touch device, record a transcript and confirm it lands in the text input (editable) rather than being sent immediately.

