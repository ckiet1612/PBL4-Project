#!/usr/bin/env bash
set -euo pipefail

require=${1:-}
if [[ "$require" != "--require" ]]; then
  printf '%s\n' '{"status":"not-run","reason":"pass --require to execute real Docker scenarios"}'
  exit 0
fi

if ! docker info >/dev/null 2>&1; then
  printf '%s\n' '{"status":"blocked","reason":"Docker daemon unavailable"}'
  exit 2
fi

docker_os=$(docker info --format '{{.OSType}}' 2>/dev/null || true)
docker_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)
if [[ "$docker_os" != "linux" ]]; then
  printf '{"status":"blocked","reason":"Docker engine is not Linux","docker_os":"%s","docker_arch":"%s"}\n' "$docker_os" "$docker_arch"
  exit 2
fi

image_ref=${NEXA_B09_IMAGE_REF:-}
if [[ "$image_ref" != *@sha256:* ]]; then
  printf '%s\n' '{"status":"blocked","reason":"NEXA_B09_IMAGE_REF must be an exact digest reference"}'
  exit 2
fi

python_bin=python3
if [[ -x .venv/bin/python ]]; then
  python_bin=.venv/bin/python
fi
printf '{"status":"running","docker_os":"%s","docker_arch":"%s","image":"%s"}\n' "$docker_os" "$docker_arch" "$image_ref"
NEXA_RUN_DOCKER=1 NEXA_B09_EVIDENCE=1 NEXA_B09_IMAGE_REF="$image_ref" \
  PYTHONPATH=src "$python_bin" -m pytest -q -s tests/docker
