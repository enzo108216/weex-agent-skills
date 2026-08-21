const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");
const promptfooMain = path.join(
  packageRoot,
  "node_modules",
  "promptfoo",
  "dist",
  "src",
  "main.js",
);
const { readCodexRuntime } = require("./codex_runtime.cjs");

function checkAuth() {
  const runtime = readCodexRuntime();
  return {
    auth_present: true,
    provider: runtime.providerName,
    model: runtime.model,
    wire_api: runtime.wireApi,
    base_url: runtime.baseUrl,
    auth_transport: "runtime-only",
    auth_source: "codex-auth.json",
    key_exposed: false,
  };
}

const args = process.argv.slice(2);
if (args[0] === "check-auth") {
  try {
    const payload = checkAuth();
    process.stdout.write(
      `${JSON.stringify(payload, null, args.includes("--pretty") ? 2 : 0)}\n`,
    );
    process.exit(0);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  }
}

process.env.PROMPTFOO_DISABLE_TELEMETRY = "1";
process.env.CODEX_HOME = process.env.CODEX_HOME || path.join(os.homedir(), ".codex");

try {
  checkAuth();
} catch (error) {
  process.stderr.write(`Codex authentication preflight failed: ${error.message}\n`);
  process.exit(1);
}

const completed = spawnSync(process.execPath, [promptfooMain, ...args], {
  cwd: packageRoot,
  stdio: "inherit",
  env: process.env,
});

if (completed.error) {
  process.stderr.write(`${completed.error.message}\n`);
  process.exit(1);
}
process.exit(completed.status ?? 1);
