export function renderTodoPreview(text: string): void {
  const list = document.getElementById("todo-preview-list");
  if (!list) return;
  list.innerHTML = "";
  const lines = (text || "").split("\n").map((l) => l.trim());
  const todos = lines.filter((l) => l.startsWith("- ["));
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
