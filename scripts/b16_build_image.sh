#!/usr/bin/env bash
# Build one B16 PyTorch CPU workload image (pytorch.cifar10 or batch.inference) from a
# digest-pinned base. Pattern of scripts/b09_build_image.sh; never pushes.
set -euo pipefail

base_image_ref=${BASE_IMAGE_REF:-}
adapter_id=${ADAPTER_ID:-}
image_arch=${IMAGE_ARCH:-linux/amd64}
requirements_file=${REQUIREMENTS_FILE:-deploy/pytorch-cpu/requirements.linux-amd64.txt}

if [[ "$base_image_ref" != *@sha256:* ]]; then
  printf '%s\n' '{"status":"blocked","reason":"BASE_IMAGE_REF must include an explicit sha256 digest"}' >&2
  exit 2
fi
case "$adapter_id" in
  pytorch.cifar10) checkpoint_format=pytorch-cifar10-state-v1 ;;
  batch.inference) checkpoint_format=batch-inference-state-v1 ;;
  *)
    printf '%s\n' '{"status":"blocked","reason":"ADAPTER_ID must be pytorch.cifar10 or batch.inference"}' >&2
    exit 2
    ;;
esac
if [[ ! -f "$requirements_file" ]] || ! grep -q -- '--hash=sha256:' "$requirements_file"; then
  printf '%s\n' '{"status":"blocked","reason":"REQUIREMENTS_FILE must be a hash-locked requirements file"}' >&2
  exit 2
fi
image_ref=${IMAGE_REF:-nexa/${adapter_id/./-}:local}

docker build \
  --pull=false \
  --platform "$image_arch" \
  --build-arg "BASE_IMAGE_REF=$base_image_ref" \
  --build-arg "IMAGE_ARCH=$image_arch" \
  --build-arg "ADAPTER_ID=$adapter_id" \
  --build-arg "CHECKPOINT_FORMAT=$checkpoint_format" \
  --build-arg "REQUIREMENTS_FILE=$requirements_file" \
  --tag "$image_ref" \
  --file deploy/pytorch-cpu/Dockerfile \
  .

docker image inspect "$image_ref" --format '{"status":"built","image_ref":"'"$image_ref"'","image_id":"{{.Id}}","repo_digests":{{json .RepoDigests}},"architecture":"{{.Architecture}}","os":"{{.Os}}","size":{{.Size}},"labels":{{json .Config.Labels}}}'
