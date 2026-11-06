/**
 * Action/probe template registry — the closed vocabulary of things CAR may
 * execute (hostctl's typed-gate pattern, DESIGN §5).
 *
 * Templates live in `<state_dir>/templates.toml`; the file is data, code is the
 * executor. A template is an argv ARRAY with typed `{placeholders}`; there is no
 * shell anywhere, and argument values are validated against the declared types
 * and rejected outright if they carry shell metacharacters (defence in depth:
 * even though argv-exec makes them inert, a value that looks like an injection
 * attempt is a signal, not an input).
 */
import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { parse as parseToml } from "smol-toml";
import { z } from "zod";
import type { CarConfig } from "../config/config.ts";
import { HASH_SEP } from "../contract/events.ts";

export const ArgSpec = z.object({
  type: z.enum(["string", "path", "int", "bool", "enum"]).default("string"),
  required: z.boolean().default(true),
  /** Allowed values for `type = "enum"` (author-controlled, so trusted verbatim). */
  values: z.array(z.string()).default([]),
  max_len: z.number().int().positive().default(256),
  /** Value used when the caller omits an optional argument. */
  default: z.union([z.string(), z.number(), z.boolean()]).optional(),
});
export type ArgSpec = z.infer<typeof ArgSpec>;

export const TemplateSpec = z.object({
  /** argv array; elements may contain `{arg_name}` placeholders. Never a shell string. */
  argv: z.array(z.string()).min(1),
  args: z.record(z.string(), ArgSpec).prefault({}),
  /** Mutating templates are gated as `exec.<id>`; read-only ones as `probe`. */
  mutating: z.boolean().default(false),
  timeout_ms: z.number().int().positive().max(600_000).default(30_000),
  /** Optional working directory; may contain placeholders. */
  cwd: z.string().optional(),
  /** Optional policy class override; defaults to the id-derived class. */
  class: z.string().optional(),
  description: z.string().default(""),
});
export type TemplateSpec = z.infer<typeof TemplateSpec>;

export const TemplatesFile = z.object({
  templates: z.record(z.string(), TemplateSpec).prefault({}),
});
export type TemplatesFile = z.infer<typeof TemplatesFile>;

/**
 * Shipped defaults, used verbatim when `<state_dir>/templates.toml` is absent.
 * Read-only probes only — nothing here can change the world.
 */
export const DEFAULT_TEMPLATES_TOML = `# CAR v3 action/probe templates.
# Copied from the shipped defaults when ~/.car/templates.toml is absent.
# argv is an ARRAY and is exec'd directly: no shell, no quoting, no injection.
# Placeholders are {arg_name} and must be declared under [templates.<id>.args].

[templates."git.status"]
description = "Working tree + branch state of a repo (read-only)."
argv = ["git", "-C", "{repo}", "status", "--short", "--branch"]
mutating = false
timeout_ms = 15000
[templates."git.status".args]
repo = { type = "path", required = true, max_len = 1024 }

[templates."git.log"]
description = "Recent commits on the current branch (read-only)."
argv = ["git", "-C", "{repo}", "log", "--oneline", "-n", "{count}"]
mutating = false
timeout_ms = 15000
[templates."git.log".args]
repo = { type = "path", required = true, max_len = 1024 }
count = { type = "int", required = false, max_len = 4, default = 20 }

[templates."agentctl.status"]
description = "One normalized agentctl execution envelope (read-only)."
argv = ["agentctl", "status", "{execution_id}"]
mutating = false
timeout_ms = 20000
[templates."agentctl.status".args]
execution_id = { type = "string", required = true, max_len = 128 }

[templates."agentctl.recent"]
description = "Recent agentctl executions (read-only)."
argv = ["agentctl", "recent", "--limit", "{limit}"]
mutating = false
timeout_ms = 20000
[templates."agentctl.recent".args]
limit = { type = "int", required = false, max_len = 3, default = 20 }
`;

export function templatesPath(cfg: CarConfig): string {
  return join(cfg.state_dir, "templates.toml");
}

export interface LoadedTemplates {
  templates: Record<string, TemplateSpec>;
  source: "file" | "defaults";
  path: string;
  mtimeMs: number;
}

export function parseTemplates(text: string): Record<string, TemplateSpec> {
  return TemplatesFile.parse(parseToml(text)).templates;
}

/** Load templates.toml, or the shipped defaults when it is absent. Throws on invalid TOML/schema. */
export function loadTemplates(cfg: CarConfig): LoadedTemplates {
  const path = templatesPath(cfg);
  let text: string;
  let mtimeMs = 0;
  try {
    text = readFileSync(path, "utf8");
    mtimeMs = statSync(path).mtimeMs;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
    return { templates: parseTemplates(DEFAULT_TEMPLATES_TOML), source: "defaults", path, mtimeMs: 0 };
  }
  return { templates: parseTemplates(text), source: "file", path, mtimeMs };
}

/* ------------------------------------------------------------ validation */

/**
 * Shell metacharacters. We exec argv arrays so these are inert, but a value
 * containing one is refused loudly rather than passed through quietly.
 */
export const SHELL_METACHARACTERS = /[;&|`$<>(){}\[\]!*?~"'\\\n\r\t\0]/;

export class TemplateError extends Error {
  constructor(
    message: string,
    readonly reason: string,
  ) {
    super(message);
    this.name = "TemplateError";
  }
}

/** Normalized, stringified argument values ready for substitution. */
export type ResolvedArgs = Record<string, string>;

export function validateArgs(
  templateId: string,
  spec: TemplateSpec,
  args: Record<string, unknown>,
): ResolvedArgs {
  const out: ResolvedArgs = {};

  for (const name of Object.keys(args)) {
    if (!(name in spec.args)) {
      throw new TemplateError(`template ${templateId}: unknown argument '${name}'`, "unknown_arg");
    }
  }

  for (const [name, argSpec] of Object.entries(spec.args)) {
    const supplied = args[name];
    const raw = supplied === undefined || supplied === null ? argSpec.default : supplied;
    if (raw === undefined || raw === null) {
      if (argSpec.required) {
        throw new TemplateError(`template ${templateId}: missing required argument '${name}'`, "missing_arg");
      }
      continue;
    }
    out[name] = coerceArg(templateId, name, argSpec, raw);
  }
  return out;
}

function coerceArg(templateId: string, name: string, spec: ArgSpec, raw: unknown): string {
  const where = `template ${templateId}: argument '${name}'`;
  switch (spec.type) {
    case "bool": {
      if (typeof raw !== "boolean") throw new TemplateError(`${where} must be a boolean`, "bad_type");
      return raw ? "true" : "false";
    }
    case "int": {
      const n = typeof raw === "number" ? raw : typeof raw === "string" ? Number(raw) : NaN;
      if (!Number.isInteger(n)) throw new TemplateError(`${where} must be an integer`, "bad_type");
      const s = String(n);
      if (s.length > spec.max_len) throw new TemplateError(`${where} exceeds max_len`, "too_long");
      return s;
    }
    case "enum": {
      if (typeof raw !== "string") throw new TemplateError(`${where} must be a string`, "bad_type");
      if (!spec.values.includes(raw)) {
        throw new TemplateError(`${where} is not one of the allowed values`, "not_allowed");
      }
      return raw;
    }
    case "path":
    case "string": {
      if (typeof raw !== "string") throw new TemplateError(`${where} must be a string`, "bad_type");
      if (raw.length === 0) throw new TemplateError(`${where} must not be empty`, "bad_type");
      if (raw.length > spec.max_len) throw new TemplateError(`${where} exceeds max_len`, "too_long");
      if (SHELL_METACHARACTERS.test(raw)) {
        throw new TemplateError(`${where} contains shell metacharacters`, "shell_metacharacter");
      }
      // Refuse flag-shaped values: argv-exec makes injection into the *command*
      // impossible, but a value starting with '-' is still option injection.
      if (raw.startsWith("-")) {
        throw new TemplateError(`${where} must not start with '-'`, "flag_injection");
      }
      if (spec.type === "path" && raw.includes("..")) {
        throw new TemplateError(`${where} must not contain '..'`, "path_traversal");
      }
      return raw;
    }
  }
}

const PLACEHOLDER = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;

/** Substitute `{name}` placeholders inside one argv element. Never splits an element. */
export function substitute(templateId: string, element: string, args: ResolvedArgs): string {
  return element.replace(PLACEHOLDER, (_m, name: string) => {
    const value = args[name];
    if (value === undefined) {
      throw new TemplateError(
        `template ${templateId}: placeholder '{${name}}' has no value`,
        "unbound_placeholder",
      );
    }
    return value;
  });
}

/** Build the final argv (and cwd) for a validated template invocation. */
export function buildArgv(
  templateId: string,
  spec: TemplateSpec,
  args: ResolvedArgs,
): { argv: string[]; cwd?: string } {
  // Every element is substituted in place: an argv slot never splits, and an
  // unbound placeholder is an error rather than a silently dropped argument.
  const argv = spec.argv.map((element) => substitute(templateId, element, args));
  if (argv.length === 0 || !argv[0]) {
    throw new TemplateError(`template ${templateId}: empty argv`, "empty_argv");
  }
  const cwd = spec.cwd ? substitute(templateId, spec.cwd, args) : undefined;
  return { argv, cwd };
}

/**
 * Policy class for a template. Read-only templates share the `probe` class
 * (DESIGN §5 `[classes.probe]`); mutating ones get their own `exec.<id>` class
 * (`[classes.exec.restart_service]`). `class = ` in the template overrides.
 */
export function policyClassFor(templateId: string, spec: TemplateSpec): string {
  if (spec.class) return spec.class;
  return spec.mutating ? `exec.${templateId}` : "probe";
}

/** Stable dedupe hash over (template id, args) — DESIGN §5 runaway protection. */
export function dedupeHash(templateId: string, args: Record<string, unknown>): string {
  const canonical = JSON.stringify(
    Object.keys(args)
      .sort()
      .map((k) => [k, args[k]]),
  );
  return new Bun.CryptoHasher("sha256").update([templateId, canonical].join(HASH_SEP)).digest("hex");
}
