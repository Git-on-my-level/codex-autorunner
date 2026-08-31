import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildV2CutoverReport, writeV2CutoverReport } from "../../src/migration/cutover.ts";
import { openStore } from "../../src/store/db.ts";

describe("one-way v2 cutover audit", () => {
  test("fails closed on active rows and requires review for unclassified history", () => {
    const root = mkdtempSync(join(tmpdir(), "car-v2-cutover-source-"));
    const path = join(root, "state.sqlite3");
    try {
      const db = new Database(path, { create: true });
      db.exec("CREATE TABLE work (id TEXT PRIMARY KEY, state TEXT NOT NULL)");
      db.exec("CREATE TABLE historical_metadata (key TEXT PRIMARY KEY, value TEXT)");
      db.query("INSERT INTO work VALUES (?, ?)").run("a", "active");
      db.query("INSERT INTO work VALUES (?, ?)").run("b", "done");
      db.query("INSERT INTO historical_metadata VALUES (?, ?)").run("version", "2");
      db.close();

      const blocked = buildV2CutoverReport(root, new Date("2026-08-27T12:00:00Z"));
      expect(blocked.verdict).toBe("blocked");
      expect(blocked.active_items).toContainEqual({
        database: "state.sqlite3",
        table: "work",
        column: "state",
        value: "active",
        count: 1,
      });
      expect(blocked.blockers.join(" ")).toContain("active row");

      const drained = new Database(path);
      drained.query("UPDATE work SET state = 'done' WHERE id = 'a'").run();
      drained.close();
      const review = buildV2CutoverReport(root);
      expect(review.verdict).toBe("review_required");
      expect(review.unclassified_nonempty_tables).toContainEqual({
        database: "state.sqlite3",
        table: "historical_metadata",
        rows: 1,
      });

      const classified = new Database(path);
      classified.exec("DROP TABLE historical_metadata");
      classified.close();
      expect(buildV2CutoverReport(root).verdict).toBe("ready_to_archive");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("writes an atomic immutable report and audits it in v3", () => {
    const root = mkdtempSync(join(tmpdir(), "car-v2-cutover-write-"));
    const source = join(root, "v2");
    const output = join(root, "v3", "migration", "report.json");
    mkdirSync(join(root, "v3"), { recursive: true });
    const store = openStore(join(root, "v3", "car.db"));
    try {
      mkdirSync(source, { recursive: true });
      writeFileSync(join(source, "README.txt"), "archived v2 state\n");
      const report = writeV2CutoverReport(store, source, output, new Date("2026-08-27T12:00:00Z"));
      expect(report.verdict).toBe("ready_to_archive");
      expect(existsSync(output)).toBe(true);
      expect(JSON.parse(readFileSync(output, "utf8"))).toMatchObject({
        contract: "car.v2-cutover-report.v1",
        source_tree_sha256: report.source_tree_sha256,
      });
      expect(
        (store.db.query("SELECT COUNT(*) AS n FROM audit WHERE verb = 'migration.cutover_audited'").get() as { n: number }).n,
      ).toBe(1);
      expect(() => writeV2CutoverReport(store, source, output)).toThrow("already exists");
    } finally {
      store.db.close();
      rmSync(root, { recursive: true, force: true });
    }
  });
});
