# Ombre-Brain VPS Docker Migration

This is the first-stage migration plan for moving Ombre-Brain from Render to a VPS Docker deployment. Render stays online as the rollback target until linche-api is switched and verified.

## Current Rollback Position

- Keep the Render service and Render disk in place.
- Do not delete Render until the VPS deployment has run cleanly through real traffic.
- Rollback is changing linche-api's Ombre Gateway URL back to the Render Gateway URL.

## Data Backup First

Before starting the VPS container, back up the full Render bucket disk:

```bash
/opt/render/project/src/buckets
```

The backup must include:

- bucket Markdown files under `permanent/`, `dynamic/`, `archive/`, and `feel/`
- `embeddings.db`
- `gateway_state.db`
- `state/`, including SQLite, JSONL, runtime config, dashboard auth, darkroom, portrait, and dream state
- SQLite sidecar files such as `*.db-wal`, `*.db-shm`, `*.sqlite-wal`, and `*.sqlite-shm` if present

## VPS Target Layout

Use this host-side target path for migrated data:

```bash
/opt/ombre-brain/data/buckets
```

State is not split into a separate host directory. It remains under:

```bash
/opt/ombre-brain/data/buckets/state
```

The container maps it as:

```text
./data/buckets:/app/data/buckets
```

and sets:

```text
OMBRE_BUCKETS_DIR=/app/data/buckets
OMBRE_STATE_DIR=/app/data/buckets/state
OMBRE_RUNTIME_CONFIG_PATH=/app/data/buckets/state/config.runtime.yaml
```

## Environment Setup

Before startup, copy the example environment file and fill real secrets on the VPS:

```bash
cp .env.example .env
```

Do not commit `.env`. The repository ignores `.env` and `data/`.

Required placeholders in `.env` include:

- `OMBRE_API_KEY`
- `OMBRE_GATEWAY_TOKEN`
- `RIJI_MCP_BEARER_TOKEN`
- `OMBRE_EMBEDDING_API_KEY`
- `OMBRE_GATEWAY_UPSTREAM_API_KEY`
- `OMBRE_PERSONA_API_KEY`
- `OMBRE_REFLECTION_API_KEY`

## Known Production Config Path Note

`config.lin.production.yaml` still contains a Render absolute path:

```yaml
state_dir: "/opt/render/project/src/buckets/state"
```

Do not edit production config during this phase. Docker overrides it with:

```text
OMBRE_STATE_DIR=/app/data/buckets/state
```

Confirm this env is present before starting the container.

## Startup

From the VPS deployment directory:

```bash
docker compose up -d --build
```

The compose file publishes only to localhost:

```text
127.0.0.1:8789:8000
```

Do not expose Ombre Gateway, dashboard, or MCP directly to the public internet.

## Health Check

Test health from the VPS:

```bash
curl http://127.0.0.1:8789/health
```

The Docker healthcheck calls the same service inside the container at:

```text
http://127.0.0.1:8000/health
```

## Gateway Test

Testing `/v1/chat/completions` must include the Gateway token:

```bash
curl http://127.0.0.1:8789/v1/chat/completions \
  -H "Authorization: Bearer $OMBRE_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-opus-4.6",
    "messages": [
      {"role": "user", "content": "health check"}
    ]
  }'
```

## Write Path Verification

Before changing linche-api, confirm any new Ombre writes land under:

```bash
./data/buckets
```

Expected write locations include:

- `./data/buckets/permanent`
- `./data/buckets/dynamic`
- `./data/buckets/archive`
- `./data/buckets/feel`
- `./data/buckets/embeddings.db`
- `./data/buckets/gateway_state.db`
- `./data/buckets/state`

If files appear inside the container outside `/app/data/buckets`, stop and fix the env/volume mapping before continuing.

## linche-api Cutover

Only after backup, startup, health check, Gateway test, and write-path verification should linche-api be changed.

Change linche-api's Ombre Gateway URL to:

```text
http://127.0.0.1:8789
```

Keep the existing Render Gateway URL as the rollback value.

## Rollback

Rollback does not require deleting the VPS container.

Change linche-api's Ombre Gateway URL back to the Render Gateway URL, then observe traffic on Render. Do not overwrite Render data with VPS data unless a separate data reconciliation step is planned.
