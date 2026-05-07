# WEEX Agent Skills

This repository is a skills-first WEEX automation catalog for multiple AI tools.

The source of truth lives under `skills/`:

- `skills/weex-trader-skill`: WEEX REST automation, saved-profile management, vault flows, and live order execution
- `skills/weex-analysis-skill`: read-only snapshot, fill, replay, profile, order-risk, and account-risk analysis from normalized JSON payloads

Discovery entrypoints for supported agents live under:

- `.agents/skills/`
- `.claude/skills/`
- `.github/skills/`

Those entrypoints should point back to the same directories under `skills/` instead of maintaining duplicated skill copies. In this repository they are lightweight symlinks to the canonical skill directories.

## Install and Use

For catalog-style installation, use the published GitHub repo URL with your skills installer, for example:

```bash
npx skills add https://github.com/weex-labs/weex-trader-skill --all
```

For local-checkout installs, use the wrapper that exports a clean subset and then calls `gh skill install`:

```bash
python3 tools/install_local_skills.py --skill weex-trader-skill --agent codex
python3 tools/install_local_skills.py --skill weex-analysis-skill --agent codex
```

The wrapper exports only the selected skill directories plus small repo metadata files from tracked content, so local installs do not depend on manual cleanup of a clean local checkout or on generated noise such as `.DS_Store` or `__pycache__`. Use `python3 tools/install_local_skills.py --all --agent codex --dry-run` to inspect the generated `gh skill install` commands before execution. If you intentionally need local untracked skill files during development, add `--include-untracked`. If you need to scrub an existing working tree before packaging checks, run `python3 tools/clean_local_skill_checkout.py`.

Before publishing, run a packaging check from the repository root:

```bash
gh skill publish --dry-run
```

For repo-local discovery, clone the repository and let your agent read the skill entrypoints in the tool-specific folders above.

## Repository Layout

```text
skills/
  _shared/
  weex-trader-skill/
  weex-analysis-skill/
.agents/skills/      # symlinks to skills/*
.claude/skills/      # symlinks to skills/*
.github/skills/      # symlinks to skills/*
tools/
```

`skills/_shared/` stores the shared risk-review core that is vendored into both installable skills.

## Maintenance

Check that the shared risk-review core is still synced before publishing:

```bash
python3 tools/sync_weex_risk_review_core.py --check
```

## Testing

Run all skill tests from the repository root:

```bash
python3 tools/run_skill_tests.py
```

Repository CI also checks the clean-checkout gate, shared risk-core sync, local install smoke tests, and `gh skill publish --dry-run`.
