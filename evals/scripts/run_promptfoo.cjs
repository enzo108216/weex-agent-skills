const path = require("node:path");
const { spawnSync } = require("node:child_process");

process.env.PROMPTFOO_DISABLE_TELEMETRY = "1";

const packageRoot = path.resolve(__dirname, "..");
const promptfooMain = path.join(
  packageRoot,
  "node_modules",
  "promptfoo",
  "dist",
  "src",
  "main.js",
);

const completed = spawnSync(process.execPath, [promptfooMain, ...process.argv.slice(2)], {
  cwd: packageRoot,
  stdio: "inherit",
  env: process.env,
});

if (completed.error) {
  console.error(completed.error.message);
  process.exit(1);
}
process.exit(completed.status ?? 1);
