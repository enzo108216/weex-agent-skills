# WEEX 本地 Skill 评测

本目录是开发和 CI 评测层，不是任何 WEEX Skill 的运行时依赖。

## 安全边界

- 只运行本地确定性脚本和文档契约检查。
- 不读取 Profile、Vault、API key、secret 或 passphrase。
- 不访问 WEEX REST。
- 不传入 live/demo mutation confirmation。
- Monitor 使用临时 `WEEX_MONITOR_SKILL_HOME`。
- `skills/` 仍然是唯一 Skill 事实源。

## Python 直接运行

在仓库根目录执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_local_evals.py --json
```

运行单个 case：

```bash
python3 tools/run_local_evals.py --case-id monitor.confirmation_token_binding --pretty
```

## Promptfoo 本地运行

首次安装：

```bash
npm --prefix evals install
```

运行：

```bash
npm --prefix evals run eval
```

Promptfoo 只负责 case 编排、provider 调用和报告；真正的行为断言在 `tools/run_local_evals.py` 及 `evals/graders/` 中。
评测脚本会自动设置 `PROMPTFOO_DISABLE_TELEMETRY=1`，不启用 Promptfoo telemetry。

## 当前 Codex 模型评测

这是本地显式模型评测，不进入公共 CI。评测目标由本机环境变量指定，认证由 Codex CLI/SDK 登录态处理；评测器不会解析 `auth.json`，不会把认证值写入 argv 或报告。

首次使用时，在 `~/.zshrc`（或当前 shell 的等价启动文件）保存非秘密目标变量：

```bash
export WEEX_CODEX_EVAL_MODEL="gpt-5.6-sol"
export WEEX_CODEX_EVAL_MODEL_PROVIDER="custom"
export WEEX_CODEX_EVAL_REASONING_EFFORT="xhigh"
export WEEX_CODEX_EVAL_REPEAT="3"
```

这些变量必须与当前 `~/.codex/config.toml` 的 model/provider 一致；不一致时预检会 fail-closed。自定义 provider 所需的认证环境变量只由受信 Codex 进程使用，模型控制的 shell 会启用 Codex 默认 secret 名称过滤。生成 HTML/JSON 后，wrapper 会再次扫描敏感标记和已配置的 provider secret，命中即返回非零。

先检查当前 Codex provider/model：

```bash
node evals/scripts/run_codex_promptfoo.cjs check-auth --json
```

生成同一次 eval 的完整 Promptfoo HTML 和 JSON：

```bash
npm --prefix evals run eval:codex:html
```

命令输出的 eval ID 例如 `eval-ep8-2026-08-21T07:13:39`。如需单独导出，可使用 Promptfoo 官方 export 从同一个 eval ID 生成配套 JSON：

```bash
node evals/scripts/run_codex_promptfoo.cjs export eval \
  eval-ep8-2026-08-21T07:13:39 \
  -o artifacts/codex-model-eval.json
```

结果文件：

- `evals/artifacts/codex-model-eval.html`：可直接在浏览器打开的完整 Promptfoo 报告。
- `evals/artifacts/codex-model-eval.json`：同一 eval ID 的机器可读结果。
- HTML 汇总表默认只保留 `case_id`、路由模式、query、预期 route、预期 operation 和模型输出；评分辅助字段按列名隐藏，不依赖固定列序号，因此新增变量不会再造成表头错位。

模型评测使用只读工作区、禁用网络、`approval_policy=never`、最大并发 2、默认重复 3 次、`--no-cache`、`--no-share` 和关闭 telemetry。目录包含 63 个不同 query；完整默认运行会产生 189 条结果。`guided_policy` case 检查已选 Skill 的策略遵循，`auto_router` case 不提供 Skill 提示并要求从 `AGENTS.md` 独立路由。每条输出同时评分 route、canonical operation、确认门禁和禁止执行边界。它不访问 WEEX REST、不读取 Profile/Vault、不执行真实或模拟盘 mutation。

## 评测范围

- 本地确定性 suite：18 个 case；Codex 模型 suite：63 个不同 query（10 Analysis、12 Monitor、18 Partner、15 Trader、8 auto-router）。
- Analysis：八个分析入口、缺失实时快照、交易模式保留和投资建议对抗请求。
- Monitor：仓位/订单基线 PnL、缺 profile/模式/方向、数量冲突、有限时长、合并确认、价格条件跨 Skill、现货拒绝、list/cancel。
- Partner：17 条自然语言契约完整映射、七项只读 operation、缺字段、越权/写操作拒绝和 Trader 委派。
- Trader：合约/现货行情、私有查询模式、真实/模拟盘确认、撤单、全仓 TP/SL、过期 intent、profile/Vault、自动授权、恢复和数据库绕过。
- Auto-router：四条正向路由、实时采集与 Partner 下单跨 Skill、提现拒绝和 prompt injection。
- Repository：`skills/` 唯一事实源、离线网络阻断和评测 catalog 完整性。

本地确定性评测通过不代表 Codex、Claude、Cursor、GitHub Copilot 或 OpenClaw 的真实宿主自然语言路由已经完成验收；宿主评测需要单独的隔离 provider 和无 mutation 工具边界。
