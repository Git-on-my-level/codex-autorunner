"use strict";

const fs = require("fs");
const path = require("path");
const { validateConfig } = require("./config-validator.cjs");

function loadCredentials(inputPath, options = {}) {
  const strictMode = Boolean(options.strict);
  const resolvedPath = path.resolve(process.cwd(), inputPath || "credentials.local.json");

  const raw = fs.readFileSync(resolvedPath, "utf8");
  const config = JSON.parse(raw);
  const validation = validateConfig(config, { strict: strictMode });

  if (!validation.ok) {
    const details = [...validation.errors, ...validation.warnings.map((w) => `WARN: ${w}`)];
    const err = new Error(`Invalid credentials config at ${resolvedPath}`);
    err.details = details;
    throw err;
  }

  const activeIntegrations = Object.fromEntries(
    Object.entries(config.integrations).filter(([, v]) => v.status === "active")
  );

  return {
    path: resolvedPath,
    config,
    activeIntegrations,
    warnings: validation.warnings
  };
}

module.exports = {
  loadCredentials
};
