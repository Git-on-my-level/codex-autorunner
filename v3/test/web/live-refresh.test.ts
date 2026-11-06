import { describe, expect, test } from "bun:test";
import { LIVE_REFRESH_JS } from "../../src/surfaces/web/live_refresh.ts";

type ListenerEvent = {
  target?: unknown; preventDefault?: () => void;
  metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean; altKey?: boolean;
  key?: string; defaultPrevented?: boolean; isComposing?: boolean; repeat?: boolean;
};
type Listener = (event: ListenerEvent) => void;

type MockElementOptions = { form?: boolean; href?: string; manualRefresh?: boolean; target?: string };

class MockElement {
  tagName = "DIV";
  checked = true;
  value = "";
  target: string;
  href: string;
  hidden = false;
  focused = false;
  private readonly options: MockElementOptions;
  private readonly listeners = new Map<string, (() => void)[]>();
  constructor(private readonly selectors: string[] = [], options: MockElementOptions | boolean = {}) {
    this.options = typeof options === "boolean" ? { form: options } : options;
    this.target = this.options.target ?? "";
    this.href = this.options.href ?? "";
  }
  matches(selector: string): boolean { return selector.split(",").some((part) => this.selectors.includes(part.trim())); }
  closest(selector: string): object | null {
    if (this.selectors.includes(selector)) return this;
    if (this.options.form && selector === "form") return {};
    if (this.options.manualRefresh && selector === "[data-manual-refresh]") return this;
    if (this.options.href !== undefined && selector === "a[href]") return this;
    return null;
  }
  getAttribute(name: string): string | null { return name === "href" ? this.href : null; }
  addEventListener(type: string, listener: () => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  click(): void { for (const listener of this.listeners.get("click") ?? []) listener(); }
  dispatch(type: string): void { for (const listener of this.listeners.get(type) ?? []) listener(); }
  focus(): void { this.focused = true; }
  getClientRects(): unknown[] { return this.hidden ? [] : [{}]; }
}

function harness(storage: Map<string, string> | null = new Map()) {
  const queries = new Map<string, MockElement>();
  const listeners = new Map<string, Listener[]>();
  const timers: (() => void)[] = [];
  const notice = { hidden: true };
  const draftGuard = new MockElement([], { }); draftGuard.hidden = true;
  const discardLink = new MockElement([], { href: "" });
  const keepButton = new MockElement();
  const shortcutToggle = new MockElement();
  const times = [
    { dataset: {}, dateTime: "2026-08-26T12:00:00.000Z", textContent: "raw-full" },
    { dataset: { format: "compact" }, dateTime: "2026-08-26T12:00:00.000Z", textContent: "raw-compact" },
  ];
  const documentMock = {
    currentScript: { dataset: { refreshMs: "5000" } },
    visibilityState: "visible",
    activeElement: { tagName: "BODY" },
    getElementById(id: string) {
      return id === "refresh-paused" ? notice : id === "draft-navigation" ? draftGuard : id === "discard-draft" ? discardLink : id === "keep-draft" ? keepButton : id === "letter-shortcuts" ? shortcutToggle : null;
    },
    addEventListener(type: string, listener: Listener) {
      listeners.set(type, [...(listeners.get(type) ?? []), listener]);
    },
    querySelector(selector: string) { return queries.get(selector) ?? null; },
    querySelectorAll(selector: string) { return selector === "time[datetime]" ? times : []; },
  };
  let reloads = 0;
  let confirms = 0;
  let confirmResult = false;
  const windowMock = {
    confirm: () => { confirms++; return confirmResult; },
    location: { reload: () => { reloads++; } },
    setTimeout: (callback: () => void) => { timers.push(callback); return timers.length; },
    ...(storage === null ? {} : { localStorage: { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value); } } }),
  };
  const globals = globalThis as unknown as { Element?: unknown; location?: unknown };
  const previousElement = globals.Element;
  const previousLocation = globals.location;
  globals.Element = MockElement;
  globals.location = windowMock.location;
  try {
    new Function("document", "window", LIVE_REFRESH_JS)(documentMock, windowMock);
  } catch (error) {
    if (previousElement === undefined) delete globals.Element;
    else globals.Element = previousElement;
    if (previousLocation === undefined) delete globals.location;
    else globals.location = previousLocation;
    throw error;
  }
  const dispatch = (type: string, event: ListenerEvent) => {
    for (const listener of listeners.get(type) ?? []) listener(event);
  };
  const runTimer = () => timers.shift()?.();
  return {
    document: documentMock,
    queries,
    notice,
    times,
    draftGuard,
    discardLink,
    keepButton,
    shortcutToggle,
    storage,
    dispatch,
    runTimer,
    setConfirm(result: boolean) { confirmResult = result; },
    get confirms() { return confirms; },
    click(target: MockElement, modifiers: Pick<ListenerEvent, "metaKey" | "ctrlKey" | "shiftKey" | "altKey"> = {}) {
      let prevented = false;
      dispatch("click", { target, ...modifiers, preventDefault: () => { prevented = true; } });
      return prevented;
    },
    get reloads() { return reloads; },
    restore() {
      if (previousElement === undefined) delete globals.Element;
      else globals.Element = previousElement;
      if (previousLocation === undefined) delete globals.location;
      else globals.location = previousLocation;
    },
  };
}

describe("live refresh progressive enhancement", () => {
  test("list-only mobile readers ignore shortcuts without creating hidden drafts", () => {
    const page = harness();
    try {
      const reader = new MockElement(); reader.hidden = true;
      page.queries.set('.mailbox-reader', reader);
      const next = new MockElement([], { href: "/ui/decisions/next" });
      let navigations = 0;
      next.addEventListener("click", () => { navigations++; });
      page.queries.set('.reader-navigation a[aria-label="Next decision"]', next);
      for (const key of ["j", "k", "r", "1", "Escape"]) {
        page.dispatch("keydown", { target: new MockElement(), key, preventDefault() {} });
      }
      expect(navigations).toBe(0);
      expect(page.notice.hidden).toBe(true);
      expect(page.draftGuard.hidden).toBe(true);
    } finally { page.restore(); }
  });
  test("keyboard navigation uses the draft guard and ignores typing, repeat, and modifier keys", () => {
    const page = harness();
    try {
      const next = new MockElement([], { href: "/ui/decisions/next" });
      let navigations = 0;
      next.addEventListener("click", () => { if (!page.click(next)) navigations++; });
      page.queries.set('.reader-navigation a[aria-label="Next decision"]', next);
      const key = (target: MockElement, extra: Partial<ListenerEvent> = {}) => page.dispatch("keydown", { target, key: "j", preventDefault() {}, ...extra });
      key(new MockElement());
      expect(navigations).toBe(1);
      key(new MockElement(), { repeat: true });
      key(new MockElement(), { isComposing: true });
      key(new MockElement(), { ctrlKey: true });
      key(new MockElement(['input:not([type="radio"]),textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]']));
      expect(navigations).toBe(1);
      page.dispatch("input", { target: new MockElement([], true) });
      key(new MockElement());
      expect(navigations).toBe(1);
      expect(page.draftGuard.hidden).toBe(false);
      expect(page.discardLink.href).toBe("/ui/decisions/next");
    } finally { page.restore(); }
  });
  test("clean pages reload, but dirty forms stay paused after blur", () => {
    const clean = harness();
    try {
      clean.runTimer();
      expect(clean.reloads).toBe(1);
    } finally { clean.restore(); }

    const dirty = harness();
    try {
      dirty.dispatch("input", { target: new MockElement([], true) });
      dirty.document.activeElement = { tagName: "BODY" };
      dirty.runTimer();
      expect(dirty.reloads).toBe(0);
      expect(dirty.notice.hidden).toBe(false);
    } finally { dirty.restore(); }
  });

  test("scrolling either mailbox pane pauses refresh and keeps the notice visible", () => {
    const page = harness();
    try {
      page.dispatch("scroll", { target: new MockElement([".mailbox-reader"]) });
      page.runTimer();
      expect(page.reloads).toBe(0);
      expect(page.notice.hidden).toBe(false);
    } finally { page.restore(); }
  });

  test("dirty row navigation is blocked by the inline draft guard", () => {
    const page = harness();
    try {
      const field = new MockElement([], true);
      page.dispatch("input", { target: field });
      expect(page.click(new MockElement([], { href: "/ui/watching" }))).toBe(true);
      expect(page.draftGuard.hidden).toBe(false);
      expect(page.discardLink.href).toBe("/ui/watching");
      expect(page.keepButton.focused).toBe(true);
      page.keepButton.click();
      expect(page.draftGuard.hidden).toBe(true);
      expect(field.focused).toBe(true);
      expect(page.click(new MockElement([], { href: "/ui/handled" }))).toBe(true);
    } finally { page.restore(); }
  });

  test("explicit discard allows navigation and manual refresh uses the same guard", () => {
    const discard = harness();
    try {
      discard.dispatch("input", { target: new MockElement([], true) });
      expect(discard.click(new MockElement([], { href: "/ui/watching" }))).toBe(true);
      expect(discard.click(discard.discardLink)).toBe(false);
      expect(discard.click(new MockElement([], { href: "/ui/handled" }))).toBe(false);
    } finally { discard.restore(); }

    const manual = harness();
    try {
      manual.dispatch("input", { target: new MockElement([], true) });
      expect(manual.click(new MockElement([], { href: "", manualRefresh: true }))).toBe(true);
      expect(manual.draftGuard.hidden).toBe(false);
    } finally { manual.restore(); }
  });

  test("clean navigation and modified/hash/new-tab clicks are exempt", () => {
    const clean = harness();
    try {
      expect(clean.click(new MockElement([], { href: "/ui/watching" }))).toBe(false);
    } finally { clean.restore(); }

    const dirty = harness();
    try {
      dirty.dispatch("input", { target: new MockElement([], true) });
      expect(dirty.click(new MockElement([], { href: "/ui/watching" }), { metaKey: true })).toBe(false);
      expect(dirty.click(new MockElement([], { href: "#reply" }))).toBe(false);
      expect(dirty.click(new MockElement([], { href: "/ui/watching", target: "_blank" }))).toBe(false);
      expect(dirty.draftGuard.hidden).toBe(true);
    } finally { dirty.restore(); }
  });

  test("clicking a non-link does not clear the dirty draft", () => {
    const page = harness();
    try {
      page.dispatch("input", { target: new MockElement([], true) });
      expect(page.click(new MockElement())).toBe(false);
      expect(page.click(new MockElement([], { href: "/ui/watching" }))).toBe(true);
    } finally { page.restore(); }
  });

  test("shortcut preference survives navigation and storage failure keeps enhancement usable", () => {
    const storage = new Map([["car.v3.keyboard-shortcuts.enabled", "0"]]);
    const disabled = harness(storage);
    try {
      expect(disabled.shortcutToggle.checked).toBe(false);
      disabled.shortcutToggle.checked = true;
      disabled.shortcutToggle.dispatch("change");
      expect(storage.get("car.v3.keyboard-shortcuts.enabled")).toBe("1");
    } finally { disabled.restore(); }

    const unavailable = harness(null);
    try {
      expect(unavailable.shortcutToggle.checked).toBe(true);
      unavailable.shortcutToggle.checked = false;
      unavailable.shortcutToggle.dispatch("change");
      expect(unavailable.shortcutToggle.checked).toBe(false);
    } finally { unavailable.restore(); }
  });

  test("Escape returns to the list even when letter and number shortcuts are disabled", () => {
    const page = harness();
    try {
      page.shortcutToggle.checked = false;
      const back = new MockElement([".reader-back"], { href: "/ui" });
      let navigations = 0;
      back.addEventListener("click", () => { navigations++; });
      page.queries.set(".reader-back", back);
      page.dispatch("keydown", { target: new MockElement(), key: "Escape", preventDefault() {} });
      expect(navigations).toBe(1);
    } finally { page.restore(); }
  });

  test("compact timestamps stay compact while default timestamps remain full", () => {
    const page = harness();
    try {
      expect(page.times[0]!.textContent).not.toBe("raw-full");
      expect(page.times[1]!.textContent).not.toBe("raw-compact");
      expect(page.times[0]!.textContent).not.toBe(page.times[1]!.textContent);
    } finally { page.restore(); }
  });
});
