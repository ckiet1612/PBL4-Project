#!/usr/bin/env bash
set -euo pipefail

base_image_ref=${BASE_IMAGE_REF:-}
image_ref=${IMAGE_REF:-nexa/cpu-iterative:local}
image_arch=${IMAGE_ARCH:-linux/amd64}

if [[ "$base_image_ref" != *@sha256:* ]]; then
  printf '%s\n' '{"status":"blocked","reason":"BASE_IMAGE_REF must include an explicit sha256 digest"}' >&2
  exit 2
fi

docker build \
  --pull=false \
  --build-arg "BASE_IMAGE_REF=$base_image_ref" \
  --build-arg "IMAGE_ARCH=$image_arch" \
  --tag "$image_ref" \
  --file deploy/cpu-iterative/Dockerfile \
  .

docker image inspect "$image_ref" --format '{"status":"built","image_id":"{{.Id}}","repo_digests":{{json .RepoDigests}},"architecture":"{{.Architecture}}","os":"{{.Os}}"}'
