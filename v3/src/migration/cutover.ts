import { Database } from "bun:sqlite";
import {
  copyFileSync,
  lstatSync,
  mkdirSync,
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, extname, join, relative, resolve } from "node:path";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import type { Store } from "../store/db.ts";

const SQLITE_EXTENSIONS = new Set([".db", ".sqlite", ".sqlite3"]);
const ACTIVE_VALUES = new Set([
  "active",
  "assigned",
  "blocked",
  "claimed",
  "coalescing",
  "deferred",
  "in_progress",
  "open",
  "pending",
  "queued",
  "retry",
  "running",
  "sending",
  "started",
  "waiting",
]);
const STATE_COLUMNS = ["state", "status", "triage_state", "route_state", "lifecycle_state"];
const MAX_FILES = 10_000;
const MAX_DATABASES = 512;

export interface CutoverStateCount {
  column: string;
  value: string | null;
  count: number;
  active: boolean;
}

export interface CutoverTableReport {
  table: string;
  rows: number;
  state_counts: CutoverStateCount[];
  classification: "empty" | "terminal" | "active" | "unclassified";
}

export interface CutoverDatabaseReport {
  path: string;
  size_bytes: number;
  sha256: string;
  journal_evidence: string[];
  tables: CutoverTableReport[];
  error?: string;
}

export interface CutoverReport {
  contract: "car.v2-cutover-report.v1";
  generated_at: string;
  source_root: string;
  source_file_count: number;
  source_total_bytes: number;
  source_tree_sha256: string;
  databases: CutoverDatabaseReport[];
  active_items: Array<{ database: string; table: string; column: string; value: string | null; count: number }>;
  unclassified_nonempty_tables: Array<{ database: string; table: string; rows: number }>;
  blockers: string[];
  verdict: "blocked" | "review_required" | "ready_to_archive";
  next_action: string;
}

interface InventoryFile {
  absolute: string;
  relative: string;
  size: number;
  sha256: string;
}

function quoteIdentifier(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function hashFile(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function inventoryTree(root: string): { files: InventoryFile[]; blockers: string[] } {
  const files: InventoryFile[] = [];
  const blockers: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(dir, entry.name);
      const rel = relative(root, path);
      const stat = lstatSync(path);
      if (stat.isSymbolicLink()) {
        blockers.push(`symbolic link requires explicit review: ${rel}`);
        continue;
      }
      if (entry.isDirectory()) {
        walk(path);
        continue;
      }
      if (!entry.isFile()) continue;
      if (files.length >= MAX_FILES) throw new Error(`source inventory exceeds ${MAX_FILES} files`);
      files.push({ absolute: path, relative: rel, size: stat.size, sha256: hashFile(path) });
    }
  };
  walk(root);
  return { files, blockers };
}

function inspectDatabase(root: string, file: InventoryFile, allFiles: Set<string>): CutoverDatabaseReport {
  const journalEvidence = [`${file.absolute}-wal`, `${file.absolute}-journal`, `${file.absolute}-shm`]
    .filter((path) => allFiles.has(path) && statSync(path).size > 0)
    .map((path) => relative(root, path));
  const report: CutoverDatabaseReport = {
    path: file.relative,
    size_bytes: file.size,
    sha256: file.sha256,
    journal_evidence: journalEvidence,
    tables: [],
  };
  const inspectionDir = mkdtempSync(join(tmpdir(), "car-v2-db-inspect-"));
  const inspectionPath = join(inspectionDir, basename(file.absolute));
  try {
    // Inspect a private copy so even WAL shared-memory initialization cannot
    // mutate the legacy source tree. Journal evidence still blocks cutover.
    copyFileSync(file.absolute, inspectionPath);
    for (const suffix of ["-wal", "-journal", "-shm"]) {
      const source = `${file.absolute}${suffix}`;
      if (allFiles.has(source)) copyFileSync(source, `${inspectionPath}${suffix}`);
    }
    const db = new Database(inspectionPath, { readonly: true, strict: true });
    try {
      db.exec("PRAGMA query_only = ON;");
      const tables = db
        .query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        .all() as Array<{ name: string }>;
      for (const { name } of tables) {
        const table = quoteIdentifier(name);
        const rows = Number((db.query(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }).n);
        const columns = db.query(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
        const stateColumns = STATE_COLUMNS.filter((candidate) => columns.some((column) => column.name === candidate));
        const stateCounts: CutoverStateCount[] = [];
        for (const column of stateColumns) {
          const quotedColumn = quoteIdentifier(column);
          const counts = db
            .query(`SELECT ${quotedColumn} AS value, COUNT(*) AS n FROM ${table} GROUP BY ${quotedColumn}`)
            .all() as Array<{ value: unknown; n: number }>;
          for (const count of counts) {
            const value = count.value === null ? null : String(count.value);
            stateCounts.push({
              column,
              value,
              count: Number(count.n),
              active: value !== null && ACTIVE_VALUES.has(value.toLowerCase()),
            });
          }
        }
        const classification: CutoverTableReport["classification"] =
          rows === 0
            ? "empty"
            : stateCounts.some((count) => count.active)
              ? "active"
              : stateColumns.length > 0
                ? "terminal"
                : "unclassified";
        report.tables.push({ table: name, rows, state_counts: stateCounts, classification });
      }
    } finally {
      db.close();
    }
  } catch (error) {
    report.error = error instanceof Error ? error.message : String(error);
  } finally {
    rmSync(inspectionDir, { recursive: true, force: true });
  }
  return report;
}

/**
 * Inventory a quiesced v2 root without mutating it. This intentionally does not
 * synthesize v3 events: unknown history remains visible for a human import decision.
 */
export function buildV2CutoverReport(sourceRoot: string, now = new Date()): CutoverReport {
  const requested = resolve(sourceRoot);
  const root = realpathSync(requested);
  if (!lstatSync(root).isDirectory()) throw new Error(`v2 source root is not a directory: ${requested}`);

  const { files, blockers } = inventoryTree(root);
  const sqliteFiles = files.filter((file) => SQLITE_EXTENSIONS.has(extname(file.relative).toLowerCase()));
  if (sqliteFiles.length > MAX_DATABASES) throw new Error(`source inventory exceeds ${MAX_DATABASES} SQLite databases`);
  const absoluteFiles = new Set(files.map((file) => file.absolute));
  const databases = sqliteFiles.map((file) => inspectDatabase(root, file, absoluteFiles));
  const activeItems = databases.flatMap((database) =>
    database.tables.flatMap((table) =>
      table.state_counts
        .filter((count) => count.active)
        .map((count) => ({
          database: database.path,
          table: table.table,
          column: count.column,
          value: count.value,
          count: count.count,
        })),
    ),
  );
  const unclassified = databases.flatMap((database) =>
    database.tables
      .filter((table) => table.classification === "unclassified")
      .map((table) => ({ database: database.path, table: table.table, rows: table.rows })),
  );
  for (const database of databases) {
    if (database.error) blockers.push(`cannot inspect ${database.path}: ${database.error}`);
    if (database.journal_evidence.length > 0) {
      blockers.push(`source may be live or uncheckpointed: ${database.journal_evidence.join(", ")}`);
    }
  }
  if (activeItems.length > 0) blockers.push(`${activeItems.reduce((sum, item) => sum + item.count, 0)} active row(s) require drain or explicit import`);

  const treeHash = createHash("sha256");
  for (const file of files) treeHash.update(`${file.relative}\0${file.size}\0${file.sha256}\n`);
  const verdict = blockers.length > 0 ? "blocked" : unclassified.length > 0 ? "review_required" : "ready_to_archive";
  return {
    contract: "car.v2-cutover-report.v1",
    generated_at: now.toISOString(),
    source_root: root,
    source_file_count: files.length,
    source_total_bytes: files.reduce((sum, file) => sum + file.size, 0),
    source_tree_sha256: treeHash.digest("hex"),
    databases,
    active_items: activeItems,
    unclassified_nonempty_tables: unclassified,
    blockers,
    verdict,
    next_action:
      verdict === "blocked"
        ? "Stop v2 writers, checkpoint databases, and drain or explicitly import every active row; then rerun this audit."
        : verdict === "review_required"
          ? "Classify the listed nonempty tables in the human-reviewed import record before archiving v2."
          : "Record human approval, archive this exact source tree read-only, and switch adapters to the single v3 authority.",
  };
}

export function writeV2CutoverReport(store: Store, sourceRoot: string, outputPath: string, now = new Date()): CutoverReport {
  const report = buildV2CutoverReport(sourceRoot, now);
  const target = resolve(outputPath);
  if (existsSync(target)) throw new Error(`cutover report already exists: ${target}`);
  mkdirSync(dirname(target), { recursive: true });
  const temporary = join(dirname(target), `.${basename(target)}.${process.pid}.tmp`);
  writeFileSync(temporary, `${JSON.stringify(report, null, 2)}\n`, { flag: "wx", mode: 0o600 });
  renameSync(temporary, target);
  store.audit("human", "migration.cutover_audited", "migration_report", target, {
    source_root: report.source_root,
    source_tree_sha256: report.source_tree_sha256,
    verdict: report.verdict,
    active_items: report.active_items.reduce((sum, item) => sum + item.count, 0),
    blockers: report.blockers,
  });
  return report;
}

export function defaultCutoverReportPath(stateDir: string, now = new Date()): string {
  return join(stateDir, "migration", `v2-cutover-${now.toISOString().replaceAll(":", "-")}.json`);
}
