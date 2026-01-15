import subprocess
import textwrap
from pathlib import Path


def test_doc_chat_ui_stream_flow():
    docs_js = Path("src/codex_autorunner/static/docs.js").resolve()
    script = textwrap.dedent(
        f"""
        import assert from "node:assert";
        import {{ pathToFileURL }} from "node:url";

        class StubElement {{
          constructor(id) {{
            this.id = id;
            this.textContent = "";
            this.value = "";
            this.disabled = false;
            this.innerHTML = "";
            this.children = [];
            this.style = {{}};
            this.classList = {{
              classes: new Set(),
              add: (...cls) => cls.forEach((c) => this.classList.classes.add(c)),
              remove: (...cls) => cls.forEach((c) => this.classList.classes.delete(c)),
              toggle: (cls, force) => {{
                if (force === undefined) {{
                  if (this.classList.classes.has(cls)) {{
                    this.classList.classes.delete(cls);
                    return false;
                  }}
                  this.classList.classes.add(cls);
                  return true;
                }}
                if (force) {{
                  this.classList.classes.add(cls);
                }} else {{
                  this.classList.classes.delete(cls);
                }}
                return force;
              }},
              contains: (cls) => this.classList.classes.has(cls),
            }};
          }}

          appendChild(child) {{
            this.children.push(child);
            return child;
          }}

          setAttribute(_name, _value) {{
            // stub - do nothing
          }}

          addEventListener(event, handler) {{
            // stub - do nothing
          }}
        }}

        const elements = new Map();
        const getEl = (id) => {{
          if (!elements.has(id)) elements.set(id, new StubElement(id));
          return elements.get(id);
        }};

        globalThis.document = {{
          querySelectorAll: () => [],
          getElementById: (id) => getEl(id),
          createElement: (tag) => new StubElement(tag),
        }};

        globalThis.window = {{
          confirm: () => true,
          prompt: () => "CLEAR",
        }};

        const encoder = new TextEncoder();
        const ssePayload = [
          'event: status',
          'data: {{"status":"running"}}',
          '',
          'event: update',
          'data: {{"status":"ok","agent_message":"Done","updated":["todo"],"drafts":{{"todo":{{"patch":"--- a/.codex-autorunner/TODO.md\\\\n+++ b/.codex-autorunner/TODO.md\\\\n@@\\\\n- [ ] first\\\\n+ [ ] streamed task","content":"- [ ] streamed task","agent_message":"Done"}}}}}}',
          '',
          'event: done',
          'data: {{"status":"ok"}}',
          '',
        ].join("\\n");

        globalThis.fetch = async (url, options = {{}}) => {{
          const urlStr = String(url);
          if (urlStr.includes("/api/docs/") && urlStr.endsWith("/chat")) {{
            return {{
              ok: true,
              headers: {{ get: () => "text/event-stream" }},
              body: {{
                getReader() {{
                  let sent = false;
                  return {{
                    async read() {{
                      if (sent) return {{ done: true }};
                      sent = true;
                      return {{ done: false, value: encoder.encode(ssePayload) }};
                    }},
                  }};
                }},
              }},
            }};
          }}
          if (urlStr.includes("/chat/apply")) {{
            return {{
              ok: true,
              headers: {{ get: () => "application/json" }},
              json: async () => ({{
                status: "ok",
                content: "- [ ] streamed task",
                agent_message: "Done",
              }}),
              text: async () => "{{}}",
            }};
          }}
          if (urlStr.includes("/chat/discard")) {{
            return {{
              ok: true,
              headers: {{ get: () => "application/json" }},
              json: async () => ({{ status: "ok" }}),
              text: async () => "{{}}",
            }};
          }}
          return {{
            ok: true,
            headers: {{ get: () => "application/json" }},
            json: async () => ({{ status: "idle" }}),
            text: async () => "{{}}",
          }};
        }};

        const moduleUrl = pathToFileURL("{docs_js.as_posix()}").href;
        const mod = await import(moduleUrl);
        const helpers = mod.__docChatTest;

        const textarea = document.getElementById("doc-content");
        textarea.value = "";
        const state = helpers.getChatState();
        state.status = "running";
        state.controller = new AbortController();
        const entry = {{
          id: "1",
          prompt: "rewrite",
          targets: ["todo"],
          response: "",
          status: "running",
          time: Date.now(),
          drafts: {{}},
          updated: [],
        }};
        state.history.unshift(entry);

        await helpers.performDocChatRequest(entry, state);
        assert.equal(
          document.getElementById("doc-patch-body").innerHTML.includes("streamed task"),
          true
        );
        assert.equal(textarea.value.trim(), "");
        await helpers.applyPatch("todo");
        state.status = entry.status === "error" ? "error" : "idle";
        helpers.renderChat();

        assert.equal(entry.status, "done");
        assert.equal(state.streamText.trim(), "Done");
        assert.equal(textarea.value.trim(), "- [ ] streamed task");
        assert.equal(document.getElementById("doc-status").textContent, "Editing TODO");
        assert.ok(
          (document.getElementById("doc-chat-response").textContent || "").includes("Done")
        );
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)
