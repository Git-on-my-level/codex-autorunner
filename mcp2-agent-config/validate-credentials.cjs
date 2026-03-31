#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { validateConfig } = require("./lib/config-validator.cjs");

function main() {
  const args = process.argv.slice(2);
  const strictMode = args.includes("--strict");
  const fileArg = args.find((arg) => arg !== "--strict");

  const inputPath = fileArg
    ? path.resolve(process.cwd(), fileArg)
    : path.resolve(process.cwd(), "credentials.local.json");

  let raw;
  try {
    raw = fs.readFileSync(inputPath, "utf8");
  } catch (err) {
    console.error(`ERROR: Cannot read file: ${inputPath}`);
    console.error(`DETAIL: ${err.message}`);
    process.exit(1);
  }

  let config;
  try {
    config = JSON.parse(raw);
  } catch (err) {
    console.error(`ERROR: Invalid JSON in ${inputPath}`);
    console.error(`DETAIL: ${err.message}`);
    process.exit(1);
  }

  const { ok, errors, warnings } = validateConfig(config, { strict: strictMode });

  if (!ok) {
    console.error("VALIDATION FAILED");
    for (const err of errors) {
      console.error(`- ERROR: ${err}`);
    }
    for (const warn of warnings) {
      console.error(`- WARN: ${warn}`);
    }
    process.exit(1);
  }

  console.log(`VALIDATION OK${strictMode ? " (strict)" : ""}`);
  if (warnings.length > 0) {
    for (const warn of warnings) {
      console.log(`- WARN: ${warn}`);
    }
  }
}

main();
