const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const DEFAULT_CODEX_HOME = path.join(os.homedir(), ".codex");
const REQUIRED_EVAL_ENVIRONMENT = [
  "WEEX_CODEX_EVAL_MODEL",
  "WEEX_CODEX_EVAL_MODEL_PROVIDER",
  "WEEX_CODEX_EVAL_REASONING_EFFORT",
];
const REASONING_EFFORTS = new Set([
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
]);

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
    if (section === "" || section.startsWith("model_providers.")) {
      values[section ? `${section}.${key}` : key] = parseTomlScalar(rawValue);
    }
  }
  return values;
}

function requiredEnvironmentValue(environment, key) {
  const value = typeof environment[key] === "string" ? environment[key].trim() : "";
  if (!value) {
    throw new Error(`Codex evaluation requires local environment variable ${key}`);
  }
  return value;
}

function readEvaluationTarget(environment = process.env) {
  const target = Object.fromEntries(
    REQUIRED_EVAL_ENVIRONMENT.map((key) => [key, requiredEnvironmentValue(environment, key)]),
  );
  const reasoningEffort = target.WEEX_CODEX_EVAL_REASONING_EFFORT;
  if (!REASONING_EFFORTS.has(reasoningEffort)) {
    throw new Error(
      `reasoning effort WEEX_CODEX_EVAL_REASONING_EFFORT must be one of ${[...REASONING_EFFORTS].join(", ")}`,
    );
  }
  return {
    model: target.WEEX_CODEX_EVAL_MODEL,
    providerName: target.WEEX_CODEX_EVAL_MODEL_PROVIDER,
    reasoningEffort,
  };
}

function readCodexRuntime(environment = process.env) {
  const codexHome = environment.CODEX_HOME || DEFAULT_CODEX_HOME;
  const configPath = path.join(codexHome, "config.toml");
  if (!fs.existsSync(configPath)) {
    throw new Error(`Codex config not found: ${configPath}`);
  }

  const config = parseCodexConfig(fs.readFileSync(configPath, "utf8"));
  const model = config.model;
  const providerName = config.model_provider || "custom";
  const providerPrefix = `model_providers.${providerName}`;
  const baseUrl = config[`${providerPrefix}.base_url`];
  const providerAuthEnvironmentKey = config[`${providerPrefix}.env_key`];
  const wireApi = config[`${providerPrefix}.wire_api`] || "responses";
  if (!model) {
    throw new Error("Codex config is missing model");
  }
  if (providerName === "custom" && !baseUrl) {
    throw new Error("Codex custom provider config is missing base_url");
  }
  if (wireApi !== "responses") {
    throw new Error(`Unsupported Codex wire_api for this eval: ${wireApi}`);
  }
  if (
    providerAuthEnvironmentKey !== undefined &&
    !/^[A-Za-z_][A-Za-z0-9_]*$/.test(providerAuthEnvironmentKey)
  ) {
    throw new Error("Codex custom provider env_key is not a valid environment variable name");
  }
  if (providerAuthEnvironmentKey && !String(environment[providerAuthEnvironmentKey] || "").trim()) {
    throw new Error(`Codex custom provider requires local environment variable ${providerAuthEnvironmentKey}`);
  }

  const target = readEvaluationTarget(environment);
  if (target.model !== model) {
    throw new Error(
      `WEEX_CODEX_EVAL_MODEL=${target.model} does not match current Codex config model=${model}`,
    );
  }
  if (target.providerName !== providerName) {
    throw new Error(
      "WEEX_CODEX_EVAL_MODEL_PROVIDER does not match current Codex config model_provider",
    );
  }
  if (config.model_reasoning_effort && config.model_reasoning_effort !== target.reasoningEffort) {
    throw new Error(
      `WEEX_CODEX_EVAL_REASONING_EFFORT=${target.reasoningEffort} does not match current Codex config model_reasoning_effort=${config.model_reasoning_effort}`,
    );
  }

  return {
    configPath,
    model,
    providerName,
    providerAuthEnvironmentKey: providerAuthEnvironmentKey || null,
    reasoningEffort: target.reasoningEffort,
    wireApi,
  };
}

module.exports = {
  DEFAULT_CODEX_HOME,
  REQUIRED_EVAL_ENVIRONMENT,
  parseCodexConfig,
  readCodexRuntime,
  readEvaluationTarget,
};
