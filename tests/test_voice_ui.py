import subprocess
import textwrap
from pathlib import Path


def test_voice_ui_transcribe_flow_without_opt_in():
    voice_js = Path("src/codex_autorunner/static/voice.js").resolve()
    script = textwrap.dedent(
        """
        import assert from "node:assert";
        import { pathToFileURL } from "node:url";
        import { setTimeout as delay } from "node:timers/promises";

        class StubClassList {
          constructor() {
            this.classes = new Set();
          }
          add(...cls) { cls.forEach((c) => this.classes.add(c)); }
          remove(...cls) { cls.forEach((c) => this.classes.delete(c)); }
          toggle(cls, force) {
            if (force === undefined) {
              if (this.classes.has(cls)) {
                this.classes.delete(cls);
                return false;
              }
              this.classes.add(cls);
              return true;
            }
            if (force) {
              this.classes.add(cls);
            } else {
              this.classes.delete(cls);
            }
            return force;
          }
          contains(cls) { return this.classes.has(cls); }
        }

        class StubElement {
          constructor(id) {
            this.id = id;
            this.textContent = "";
            this.value = "";
            this.disabled = false;
            this.hidden = true;
            this.children = [];
            this.classList = new StubClassList();
            this.style = {};
            this.attributes = new Map();
            this.events = new Map();
          }
          appendChild(child) { this.children.push(child); return child; }
          addEventListener(event, handler) {
            this.events.set(event, handler);
            const response = globalThis.__confirmResponse;
            if (event === "click") {
              if (this.id === "confirm-modal-ok" && response === true) {
                Promise.resolve().then(() => handler({ target: this }));
              }
              if (this.id === "confirm-modal-cancel" && response === false) {
                Promise.resolve().then(() => handler({ target: this }));
              }
            }
          }
          removeEventListener() {}
          focus() {}
          querySelector() { return null; }
          querySelectorAll() { return []; }
          setAttribute(name, value) { this.attributes.set(name, value); }
          removeAttribute(name) { this.attributes.delete(name); }
          getTracks() { return []; }
        }

        const elements = new Map();
        const getEl = (id) => {
          if (!elements.has(id)) elements.set(id, new StubElement(id));
          return elements.get(id);
        };

        // Elements used by utils.js + confirmModal
        ["toast", "confirm-modal", "confirm-modal-message", "confirm-modal-ok", "confirm-modal-cancel"].forEach(getEl);

        globalThis.__voiceRequests = [];
        globalThis.__recorderIntervals = [];
        globalThis.__tracksStopped = 0;

        globalThis.document = {
          querySelectorAll: () => [],
          getElementById: (id) => getEl(id),
          createElement: (tag) => new StubElement(tag),
          addEventListener: () => {},
          removeEventListener: () => {},
          activeElement: null,
        };

        globalThis.window = {
          location: { protocol: "https:", host: "example.test", pathname: "/app" },
        };

        class StubFormData {
          constructor() {
            this.fields = [];
          }
          append(key, value) {
            this.fields.push([key, value]);
          }
        }

        class StubBlob {
          constructor(parts = [], opts = {} ) {
            this.parts = parts;
            this.type = opts.type || "";
            this.size = parts.reduce(
              (sum, part) => sum + (typeof part === "string" ? part.length : (part && part.size) || 0),
              0
            );
          }
        }

        class StubMediaRecorder {
          constructor(stream, options = {} ) {
            this.stream = stream;
            this.mimeType = (options && options.mimeType) || "audio/webm";
            this.events = new Map();
          }
          addEventListener(event, handler) {
            this.events.set(event, handler);
          }
          start(interval) {
            globalThis.__recorderIntervals.push(interval);
          }
          stop() {
            const dataHandler = this.events.get("dataavailable");
            if (dataHandler) {
              dataHandler({ data: new globalThis.Blob(["voice"]) });
            }
            const stopHandler = this.events.get("stop");
            if (stopHandler) stopHandler();
          }
        }
        StubMediaRecorder.isTypeSupported = () => true;

        globalThis.Blob = StubBlob;
        globalThis.FormData = StubFormData;
        globalThis.MediaRecorder = StubMediaRecorder;

        const nav = {
          mediaDevices: {
            async getUserMedia() {
              return {
                getTracks: () => [
                  {
                    stop() { globalThis.__tracksStopped += 1; },
                  },
                ],
              };
            },
          },
        };
        Object.defineProperty(globalThis, "navigator", { value: nav, configurable: true });
        globalThis.window.navigator = nav;
        globalThis.window.MediaRecorder = StubMediaRecorder;

        globalThis.fetch = async (url, options = {}) => {
          const urlStr = String(url);
          if (urlStr.includes("/api/voice/config")) {
            return {
              ok: true,
              json: async () => ({
                enabled: true,
                provider: "openai_whisper",
                warn_on_remote_api: true,
                chunk_ms: 700,
                sample_rate: 16000,
                latency_mode: "quality",
              }),
            };
          }
          if (urlStr.includes("/api/voice/transcribe")) {
            if (options.body) {
              globalThis.__voiceRequests.push(options.body);
            }
            return { ok: true, json: async () => ({ text: "hello from voice" }) };
          }
          return { ok: false, json: async () => ({}) };
        };

        const moduleUrl = pathToFileURL("__VOICE_JS__").href;
        const mod = await import(moduleUrl);
        const { initVoiceInput } = mod;

        const micBtn = getEl("mic-btn-1");
        const status = getEl("status-1");
        const input = getEl("input-1");
        const errors = [];
        const transcripts = [];

        const controller = await initVoiceInput({
          button: micBtn,
          input,
          statusEl: status,
          onTranscript: (t) => transcripts.push(t),
          onError: (e) => errors.push(e),
        });

        await delay(0);
        await controller.start();
        await delay(0);

        assert.equal(micBtn.classList.contains("voice-recording"), true);

        controller.stop();
        await delay(0);
        await delay(0);

        assert.equal(transcripts[0], "hello from voice");
        assert.equal(status.textContent, "Transcript ready");
        assert.equal(micBtn.classList.contains("voice-recording"), false);
        assert.equal(controller.hasPending(), false);

        const payload = globalThis.__voiceRequests[0];
        assert.equal(globalThis.__recorderIntervals[0], 700);
        assert.equal(globalThis.__tracksStopped > 0, true);
        """
    ).replace("__VOICE_JS__", voice_js.as_posix())

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)
