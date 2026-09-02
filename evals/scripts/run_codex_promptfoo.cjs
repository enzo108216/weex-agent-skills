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
const modelCaseCatalog = path.join(packageRoot, "cases", "codex-model-tests.json");
const { DEFAULT_CODEX_HOME, readCodexRuntime } = require("./codex_runtime.cjs");

const { buildSafeEnvironment } = require("./eval_environment.cjs");

const SAFE_PROCESS_ENVIRONMENT = [
  "CODEX_HOME",
  "WEEX_CODEX_EVAL_MODEL",
  "WEEX_CODEX_EVAL_MODEL_PROVIDER",
  "WEEX_CODEX_EVAL_REASONING_EFFORT",
  "WEEX_CODEX_EVAL_REPEAT",
];
const VALID_ROUTES = new Set(["analysis", "monitor", "partner", "trader", "clarify", "refuse"]);
const VALID_ROUTING_MODES = new Set(["guided_policy", "auto_router"]);
const VALID_LANGUAGES = new Set(["zh", "en"]);
const VALID_SKILLS = new Set([
  "weex-analysis-skill",
  "weex-monitor-skill",
  "weex-partner-skill",
  "weex-trader-skill",
]);

function buildEvalProcessEnv(source = process.env, providerAuthEnvironmentKey = null) {
  const environment = buildSafeEnvironment(source, {
    home: source.HOME,
    includeKeys: SAFE_PROCESS_ENVIRONMENT,
  });
  if (
    providerAuthEnvironmentKey &&
    /^[A-Za-z_][A-Za-z0-9_]*$/.test(providerAuthEnvironmentKey) &&
    typeof source[providerAuthEnvironmentKey] === "string" &&
    source[providerAuthEnvironmentKey]
  ) {
    environment[providerAuthEnvironmentKey] = source[providerAuthEnvironmentKey];
  }
  environment.CODEX_HOME ||= source.CODEX_HOME || DEFAULT_CODEX_HOME || path.join(os.homedir(), ".codex");
  environment.PROMPTFOO_DISABLE_TELEMETRY = "1";
  environment.PROMPTFOO_DISABLE_UPDATE = "1";
  environment.PROMPTFOO_DISABLE_SHARING = "1";
  environment.PROMPTFOO_CONFIG_DIR = path.join(packageRoot, ".promptfoo");
  environment.PROMPTFOO_LOG_DIR = path.join(packageRoot, ".promptfoo", "logs");
  return environment;
}

function validateModelCaseCatalog(catalogPath = modelCaseCatalog) {
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(catalogPath, "utf8"));
  } catch (error) {
    throw new Error(`Codex model case catalog cannot be read: ${error.message}`);
  }
  if (!Array.isArray(payload) || payload.length < 1) {
    throw new Error("Codex model case catalog must be a non-empty array");
  }
  const descriptions = new Set();
  const caseIds = new Set();
  const routingModeCounts = { guided_policy: 0, auto_router: 0 };
  for (const item of payload) {
    if (!item || typeof item !== "object" || !item.vars || typeof item.vars !== "object") {
      throw new Error("Codex model case must contain an object vars field");
    }
    const description = String(item.description || "").trim();
    if (!description || descriptions.has(description)) {
      throw new Error(`Codex model case descriptions must be unique: ${description || "<empty>"}`);
    }
    descriptions.add(description);
    const caseId = String(item.vars.case_id || "").trim();
    if (!caseId || caseIds.has(caseId)) {
      throw new Error(`Codex model case case_id values must be non-empty and unique: ${caseId || "<empty>"}`);
    }
    caseIds.add(caseId);
    const routingMode = String(item.vars.routing_mode || "").trim();
    if (!VALID_ROUTING_MODES.has(routingMode)) {
      throw new Error(`Codex model case ${description} contains an invalid routing_mode`);
    }
    routingModeCounts[routingMode] += 1;
    if (routingMode === "guided_policy") {
      if (typeof item.vars.skill !== "string" || !VALID_SKILLS.has(item.vars.skill.trim())) {
        throw new Error(`Codex model case ${description} contains an invalid skill`);
      }
    } else if (Object.prototype.hasOwnProperty.call(item.vars, "skill")) {
      throw new Error(`Codex model case ${description} must omit skill in auto_router mode`);
    }
    if (typeof item.vars.scenario_type !== "string" || !item.vars.scenario_type.trim()) {
      throw new Error(`Codex model case ${description} requires a non-empty scenario_type`);
    }
    if (!VALID_LANGUAGES.has(item.vars.language)) {
      throw new Error(`Codex model case ${description} contains an invalid language`);
    }
    if (typeof item.vars.query !== "string" || !item.vars.query.trim()) {
      throw new Error(`Codex model case ${description} requires a non-empty query`);
    }
    if (typeof item.vars.expected_route !== "string" || !item.vars.expected_route.trim()) {
      throw new Error(`Codex model case ${description} is missing expected_route`);
    }
    const expectedRoutes = item.vars.expected_route.split("|").map((route) => route.trim());
    if (expectedRoutes.some((route) => !VALID_ROUTES.has(route))) {
      throw new Error(`Codex model case ${description} contains an invalid expected_route`);
    }
    if (item.vars.expected_operation !== undefined) {
      if (typeof item.vars.expected_operation !== "string") {
        throw new Error(`Codex model case ${description} has invalid expected_operation`);
      }
      const expectedOperations = item.vars.expected_operation.split("|").map((value) => value.trim());
      if (
        expectedOperations.some(
          (operation) => !operation || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(operation),
        )
      ) {
        throw new Error(`Codex model case ${description} has invalid expected_operation`);
      }
    }
    if (typeof item.vars.requires_confirmation !== "boolean") {
      throw new Error(`Codex model case ${description} has invalid requires_confirmation`);
    }
    if (item.vars.must_not_execute !== true) {
      throw new Error(`Codex model case ${description} must set must_not_execute=true`);
    }
    for (const field of ["must_include", "must_include_all", "must_include_any", "must_not_include"]) {
      const value = item.vars[field];
      if (value !== undefined && typeof value !== "string" && !Array.isArray(value)) {
        throw new Error(`Codex model case ${description} has invalid ${field}`);
      }
    }
  }
  return {
    caseCount: payload.length,
    caseIds: [...caseIds],
    descriptions: [...descriptions],
    routingModeCounts,
  };
}

function assertArtifactSafety(filePath, source = process.env) {
  const resolvedPath = path.resolve(packageRoot, filePath);
  if (!fs.existsSync(resolvedPath)) {
    throw new Error(`Codex eval artifact is missing: ${filePath}`);
  }
  const content = fs.readFileSync(resolvedPath, "utf8");
  for (const [key, value] of Object.entries(source)) {
    const uppercaseKey = key.toUpperCase();
    const isSensitiveKey = [
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
    ].some((marker) => uppercaseKey.includes(marker));
    if (
      isSensitiveKey &&
      typeof value === "string" &&
      value.length >= 4 &&
      content.includes(value)
    ) {
      throw new Error(`Codex eval artifact contains sensitive value from ${key}`);
    }
  }
  if (/\b(?:jp-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}\b/.test(content)) {
    throw new Error("Codex eval artifact contains a credential-like token");
  }
  return true;
}

function outputPaths(argv) {
  const paths = [];
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--output" || value === "-o") {
      index += 1;
      while (argv[index] && !argv[index].startsWith("-")) {
        paths.push(argv[index]);
        index += 1;
      }
      index -= 1;
    } else if (value.startsWith("--output=")) {
      paths.push(value.slice("--output=".length));
    }
  }
  return paths;
}

function assertOutputArtifactsSafe(argv, source = process.env, { requireExists = true } = {}) {
  for (const filePath of outputPaths(argv)) {
    const resolvedPath = path.resolve(packageRoot, filePath);
    if (!fs.existsSync(resolvedPath) && !requireExists) continue;
    assertArtifactSafety(filePath, source);
  }
}

function clearManagedArtifacts(argv) {
  const artifactsRoot = path.join(packageRoot, "artifacts") + path.sep;
  for (const filePath of outputPaths(argv)) {
    const resolvedPath = path.resolve(packageRoot, filePath);
    if (resolvedPath.startsWith(artifactsRoot) && fs.existsSync(resolvedPath)) {
      fs.rmSync(resolvedPath, { force: true });
    }
  }
}

function compactHtmlArtifact(filePath) {
  const resolvedPath = path.resolve(packageRoot, filePath);
  if (!resolvedPath.toLowerCase().endsWith(".html") || !fs.existsSync(resolvedPath)) return false;
  const content = fs.readFileSync(resolvedPath, "utf8");
  const compactStyles = `
    <style id="weex-report-compact-table">
      table[data-results-table] { width: 100%; min-width: 960px; table-layout: fixed; }
      table[data-results-table] th,
      table[data-results-table] td { min-width: 0; }
      table[data-results-table] .weex-hidden-column,
      table[data-results-table] td[data-variable-name="skill"],
      table[data-results-table] td[data-variable-name="scenario_type"],
      table[data-results-table] td[data-variable-name="language"],
      table[data-results-table] td[data-variable-name="requires_confirmation"],
      table[data-results-table] td[data-variable-name="must_not_execute"],
      table[data-results-table] td[data-variable-name="must_include"],
      table[data-results-table] td[data-variable-name="must_include_all"],
      table[data-results-table] td[data-variable-name="must_include_any"],
      table[data-results-table] td[data-variable-name="must_not_include"],
      table[data-results-table] td[data-variable-name="forbidden_execution_terms"],
      table[data-results-table] td[data-variable-name="forbidden_no_confirmation_terms"],
      table[data-results-table] td[data-variable-name="sessionId"] { display: none; }
      table[data-results-table] .weex-case-column { width: 16%; }
      table[data-results-table] .weex-mode-column { width: 11%; }
      table[data-results-table] .weex-query-column { width: 30%; }
      table[data-results-table] .weex-route-column { width: 12%; }
      table[data-results-table] .weex-operation-column { width: 17%; }
      table[data-results-table] .weex-output-column { width: 34%; }
      table[data-results-table] td[data-variable-name="query"] .cell-content,
      table[data-results-table] td[data-output-cell="true"] .output-text,
      table[data-results-table] td[data-output-cell="true"] .output-reason {
        display: -webkit-box;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }
      table[data-results-table] td[data-variable-name="query"] .cell-content {
        -webkit-line-clamp: 3;
      }
      table[data-results-table] td[data-output-cell="true"] .output-text {
        -webkit-line-clamp: 3;
      }
      table[data-results-table] td[data-output-cell="true"] .output-reason {
        -webkit-line-clamp: 2;
      }
    </style>`;
  const columnLayout = `
    <script id="weex-report-column-layout">
      (() => {
        const hidden = new Set([
          "skill", "scenario_type", "language", "requires_confirmation",
          "must_not_execute", "must_include", "must_include_all", "must_include_any",
          "must_not_include", "forbidden_execution_terms",
          "forbidden_no_confirmation_terms", "sessionId"
        ]);
        const visibleClasses = {
          case_id: "weex-case-column",
          routing_mode: "weex-mode-column",
          query: "weex-query-column",
          expected_route: "weex-route-column",
          expected_operation: "weex-operation-column"
        };
        const apply = (table) => {
          const headers = Array.from(table.querySelectorAll("thead th"));
          const rows = Array.from(table.querySelectorAll("tbody tr"));
          headers.forEach((header, index) => {
            const name = header.textContent.trim();
            const className = hidden.has(name)
              ? "weex-hidden-column"
              : (visibleClasses[name] || (index === headers.length - 1 ? "weex-output-column" : ""));
            if (!className) return;
            header.classList.add(className);
            for (const row of rows) row.cells[index]?.classList.add(className);
          });
        };
        const applyAll = () => document.querySelectorAll("table[data-results-table]").forEach(apply);
        applyAll();
        new MutationObserver(applyAll).observe(document.body, { childList: true, subtree: true });
      })();
    </script>`;
  const hasCompactStyles = content.includes('id="weex-report-compact-table"');
  const hasColumnLayout = content.includes('id="weex-report-column-layout"');
  if (hasCompactStyles && hasColumnLayout) return false;
  let compacted = content;
  if (hasCompactStyles) {
    compacted = compacted.replace(
      /<style id="weex-report-compact-table">[\s\S]*?<\/style>/,
      compactStyles.trim(),
    );
  } else {
    const styled = compacted.replace("</head>", `${compactStyles}\n  </head>`);
    if (styled === compacted) throw new Error(`HTML artifact has no </head> marker: ${filePath}`);
    compacted = styled;
  }
  if (!hasColumnLayout) {
    const scripted = compacted.replace("</body>", `${columnLayout}\n  </body>`);
    if (scripted === compacted) throw new Error(`HTML artifact has no </body> marker: ${filePath}`);
    compacted = scripted;
  }
  fs.writeFileSync(resolvedPath, compacted, "utf8");
  return true;
}

function validateManagedArtifactCompleteness(argv, source = process.env) {
  if (argv[0] !== "eval") return true;
  const outputs = outputPaths(argv).map((filePath) => path.resolve(packageRoot, filePath));
  const artifactsRoot = path.join(packageRoot, "artifacts") + path.sep;
  const jsonPath = outputs.find(
    (filePath) => filePath.startsWith(artifactsRoot) && filePath.toLowerCase().endsWith(".json"),
  );
  if (!jsonPath) return true;
  const selectionFlags = ["--filter", "--filter-pattern", "--filter-range", "--filter-prompts", "--filter-sample", "--filter-first-n"];
  if (selectionFlags.some((flag) => argv.includes(flag) || argv.some((value) => value.startsWith(`${flag}=`)))) {
    return true;
  }
  const payload = JSON.parse(fs.readFileSync(jsonPath, "utf8"));
  const resultCount = payload?.results?.results?.length;
  const stats = payload?.results?.stats;
  const repeatIndex = argv.indexOf("--repeat");
  const inlineRepeat = argv.find((value) => value.startsWith("--repeat="));
  const repeatRaw = repeatIndex >= 0 ? argv[repeatIndex + 1] : inlineRepeat?.slice("--repeat=".length);
  const repeat = Number((repeatRaw || source.WEEX_CODEX_EVAL_REPEAT) || "3");
  if (!Number.isInteger(repeat) || repeat < 1 || repeat > 10) {
    throw new Error("Codex eval artifact repeat value is invalid");
  }
  const expectedCount = validateModelCaseCatalog().caseCount * repeat;
  if (payload?.evalId == null || resultCount !== expectedCount) {
    throw new Error(`Codex eval artifact result count mismatch: expected ${expectedCount}, got ${resultCount}`);
  }
  if (!stats || stats.successes !== resultCount || stats.failures !== 0 || stats.errors !== 0) {
    throw new Error("Codex eval artifact contains failed or errored results");
  }
  return true;
}

function checkAuth(source = process.env, spawn = spawnSync) {
  const catalog = validateModelCaseCatalog();
  const runtime = readCodexRuntime(source);
  const completed = spawn("codex", ["login", "status"], {
    cwd: packageRoot,
    env: buildEvalProcessEnv(source, runtime.providerAuthEnvironmentKey),
    stdio: "ignore",
    shell: false,
  });
  if (completed.error || completed.status !== 0) {
    throw new Error("Codex login status check failed; sign in with Codex before running model evals");
  }
  return {
    auth_present: true,
    provider: runtime.providerName,
    model: runtime.model,
    reasoning_effort: runtime.reasoningEffort,
    wire_api: runtime.wireApi,
    auth_transport: "codex-login-status",
    auth_source: "codex-managed-login-state",
    key_exposed: false,
    case_count: catalog.caseCount,
  };
}

function addDefaultRepeat(argv, source) {
  if (argv[0] !== "eval") return [...argv];
  if (argv.some((value) => value === "--repeat" || value.startsWith("--repeat="))) return [...argv];
  const raw = String(source.WEEX_CODEX_EVAL_REPEAT || "3").trim();
  const repeat = Number(raw);
  if (!Number.isInteger(repeat) || repeat < 1 || repeat > 10) {
    throw new Error("WEEX_CODEX_EVAL_REPEAT must be an integer from 1 to 10");
  }
  return [...argv, "--repeat", String(repeat)];
}

function validateModelEvalArguments(argv) {
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
    const expected = path.resolve(packageRoot, "promptfooconfig.codex.yaml");
    const invalid = configPaths.some((configPath) => path.resolve(packageRoot, configPath) !== expected);
    if (invalid) {
      throw new Error("Codex model runner only permits promptfooconfig.codex.yaml");
    }
    return [...argv];
  }
  return [argv[0], "--config", "promptfooconfig.codex.yaml", ...argv.slice(1)];
}

function main(argv = process.argv.slice(2), source = process.env, spawn = spawnSync) {
  const isolatedHome = fs.mkdtempSync(path.join(os.tmpdir(), "weex-codex-eval-home-"));
  const evaluationSource = { ...source, HOME: isolatedHome };
  try {
    if (argv[0] === "check-auth") {
      try {
        const payload = checkAuth(evaluationSource, spawn);
        process.stdout.write(`${JSON.stringify(payload, null, argv.includes("--pretty") ? 2 : 0)}\n`);
        return 0;
      } catch (error) {
        process.stderr.write(`${error.message}\n`);
        return 1;
      }
    }

    let runtime;
    try {
      validateModelCaseCatalog();
      runtime = readCodexRuntime(source);
      checkAuth(evaluationSource, spawn);
    } catch (error) {
      process.stderr.write(`Codex authentication preflight failed: ${error.message}\n`);
      return 1;
    }

    const safeArguments = validateModelEvalArguments(argv);
    clearManagedArtifacts(safeArguments);
    const completed = spawn(process.execPath, [promptfooMain, ...addDefaultRepeat(safeArguments, source)], {
      cwd: packageRoot,
      stdio: "inherit",
      env: buildEvalProcessEnv(evaluationSource, runtime.providerAuthEnvironmentKey),
      shell: false,
    });
    try {
      for (const output of outputPaths(safeArguments)) compactHtmlArtifact(output);
      assertOutputArtifactsSafe(safeArguments, source, { requireExists: completed.status === 0 });
      if (completed.status === 0) validateManagedArtifactCompleteness(safeArguments, source);
    } catch (error) {
      process.stderr.write(`${error.message}\n`);
      return 1;
    }
    if (completed.error) {
      process.stderr.write(`${completed.error.message}\n`);
      return 1;
    }
    return completed.status ?? 1;
  } finally {
    fs.rmSync(isolatedHome, { recursive: true, force: true });
  }
}

if (require.main === module) {
  process.exit(main());
}

module.exports = {
  addDefaultRepeat,
  assertArtifactSafety,
  assertOutputArtifactsSafe,
  buildEvalProcessEnv,
  checkAuth,
  main,
  outputPaths,
  clearManagedArtifacts,
  compactHtmlArtifact,
  validateManagedArtifactCompleteness,
  validateModelEvalArguments,
  validateModelCaseCatalog,
};
