// GENERATED FILE - do not edit directly. Source: static_src/
import { CONSTANTS } from "./constants.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { REPO_ID, BASE_PATH } from "./env.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
const XTERM_COLOR_MODE_DEFAULT = 0;
const XTERM_COLOR_MODE_PALETTE_16 = 0x01000000;
const XTERM_COLOR_MODE_PALETTE_256 = 0x02000000;
const XTERM_COLOR_MODE_RGB = 0x03000000;
export function createTranscriptState() {
    return {
        lines: [],
        lineCells: [],
        cursor: 0,
        maxLines: 2000,
        hydrated: false,
        ansiState: {
            mode: "text",
            oscEsc: false,
            csiParams: "",
            fg: null,
            bg: null,
            fgRgb: null,
            bgRgb: null,
            bold: false,
            className: "",
            style: "",
        },
        persistTimer: null,
        decoder: new TextDecoder(),
        resetForConnect: false,
        altScrollbackLines: [],
        altSnapshotPlain: null,
        altSnapshotHtml: null,
        xtermPalette: null,
    };
}
export function transcriptStorageKey() {
    const scope = REPO_ID || BASE_PATH || "default";
    return `codex_terminal_transcript:${scope}`;
}
export function escapeHtml(text) {
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
export function cellsToSegments(cells) {
    if (!Array.isArray(cells))
        return [];
    const segments = [];
    let current = null;
    for (const cell of cells) {
        if (!cell)
            continue;
        const cls = cell.c || "";
        const style = cell.s || "";
        if (!current || current.c !== cls || (current.s || "") !== style) {
            current = { t: cell.t || "", c: cls };
            if (style) {
                current.s = style;
            }
            segments.push(current);
        }
        else {
            current.t += cell.t || "";
        }
    }
    return segments;
}
export function segmentsToCells(segments) {
    if (typeof segments === "string") {
        return Array.from(segments).map((ch) => ({ t: ch, c: "", s: "" }));
    }
    if (!Array.isArray(segments))
        return null;
    const cells = [];
    for (const seg of segments) {
        if (!seg || typeof seg.t !== "string")
            continue;
        const cls = typeof seg.c === "string" ? seg.c : "";
        const style = typeof seg.s === "string" ? seg.s : "";
        for (const ch of seg.t) {
            cells.push({ t: ch, c: cls, s: style });
        }
    }
    return cells;
}
export function cellsToHtml(cells) {
    if (!cells.length)
        return "";
    const segments = cellsToSegments(cells);
    let html = "";
    for (const seg of segments) {
        const text = escapeHtml(seg.t);
        if (!seg.c && !seg.s) {
            html += text;
        }
        else if (seg.c && seg.s) {
            html += `<span class="${seg.c}" style="${seg.s}">${text}</span>`;
        }
        else if (seg.c) {
            html += `<span class="${seg.c}">${text}</span>`;
        }
        else {
            html += `<span style="${seg.s}">${text}</span>`;
        }
    }
    return html;
}
export function cellsToPlainText(cells) {
    if (!Array.isArray(cells) || !cells.length)
        return "";
    let text = "";
    for (const cell of cells) {
        if (!cell) {
            text += " ";
            continue;
        }
        text += cell.t || " ";
    }
    return text;
}
export function resetTranscript(state) {
    state.lines = [];
    state.lineCells = [];
    state.cursor = 0;
    state.hydrated = false;
    clearAltScrollbackState(state);
    state.ansiState = {
        mode: "text",
        oscEsc: false,
        csiParams: "",
        fg: null,
        bg: null,
        fgRgb: null,
        bgRgb: null,
        bold: false,
        className: "",
        style: "",
    };
    state.decoder = new TextDecoder();
    persistTranscript(state, true);
}
export function restoreTranscript(state) {
    try {
        const key = transcriptStorageKey();
        let raw = null;
        let fromSessionStorage = false;
        try {
            raw = localStorage.getItem(key);
        }
        catch (_err) {
            raw = null;
        }
        if (!raw) {
            try {
                raw = sessionStorage.getItem(key);
                fromSessionStorage = Boolean(raw);
            }
            catch (_err) {
                raw = null;
            }
        }
        if (!raw)
            return;
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed?.lines)) {
            state.lines = parsed
                .lines.map((line) => segmentsToCells(line))
                .filter(Boolean);
        }
        if (Array.isArray(parsed?.line)) {
            state.lineCells = segmentsToCells(parsed.line) || [];
        }
        if (Number.isInteger(parsed?.cursor)) {
            state.cursor = Math.max(0, parsed.cursor);
        }
        if (fromSessionStorage) {
            try {
                localStorage.setItem(key, raw);
            }
            catch (_err) {
                // ignore storage errors
            }
        }
    }
    catch (_err) {
        // ignore restore errors
    }
}
export function persistTranscript(state, clear = false) {
    try {
        const key = transcriptStorageKey();
        if (clear) {
            try {
                localStorage.removeItem(key);
            }
            catch (_err) {
                // ignore storage errors
            }
            try {
                sessionStorage.removeItem(key);
            }
            catch (_err) {
                // ignore storage errors
            }
            return;
        }
        const payload = JSON.stringify({
            lines: state.lines.map((line) => cellsToSegments(line)),
            line: cellsToSegments(state.lineCells),
            cursor: state.cursor,
        });
        try {
            localStorage.setItem(key, payload);
            return;
        }
        catch (_err) {
            // ignore storage errors
        }
        try {
            sessionStorage.setItem(key, payload);
        }
        catch (_err) {
            // ignore storage errors
        }
    }
    catch (_err) {
        // ignore storage errors
    }
}
export function persistTranscriptSoon(state) {
    if (state.persistTimer)
        return;
    state.persistTimer = window.setTimeout(() => {
        state.persistTimer = null;
        persistTranscript(state, false);
    }, 500);
}
export function getTranscriptLines(state) {
    const lines = state.lines.slice();
    if (state.lineCells.length) {
        lines.push(state.lineCells);
    }
    return lines;
}
export function bufferLineToText(line) {
    if (!line)
        return "";
    if (typeof line.translateToString === "function") {
        return line.translateToString(true);
    }
    if (typeof line.toString === "function") {
        return line.toString();
    }
    return "";
}
export function getBufferSnapshot(term, maxLines) {
    if (!term?.buffer?.active)
        return null;
    const bufferNamespace = term.buffer;
    const buffer = bufferNamespace.active;
    const lineCount = Number.isInteger(buffer.length) ? buffer.length : buffer.lines?.length;
    if (!Number.isInteger(lineCount))
        return null;
    const start = Math.max(0, lineCount - maxLines);
    const lines = [];
    for (let idx = start; idx < lineCount; idx++) {
        let line = null;
        if (typeof buffer.getLine === "function") {
            line = buffer.getLine(idx);
        }
        else if (typeof buffer.lines?.get === "function") {
            line = buffer.lines.get(idx);
        }
        lines.push(line);
    }
    const cols = Number.isInteger(buffer.cols) ? buffer.cols : term.cols;
    return { bufferNamespace, buffer, lines, cols };
}
export function snapshotBufferLines(_state, term, snapshot) {
    if (!snapshot)
        return null;
    const cols = snapshot.cols ?? term?.cols;
    const plain = [];
    const html = [];
    for (const line of snapshot.lines) {
        plain.push(bufferLineToText(line));
        html.push(bufferLineToHtml(line, cols));
    }
    return { plain, html };
}
export function findLineOverlap(prevRegion, nextRegion) {
    const maxOverlap = Math.min(prevRegion.length, nextRegion.length);
    for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
        let matches = true;
        for (let idx = 0; idx < overlap; idx += 1) {
            if (prevRegion[prevRegion.length - overlap + idx] !== nextRegion[idx]) {
                matches = false;
                break;
            }
        }
        if (matches)
            return overlap;
    }
    return 0;
}
export function trimAltScrollback(state) {
    if (!Array.isArray(state.altScrollbackLines))
        return;
    const overflow = state.altScrollbackLines.length - state.maxLines;
    if (overflow > 0) {
        state.altScrollbackLines.splice(0, overflow);
    }
}
export function clearAltScrollbackState(state) {
    state.altScrollbackLines = [];
    state.altSnapshotPlain = null;
    state.altSnapshotHtml = null;
}
export function updateAltScrollback(state, snapshotPlain, snapshotHtml) {
    if (!Array.isArray(snapshotPlain) || !Array.isArray(snapshotHtml))
        return;
    if (!Array.isArray(state.altScrollbackLines)) {
        state.altScrollbackLines = [];
    }
    if (!Array.isArray(state.altSnapshotPlain)) {
        state.altSnapshotPlain = snapshotPlain;
        state.altSnapshotHtml = snapshotHtml;
        return;
    }
    const prevPlain = state.altSnapshotPlain;
    const prevHtml = state.altSnapshotHtml || [];
    const nextPlain = snapshotPlain;
    const len = Math.min(prevPlain.length, nextPlain.length);
    let prefix = 0;
    while (prefix < len && prevPlain[prefix] === nextPlain[prefix]) {
        prefix += 1;
    }
    let suffix = 0;
    while (suffix < len - prefix &&
        prevPlain[prevPlain.length - 1 - suffix] ===
            nextPlain[nextPlain.length - 1 - suffix]) {
        suffix += 1;
    }
    const prevStart = prefix;
    const prevEnd = prevPlain.length - suffix;
    const nextStart = prefix;
    const nextEnd = nextPlain.length - suffix;
    const prevRegion = prevPlain.slice(prevStart, prevEnd);
    const nextRegion = nextPlain.slice(nextStart, nextEnd);
    const overlap = findLineOverlap(prevRegion, nextRegion);
    if (overlap > 0) {
        const removedCount = prevRegion.length - overlap;
        if (removedCount > 0) {
            const removedLines = prevHtml.slice(prevStart, prevStart + removedCount);
            state.altScrollbackLines.push(...removedLines);
            trimAltScrollback(state);
        }
    }
    state.altSnapshotPlain = nextPlain;
    state.altSnapshotHtml = snapshotHtml;
}
export function isAltBufferActive(term) {
    const bufferNamespace = term?.buffer;
    if (!bufferNamespace?.active || !bufferNamespace?.alternate)
        return false;
    return bufferNamespace.active === bufferNamespace.alternate;
}
function paletteIndexToCss(state, index) {
    if (!Number.isInteger(index) || index < 0)
        return null;
    if (!state.xtermPalette) {
        const theme = CONSTANTS.THEME.XTERM;
        state.xtermPalette = [
            theme.black,
            theme.red,
            theme.green,
            theme.yellow,
            theme.blue,
            theme.magenta,
            theme.cyan,
            theme.white,
            theme.brightBlack,
            theme.brightRed,
            theme.brightGreen,
            theme.brightYellow,
            theme.brightBlue,
            theme.brightMagenta,
            theme.brightCyan,
            theme.brightWhite,
        ];
    }
    if (index < state.xtermPalette.length) {
        return state.xtermPalette[index];
    }
    return ansi256ToRgb(index);
}
function rgbNumberToCss(value) {
    if (!Number.isInteger(value) || value < 0)
        return null;
    const r = (value >> 16) & 0xff;
    const g = (value >> 8) & 0xff;
    const b = value & 0xff;
    return `rgb(${r}, ${g}, ${b})`;
}
function resolveXtermColor(state, mode, value) {
    if (!Number.isInteger(mode) || value === -1)
        return null;
    if (mode === XTERM_COLOR_MODE_DEFAULT)
        return null;
    if (mode === XTERM_COLOR_MODE_RGB) {
        return rgbNumberToCss(value);
    }
    if (mode === XTERM_COLOR_MODE_PALETTE_16 ||
        mode === XTERM_COLOR_MODE_PALETTE_256) {
        return paletteIndexToCss(state, value);
    }
    if (Number.isInteger(value)) {
        return value > 255 ? rgbNumberToCss(value) : paletteIndexToCss(state, value);
    }
    return null;
}
function getCellStyle(state, cell) {
    const bold = typeof cell.isBold === "function" ? cell.isBold() : false;
    const inverse = typeof cell.isInverse === "function" ? cell.isInverse() : false;
    const fgMode = typeof cell.getFgColorMode === "function" ? cell.getFgColorMode() : null;
    const bgMode = typeof cell.getBgColorMode === "function" ? cell.getBgColorMode() : null;
    const fgValue = typeof cell.getFgColor === "function" ? cell.getFgColor() : null;
    const bgValue = typeof cell.getBgColor === "function" ? cell.getBgColor() : null;
    let fg = fgMode !== null && fgValue !== null ? resolveXtermColor(state, fgMode, fgValue) : null;
    let bg = bgMode !== null && bgValue !== null ? resolveXtermColor(state, bgMode, bgValue) : null;
    if (inverse) {
        const theme = CONSTANTS.THEME.XTERM;
        const defaultFg = theme.foreground;
        const defaultBg = theme.background;
        const resolvedFg = fg ?? defaultFg;
        const resolvedBg = bg ?? defaultBg;
        fg = resolvedBg;
        bg = resolvedFg;
    }
    const styles = [];
    if (fg)
        styles.push(`color: ${fg}`);
    if (bg)
        styles.push(`background-color: ${bg}`);
    return {
        className: bold ? "ansi-bold" : "",
        style: styles.join("; "),
    };
}
function getCellWidth(cell) {
    if (typeof cell.getWidth === "function") {
        return cell.getWidth();
    }
    if (Number.isInteger(cell.width)) {
        return cell.width;
    }
    return 1;
}
function getCellChars(cell, width) {
    let chars = "";
    if (typeof cell.getChars === "function") {
        chars = cell.getChars();
    }
    if (!chars && typeof cell.getCode === "function") {
        const code = cell.getCode();
        if (Number.isInteger(code) && code > 0) {
            chars = String.fromCodePoint(code);
        }
    }
    if (!chars) {
        chars = " ";
    }
    if (width > 1 && chars === " ") {
        return " ".repeat(width);
    }
    return chars;
}
export function bufferLineToHtml(line, cols, state) {
    if (!line)
        return "";
    const st = state || createTranscriptState();
    if (typeof line.getCell !== "function") {
        return escapeHtml(bufferLineToText(line));
    }
    let html = "";
    let currentText = "";
    let currentClass = "";
    let currentStyle = "";
    const flush = () => {
        if (!currentText)
            return;
        const text = escapeHtml(currentText);
        if (!currentClass && !currentStyle) {
            html += text;
        }
        else if (currentClass && currentStyle) {
            html += `<span class="${currentClass}" style="${currentStyle}">${text}</span>`;
        }
        else if (currentClass) {
            html += `<span class="${currentClass}">${text}</span>`;
        }
        else {
            html += `<span style="${currentStyle}">${text}</span>`;
        }
        currentText = "";
    };
    const lineLength = Number.isInteger(line.length) ? line.length : 0;
    const maxCols = Number.isInteger(cols) ? cols : lineLength;
    for (let col = 0; col < maxCols; col++) {
        const cell = line.getCell(col);
        if (!cell) {
            if (currentClass || currentStyle) {
                flush();
                currentClass = "";
                currentStyle = "";
            }
            currentText += " ";
            continue;
        }
        const width = getCellWidth(cell);
        if (width === 0) {
            continue;
        }
        const isInvisible = typeof cell.isInvisible === "function" ? cell.isInvisible() : false;
        const { className, style } = getCellStyle(st, cell);
        const chars = isInvisible
            ? " ".repeat(Math.max(1, width))
            : getCellChars(cell, width);
        if (className !== currentClass || style !== currentStyle) {
            flush();
            currentClass = className;
            currentStyle = style;
        }
        currentText += chars;
    }
    flush();
    return html;
}
function ansiClassName(ansiState) {
    const parts = [];
    if (ansiState.bold)
        parts.push("ansi-bold");
    if (ansiState.fg)
        parts.push(`ansi-fg-${ansiState.fg}`);
    if (ansiState.bg)
        parts.push(`ansi-bg-${ansiState.bg}`);
    return parts.join(" ");
}
function ansiStyle(ansiState) {
    const styles = [];
    if (ansiState.fgRgb)
        styles.push(`color: ${ansiState.fgRgb}`);
    if (ansiState.bgRgb)
        styles.push(`background-color: ${ansiState.bgRgb}`);
    return styles.join("; ");
}
function ansi256ToRgb(value) {
    if (!Number.isInteger(value) || value < 0 || value > 255)
        return null;
    if (value >= 232) {
        const shade = 8 + (value - 232) * 10;
        return `rgb(${shade}, ${shade}, ${shade})`;
    }
    if (value < 16)
        return null;
    const index = value - 16;
    const r = Math.floor(index / 36);
    const g = Math.floor((index % 36) / 6);
    const b = index % 6;
    const steps = [0, 95, 135, 175, 215, 255];
    return `rgb(${steps[r]}, ${steps[g]}, ${steps[b]})`;
}
function applyAnsiPaletteColor(isForeground, value, ansiState) {
    if (!Number.isInteger(value))
        return;
    if (value >= 0 && value <= 7) {
        if (isForeground) {
            ansiState.fg = String(30 + value);
            ansiState.fgRgb = null;
        }
        else {
            ansiState.bg = String(40 + value);
            ansiState.bgRgb = null;
        }
        return;
    }
    if (value >= 8 && value <= 15) {
        if (isForeground) {
            ansiState.fg = String(90 + (value - 8));
            ansiState.fgRgb = null;
        }
        else {
            ansiState.bg = String(100 + (value - 8));
            ansiState.bgRgb = null;
        }
        return;
    }
    const rgb = ansi256ToRgb(value);
    if (!rgb)
        return;
    if (isForeground) {
        ansiState.fg = null;
        ansiState.fgRgb = rgb;
    }
    else {
        ansiState.bg = null;
        ansiState.bgRgb = rgb;
    }
}
function pushTranscriptLine(state, lineCells) {
    state.lines.push(lineCells.slice());
    const overflow = state.lines.length - state.maxLines;
    if (overflow > 0) {
        state.lines.splice(0, overflow);
    }
}
export function appendTranscriptChunk(state, data) {
    if (!data)
        return;
    const text = typeof data === "string"
        ? data
        : state.decoder.decode(data, { stream: true });
    if (!text)
        return;
    const ansiState = state.ansiState;
    let didChange = false;
    const parseParams = (raw) => {
        if (!raw)
            return [];
        return raw.split(";").map((part) => {
            const match = part.match(/(\d+)/);
            return match ? Number.parseInt(match[1], 10) : null;
        });
    };
    const getParam = (params, index, fallback) => {
        const value = params[index];
        return Number.isInteger(value) ? value : fallback;
    };
    const writeChar = (char) => {
        if (state.cursor > state.lineCells.length) {
            const padCount = state.cursor - state.lineCells.length;
            for (let idx = 0; idx < padCount; idx++) {
                state.lineCells.push({ t: " ", c: "" });
            }
        }
        const cell = { t: char, c: ansiState.className, s: ansiState.style || undefined };
        if (ansiState.style) {
            cell.s = ansiState.style;
        }
        if (state.cursor === state.lineCells.length) {
            state.lineCells.push(cell);
        }
        else {
            state.lineCells[state.cursor] = cell;
        }
        state.cursor += 1;
        didChange = true;
    };
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        if (ansiState.mode === "osc") {
            if (ansiState.oscEsc) {
                ansiState.oscEsc = false;
                if (ch === "\\") {
                    ansiState.mode = "text";
                }
                continue;
            }
            if (ch === "\x07") {
                ansiState.mode = "text";
                continue;
            }
            if (ch === "\x1b") {
                ansiState.oscEsc = true;
            }
            continue;
        }
        if (ansiState.mode === "csi") {
            if (ch >= "@" && ch <= "~") {
                const params = parseParams(ansiState.csiParams);
                const param = getParam(params, 0, 0);
                if (ch === "m") {
                    const codes = params.length ? params : [0];
                    for (let idx = 0; idx < codes.length; idx++) {
                        const code = codes[idx];
                        if (code === 0 || code === null) {
                            ansiState.fg = null;
                            ansiState.bg = null;
                            ansiState.fgRgb = null;
                            ansiState.bgRgb = null;
                            ansiState.bold = false;
                            continue;
                        }
                        if (code === 1) {
                            ansiState.bold = true;
                            continue;
                        }
                        if (code === 22) {
                            ansiState.bold = false;
                            continue;
                        }
                        if (code === 38 || code === 48) {
                            const isForeground = code === 38;
                            const mode = codes[idx + 1];
                            if (mode === 2) {
                                const r = codes[idx + 2];
                                const g = codes[idx + 3];
                                const b = codes[idx + 4];
                                if (Number.isInteger(r) &&
                                    Number.isInteger(g) &&
                                    Number.isInteger(b)) {
                                    const rr = Math.max(0, Math.min(255, r));
                                    const gg = Math.max(0, Math.min(255, g));
                                    const bb = Math.max(0, Math.min(255, b));
                                    const rgb = `rgb(${rr}, ${gg}, ${bb})`;
                                    if (isForeground) {
                                        ansiState.fg = null;
                                        ansiState.fgRgb = rgb;
                                    }
                                    else {
                                        ansiState.bg = null;
                                        ansiState.bgRgb = rgb;
                                    }
                                }
                                idx += 4;
                            }
                            else if (mode === 5) {
                                const colorIndex = codes[idx + 2];
                                if (Number.isInteger(colorIndex)) {
                                    applyAnsiPaletteColor(isForeground, colorIndex, ansiState);
                                }
                                idx += 2;
                            }
                            continue;
                        }
                        if (code >= 30 && code <= 37) {
                            ansiState.fg = String(code);
                            ansiState.fgRgb = null;
                            continue;
                        }
                        if (code === 39) {
                            ansiState.fg = null;
                            ansiState.fgRgb = null;
                            continue;
                        }
                        if (code >= 40 && code <= 47) {
                            ansiState.bg = String(code);
                            ansiState.bgRgb = null;
                            continue;
                        }
                        if (code === 49) {
                            ansiState.bg = null;
                            ansiState.bgRgb = null;
                            continue;
                        }
                        if (code >= 90 && code <= 97) {
                            ansiState.fg = String(code);
                            ansiState.fgRgb = null;
                            continue;
                        }
                        if (code >= 100 && code <= 107) {
                            ansiState.bg = String(code);
                            ansiState.bgRgb = null;
                        }
                    }
                    ansiState.className = ansiClassName(ansiState);
                    ansiState.style = ansiStyle(ansiState);
                }
                else if (ch === "K") {
                    if (param === 2) {
                        state.lineCells = [];
                        state.cursor = 0;
                    }
                    else if (param === 1) {
                        for (let idx = 0; idx < state.cursor; idx++) {
                            if (state.lineCells[idx]) {
                                state.lineCells[idx].t = " ";
                            }
                            else {
                                state.lineCells[idx] = { t: " ", c: "" };
                            }
                        }
                    }
                    else {
                        state.lineCells = state.lineCells.slice(0, state.cursor);
                    }
                    didChange = true;
                }
                else if (ch === "G") {
                    state.cursor = Math.max(0, param - 1);
                }
                else if (ch === "C") {
                    state.cursor = Math.max(0, state.cursor + (param || 1));
                }
                else if (ch === "D") {
                    state.cursor = Math.max(0, state.cursor - (param || 1));
                }
                else if (ch === "H" || ch === "f") {
                    const col = getParam(params, 1, getParam(params, 0, 1));
                    state.cursor = Math.max(0, (col || 1) - 1);
                }
                ansiState.mode = "text";
                ansiState.csiParams = "";
            }
            else {
                ansiState.csiParams += ch;
            }
            continue;
        }
        if (ansiState.mode === "esc") {
            if (ch === "[") {
                ansiState.mode = "csi";
                ansiState.csiParams = "";
                continue;
            }
            if (ch === "]") {
                ansiState.mode = "osc";
                ansiState.oscEsc = false;
                continue;
            }
            ansiState.mode = "text";
            continue;
        }
        if (ch === "\x1b") {
            ansiState.mode = "esc";
            continue;
        }
        if (ch === "\x07") {
            continue;
        }
        if (ch === "\r") {
            state.cursor = 0;
            continue;
        }
        if (ch === "\n") {
            pushTranscriptLine(state, state.lineCells);
            state.lineCells = [];
            state.cursor = 0;
            didChange = true;
            continue;
        }
        if (ch === "\b") {
            if (state.cursor > 0) {
                const idx = state.cursor - 1;
                if (state.lineCells[idx]) {
                    state.lineCells[idx].t = " ";
                }
                state.cursor = idx;
                didChange = true;
            }
            continue;
        }
        if (ch >= " " || ch === "\t") {
            if (ch === "\t") {
                writeChar(" ");
                writeChar(" ");
            }
            else {
                writeChar(ch);
            }
        }
    }
    if (didChange) {
        persistTranscriptSoon(state);
    }
}
export function hydrateTerminalFromTranscript(state, term) {
    if (!term || state.hydrated)
        return;
    const lines = getTranscriptLines(state);
    if (!lines.length)
        return;
    const output = lines.map((line) => cellsToPlainText(line)).join("\r\n");
    if (output) {
        term.write(output);
        state.hydrated = true;
    }
}
