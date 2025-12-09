import { api, flash } from "./utils.js";
import { loadState } from "./state.js";
import { publish } from "./bus.js";

const docButtons = document.querySelectorAll(".chip[data-doc]");
let docsCache = {
  todo: "",
  progress: "",
  opinions: "",
};
let activeDoc = "todo";

function renderTodoPreview(text) {
  const list = document.getElementById("todo-preview-list");
  list.innerHTML = "";
  const lines = text.split("\n").map((l) => l.trim());
  const todos = lines.filter((l) => l.startsWith("- [")).slice(0, 8);
  if (todos.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No TODO items found.";
    list.appendChild(li);
    return;
  }
  todos.forEach((line) => {
    const li = document.createElement("li");
    const box = document.createElement("div");
    box.className = "box";
    const done = line.toLowerCase().startsWith("- [x]");
    if (done) box.classList.add("done");
    const textSpan = document.createElement("span");
    textSpan.textContent = line.substring(5).trim();
    li.appendChild(box);
    li.appendChild(textSpan);
    list.appendChild(li);
  });
}

async function loadDocs() {
  try {
    const data = await api("/api/docs");
    docsCache = { ...docsCache, ...data };
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    document.getElementById("doc-status").textContent = "Loaded";
    publish("docs:loaded", docsCache);
  } catch (err) {
    flash(err.message);
  }
}

function setDoc(kind) {
  activeDoc = kind;
  docButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.doc === kind));
  const textarea = document.getElementById("doc-content");
  textarea.value = docsCache[kind] || "";
  document.getElementById("doc-status").textContent = `Editing ${kind.toUpperCase()}`;
}

async function saveDoc() {
  const content = document.getElementById("doc-content").value;
  const saveBtn = document.getElementById("save-doc");
  saveBtn.disabled = true;
  try {
    await api(`/api/docs/${activeDoc}`, { method: "PUT", body: { content } });
    docsCache[activeDoc] = content;
    flash(`${activeDoc.toUpperCase()} saved`);
    publish("docs:updated", { kind: activeDoc, content });
    if (activeDoc === "todo") {
      renderTodoPreview(content);
      await loadState({ notify: false });
    }
  } catch (err) {
    flash(err.message);
  } finally {
    saveBtn.disabled = false;
  }
}

export function initDocs() {
  docButtons.forEach((btn) =>
    btn.addEventListener("click", () => {
      setDoc(btn.dataset.doc);
    })
  );
  document.getElementById("save-doc").addEventListener("click", saveDoc);
  document.getElementById("reload-doc").addEventListener("click", loadDocs);
  document.getElementById("refresh-preview").addEventListener("click", loadDocs);

  loadDocs();
}
