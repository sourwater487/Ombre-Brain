#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_ops_common.sh"

cd "$(ombre_repo_root)"

COMPOSE_FILE="$(ombre_compose_file)"
BRAIN_SERVICE="${BRAIN_SERVICE:-${OMBRE_SERVICE:-ombre-brain}}"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-${OMBRE_GATEWAY_SERVICE:-ombre-gateway}}"

echo "Repo: $(pwd)"
echo "Compose: ${COMPOSE_FILE}"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ombre_update_git_checkout
fi

echo "Update containers..."
compose_up_args=(-f "${COMPOSE_FILE}" up -d --remove-orphans)
if [[ "${OMBRE_FORCE_RECREATE:-0}" == "1" ]]; then
  compose_up_args+=(--force-recreate)
fi
if grep -Eq '^[[:space:]]*build:' "${COMPOSE_FILE}"; then
  if [[ "${OMBRE_BUILD_NO_CACHE:-0}" == "1" ]]; then
    echo "Docker build cache disabled for this deployment."
    ombre_compose -f "${COMPOSE_FILE}" build --no-cache
  else
    compose_up_args+=(--build)
  fi
  ombre_compose "${compose_up_args[@]}"
else
  ombre_compose -f "${COMPOSE_FILE}" pull
  ombre_compose "${compose_up_args[@]}"
fi

ombre_compose -f "${COMPOSE_FILE}" ps
if [[ -z "${HEALTH_URL:-}" ]]; then
  if ombre_compose_service_exists "${COMPOSE_FILE}" "${BRAIN_SERVICE}"; then
    HEALTH_URL="$(ombre_compose_service_health_url "${COMPOSE_FILE}" "${BRAIN_SERVICE}" "8000" "$(ombre_default_health_url "${COMPOSE_FILE}")")"
  else
    HEALTH_URL="$(ombre_default_health_url "${COMPOSE_FILE}")"
  fi
fi
ombre_wait_for_health "${HEALTH_URL}" "${HEALTH_TRIES:-30}" "${HEALTH_DELAY:-2}"
if ombre_compose_service_exists "${COMPOSE_FILE}" "${GATEWAY_SERVICE}"; then
  GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-$(ombre_compose_service_health_url "${COMPOSE_FILE}" "${GATEWAY_SERVICE}" "8010" "http://127.0.0.1:18002/health")}"
  ombre_wait_for_health "${GATEWAY_HEALTH_URL}" "${HEALTH_TRIES:-30}" "${HEALTH_DELAY:-2}"
else
  echo "Gateway service not found in compose; skip gateway health check."
fi

echo "HEAD: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "Update done."
