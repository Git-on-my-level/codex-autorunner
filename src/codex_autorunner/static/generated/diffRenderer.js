// GENERATED FILE - do not edit directly. Source: static_src/
export function renderDiff(patch, container) {
    if (!container)
        return;
    container.innerHTML = "";
    const lines = (patch || "").split("\n");
    if (!lines.length)
        return;
    for (const line of lines) {
        const row = document.createElement("div");
        row.classList.add("diff-line");
        if (line.startsWith("@@")) {
            row.classList.add("diff-hunk");
        }
        else if (line.startsWith("+") && !line.startsWith("+++")) {
            row.classList.add("diff-add");
        }
        else if (line.startsWith("-") && !line.startsWith("---")) {
            row.classList.add("diff-del");
        }
        else if (line.startsWith("---") || line.startsWith("+++")) {
            row.classList.add("diff-file");
        }
        else {
            row.classList.add("diff-ctx");
        }
        const sign = document.createElement("span");
        sign.classList.add("diff-sign");
        sign.textContent = line.charAt(0) || " ";
        const content = document.createElement("span");
        content.classList.add("diff-content");
        content.textContent = line.slice(1);
        row.appendChild(sign);
        row.appendChild(content);
        container.appendChild(row);
    }
}
