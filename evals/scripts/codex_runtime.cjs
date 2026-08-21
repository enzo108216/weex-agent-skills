const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const DEFAULT_CODEX_HOME = path.join(os.homedir(), ".codex");

function parseTomlScalar(value) {
  const trimmed = value.trim();
  if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return JSON.parse(trimmed);
  }
  return trimmed.replace(/\s+#.*$/, "").trim();
}

function parseCodexConfig(configText) {
  const values = {};
  let section = "";
  for (const rawLine of configText.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const sectionMatch = line.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      section = sectionMatch[1];
      continue;
    }
    const fieldMatch = line.match(/^([A-Za-z0-9_-]+)\s*=\s*(.+)$/);
    if (!fieldMatch) continue;
    const [, key, rawValue] = fieldMatch;
    if (section === "" || section === "model_providers.custom") {
      values[section ? `${section}.${key}` : key] = parseTomlScalar(rawValue);
    }
  }
  return values;
}

function readCodexRuntime() {
  const codexHome = process.env.CODEX_HOME || DEFAULT_CODEX_HOME;
  const configPath = path.join(codexHome, "config.toml");
  const authPath = path.join(codexHome, "auth.json");
  if (!fs.existsSync(configPath)) {
    throw new Error(`Codex config not found: ${configPath}`);
  }
  if (!fs.existsSync(authPath)) {
    throw new Error(`Codex auth not found: ${authPath}`);
  }

  const config = parseCodexConfig(fs.readFileSync(configPath, "utf8"));
  const auth = JSON.parse(fs.readFileSync(authPath, "utf8"));
  const apiKey = typeof auth.OPENAI_API_KEY === "string" ? auth.OPENAI_API_KEY : "";
  if (!apiKey) {
    throw new Error("Codex auth.json does not contain an OPENAI_API_KEY value");
  }

  const providerName = config.model_provider || "custom";
  const model = config.model;
  const baseUrl = config["model_providers.custom.base_url"];
  const wireApi = config["model_providers.custom.wire_api"] || "responses";
  if (!model || !baseUrl) {
    throw new Error("Codex config is missing model or custom provider base_url");
  }
  if (wireApi !== "responses") {
    throw new Error(`Unsupported Codex wire_api for this eval: ${wireApi}`);
  }

  return {
    authPath,
    baseUrl: baseUrl.replace(/\/+$/, ""),
    model,
    providerName,
    wireApi,
  };
}

module.exports = { readCodexRuntime };
