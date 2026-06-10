# Project instructions for Codex

This repository is Lin's private Ombre-Brain fork.

## Verification policy

Do not run pytest in this repository.

Forbidden commands:
- `pytest`
- `python -m pytest`
- `uv run pytest`
- `venv/bin/pytest`
- `./venv/bin/pytest`
- any command that invokes pytest directly or indirectly

Reason:
Pytest is not a reliable verification method in this repository. It often hangs or times out and wastes execution budget.

Allowed verification methods:
- `python -m compileall <changed files or relevant modules>`
- direct import smoke checks
- small purpose-built verification scripts under `scripts/`
- syntax checks for changed files
- manual code inspection when runtime verification is not practical

Default verification:
- Use `bash scripts/verify_quick.sh` if it exists.
- If no quick verification script exists, run targeted `python -m compileall` on changed Python files only.

When a change would normally require tests:
- Do not substitute pytest.
- State that pytest was intentionally not run because it is forbidden in this repository.
- Use the smallest non-pytest verification available.

## Scope control

Make minimal, targeted changes.
Do not rewrite unrelated modules.
Do not change production identity, prompt wording, Gateway routes, MCP tool exposure, embeddings, dashboard behavior, or memory schemas unless the task explicitly asks for it.

## Production config

The production config file is:

`config.lin.production.yaml`

Render uses:

`OMBRE_CONFIG_PATH=/opt/render/project/src/config.lin.production.yaml`

Do not assume `config.yaml` is the production config.

## Secrets

Do not write API keys, bearer tokens, or secrets into tracked files.
Use environment variables for secrets.
