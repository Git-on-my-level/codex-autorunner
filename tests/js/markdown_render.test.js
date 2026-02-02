import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.navigator = dom.window.navigator;
globalThis.HTMLElement = dom.window.HTMLElement;

const { renderMarkdown } = await import("../../src/codex_autorunner/static/messages.js");

test("renders relative markdown links", () => {
  const html = renderMarkdown("See [file](/car/hub/filebox/foo.zip)");
  assert.match(html, /<a href="\/car\/hub\/filebox\/foo.zip"[^>]*>file<\/a>/);
});

test("leaves unsafe markdown links as text", () => {
  const html = renderMarkdown("Do not [run](javascript:alert(1))");
  assert.match(html, /\[run\]\(javascript:alert\(1\)\)/);
  assert.doesNotMatch(html, /href="javascript:alert\(1\)"/);
});
