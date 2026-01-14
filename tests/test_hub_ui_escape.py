import subprocess
import textwrap
from pathlib import Path


def test_hub_ui_escapes_repo_fields():
    hub_js = Path("src/codex_autorunner/static/hub.js").resolve()
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
            this._innerHTML = "";
            this.children = [];
            this.dataset = {{}};
            this.style = {{}};
            this.className = "";
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

          get innerHTML() {{
            return this._innerHTML;
          }}

          set innerHTML(value) {{
            this._innerHTML = value;
            if (value === "") {{
              this.children = [];
            }}
          }}

          appendChild(child) {{
            this.children.push(child);
            return child;
          }}

          removeChild(child) {{
            const idx = this.children.indexOf(child);
            if (idx >= 0) {{
              this.children.splice(idx, 1);
            }}
            return child;
          }}

          setAttribute(name, value) {{
            this[name] = value;
          }}

          querySelector() {{
            return null;
          }}

          addEventListener() {{
            // stub - no-op
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
          location: {{ pathname: "/" }},
        }};

        globalThis.sessionStorage = {{
          getItem: () => null,
          setItem: () => {{}},
          removeItem: () => {{}},
        }};

        const moduleUrl = pathToFileURL("{hub_js.as_posix()}").href;
        const mod = await import(moduleUrl);
        const helpers = mod.__hubTest;

        const malicious = '"><img src=x onerror=alert(1)>';
        const repo = {{
          id: malicious,
          display_name: malicious,
          status: malicious,
          initialized: true,
          exists_on_disk: true,
          mounted: false,
          kind: "base",
          last_run_id: null,
          last_exit_code: null,
          last_run_finished_at: null,
          last_run_started_at: null,
        }};

        helpers.renderRepos([repo]);

        const repoList = document.getElementById("hub-repo-list");
        assert.equal(repoList.children.length, 1);
        const card = repoList.children[0];
        assert.ok(!card.innerHTML.includes(malicious));
        assert.ok(card.innerHTML.includes("&lt;img"));
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)
