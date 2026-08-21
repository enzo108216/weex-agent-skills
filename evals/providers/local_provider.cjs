const path = require("node:path");
const { spawnSync } = require("node:child_process");

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
    const safeEnv = Object.fromEntries(
      Object.entries(process.env).filter(([key]) => !key.startsWith("WEEX_")),
    );
    safeEnv.PYTHONDONTWRITEBYTECODE = "1";
    safeEnv.WEEX_EVAL_OFFLINE = "1";

    const completed = spawnSync(
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
