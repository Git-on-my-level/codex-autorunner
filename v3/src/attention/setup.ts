/** Local operator setup. These commands are not exposed through the agent HTTP/MCP surface. */
import { existsSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { homedir, hostname } from "node:os";
import { parse, stringify } from "smol-toml";
import { privateDirectory, readPrivateJson, writePrivateJson, syncDirectory } from "./files.ts";
import { checkedServerUrl } from "./client.ts";

export function initialize(configPath = join(homedir(), ".car", "config.toml")) {
  configPath = resolve(configPath);
  const stateDir = dirname(configPath);
  const credentialsPath = join(stateDir, "credentials.json");
  const connectionPath = join(stateDir, "agent.json");
  for (const path of [configPath, credentialsPath, connectionPath]) if (existsSync(path)) throw new Error(`Refusing to overwrite ${path}. Choose a different empty directory; setup never overwrites existing state.`);
  privateDirectory(stateDir);
  // The human web token is opt-in. Local/trusted workspaces open the UI
  // directly; operators who need a login can add a web token later.
  const secrets = { CAR_AGENT_TOKEN: randomBytes(32).toString("base64url") };
  if (!writePrivateJson(credentialsPath, secrets) || !writePrivateJson(connectionPath, { url: "http://127.0.0.1:7171", token: secrets.CAR_AGENT_TOKEN, client_id: "local" })) throw new Error("Setup files already exist; nothing was overwritten");
  const config = {
    state_dir: stateDir, credentials_file: credentialsPath,
    http: { host: "127.0.0.1", port: 7171, private_reads: true, web_auth: "optional" },
    attention: { workspace_id: "default", clients: { local: { token_env: "CAR_AGENT_TOKEN", host: hostname() } } },
  };
  writeFileSync(configPath, stringify(config), { flag: "wx", mode: 0o600 });
  return { config: configPath, agent_connection: connectionPath, credentials_file: credentialsPath,
    next_action: `Start card serve --config ${JSON.stringify(configPath)}; open http://127.0.0.1:7171/ui. The local UI is open by default; set http.web_auth = "required" and configure a web token when a login is needed. Give agents only agent.json, never credentials.json.` };
}
export function addClient(input: { configPath?: string; name: string; host: string; url: string; output: string; allowHttp?: boolean }) {
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(input.name) || !input.host || input.host.length > 128) throw new Error("Use a short client name and the source host name");
  const origin = checkedServerUrl(input.url, input.allowHttp);
  const configPath = resolve(input.configPath ?? join(homedir(), ".car", "config.toml"));
  const config = parse(readFileSync(configPath, "utf8")) as Record<string, any>;
  if (typeof config.credentials_file !== "string") throw new Error("client add requires an explicit credentials_file. Initialize a workspace with card init before adding clients.");
  const credentialsPath = resolve(dirname(configPath), config.credentials_file);
  const secrets = readPrivateJson(credentialsPath) as Record<string, string>;
  config.attention ??= {}; config.attention.clients ??= {};
  if (config.attention.clients[input.name]) throw new Error("That client already exists; use a distinct name for each host");
  if (existsSync(input.output)) throw new Error("Connection output already exists; refusing to overwrite it");
  const env = `CAR_CLIENT_${input.name.toUpperCase().replaceAll("-", "_")}_TOKEN`;
  if (secrets[env] || Object.values(config.attention.clients).some((value: any) => value.token_env === env)) throw new Error("That name collides with an existing credential; choose another name");
  const token = randomBytes(32).toString("base64url");
  // Secret first: a crash here leaves an unused secret, not an enabled identity
  // with an unknown credential. Do not hot-reload partially updated config.
  writePrivateJson(credentialsPath, { ...secrets, [env]: token }, true);
  config.attention.clients[input.name] = { token_env: env, host: input.host };
  const text = stringify(config);
  // Configuration carries no raw tokens. Atomic rename prevents a torn config.
  const temp = `${configPath}.${randomBytes(8).toString("hex")}.tmp`;
  writeFileSync(temp, text, { flag: "wx", mode: 0o600 });
  const fs = require("node:fs") as typeof import("node:fs");
  const fd = fs.openSync(temp, "r"); try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(temp, configPath); chmodSync(configPath, 0o600); syncDirectory(dirname(configPath));
  if (!writePrivateJson(resolve(input.output), { url: origin, token, client_id: input.name, allow_http: Boolean(input.allowHttp) })) throw new Error("Connection output was created concurrently. Remove the new client explicitly and retry with another output path.");
  return { client: input.name, host: input.host, connection_file: resolve(input.output), next_action: "Restart CAR after configuration changes. Transfer only this connection file to its host and set CAR_CONNECTION_FILE. Never expose the human credential." };
}
