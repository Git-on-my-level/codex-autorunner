import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { glob } from "glob";

const ROOT = process.cwd();
const STATIC_SRC = path.join(ROOT, "src", "codex_autorunner", "static_src");
const GENERATED = path.join(ROOT, "src", "codex_autorunner", "static", "generated");
const BANNER = "// GENERATED FILE - do not edit directly. Source: static_src/";

const SKIP_MODULES = new Set(["types"]);

async function collectTsModules() {
  const pattern = path.join(STATIC_SRC, "**", "*.ts").replace(/\\/g, "/");
  const files = await glob(pattern);
  return files
    .map((f) => path.relative(STATIC_SRC, f))
    .filter((f) => !f.endsWith(".d.ts"))
    .filter((f) => !SKIP_MODULES.has(path.basename(f, ".ts")));
}

function expectedJsName(tsRelative) {
  return tsRelative.replace(/\.ts$/, ".js");
}

test("every static_src .ts module has a matching generated .js file", async () => {
  const tsModules = await collectTsModules();
  assert.ok(tsModules.length > 10, `expected many TS modules, found ${tsModules.length}`);

  const missing = [];
  for (const tsRel of tsModules) {
    const jsRel = expectedJsName(tsRel);
    const jsPath = path.join(GENERATED, jsRel);
    if (!fs.existsSync(jsPath)) {
      missing.push(jsRel);
    }
  }

  assert.equal(missing.length, 0, `Missing generated JS files:\n${missing.join("\n")}`);
});

test("every generated .js file has the auto-generated banner", async () => {
  const pattern = path.join(GENERATED, "*.js").replace(/\\/g, "/");
  const files = await glob(pattern);

  assert.ok(files.length > 10, `expected many generated JS files, found ${files.length}`);

  const missing = [];
  for (const file of files) {
    const content = fs.readFileSync(file, "utf8");
    if (!content.startsWith(BANNER)) {
      missing.push(path.relative(GENERATED, file));
    }
  }

  assert.equal(missing.length, 0, `Files missing banner:\n${missing.join("\n")}`);
});

test("no generated .js file is empty", async () => {
  const pattern = path.join(GENERATED, "*.js").replace(/\\/g, "/");
  const files = await glob(pattern);

  const empty = [];
  for (const file of files) {
    const stat = fs.statSync(file);
    if (stat.size === 0) {
      empty.push(path.relative(GENERATED, file));
    }
  }

  assert.equal(empty.length, 0, `Empty generated files:\n${empty.join("\n")}`);
});

test("asset manifest lists all generated .js files", async () => {
  const manifestPath = path.join(
    ROOT, "src", "codex_autorunner", "static", "assets.json"
  );
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));

  const manifestPaths = new Set(manifest.generated.map((e) => e.path));
  const pattern = path.join(GENERATED, "*.js").replace(/\\/g, "/");
  const files = await glob(pattern);

  const missing = [];
  for (const file of files) {
    const rel = path.relative(
      path.join(ROOT, "src", "codex_autorunner", "static"),
      file
    ).replace(/\\/g, "/");
    if (!manifestPaths.has(rel)) {
      missing.push(rel);
    }
  }

  assert.equal(missing.length, 0, `Generated files missing from manifest:\n${missing.join("\n")}`);
});

test("index.html loads bootstrap and loader with asset version placeholder", () => {
  const htmlPath = path.join(ROOT, "src", "codex_autorunner", "static", "index.html");
  const html = fs.readFileSync(htmlPath, "utf8");

  assert.match(html, /data-car-bootstrap/, "expected data-car-bootstrap attribute");
  assert.match(html, /generated\/bootstrap\.js/, "expected bootstrap.js script");
  assert.match(html, /data-car-loader/, "expected data-car-loader attribute");
  assert.match(html, /generated\/loader\.js/, "expected loader.js script");
  assert.match(html, /__CAR_ASSET_VERSION__/, "expected asset version placeholder");
});

test("loader.js references app.js as entry point", () => {
  const loaderPath = path.join(GENERATED, "loader.js");
  const loaderContent = fs.readFileSync(loaderPath, "utf8");

  assert.match(loaderContent, /app\.js/, "loader should reference app.js");
});
