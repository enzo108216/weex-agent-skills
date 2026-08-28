const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { buildSafeEnvironment } = require("../scripts/eval_environment.cjs");

class WeexLocalProvider {
  constructor(options) {
    this.providerId = options.id || "weex-local-deterministic";
    this.config = options.config || {};
  }

  id() {
    return this.providerId;
  }

  async callApi(prompt) {
    const caseId = String(prompt || "").trim();
    const repositoryRoot = path.resolve(__dirname, "..", "..");
    const python = this.config.python || process.env.PYTHON || "python3";
    const temporaryHome = fs.mkdtempSync(path.join(os.tmpdir(), "weex-local-provider-home-"));
    const safeEnv = buildSafeEnvironment(process.env, { home: temporaryHome });
    safeEnv.PYTHONDONTWRITEBYTECODE = "1";
    safeEnv.WEEX_EVAL_OFFLINE = "1";

    let completed;
    try {
      completed = spawnSync(
        python,
        [path.join(repositoryRoot, "tools", "run_local_evals.py"), "--case-id", caseId, "--json"],
        {
          cwd: repositoryRoot,
          encoding: "utf8",
          env: safeEnv,
          shell: false,
          timeout: 30000,
        },
      );
    } finally {
      fs.rmSync(temporaryHome, { recursive: true, force: true });
    }

    if (completed.error) {
      return {
        output: JSON.stringify({
          case_id: caseId,
          ok: false,
          error: completed.error.message,
        }),
      };
    }

    const output = (completed.stdout || "").trim();
    if (!output) {
      return {
        output: JSON.stringify({
          case_id: caseId,
          ok: false,
          error: (completed.stderr || "local eval returned no output").trim(),
        }),
      };
    }
    return {
      output,
      metadata: {
        exit_code: completed.status,
        stderr: (completed.stderr || "").trim(),
        offline: true,
      },
    };
  }
}

module.exports = WeexLocalProvider;
