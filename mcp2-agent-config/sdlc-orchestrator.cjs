#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { loadCredentials } = require("./lib/load-credentials.cjs");

const STAGES = ["prd", "techdoc", "design", "coding"];

function parseArgs(argv) {
  const args = {
    credentials: "credentials.local.json",
    connectors: "connectors.map.json",
    out: "artifacts",
    strict: false,
    project: "unnamed-project"
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--credentials") args.credentials = argv[++i];
    else if (arg === "--connectors") args.connectors = argv[++i];
    else if (arg === "--out") args.out = argv[++i];
    else if (arg === "--project") args.project = argv[++i];
    else if (arg === "--strict") args.strict = true;
  }

  return args;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function getStageRequirements(connectorsMap) {
  const requirements = new Map();
  for (const stage of STAGES) requirements.set(stage, []);

  const entries = Object.entries(connectorsMap.connectors || {});
  for (const [integrationName, connector] of entries) {
    const stages = connector.required_for_stages || [];
    for (const stage of stages) {
      if (requirements.has(stage)) {
        requirements.get(stage).push(integrationName);
      }
    }
  }
  return requirements;
}

function buildStageContract(stage, requiredIntegrations, activeIntegrations, artifactPath) {
  const missing = requiredIntegrations.filter((name) => !(name in activeIntegrations));
  const pass = missing.length === 0;

  return {
    stage,
    status: pass ? "pass" : "revise",
    blocking_issues: pass
      ? []
      : missing.map((name) => `Integration '${name}' is required but not active.`),
    artifact_path: artifactPath,
    next_agent: pass ? `${stage}-agent` : "review-agent"
  };
}

function writeStageArtifact(outDir, stage, contract, context) {
  const stageDir = path.join(outDir, stage);
  fs.mkdirSync(stageDir, { recursive: true });

  const artifactPath = path.join(stageDir, "artifact.md");
  const content = [
    `# ${stage.toUpperCase()} Artifact`,
    "",
    "## Contract",
    "```json",
    JSON.stringify(contract, null, 2),
    "```",
    "",
    "## Context",
    `- project: ${context.project}`,
    `- generated_at: ${context.generatedAt}`,
    `- required_integrations: ${context.requiredIntegrations.join(", ") || "none"}`,
    `- active_integrations: ${context.activeIntegrations.join(", ") || "none"}`
  ].join("\n");

  fs.writeFileSync(artifactPath, content, "utf8");
  return artifactPath;
}

function run() {
  const args = parseArgs(process.argv.slice(2));
  const root = process.cwd();
  const outDir = path.resolve(root, args.out);
  const connectorsPath = path.resolve(root, args.connectors);

  let credentials;
  try {
    credentials = loadCredentials(args.credentials, { strict: args.strict });
  } catch (err) {
    console.error(`ERROR: ${err.message}`);
    if (Array.isArray(err.details)) {
      for (const d of err.details) console.error(`- ${d}`);
    }
    process.exit(1);
  }

  const connectorsMap = readJson(connectorsPath);
  const requirements = getStageRequirements(connectorsMap);
  const activeIntegrationNames = Object.keys(credentials.activeIntegrations);
  const generatedAt = new Date().toISOString();

  fs.mkdirSync(outDir, { recursive: true });

  const stageResults = [];
  for (const stage of STAGES) {
    const requiredIntegrations = requirements.get(stage) || [];
    const draftContract = buildStageContract(
      stage,
      requiredIntegrations,
      credentials.activeIntegrations,
      ""
    );

    const artifactPath = writeStageArtifact(
      outDir,
      stage,
      { ...draftContract, artifact_path: "" },
      {
        project: args.project,
        generatedAt,
        requiredIntegrations,
        activeIntegrations: activeIntegrationNames
      }
    );

    const finalContract = {
      ...draftContract,
      artifact_path: artifactPath
    };

    fs.writeFileSync(
      path.join(outDir, stage, "contract.json"),
      `${JSON.stringify(finalContract, null, 2)}\n`,
      "utf8"
    );

    stageResults.push(finalContract);
  }

  const firstBlocked = stageResults.find((s) => s.status !== "pass");
  const state = {
    schema_version: 1,
    generated_at: generatedAt,
    project: args.project,
    current_stage: firstBlocked ? firstBlocked.stage : "done",
    pipeline_status: firstBlocked ? "blocked" : "ready",
    approvals: [],
    retry_count: 0,
    stages: stageResults
  };

  fs.writeFileSync(path.join(outDir, "pipeline-state.json"), `${JSON.stringify(state, null, 2)}\n`, "utf8");

  console.log(`Orchestrator output created at: ${outDir}`);
  console.log(`Pipeline status: ${state.pipeline_status}`);
  console.log(`Current stage: ${state.current_stage}`);
}

run();
