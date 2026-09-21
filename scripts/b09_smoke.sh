#!/usr/bin/env bash
set -euo pipefail

image_ref=${IMAGE_REF:-}
if [[ "$image_ref" != *@sha256:* ]]; then
  printf '%s\n' '{"status":"blocked","reason":"IMAGE_REF must be an exact digest reference"}' >&2
  exit 2
fi

tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/nexa-b09-smoke.XXXXXX")
trap 'rm -rf "$tmp_root"' EXIT
printf '%s' '{"initial_value":17}' > "$tmp_root/input.json"
mkdir "$tmp_root/output"

docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 128 \
  --memory 256m \
  --memory-swap 256m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --mount "type=bind,src=$tmp_root/input.json,dst=/input/input.json,readonly" \
  --mount "type=bind,src=$tmp_root/output,dst=/output" \
  --entrypoint python \
  "$image_ref" -m nexa.workloads.cpu_entrypoint \
  --input /input/input.json --output /output/result.json \
  --iterations 3 --seed 7 --modulus 1000000007 \
  --spec-checksum "sha256:$(printf '%064d' 0 | tr 0 f)"

python3 - "$tmp_root/output/result.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert list(payload) == ["iterations", "final_accumulator", "input_checksum", "spec_checksum"]
assert payload["final_accumulator"] == 915488392
print(json.dumps({"status": "pass", "result": payload}, sort_keys=True))
PY
