// GENERATED FILE - do not edit directly. Source: static_src/
import { createWorkspaceFolder, deleteWorkspaceFile, deleteWorkspaceFolder, downloadWorkspaceFile, downloadWorkspaceZip, } from "./workspaceApi.js";
import { flash } from "./utils.js";
const INDENT_PX = 14;
export class WorkspaceFileBrowser {
    constructor(options) {
        this.tree = [];
        this.currentPath = "";
        this.selectedPath = null;
        this.expanded = new Set();
        this.container = options.container;
        this.selectEl = options.selectEl ?? null;
        this.breadcrumbsEl = options.breadcrumbsEl ?? null;
        this.onSelect = options.onSelect;
        this.onRefresh = options.onRefresh;
        this.fileBtnEl = document.getElementById("workspace-file-pill");
        this.fileBtnNameEl = document.getElementById("workspace-file-pill-name");
        this.modalEl = document.getElementById("file-picker-modal");
        this.modalBodyEl = document.getElementById("file-picker-body");
        this.modalCloseEl = document.getElementById("file-picker-close");
        this.initFilePicker();
    }
    setTree(tree, defaultPath) {
        this.tree = tree || [];
        const next = this.pickInitialSelection(defaultPath);
        if (next) {
            this.expandAncestors(next);
            const shouldTrigger = next !== this.selectedPath;
            this.select(next, shouldTrigger);
        }
        else {
            this.render();
        }
    }
    getCurrentPath() {
        return this.currentPath;
    }
    navigateTo(path) {
        this.expandAncestors(path);
        this.currentPath = path;
        this.render();
    }
    async createFolder(path) {
        try {
            await createWorkspaceFolder(path);
            await this.onRefresh();
        }
        catch (err) {
            flash(err.message || "Failed to create folder", "error");
        }
    }
    select(path, trigger = true) {
        const node = this.findNode(path);
        if (!node || node.type !== "file")
            return;
        this.selectedPath = path;
        this.currentPath = this.parentPath(path);
        this.expandAncestors(path);
        this.updateFileName(node.name);
        this.updateSelect(path);
        this.render();
        if (trigger)
            this.onSelect(node);
    }
    refresh() {
        this.render();
        this.renderModal();
    }
    pickInitialSelection(defaultPath) {
        if (defaultPath)
            return defaultPath;
        if (this.selectedPath && this.findNode(this.selectedPath)) {
            return this.selectedPath;
        }
        const firstFile = this.flattenFiles(this.tree).find((n) => n.type === "file");
        return firstFile ? firstFile.path : null;
    }
    parentPath(path) {
        const parts = path.split("/").filter(Boolean);
        if (parts.length <= 1)
            return "";
        parts.pop();
        return parts.join("/");
    }
    expandAncestors(path) {
        const parts = path.split("/").filter(Boolean);
        let accum = "";
        for (const part of parts.slice(0, -1)) {
            accum = accum ? `${accum}/${part}` : part;
            this.expanded.add(accum);
        }
    }
    flattenFiles(nodes) {
        const acc = [];
        const walk = (list) => {
            list.forEach((n) => {
                if (n.type === "file")
                    acc.push(n);
                if (n.children?.length)
                    walk(n.children);
            });
        };
        walk(nodes);
        return acc;
    }
    findNode(path, nodes) {
        const list = nodes || this.tree;
        for (const node of list) {
            if (node.path === path)
                return node;
            if (node.children?.length) {
                const found = this.findNode(path, node.children);
                if (found)
                    return found;
            }
        }
        return null;
    }
    getChildren(path) {
        if (!path)
            return this.tree;
        const node = this.findNode(path);
        return node?.children || [];
    }
    updateFileName(name) {
        if (this.fileBtnNameEl)
            this.fileBtnNameEl.textContent = name || "Select file";
    }
    updateSelect(path) {
        if (!this.selectEl)
            return;
        const options = this.flattenFiles(this.tree);
        this.selectEl.innerHTML = "";
        options.forEach((node) => {
            const opt = document.createElement("option");
            opt.value = node.path;
            opt.textContent = node.name;
            this.selectEl.appendChild(opt);
        });
        this.selectEl.value = path;
        this.selectEl.onchange = () => this.select(this.selectEl.value);
    }
    makeActionButton(label, title, handler) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost sm";
        btn.title = title;
        btn.textContent = label;
        btn.addEventListener("click", (evt) => {
            evt.stopPropagation();
            void handler();
        });
        return btn;
    }
    renderBreadcrumbs() {
        if (!this.breadcrumbsEl)
            return;
        this.breadcrumbsEl.innerHTML = "";
        const nav = document.createElement("div");
        nav.className = "workspace-breadcrumbs-inner";
        const rootBtn = document.createElement("button");
        rootBtn.type = "button";
        rootBtn.textContent = "Workspace";
        rootBtn.addEventListener("click", () => this.navigateTo(""));
        nav.appendChild(rootBtn);
        const parts = this.currentPath ? this.currentPath.split("/") : [];
        let accum = "";
        parts.forEach((part) => {
            const sep = document.createElement("span");
            sep.textContent = " / ";
            nav.appendChild(sep);
            accum = accum ? `${accum}/${part}` : part;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = part;
            const target = accum;
            btn.addEventListener("click", () => this.navigateTo(target));
            nav.appendChild(btn);
        });
        this.breadcrumbsEl.appendChild(nav);
    }
    render() {
        this.container.innerHTML = "";
        this.renderBreadcrumbs();
        const renderNodes = (nodes, depth = 0) => {
            nodes.forEach((node) => {
                const row = document.createElement("div");
                row.className = `workspace-tree-row ${node.type === "folder" ? "workspace-folder-row" : "workspace-file-row"}`;
                if (node.path === this.selectedPath)
                    row.classList.add("active");
                row.dataset.path = node.path;
                row.style.paddingLeft = `${depth * INDENT_PX}px`;
                const label = document.createElement("div");
                label.className = "workspace-tree-label";
                const main = document.createElement("div");
                main.className = "workspace-tree-main";
                if (node.type === "folder") {
                    const caret = document.createElement("span");
                    caret.className = "workspace-tree-caret";
                    caret.textContent = this.expanded.has(node.path) ? "▾" : "▸";
                    main.appendChild(caret);
                }
                else {
                    const bullet = document.createElement("span");
                    bullet.className = "workspace-tree-dot";
                    bullet.textContent = "•";
                    main.appendChild(bullet);
                }
                const name = document.createElement("button");
                name.type = "button";
                name.className = "workspace-tree-name";
                name.textContent = node.name;
                if (node.is_pinned)
                    name.classList.add("pinned");
                if (node.type === "folder") {
                    name.addEventListener("click", () => {
                        if (this.expanded.has(node.path)) {
                            this.expanded.delete(node.path);
                        }
                        else {
                            this.expanded.add(node.path);
                        }
                        this.currentPath = node.path;
                        this.render();
                        this.renderModal();
                    });
                }
                else {
                    name.addEventListener("click", () => this.select(node.path));
                }
                main.appendChild(name);
                label.appendChild(main);
                const meta = document.createElement("span");
                meta.className = "workspace-tree-meta";
                if (node.type === "file" && node.size != null) {
                    meta.textContent = this.prettySize(node.size);
                }
                else if (node.type === "folder" && node.children) {
                    const count = node.children.filter((c) => c.type === "file").length;
                    meta.textContent = count ? `${count} file${count === 1 ? "" : "s"}` : "";
                }
                if (meta.textContent)
                    label.appendChild(meta);
                const actions = document.createElement("div");
                actions.className = "workspace-item-actions";
                if (node.type === "file") {
                    const dlBtn = this.makeActionButton("↓", "Download", () => downloadWorkspaceFile(node.path));
                    actions.appendChild(dlBtn);
                    if (!node.is_pinned) {
                        const delBtn = this.makeActionButton("✕", "Delete", async () => {
                            if (!confirm(`Delete ${node.name}?`))
                                return;
                            try {
                                await deleteWorkspaceFile(node.path);
                                await this.onRefresh();
                            }
                            catch (err) {
                                flash(err.message || "Failed to delete file", "error");
                            }
                        });
                        delBtn.classList.add("danger");
                        actions.appendChild(delBtn);
                    }
                }
                else {
                    const zipBtn = this.makeActionButton("⬇", "Download folder", () => downloadWorkspaceZip(node.path));
                    actions.appendChild(zipBtn);
                    const delBtn = this.makeActionButton("✕", "Delete folder", async () => {
                        if (!confirm(`Delete folder ${node.name}? (must be empty)`))
                            return;
                        try {
                            await deleteWorkspaceFolder(node.path);
                            await this.onRefresh();
                        }
                        catch (err) {
                            flash(err.message || "Failed to delete folder", "error");
                        }
                    });
                    delBtn.classList.add("danger");
                    actions.appendChild(delBtn);
                }
                row.appendChild(label);
                row.appendChild(actions);
                this.container.appendChild(row);
                if (node.type === "folder" && this.expanded.has(node.path) && node.children?.length) {
                    renderNodes(node.children, depth + 1);
                }
            });
        };
        renderNodes(this.tree, 0);
    }
    renderModal() {
        if (!this.modalBodyEl)
            return;
        this.modalBodyEl.innerHTML = "";
        const crumbs = document.createElement("div");
        crumbs.className = "file-picker-crumbs";
        const root = document.createElement("button");
        root.type = "button";
        root.textContent = "Workspace";
        root.addEventListener("click", () => {
            this.currentPath = "";
            this.render();
            this.renderModal();
        });
        crumbs.appendChild(root);
        const parts = this.currentPath ? this.currentPath.split("/") : [];
        let accum = "";
        parts.forEach((part) => {
            const sep = document.createElement("span");
            sep.textContent = " / ";
            crumbs.appendChild(sep);
            accum = accum ? `${accum}/${part}` : part;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = part;
            const target = accum;
            btn.addEventListener("click", () => {
                this.currentPath = target;
                this.render();
                this.renderModal();
            });
            crumbs.appendChild(btn);
        });
        this.modalBodyEl.appendChild(crumbs);
        const nodes = this.getChildren(this.currentPath);
        if (!nodes.length) {
            const empty = document.createElement("div");
            empty.className = "file-picker-empty";
            empty.textContent = "Empty folder";
            this.modalBodyEl.appendChild(empty);
            return;
        }
        nodes.forEach((node) => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "file-picker-item";
            item.textContent = node.name;
            item.dataset.path = node.path;
            if (node.type === "folder") {
                item.classList.add("folder");
                item.addEventListener("click", () => {
                    this.currentPath = node.path;
                    this.expanded.add(node.path);
                    this.render();
                    this.renderModal();
                });
            }
            else {
                item.classList.add("file");
                item.classList.toggle("active", node.path === this.selectedPath);
                item.addEventListener("click", () => {
                    this.select(node.path);
                    this.closeModal();
                });
            }
            this.modalBodyEl.appendChild(item);
        });
    }
    openModal() {
        if (!this.modalEl)
            return;
        this.renderModal();
        this.modalEl.hidden = false;
        this.modalBodyEl?.querySelector(".file-picker-item")?.focus();
    }
    closeModal() {
        if (this.modalEl)
            this.modalEl.hidden = true;
    }
    initFilePicker() {
        if (this.fileBtnEl) {
            this.fileBtnEl.addEventListener("click", (e) => {
                e.stopPropagation();
                this.openModal();
            });
        }
        if (this.modalCloseEl) {
            this.modalCloseEl.addEventListener("click", () => this.closeModal());
        }
        if (this.modalEl) {
            this.modalEl.addEventListener("click", (e) => {
                if (e.target === this.modalEl)
                    this.closeModal();
            });
            document.addEventListener("keydown", (e) => {
                if (e.key === "Escape" && !this.modalEl.hidden)
                    this.closeModal();
            });
        }
    }
    prettySize(bytes) {
        if (bytes < 1024)
            return `${bytes} B`;
        const kb = bytes / 1024;
        if (kb < 1024)
            return `${kb.toFixed(1)} KB`;
        const mb = kb / 1024;
        return `${mb.toFixed(1)} MB`;
    }
}
