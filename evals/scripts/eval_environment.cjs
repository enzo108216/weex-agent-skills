const os = require("node:os");
const path = require("node:path");

const SAFE_PROCESS_ENVIRONMENT = [
  "PATH",
  "Path",
  "USER",
  "USERNAME",
  "USERPROFILE",
  "TMPDIR",
  "TMP",
  "TEMP",
  "SHELL",
  "COMSPEC",
  "SystemRoot",
  "PATHEXT",
  "LANG",
  "LC_ALL",
  "TERM",
];

const SECRET_ENV_MARKERS = [
  "API_KEY",
  "API_SECRET",
  "ACCESS_TOKEN",
  "TOKEN",
  "PASSWORD",
  "PASSPHRASE",
  "PRIVATE_KEY",
  "CREDENTIAL",
  "AUTH",
  "SECRET",
];

function isSensitiveEnvironmentKey(key) {
  const uppercase = String(key).toUpperCase();
  return SECRET_ENV_MARKERS.some((marker) => uppercase.includes(marker));
}

function buildSafeEnvironment(source = process.env, { home, includeKeys = [] } = {}) {
  const environment = {};
  for (const key of SAFE_PROCESS_ENVIRONMENT) {
    if (typeof source[key] === "string" && source[key]) environment[key] = source[key];
  }
  for (const key of includeKeys) {
    if (
      typeof key === "string" &&
      /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) &&
      typeof source[key] === "string" &&
      source[key]
    ) {
      environment[key] = source[key];
    }
  }
  environment.HOME = home || path.join(os.tmpdir(), "weex-eval-home");
  return environment;
}

module.exports = {
  SAFE_PROCESS_ENVIRONMENT,
  SECRET_ENV_MARKERS,
  buildSafeEnvironment,
  isSensitiveEnvironmentKey,
};
