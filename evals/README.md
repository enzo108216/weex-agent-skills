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

这是本地显式模型评测，不进入公共 CI。Provider 使用当前 Codex CLI/SDK 会话读取本机 Codex 登录态；仓库不读取或输出密钥，不把密钥放入 argv，也不把密钥写入报告。

先检查当前 Codex provider/model：

```bash
node evals/scripts/run_codex_promptfoo.cjs check-auth --json
```

生成完整 Promptfoo HTML：

```bash
npm --prefix evals run eval:codex:html
```

命令输出的 eval ID 例如 `eval-ep8-2026-08-21T07:13:39`。使用 Promptfoo 官方 export 从同一个 eval ID 生成配套 JSON：

```bash
node evals/scripts/run_codex_promptfoo.cjs export eval \
  eval-ep8-2026-08-21T07:13:39 \
  -o artifacts/codex-model-eval.json
```

结果文件：

- `evals/artifacts/codex-model-eval.html`：可直接在浏览器打开的完整 Promptfoo 报告。
- `evals/artifacts/codex-model-eval.json`：同一 eval ID 的机器可读结果。

模型评测使用只读工作区、禁用网络、`approval_policy=never`、最大并发 2、`--no-cache`、`--no-share` 和关闭 telemetry。13 个 case 覆盖 Analysis、Monitor、Partner、Trader 的路由、前置条件、确认门禁、只读边界和 secret transport。它不访问 WEEX REST、不读取 Profile/Vault、不执行真实或模拟盘 mutation。

## 评测范围

- Analysis：缺字段降级、空风险输入、Replay scope 继承。
- Monitor：显式交易模式、价格条件拒绝、token 绑定、dry-run 触发、订单基准数量。
- Partner：七项只读目录、自然语言 regression fixture、缺 UID fail-closed。
- Trader：确认 flag、intent/risk signature、secret transport 文档契约。
- Repository：`skills/` 唯一事实源和离线安全。

本地确定性评测通过不代表 Codex、Claude、Cursor、GitHub Copilot 或 OpenClaw 的真实宿主自然语言路由已经完成验收；宿主评测需要单独的隔离 provider 和无 mutation 工具边界。
