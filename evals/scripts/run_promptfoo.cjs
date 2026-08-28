const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { buildSafeEnvironment } = require("./eval_environment.cjs");

const packageRoot = path.resolve(__dirname, "..");
const promptfooMain = path.join(
  packageRoot,
  "node_modules",
  "promptfoo",
  "dist",
  "src",
  "main.js",
);

function buildEvalProcessEnv(source = process.env) {
  const environment = buildSafeEnvironment(source, { home: source.HOME });
  environment.PROMPTFOO_DISABLE_TELEMETRY = "1";
  environment.PROMPTFOO_DISABLE_UPDATE = "1";
  environment.PROMPTFOO_DISABLE_SHARING = "1";
  environment.PROMPTFOO_CONFIG_DIR = path.join(packageRoot, ".promptfoo");
  environment.PROMPTFOO_LOG_DIR = path.join(packageRoot, ".promptfoo", "logs");
  return environment;
}

function validateLocalEvalArguments(argv) {
  if (argv[0] !== "eval") return [...argv];
  const configPaths = [];
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--config" || argv[index] === "-c") {
      if (argv[index + 1]) configPaths.push(argv[index + 1]);
      index += 1;
    } else if (argv[index].startsWith("--config=")) {
      configPaths.push(argv[index].slice("--config=".length));
    }
  }
  if (configPaths.length > 0) {
    const expected = path.resolve(packageRoot, "promptfooconfig.yaml");
    if (configPaths.some((configPath) => path.resolve(packageRoot, configPath) !== expected)) {
      throw new Error("Local eval runner only permits promptfooconfig.yaml");
    }
    return [...argv];
  }
  return [argv[0], "--config", "promptfooconfig.yaml", ...argv.slice(1)];
}

function main(argv = process.argv.slice(2), source = process.env, spawn = spawnSync) {
  const temporaryHome = fs.mkdtempSync(path.join(os.tmpdir(), "weex-local-promptfoo-home-"));
  try {
    const environment = buildEvalProcessEnv(source);
    environment.HOME = temporaryHome;
    const safeArguments = validateLocalEvalArguments(argv);
    const completed = spawn(process.execPath, [promptfooMain, ...safeArguments], {
      cwd: packageRoot,
      stdio: "inherit",
      env: environment,
      shell: false,
    });
    if (completed.error) {
      console.error(completed.error.message);
      return 1;
    }
    return completed.status ?? 1;
  } finally {
    fs.rmSync(temporaryHome, { recursive: true, force: true });
  }
}

if (require.main === module) process.exit(main());

module.exports = { buildEvalProcessEnv, main, validateLocalEvalArguments };
