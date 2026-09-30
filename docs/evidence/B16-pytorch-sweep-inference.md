# B16 PyTorch CPU training, hyperparameter sweep and chunked batch inference evidence

Trạng thái: **vòng 2 đã triển khai, chờ Task Review** (chưa được duyệt). Task Review vòng 1 trả
"Không duyệt" với R24/R25/R26 blocking. Các sửa đổi, lượt chạy lại và evidence mới nằm ở
[§Vòng 2](#vòng-2-sau-task-review-vòng-1); các phần khác giữ số liệu vòng 1 trừ chỗ ghi rõ "vòng 2".
Baseline `05ed7f4` (B01–B15 đã duyệt),
working tree sạch lúc bắt đầu; toàn bộ thay đổi B16 là diff chưa commit trên `05ed7f4`.
Không dùng Superpowers theo quyết định của user cho B16. Chỉ CPU; CUDA/GPU thuộc B23.

## Kế hoạch

### Context map

| Requirement (PLAN §13 B16 / prompt) | Contract | Code at 05ed7f4 | Tests at 05ed7f4 | Gap closed by B16 |
|---|---|---|---|---|
| Adapter registry, no `cpu.iterative` hardcode | internal-interfaces.md:149–165 | ~30 hardcode sites: coordinator eligibility/snapshot, worker probes/models/executor/dispatch, runner validators, checkpoint/result validation, upload allowlist | CPU-only fixtures everywhere | `nexa.domain.workload_adapters` static descriptors, per-template lookup; CPU bytes unchanged |
| Templates (list/get/register/CLI) | openapi listTemplates/getTemplate, Template schema; domain-model.md:30 | tables only; no route; `cli/commands/template.py` unwired; tests insert rows | test_template_commands asserts "No such command" | versioned JSON in `deploy/templates/`, `nexa-maintenance register-template`, routes, CLI |
| Offline fixtures | workloads-checkpoints.md:53–84, 102–108 | none | none | `scripts/b16_prepare_fixtures.py`, `tests/fixtures/workloads/*` |
| PyTorch image | PLAN §9/§10; ACC-25 | CPU image only (stdlib) | B09 hardening tests | `deploy/pytorch-cpu/` Dockerfile + hash lock; build script |
| pytorch.cifar10 training | workloads-checkpoints.md:53–84 | enum components exist, rejected | none | adapter, runner split of safetensors snapshot, server component/cursor/compat rules |
| Sweep | workloads-checkpoints.md:86–100; openapi submitSweep/getSweep | tables only | none | `SweepService`, routes, CLI, migration 0020 columns |
| batch.inference + chunks | workloads-checkpoints.md:102–108, 128–144; internal-interfaces.md:256–282 | protocol frames validated; worker/runner/server ignore chunks; `recognized_chunks` unused | protocol unit tests | chunk cycle in runner/worker, RecognizedChunk insert-or-verify, carry-forward, coverage, restore revalidation |
| Upload/checkpoint/restore/result validation | workloads-checkpoints.md:110–189 | CPU-only | B11/B14 tests | per-adapter rules |

### Design decisions (5.A–K)

- **5.A Registry.** New stdlib-only module `src/nexa/domain/workload_adapters.py` with frozen
  `AdapterDescriptor` rows keyed by `(adapter_id, adapter_version)` and looked up by
  `(template_id, template_version)`: framework (API enum `NEXA_CPU|PYTORCH`, manifest enum
  `PYTHON|PYTORCH`), checkpoint-format label value, input/model artifact kind + media type,
  input/model mount targets, checkpoint state components, allowed checkpoint/result/chunk media
  types, entrypoint module, scratch bound. It is imported by application, coordinator, worker and
  runner (domain is dependency-free), never imports torch. Every former `if adapter ==
  "cpu.iterative"` becomes a descriptor lookup; unknown adapter → the same rejection path as before.
  CPU launch spec, journal binding, supervisor command and result bytes are unchanged (B14/B15 R0
  checksum regression). Coordinator eligibility compares template capability requirement to the
  worker inventory (adapter id/version, image digest, architecture, device, framework/version) and
  still excludes `gpu_count≠0` (ACC-06, no GPU provider). Worker takes `NEXA_WORKLOAD_IMAGE_REFS`
  (comma-separated `repo@sha256:` list) plus legacy `NEXA_CPU_IMAGE_REF`; each image is probed and
  advertises its adapter/framework only when labels match a registry descriptor exactly (runner
  protocol, adapter id/version, framework, checkpoint format). Executor picks the image whose digest
  equals `ExecutionContext.image_digest`. Why: one declarative table removes scattered if/else and
  keeps ML code only in the image.
- **5.B Templates.** `deploy/templates/<template_id>.v<n>.json` (cpu-iterative, pytorch-cifar10-cnn,
  batch-inference v1) hold everything except the image digest. `nexa-maintenance register-template
  --file F --image-digest sha256:…` (pattern of `reopen-worker-bootstrap`, local DB maintenance, no
  REST) inserts `templates` + `template_versions` + audit in one transaction; identical content is a
  no-op, different content for the same `(template_id, version)` is a conflict, rows are never
  updated. B06 has no registration path (checked: no insert into `template_versions` outside tests).
  `_TEMPLATE_ARTIFACT_REQUIREMENTS` is replaced by the registry lookup (no version-1 hardcode, same
  media types). `GET /v1/templates` (`enabled` filter, default true) and `GET
  /v1/templates/{template_id}` (current = highest enabled version) with tenant context and read
  scope; `allowed_devices` from `capability_requirements.device`. PyTorch templates (as implemented):
  CPU 1000–8000 millicores, RAM 1–8 GiB, `gpu_count` 0; the planned 512 MiB minimum was not
  kept; the registered minimum is 1 GiB (VPS1 peaks up to 384 MiB with runner, §RAM). Sweep has no Template row (B16-R01).
- **5.C Fixtures.** `scripts/b16_prepare_fixtures.py`: `verify-archive` (stdlib MD5/SHA-256 of the
  official binary archive, no pickle) and `build` (inside the PyTorch image, `--network none`,
  `--memory 4g`): streams 3073-byte records, deterministic subset from seed, Arrow IPC file with
  JSON schema metadata (B16-R02), model safetensors with `__metadata__`. Re-run gives identical
  checksums (verified twice).
- **5.D Image.** `deploy/pytorch-cpu/` Dockerfile (base python:3.12-slim by digest), `runner-config.json`,
  `requirements.linux-amd64.txt` (`--require-hashes`, torch CPU wheel from the PyTorch CPU index,
  numpy, safetensors, pyarrow + torch transitive deps). **Two images from one Dockerfile**
  (`ADAPTER_ID` build arg → labels) for `pytorch.cifar10` and `batch.inference`: layers are shared,
  but one image = one adapter keeps probe → capability → template digest 1:1 and the checkpoint
  format label unambiguous (B16-R03). Torch/numpy/pyarrow/safetensors never enter `uv.lock`; a
  boundary test imports control-plane packages and asserts none are loaded.
- **5.E Training.** Architecture `nexa-cifar10-smallcnn-v1` (conv 3→16, conv 16→32, fc 2048→64→10,
  ReLU/maxpool), SGD momentum 0.9 (standard, stateful → exercises OPTIMIZER restore). Seeds for
  Python/NumPy/torch, `torch.use_deterministic_algorithms(True)`, threads = max(1, cpu_millis//1000),
  own sampler: permutation per epoch from `(seed, epoch)`, cursor `(epoch, batch_index)`. The
  workload atomically replaces one `/output/state.safetensors` (tensors `model.*`, `optimizer.*`,
  `rng.torch_cpu`, `rng.numpy_keys`; `__metadata__` JSON: cursor, sampler, Python/NumPy RNG scalars,
  identity) at batch boundaries at most once per second. The stdlib runner reads it through one fd,
  checks header/key/shape/dtype allowlist + monotonic step, and splits it into `model.safetensors`,
  `optimizer.safetensors`, `rng.safetensors`, `training-state.json` (components MODEL, OPTIMIZER,
  RNG_PYTHON, RNG_NUMPY, RNG_TORCH_CPU, SAMPLER). Restore mounts the 4 files read-only under
  `/input/restore/`. No `torch.save/load`, pickle, marshal, eval/exec (AST test). Result:
  `model.safetensors` + `metrics.json` (closed schema). Non-finite loss → FAILED INTERNAL.
- **5.F Freeze.** image → data → fixture.json + tolerance.json → SHA-256 + UTC in "Đóng băng" →
  measure. Tolerance rationale written from first principles before any acceptance run.
- **5.G Sweep.** `SweepService.submit`: request validation (422, nothing persisted) covers shape,
  dimension names in the template parameter schema, per-value type/range against the child
  template, canonical RFC 8785 dedup and product size computed by multiplication (never
  materialized); a disabled/unknown template is 422 `infeasible_request`. Parent tx:
  `begin_idempotency(submitSweep)` + parent row with 0020 columns `request_hash`, `base_spec`,
  `expansion` (ordered `{parameters, parameter_hash}` per `child_index`) + complete 207 with
  `{sweep_id}` and Location. Children: one transaction each; the parent idempotency record is
  locked `FOR UPDATE` (serializes concurrent replays), an existing outcome is skipped, then the
  same `JobService` core as `submitJob` (`_authorize`, `_admit`, `_create_job` incl. input
  ownership/resource bounds) runs in a savepoint under derived key
  `sweep-child-sha256(JCS{sweep_id,child_index,parameter_hash})`, and the immutable outcome plus
  parent count commit in the same transaction. Child admission failures (ownership, resources,
  ADMISSION_OFF, queue, quota, rate) are durable REJECTED with the error snapshot; authorization
  loss, `dependency_unavailable`, `idempotency_in_progress`, WRITE_FROZEN and an exhausted
  transaction retry (`run_transaction`, 3 attempts, 10–50 ms jitter) abort the request and a
  replay resumes the indexes without an outcome (B16-R04). ADMISSION_OFF/WRITE_FROZEN → 409 before
  the parent persists. Retention of `submitSweep` records: B16-R05.
- **5.H Inference.** N comes from the dataset Arrow metadata `item_count`; the runner verifies it
  equals the row count (INVALID_INPUT otherwise), declares it in `inference-state.json` (checkpoint)
  and `metrics.item_count` (result); the server pins `(job_id, item_count, chunk_size)` in new table
  `inference_extents` on the first publish and requires every later publish to match (B16-R06).
  Chunk files `chunk-%08d.jsonl` (fixed float format) or `.parquet` (fixed writer options). Cycle
  (as implemented): `CHUNK_FILE_BATCH` (new chunks, ≤64/frame) → upload + bind `CHUNK_OUTPUT`
  (runner unlinks bound chunk files) → `CHECKPOINT_FILES_READY [inference-state.json]` /
  `RESULT_FILE_BATCH [summary.json]` → bind without finalize → `AUXILIARY_MANIFEST_READY`
  (`CHUNK_OUTPUT_MANIFEST`, built by the runner only from committed chunk bindings) → upload + bind
  `CHUNK_OUTPUT` → finalize over all bindings of the reservation in source order. Server: insert-or-verify RecognizedChunk, carry-forward exact-row rule,
  contiguous prefix cursor, coverage `[0,N)` at complete. Restore: server revalidates the chunk
  manifest and every referenced chunk blob (checksum/size) during restore selection; missing/corrupt
  → CHECKPOINT_CORRUPT + fallback (B16-R07). The carried chunk manifest is mounted read-only at
  `/input/restore/chunk-output-manifest.json`.
- **5.I Validation.** Upload allowlist per attempt adapter (docs/artifacts.md). checkpoint/result
  validators dispatch by descriptor; server reads only bounded JSON (manifest, chunk manifest,
  inference-state/summary ≤ 4 KiB) before the transaction and rechecks identity in it.
- **5.J Migration.** `20260928_0020_b16_sweep_inference` + `schema_v17.py`: sweep columns and
  checks, parent guard trigger (identity immutable, counts non-decreasing), `sweep_children`
  insert-only trigger, `inference_extents` (fenced FK to attempts, immutable), partial index
  `ix_idempotency_records_b16_sweep_parent` for B16-R05. Upgrade refuses pre-existing sweep rows,
  downgrade refuses while sweep/extent rows exist. Upgrade/downgrade/offline/parity tests.
- **5.K Images.** PyTorch amd64 on VPS1; CPU + worker arm64 on Mac, amd64 on VPS1; `src/nexa`
  listing hash compared inside every image; digests in evidence and fixture.json; no push.

### Interpretations (B16-Rxx, details in §Findings)

B16-R01 four templates = 3 executable Template rows + sweep API; B16-R02 dataset metadata embedded
in Arrow schema metadata; B16-R03 two PyTorch images, one Dockerfile; B16-R04 sweep error
classification; B16-R05 retention for submitSweep; B16-R06 N source/pinning; B16-R07 chunk
revalidation at server restore selection; B16-R08 422 for malformed path/query; B16-R09 `template
show` alias; B16-R10 RFC 8785 dedup vs `uniqueItems`; B16-R11 B11 adapter binding for templates
outside an adapter family; B16-R13 chunk window flow control; B16-R14 result manifest prefetch;
B16-R17 chunk count/size bounds; B16-R18 inference restore files; B16-R19 chunk upload phases;
B16-R20 job-scoped inference restore; B16-R21 chunk conflict after re-computation (open);
B16-R22 hardening oracle and runc exec init; B16-R23 CPU fixture `source` erratum. B16-R12
is not used. Round 2: B16-R24 chunk-plan INVALID_INPUT, B16-R25 parser exception mapping, B16-R26
cgroup OOM, B16-R27 sweep key wording, B16-R28 sweep mode `FOR SHARE`, B16-R29 B09 UID oracle on
VPS1 (open, not fixed); B16-R22 extended to `runc init` by argv.

### Milestones

| M | Where | Files | Red test first | Command |
|---|---|---|---|---|
| M1 registry | Mac | domain/workload_adapters.py, coordinator/eligibility+snapshot, worker/probes+models+executor+dispatch+main, runner validators, application validators | tests/domain/test_workload_adapters.py, worker probe multi-image, eligibility capability | pytest tests/domain tests/worker tests/workloads tests/coordinator |
| M2 templates | Mac | deploy/templates/*.json, application/template_service.py, api/routes_templates.py, cli/main.py, cli/app.py | tests/api/test_templates_b16.py, tests/cli/test_template_commands.py | pytest + --run-postgres |
| M3 sweep | Mac | migration 0020, schema_v17, application/sweep_service.py, api routes, cli/commands/sweep.py | tests/application/test_sweep_expansion_b16.py, tests/integration/test_sweep_b16.py | --run-postgres |
| M4 image/fixtures | VPS1 | deploy/pytorch-cpu/*, scripts/b16_*.{py,sh}, tests/fixtures/workloads/* | re-run checksum identity | docker build/run on VPS1 |
| M5 training | Mac + VPS1 | workloads/pytorch_cifar10.py, runner split, checkpoint_validation/restore | component/cursor unit tests; torch tests 7.B | pytest (Mac), container pytest (VPS1) |
| M6 inference | Mac + VPS1 | workloads/batch_inference.py, runner/worker chunk cycle, application chunk validation | chunk property tests, publish tests | pytest, --run-postgres, VPS1 7.B |
| M7 regression P | Mac | — | — | full pytest, --run-postgres, Docker B09–B15 per file |
| M8 Docker L | VPS1 | tests/docker/test_b16_*.py | D1–D8 | see §Reproduction |
| M9 P-smoke | Mac (optional) | — | — | only at pressure level 1 after M8 |
| M10 docs/evidence | Mac | docs/* | — | — |

## Đóng băng (5.F)

Đóng băng lúc **2026-09-27T22:55:25Z**, trước mọi phép đo nghiệm thu (D1–D9). Thứ tự đã theo:
build image (1) → dataset/model (2) → fixture.json + tolerance.json (3) → ghi SHA-256 + UTC ở đây
(4) → đo (5). `tolerance.json` viết từ lập luận ở bước lập kế hoạch và không đổi sau đó; lần chạy
sizing trước đóng băng (30 epoch/1 thread: 32,7 s, maxrss 403 MiB; inference 40 chunk: 2,6 s,
maxrss 357 MiB, không có runner/checkpoint) chỉ để chọn `epochs` theo 5.E, không phải phép đo
nghiệm thu và không dùng để chỉnh tolerance.

| File | SHA-256 |
|---|---|
| `tests/fixtures/workloads/pytorch-cifar10-v1/tolerance.json` | `bbfd3c2503616e2b8f4a7731a34c9366df39bbd1dd4a60b574cb90762cb69637` |
| `tests/fixtures/workloads/pytorch-cifar10-v1/fixture.json` | `355351da97acf881adedb5c9036e6754e7b21dd947115aebb151ba679061e254` |
| `tests/fixtures/workloads/batch-inference-v1/fixture.json` | `409c6f65919278a253268f84d8fce482c432e6856ddc64877dca14103b484179` |
| `tests/fixtures/workloads/cpu-iterative-v1/fixture.json` | `d07c7bf092078a0f350c5cd18a676903726ae0564013a785e48f5c66551fa388` |
| `tests/fixtures/workloads/cpu-iterative-v1/input.json` | `9f955459ad04acc13894487c9dd008d30654a0ec8302595e1bc6e2487fe0a20d` |
| `tests/fixtures/workloads/hyperparameter-sweep-v1/request.json` | `2aa967a8e6d377993ced92fe1e6be816661733d23b74e6378ce5da57fab5876e` |
| `tests/fixtures/workloads/hyperparameter-sweep-v1/expansion.json` | `81cdbe67635881942c3134e424746a89e6d8489f94666db263054c47aad9283f` |

Dữ liệu ngoài repo (VPS1 `$HOME/nexa-b16/data/final`, ba lần build `dev-a`, `dev-b`, `final` cho
cùng bytes; bản `final` dựng bằng image sản phẩm cuối, không mount source):

| File | Size | Checksum |
|---|---|---|
| `cifar-10-binary.tar.gz` (MD5 công bố `c32a1d4ab5d03f1284b67883e8d87530`) | 170052171 | `sha256:c4a38c50a1bc5f3a1c5537f2155ab9d68f9f25eb1ed8d9ddda3db29a59bca1dd` |
| `cifar10-train-5000-eval-1000.arrow` | 18464634 | `sha256:3113e1a4a3537da6c1033bb385f667ebb2d78cc0dcda0f7916a8e3ca787ea41a` |
| `cifar10-inference-2000.arrow` | 6156522 | `sha256:fe0ff7d1c99456bf6cb2d7a42c21212838dba660d52da1d1c2d90f010948def8` |
| `cifar10-smallcnn-v1.safetensors` | 548232 | `sha256:a23e308c9c363803ab416d014772e85159ec4de6e428b89084c682ca2e71f147` |

Image lúc đóng băng (VPS1, linux/amd64, base
`python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`):
`nexa/pytorch-cifar10:b16` `sha256:bd06f8a7b433264c68ed1ea0a2e8b6a6d29ac6cb499310c9e603fa04b016f9bb`,
`nexa/batch-inference:b16` `sha256:2f6d0d433992a652b3d7c0134a689f5aa4b4a7afbc4cc9705662f77ed0f564f8`.
Digest đổi sau đó được ghi vào `image.history` của fixture.json và bảng lịch sử dưới đây;
tolerance.json không đổi.

**Vòng 2.** Image PyTorch được build lại sau sửa runner (B16-R24/R26), nên mỗi fixture.json
thêm đúng một mục `image.history` (`image_ref` `…:b16r2`, digest mới, lý do). Xóa mục đó thì
SHA-256 trở về đúng giá trị đóng băng ở bảng trên. Không đổi trường nào khác, không đo lại để
chỉnh fixture/tolerance. Các file còn lại trong bảng giữ nguyên SHA-256.

| File (vòng 2) | SHA-256 |
|---|---|
| `tests/fixtures/workloads/pytorch-cifar10-v1/fixture.json` | `09bf767e833df9f1eebf4b4cdb21d3e11366b483054eade281a1c98dbe28ebdc` |
| `tests/fixtures/workloads/batch-inference-v1/fixture.json` | `00c1e7c9b98afab85e2f782422f5ab1ad2bcc2b092f2a2d36c394896693c17b3` |

## Nhật ký triển khai (cập nhật theo milestone)

- M2: file thực tế là `application/template_registry.py` (validate/register/catalog),
  `api/routes_templates.py`, `tests/application/test_template_registry_b16.py`,
  `tests/integration/test_templates_b16.py`.
- B16-R08 (ghi nhận, không sửa): handler `RequestValidationError` toàn app trả 422
  `validation_failed` cho path/query sai pattern, trong khi response GET của contract liệt kê 400.
  `getTemplate` giữ cùng quy ước với `getJob`; test khẳng định 422.
- B16-R09: prompt gọi `nexa template list|show`; lệnh `get` có sẵn được giữ và `show` là alias
  cùng hàm (cùng route `/v1/templates/{template_id}`).
- B16-R11 (regression M1, đã sửa): `template_runs_on` ban đầu yêu cầu `template_id` thuộc họ
  template của adapter, làm template B11 `cpu.iterative.coordinator` (adapter `cpu.iterative`
  1.0.0) mất tương thích; 2 test B13 trong `tests/integration/test_migrations.py` fail trên working
  tree, pass trên export baseline `05ed7f4`. Sửa: `workload_adapters.runtime_descriptor` — template
  thuộc họ của một adapter không chạy trên adapter khác; template ID khác giữ ràng buộc B11 theo
  adapter ID/version. Red test `test_template_outside_an_adapter_family_keeps_the_b11_adapter_binding`.
- M3 migration: `tests/integration/test_migration_b16.py` 7 passed; tổng các suite migration 28
  passed (`--run-postgres`).
- B16-R04 (phân loại lỗi sweep): request-level (shape, tên dimension, kiểu/khoảng giá trị, tích
  >100, template không bật) → 422, không ghi gì. Child-level → REJECTED với snapshot
  `{code, message, request_id}`. Nhóm abort (`permission_denied`, `dependency_unavailable`,
  `idempotency_in_progress`, WRITE_FROZEN 409, `TransactionRetryExhausted` 503) kết thúc request,
  không ghi outcome, replay cùng key tiếp tục. Lệch khỏi câu contract "Every child calls the same
  submit use case: auth, …": auth vẫn được kiểm cho từng child, nhưng mất quyền giữa chừng abort thay
  vì ghi REJECTED, để không đóng băng một outcome do trạng thái credential tạm thời.
- B16-R05 (retention `submitSweep`): contract retention không liệt kê sweep; B15 đóng băng
  `SWEPT_OPERATIONS`. Record `submitSweep` COMPLETED hết hạn chỉ bị xóa khi mọi child có outcome và
  không child ACCEPTED nào còn record `submitJob` (tức mọi child đã qua retention terminal), qua
  `_expired_sweep_records` + partial index 0020. Test
  `test_retention_keeps_a_sweep_record_until_every_child_record_is_gone`.
- B16-R10: OpenAPI `uniqueItems` so sánh JSON thô; B16 dedup theo dạng RFC 8785 (`1` và `1.0` là
  một giá trị, giữ lần xuất hiện đầu) thay vì từ chối, số child = tích số giá trị sau dedup.
- Lỗi phát hiện bởi test M3 (đã sửa): `rejected_error=None` ghi JSON `null` vào JSONB, vi phạm
  `ck_sweep_children_exactly_one_outcome` → child ACCEPTED trả 503; dùng SQL `NULL`.
- Dữ liệu seed B05 `test_sweep_parent_ownership_is_enforced_on_insert_and_update` bổ sung 3 cột NOT
  NULL của 0020 (mục đích ownership giữ nguyên). Lượt full suite 1: 1480 passed, 3 failed, 18
  skipped — 1 do seed trên, 2 do subprocess API đọc `app.py` lúc thiếu import `SweepService`
  (đã sửa); cả 3 pass khi chạy lại.
- M3 sweep: `tests/integration/test_sweep_b16.py` 11 passed; CLI `nexa sweep submit|show`
  (`tests/cli/test_sweep_commands_b16.py`), `tests/cli` 153 passed.
- M5 server validation (theo §5.I): server không import framework. Checkpoint PyTorch: đúng 4
  file theo thứ tự rule của adapter (`model|optimizer|rng.safetensors` octet-stream ≤1 MiB,
  `training-state.json` JSON ≤16 KiB), components đóng theo descriptor, cursor
  `{step, epoch, item_cursor, sampler_state_checksum}` trong khoảng suy từ params, compatibility
  bằng đúng `adapter_launch.compatibility(arch, framework_version, restart_safe)` của template
  (amd64 ≠ arm64, framework PYTORCH, CPU). Publish không đọc blob file (như CPU v1). Restore
  (`verify_candidate`) đọc ngoài transaction: tensor chỉ kiểm size/checksum; `training-state.json`
  parse schema đóng + `check_training_state` (input/spec checksum theo provenance) + cursor khớp
  manifest; lệch → CHECKPOINT_CORRUPT `CHECKPOINT_STATE_INVALID` + fallback. Result PyTorch:
  `model.safetensors` rồi `metrics.json`, metrics manifest là tập đóng 11 khóa
  (`MANIFEST_METRICS`), `model_checksum` = checksum file model, số nguyên khớp params, float hữu
  hạn trong khoảng; server không đọc `metrics.json` (runner đã kiểm bản đầy đủ). Adapter khác (inference)
  vẫn từ chối tới M6.
- B16-R14 (thay đổi hành vi, ghi nhận): `complete_attempt` trước đây đọc blob result manifest trong
  transaction (CPU v1). Để thỏa "không đọc blob trong transaction; recheck identity lúc commit", mọi
  adapter giờ prefetch manifest ngoài transaction (`_prefetch_result_manifest`, cùng mẫu
  `_prefetch_checkpoint_manifest` của B14): replay đã ack trả trước khi cần storage; artifact lệch
  với prefetch hoặc commit sau prefetch → 503 retry; vượt 1 MiB → 422. Mã lỗi CPU v1 giữ nguyên
  (B11 closure/race suite pass).
- B16-R15 (lỗi phát hiện bởi test HTTP M5, đã sửa): `json_codec.decode_json_object` giải mã mọi số
  thực của request thành `Decimal` (giá trị binary64 chính xác), nên metrics float của manifest
  PyTorch đi qua HTTP bị 422 "metrics are invalid" dù unit test (dùng `json.loads`) pass. Sửa tối
  thiểu: `validate_result_manifest` chuyển manifest PyTorch qua `json_wire_value` (Decimal →
  float) trước khi kiểm; CPU v1 không đổi. Red test
  `test_http_decoded_manifest_numbers_are_accepted`.
- `CheckpointFixture` B14 nhận thêm kwargs tương thích ngược `input_kind`, `requirements`,
  `parameters` (mặc định giữ nguyên hằng CPU) để `tests/integration/test_pytorch_checkpoint_b16.py`
  dùng lại. Test này (22 case, `--run-postgres`): publish 4 file chấp nhận; 14 lỗi publish từng quy
  tắc bị REJECTED bền vững với reason; tensor >1 MiB bị từ chối; restore chọn bản mới nhất, file
  theo thứ tự rule, claim replay trả cùng context và bytes; newest hỏng (checksum tensor, thiếu blob,
  state lệch cursor, state khác input) → CORRUPT + fallback bản cũ, không mark lại; complete: 4
  manifest lỗi → 422 không ghi result, manifest đúng → SUCCEEDED với 2 RESULT_FILE + manifest,
  replay cùng callback trả cùng ack, callback khác → 409. Blob lưu theo nội dung nên test hỏng tensor
  dùng bytes khác nhau giữa hai checkpoint.
- B16-R16 (lỗi phát hiện bởi harness e2e M5, đã sửa): `CheckpointFlow._expected_manifest` dựng
  danh sách file từ `cycle["bindings"].values()`; journal lưu bindings theo khóa digest nên sau khi
  nạp lại thứ tự là thứ tự hash, không phải thứ tự rule, và worker đánh FAILED
  `CHECKPOINT_PROTOCOL_ERROR` ("checkpoint manifest does not match the cycle") cho manifest 4 file
  đúng của runner (CPU 1 file không lộ). Sửa: nhánh adapter sắp file theo
  `adapter.checkpoint_files`, tập tên phải bằng đúng tập rule. Red test:
  `tests/worker/test_training_flow_b16.py::test_training_attempt_publishes_server_valid_checkpoints_then_the_result`
  (fail trước sửa, pass sau).
- Harness e2e `tests/worker/test_training_flow_b16.py` (2 test, không Docker/torch): worker thật +
  runner thật + fake HTTP chạy validator server (publish: `validate_checkpoint_manifest` +
  `validate_training_state_files`; complete: `decode_json_object` rồi `validate_result_manifest`).
  Hai checkpoint 4 file tăng sequence, result `model.safetensors`+`metrics.json` bind đúng bytes,
  cleanup một lần; checkpoint đã publish được claim lại làm restore, worker tải và mount đủ 4 file,
  runner xác minh lại trước launch và chạy `--resume-dir /input/restore`.
- B16-R13 (flow control cửa sổ chunk): chunk chỉ được xả trong chu kỳ checkpoint/result, còn
  `/output` là tmpfs 16 MiB. Workload `batch.inference` giữ tối đa W = `INFERENCE_WINDOW` = 8 file
  chunk chưa được xác nhận trong `/output`; runner xóa file chunk sau khi binding được worker chấp
  nhận; workload chỉ poll (0,05 s) khi cửa sổ đầy. Khi checkpoint tắt, W = 0 (không giới hạn) và
  toàn bộ chunk phải vừa tmpfs (ENOSPC → INTERNAL). Hệ quả nhịp: thời gian inference ≈
  ceil(chunk/8) × interval checkpoint. Nếu publish bị REJECTED làm tắt checkpoint, cửa sổ đứng tới
  RUNTIME_LIMIT (có giới hạn, fail closed). Code: docstring `workloads/batch_inference.py`,
  `adapter_launch.INFERENCE_WINDOW`.
- B16-R17 (giới hạn chunk): `chunk_manifest.MAX_CHUNKS` = 2048; `MAX_MANIFEST_BYTES` =
  `MAX_CHUNK_FILE_BYTES` = 1 MiB − 16 KiB (bằng reader Docker có giới hạn của worker). Runner từ
  chối params sinh nhiều chunk hơn với INVALID_INPUT. Test trong
  `tests/workloads/test_chunk_manifest_b16.py`.
  Vòng 2 (B16-R24): workload `batch.inference` tự kiểm cùng giới hạn trước khi ghi chunk đầu tiên
  và thoát 65 → INVALID_INPUT/INVALID_INPUT, không retry. Test:
  - `tests/workloads/test_pytorch_workloads_b16.py::test_more_chunks_than_one_manifest_is_invalid_input_before_any_chunk`
    (`item_count` 2049, `chunk_size` 1 → exit 65, `/output` không có file chunk);
  - `tests/workloads/test_pytorch_workloads_b16.py::test_chunk_file_above_the_upload_bound_is_invalid_input`
    (chunk vượt `MAX_CHUNK_FILE_BYTES` → exit 65);
  - runner: `tests/workloads/test_runner_adapter_b16.py::test_workload_exit_codes_map_to_failure_classes`
    (exit 65 → FAILED INVALID_INPUT/INVALID_INPUT);
  - worker: `tests/worker/test_runner_failure_b16.py::test_runner_failure_is_forwarded_with_a_matching_observation`;
  - job: `tests/integration/test_runner_failure_b16.py::test_runner_oom_and_invalid_input_fail_the_job_without_a_retry[INVALID_INPUT-INVALID_INPUT-b16-invalid]`
    (FAILED, `retry_count` 0, không retry schedule, 1 attempt).
- B16-R18 (file restore inference): restore files = file checkpoint (`inference-state.json`) +
  artifact chunk-output manifest, mount tại `/input/restore/chunk-output-manifest.json`; bytes
  chunk không được mount, chỉ được server xác minh lúc chọn restore (B16-R07). Code:
  `application/checkpoint_restore.py`, `workloads/adapter_launch.py`, `worker/adapter_dispatch.py`.
- B16-R19 (lỗi phát hiện bởi red test, đã sửa): upload `RESULT_FILE` media chunk và
  `CHUNK_OUTPUT_MANIFEST` của adapter chunked được phép ở RUNNING hoặc CHECKPOINTING
  (`execution_artifacts._upload_phases`); trước sửa, chunk của chu kỳ checkpoint bị 409. Red test
  `tests/integration/test_execution_upload_b16.py::test_chunk_uploads_follow_the_chunked_windows`.
- B16-R20 (restore theo job): checkpoint inference của job khác (kế thừa qua retry) là INCOMPATIBLE
  `CHECKPOINT_COMPATIBILITY_MISMATCH` vì chunk đã nhận diện thuộc về job nguồn: plan kiểm tra của
  manual retry kế thừa checkpoint mang `job_id` = None nên `verify_candidate` trả INCOMPATIBLE
  (fallback hiển thị), và `adapter_dispatch` của worker từ chối restore inherited cho adapter
  chunked. Test trong `tests/integration/test_inference_chunks_b16.py`.
- B16-R21 (mở, cần quyết định user/contract): sau restore fallback về checkpoint cũ hơn hoặc
  CHECKPOINT_FALLBACK_TO_INPUT, attempt mới tính lại các chunk đã được nhận diện (artifact ID mới);
  `chunk_recognition.recognize` trả 409 `state_conflict` và không commit gì. `WorkerApiError(status,
  code)` không mang message nên worker không phân biệt được với `state_conflict` tạm thời:
  `CheckpointFlow._ready` chỉ map 422 → REJECTED, `_complete` của result lan truyền lỗi.
  Quan sát D6 trên VPS1 (runtime limit 90 s): attempt 2 restore checkpoint seq 1 (cursor 8), chu
  kỳ checkpoint seq 3 bị từ chối rồi ABANDONED; attempt dừng sau ≈94 s và worker phân loại
  INTERNAL/`RUNNER_PROTOCOL_ERROR` (container exit 0 mà worker không thấy frame terminal) thay vì
  TIMEOUT/`RUNTIME_LIMIT_REACHED`; INTERNAL không retry nên job FAILED. Recognized chunk 16 → 16
  (không row nào bị thay/gán lại), không có result. Fail closed: không nhận diện sai, không có
  result kép; contract workloads-checkpoints ("reject conflicting checksum as INTERNAL/stale")
  cho phép từ chối. Hướng đóng: mã lỗi riêng (đổi contract) hoặc restore context mang các chunk đã
  nhận diện sau cursor. Điều kiện đóng: quyết định của user; D6 ghi nhận hành vi hiện tại và không
  assert cách attempt kết thúc.
  Lần chạy cuối 234124Z: cùng scenario, attempt 2 lại kết thúc bằng TIMEOUT/`RUNTIME_LIMIT_REACHED`.
  Job vẫn FAILED, 0 result, recognized chunk 16 → 16. Vậy phân loại lỗi của attempt khác nhau
  giữa các lần chạy (INTERNAL ở 231234Z, TIMEOUT ở 234124Z), nhưng cả hai lần đều fail closed.
  Vì phân loại không ổn định, R21 vẫn mở.
- B16-R22 (diễn giải oracle hardening): D8 đọc mọi PID trong cgroup v2 của container
  (`cgroup.procs`) cùng `/proc/<pid>/status`. Lần chạy đầu thấy một tiến trình UID 1000,
  NoNewPrivs 1, CapEff/CapBnd 0 nhưng Seccomp 0: đó là giai đoạn `runc init` của `docker exec
  --user 1000:1000` mà worker dùng cho control relay và đọc `/output` (`worker/docker_client.py`);
  với no-new-privileges, runc cài seccomp ngay trước execve nên trong cửa sổ đó mã đang chạy là
  runtime, chưa phải lệnh được exec. Lần chạy full cuối (run-20260927T233659Z) lại thấy một tiến
  trình UID 0 trong cgroup ở attempt restore của D2: `runc init` còn chạy với UID 0 trước khi
  setuid sang user của exec (mọi `docker exec` của worker đều có `--user 1000:1000` hoặc
  `1001:1000`). Oracle sửa lại (chỉ test harness, không đổi product): snapshot chỉ được dùng khi
  trong cgroup không có tiến trình `comm` = `runc` hoặc `runc:[…]` (cùng vòng poll có sẵn chờ
  workload UID 1001, không thêm retry sau assert); trên snapshot đó mọi tiến trình phải UID
  ∈ {1000, 1001}, NoNewPrivs 1, Seccomp 2, CapEff=CapBnd=0, không còn ngoại lệ seccomp cho runc.
  Assert UID in toàn bộ danh sách tiến trình. Test: `tests/docker/test_b16_workloads.py::
  _hardened_while_running` và `tests/docker/b16_support.py::assert_hardened`.
- Ghi chú M6: runner không parse nội dung chunk (chỉ size/checksum/tên); kiểm "restored chunks
  claim current attempt" phía worker đã bỏ vì trùng với `recognize` phía server (carry-forward phải
  khớp đúng row trước đó, lệch → ChunkDefect).
- M6 test (Mac, `--run-postgres`): `test_inference_chunks_b16.py` 15,
  `test_execution_upload_b16.py` 13, `test_inference_validation_b16.py` 66,
  `worker/test_inference_flow_b16.py` 9, `test_chunk_manifest_b16.py` 33,
  `test_inference_state_b16.py` 20 — tổng 156 passed. Test torch trong image test trên VPS1:
  `tests/workloads` 111 passed. **Đính chính ở vòng 2:** lượt "111 passed" chạy trên image test
  cũ `nexa/b16-torch-tests:dev` (test được bake lúc 2026-09-27T21:12Z) vì build lại thiếu build arg
  `PRODUCT_IMAGE_REF` và thất bại mà script không dừng; số đó không chứng minh cho source cuối
  vòng 1. Số hợp lệ của vòng 2 ở §Vòng 2.
- Harness D1–D7 (chỉ test, không đổi product) qua 7 lần chạy Docker trên VPS1 (thư mục
  `$HOME/nexa-b16/evidence/run-<UTC>`):

  | Lần | Run | Phạm vi | Kết quả | Nguyên nhân / xử lý |
  |---|---|---|---|---|
  | 1 | 20260927T231234Z | inference | 1 passed, 257,38 s | — |
  | 2 | 20260927T231839Z | training | 1 failed, 66,20 s | harness đọc cột không tồn tại `jobs.progress_snapshot` (`b16_support.progress`); sửa truy vấn |
  | 3 | 20260927T232150Z | training | 1 failed, 101,66 s | tiến trình `runc init` Seccomp 0 (B16-R22) |
  | 4 | 20260927T232518Z | training | 1 failed, 265,14 s | `_settled(child1)` đọc counter toàn cục khi child 2 chưa release: `{(0, 1)} == {(0, 0)}`; sửa: `_released` cho mọi child trước khi kiểm (xem D7) |
  | 5 | 20260927T233113Z | training | 1 passed, 270,11 s | — |
  | 6 | 20260927T233659Z | cả hai file test | 1 failed, 106,74 s | tiến trình UID 0 của `runc init` (B16-R22, mở rộng) |
  | 7 | 20260927T234124Z | cả hai file test | **2 passed, 539,10 s** | lần chạy cuối vòng 1; raw evidence vòng 1 lấy từ lần này |
  | 8 | 20260928T022450Z | vòng 2, B16 + `test_real_runner` | lỗi setup | PG test tạo mới chưa có DB `nexa_b05_test_b16docker`; tạo DB |
  | 9 | 20260928T022606Z | vòng 2 | 1 failed, 96,56 s | D1 `assert_hardened`: tiến trình `comm` "6", UID 0, Seccomp 0 = `runc init` re-exec qua `/proc/self/fd/N` (B16-R22 mở rộng); oracle nhận diện thêm theo argv `runc init` |
  | 10 | 20260928T023025Z | vòng 2 | 1 failed, 233,10 s | D7 so replay theo cả version counter; coordinator dispatch child ACCEPTED trong lúc đó tăng version (GLOBAL 21→22) mà outstanding giữ 2; so `_spent` (outstanding + rate bucket) |
  | 11 | 20260928T023918Z | vòng 2 | 5 passed, 1 failed, 645,38 s | 3 test B16 pass (raw vòng 2); `test_real_runner` fail do oracle UID B09 (B16-R29) |
  | 12 | 20260928T025359Z | vòng 2, `test_real_runner` bỏ test đã fail | 2 passed, 1 failed, 1 deselected, 71,99 s | cùng nguyên nhân B16-R29; dừng theo quy tắc 2 lần điều tra |

  Không lần nào có assert product sai. Không thêm retry để che lỗi.

## Vòng 2 (sau Task Review vòng 1)

Task Review vòng 1: "Không duyệt", blocking R24/R25/R26, non-blocking R27/R28, residual chấp nhận
R21/R05. Vòng 2 chỉ sửa các finding đó. Không đổi migration, contract, PLAN, `tolerance.json`,
dữ liệu fixture (`data/final` không build lại) hay tham số D1–D8.

### Sửa đổi

- **B16-R24 (chunk plan quá giới hạn → INVALID_INPUT).** `workloads/batch_inference.py` kiểm
  `inference_state.chunk_count(item_count, chunk_size) > chunk_manifest.MAX_CHUNKS` trước khi ghi
  chunk đầu tiên, và kiểm mỗi file chunk ≤ `MAX_CHUNK_FILE_BYTES` trước khi ghi file đó; cả hai
  raise `WorkloadInputError`. `main` của hai workload trả 65 cho `WorkloadInputError`, 70 cho lỗi
  khác. Runner map exit 65 của adapter launch spec v3 → FAILED INVALID_INPUT/INVALID_INPUT. Test
  liệt kê ở bullet B16-R17 (§Nhật ký).
- **B16-R25 (lỗi parse metadata/header).** `workloads/dataset_format.py` và
  `workloads/safetensors_format.py` kiểm kiểu JSON trước khi sắp xếp hay so sánh. Mọi lỗi parse
  (JSON không strict, vượt giới hạn chữ số số nguyên, lồng quá sâu/`RecursionError`, `TypeError`,
  `ValueError`) thành `DatasetError`/`SafetensorsError`, rồi thành `WorkloadInputError` và exit 65
  ở cả hai workload. Test (chạy trong image test trên VPS1):
  - `tests/workloads/test_dataset_format_b16.py::test_metadata_wrong_json_types_raise_dataset_error` (13 case);
  - `tests/workloads/test_dataset_format_b16.py::test_parse_metadata_maps_parser_limits_to_dataset_error` (3);
  - `tests/workloads/test_safetensors_format_b16.py::test_wrong_json_types_and_parser_limits_raise_safetensors_error` (17);
  - `tests/workloads/test_pytorch_workloads_b16.py::test_training_main_maps_malformed_metadata_to_invalid_input` (3, exit 65);
  - `tests/workloads/test_pytorch_workloads_b16.py::test_inference_main_maps_malformed_model_header_to_invalid_input` (3, exit 65).
- **B16-R26 (OOM có kiểu).** `workloads/trusted_runner.py` đọc `oom_kill` trong
  `/sys/fs/cgroup/memory.events` (cgroup namespace của container, đọc có giới hạn 4 KiB) ngay
  trước START, lưu baseline vào runner state (sống qua reload). Workload thoát khác 0 mà counter
  tăng so với baseline → FAILED OOM/CONTAINER_OOM, `oom_killed` true, cho mọi adapter kể cả CPU.
  Counter đã khác 0 lúc START mà không tăng, hoặc file không đọc được/sai định dạng, không phải
  bằng chứng → INTERNAL/WORKLOAD_EXIT_NONZERO. Exit 65 của adapter vẫn là INVALID_INPUT dù counter
  tăng. `worker/execution.py` chuyển tiếp OOM chỉ khi `oom_killed` true, với observation
  CONTAINER `exit_code` null, `oom_killed` true; `oom_killed` true trên class khác bị hạ thành
  INTERNAL. Server không retry OOM và INVALID_INPUT (contract). Test:
  - runner: `tests/workloads/test_runner_oom_b16.py::test_cgroup_oom_kill_rise_is_failed_oom`
    (3 adapter × 6 case: tăng 0→1 và 2→3 → OOM; 2→2, không đọc được lúc START hoặc lúc exit,
    exit 1 không tăng → INTERNAL), `test_adapter_invalid_input_exit_stays_invalid_input_even_with_an_oom_rise`,
    `test_oom_kill_baseline_survives_runner_state_reload`, `test_malformed_memory_events_is_no_evidence` (6) — 27 test;
  - worker: `tests/worker/test_runner_failure_b16.py::test_runner_failure_is_forwarded_with_a_matching_observation` (5);
  - job (`--run-postgres`): `tests/integration/test_runner_failure_b16.py::test_runner_oom_and_invalid_input_fail_the_job_without_a_retry`
    (OOM và INVALID_INPUT: job FAILED, `retry_count` 0, `retry_ready_at` null, 0 retry schedule,
    1 attempt, allocation RELEASED, counter về 0, dù job restart-safe có checkpoint committed) và
    `test_runner_oom_without_the_oom_observation_is_rejected` (409, không ghi failure);
  - chạy thật trên L: `tests/docker/test_b16_workloads.py::test_b16_workload_oom_is_typed_and_not_retried`
    (§Vòng 2 L).
- **B16-R27 (non-blocking, tài liệu).** `docs/cli.md`: `nexa sweep submit` tự sinh key nhưng không
  in; muốn tiếp tục sau khi mất response phải truyền `--idempotency-key` từ đầu.
- **B16-R28 (non-blocking).** `SweepService` đọc operational mode của request sweep bằng
  `FOR SHARE` (giữ tới khi parent commit). Lượt kiểm mode trước từng child vẫn không khóa, vì job
  admission của child kiểm lại dưới `FOR UPDATE` trên cùng row, và `FOR SHARE` trước đó trong cùng
  transaction sẽ phải nâng khóa (nguy cơ deadlock).
- **Tài liệu:** `docs/trusted-runner.md` (đoạn "Workload exit"), `docs/worker-agent.md` (4 lớp
  failure được chuyển tiếp và quy tắc `oom_killed`), `docs/submit.md` (R17 và OOM không retry).
- **Chỉ harness test** (không đổi product):
  - `tests/docker/b16_support.py`: `register_oom_templates` đăng ký version 2 của hai template AI
    chỉ trong DB test Docker, `memory_bytes.minimum` 256 MiB (template trong `deploy/templates`
    không đổi); tiến trình trong hardening ghi thêm 2 phần tử đầu của argv;
  - B16-R22 mở rộng: `runc init` của `docker exec` re-exec qua `/proc/self/fd/N` nên `comm` là số
    (thấy "6" trên VPS1, UID 0, Seccomp 0, capability đầy đủ). Snapshot còn tiến trình có argv
    `["runc", "init"]` bị bỏ qua như `comm` `runc`/`runc:[…]`; điều kiện trên snapshot hợp lệ không
    đổi;
  - D7: so replay bằng `_spent` (outstanding theo scope + version rate bucket). Coordinator có
    thể dispatch child ACCEPTED trong lúc replay; dispatch tăng `version` cùng `active_attempts`
    (`coordinator/dispatch.py`), không đổi outstanding.

### Image vòng 2

Build trên VPS1 bằng `~/nexa-b16/tools/build_r2.sh` (không push), Mac bằng
`scripts/b09_build_image.sh` + `deploy/b10/Dockerfile`. Base
`python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`, PyTorch 2.12.1
CPU, label adapter như vòng 1.

| Image | Kiến trúc | Digest / ID | Vai trò |
|---|---|---|---|
| `nexa/pytorch-cifar10:b16r2` | linux/amd64 | `sha256:fcad40287b78e8a7f3208168c739aaadc0c2e2004290876950c1f2ae4391e797` | training, D1–D3, D7, D8, OOM |
| `nexa/batch-inference:b16r2` | linux/amd64 | `sha256:db50610aa8c93896202638ca7e731b6a0595ead3b6ea62ca331c313c4f42c4ae` | inference, D4–D6, D8, OOM |
| `nexa/cpu-iterative:b16r2-amd64` | linux/amd64 | `sha256:6d6d0635188a9ce88fccc09d2020ec7635dae30fa2b30f05d72ed4a0f8a549d5` | CPU trong harness L |
| `nexa/b16-worker:amd64-r2` | linux/amd64 | `sha256:5defa94127454e9e1a0afc74613d05d75e88729a39ef92d550d1a17a3f7abfe5` | worker L |
| `nexa/cpu-iterative:b16r2` | linux/arm64 | `sha256:95f89f118eb808ced3e475929378c40997ad0b47d6ffe27c3c9b3d0f2e194b6b` | regression Docker P |
| `nexa/b16-worker:r2-local` | linux/arm64 | `sha256:788a8fc820fb239adb3a559ebab38fb36981439dc18bb2503b90035843b41625` | regression Docker P |
| `nexa/b16-torch-tests:r2` (chỉ test) | linux/amd64 | `sha256:ef8991006122c9bbf1fa2ae21a92ef936562d259b6ca743738a6b21f7bdd907c` | torch unit test 7.B |

- Listing hash `src/nexa` (cùng cách tính vòng 1): 149 file,
  `2486dccacf654ea56730ab60f4e06cf0c11bcac065700ba6a912f65d3a76fa39`, giống hệt trên source Mac,
  bản rsync VPS1 và bên trong cả 6 image sản phẩm/worker ở bảng trên.
- `tests/`, `scripts/`, `deploy/`, `migrations/`: 251 file, hash danh sách sha256
  `05bdbcec…fcdf` giống nhau trên Mac và VPS1.
- `docker save`: `pytorch-cifar10-b16r2-amd64.tar` sha256 `368ca44a…85aa`,
  `batch-inference-b16r2-amd64.tar` sha256 `071b3c72…7da5`.
- `image.history` của hai fixture.json ghi digest vòng 2 (§Đóng băng, vòng 2). Image vòng 1
  (`bd06f8a7…`, `2f6d0d43…`) chỉ còn là evidence vòng 1.
- Không build image PyTorch arm64.

### Vòng 2 L: VPS1

- **Torch unit test 7.B** (`torch_tests.sh` đã sửa: build image test với
  `--build-arg PRODUCT_IMAGE_REF=nexa/pytorch-cifar10:b16r2` sau khi kiểm ID bằng `fcad4028…`,
  build lỗi thì ghi BUILD_FAILED và dừng, ghi ID image test và SHA-256 file test trước khi chạy):
  `tests/workloads -rs` **153 passed, 0 skipped, 10,44 s**, EXIT 0. SHA-256 file test trong lượt
  này khớp Mac (`test_pytorch_workloads_b16.py` `c3b971c0…`, `test_safetensors_format_b16.py`
  `a6797c19…`, `test_dataset_format_b16.py` `f57ef2f5…`). Lượt `-k` chỉ các test R24/R25:
  41 passed, 112 deselected.
- **Docker** (`run_docker.sh` với ref vòng 2, lệnh như §L cộng `tests/docker/test_real_runner.py`):
  - run 20260928T023918Z: `test_b16_pytorch_training_crash_resume_corruption_and_sweep`,
    `test_b16_chunked_inference_carry_forward_and_blob_loss`,
    `test_b16_workload_oom_is_typed_and_not_retried` **pass**; raw:
    [`raw/B16-r2-training.json`](raw/B16-r2-training.json),
    [`raw/B16-r2-inference.json`](raw/B16-r2-inference.json), [`raw/B16-oom.json`](raw/B16-oom.json);
  - `test_real_runner.py` trên L: **fail** (B16-R29). Tổng qua run 11 và 12: 2 passed
    (`test_authority_before_supervisor_registration_launches_once_as_workload_uid`,
    `test_supervisor_registration_after_startup_deadline_never_launches_cpu`), 2 failed
    (`test_production_executor_runner_cpu_result_handshake`,
    `test_production_container_denies_rootfs_input_network_and_runner_control`), 5 not-run (`-x`).
    Cả 9 test pass trên P/Docker (bảng dưới).
- **OOM thật (R26)**: template version 2 (chỉ DB test), `memory_bytes` 256 MiB, 1 CPU.

  | Job | Tham số | Kết quả | Attempts | Retry | Allocation | Events | Thời gian |
  |---|---|---|---|---|---|---|---|
  | training | `batch_size` 512, `subset_size` 5000, `epochs` 30 | FAILED, 0 result, 0 checkpoint | 1: fence 1, OOM/CONTAINER_OOM | `retry_count` 0, `retry_ready_at` null, 0 retry | quarantine → RELEASED `VERIFIED_CLEANUP` | JOB_ACCEPTED, JOB_DISPATCHING, ATTEMPT_STARTED, ATTEMPT_FAILED `CONTAINER_OOM`, ALLOCATION_RELEASED (5) | 10,2 s |
  | inference | `chunk_size` 2000, `batch_size` 2000 | FAILED, 0 result, 0 checkpoint | 1: fence 1, OOM/CONTAINER_OOM | như trên | như trên | như trên (5) | 15,1 s |

  `memory.peak` của cả hai attempt là 268435456 byte (bằng giới hạn). Container stop/verify, lease
  revoke, grant kết thúc; `max_retries` 2 nhưng không có retry. Test assert state, attempts,
  retry, 0 result và event `ATTEMPT_FAILED`/`CONTAINER_OOM`; peak chỉ ghi nhận.
- **D1–D8 vòng 2** (cùng scenario, image vòng 2), so với vòng 1:
  - D1: 5 checkpoint, 2370 step / 30 epoch, `eval_accuracy` 0,427, 52,9 s; `model.safetensors`
    `f4363da9…cc8e` giống vòng 1 từng byte; metrics chỉ khác `spec_checksum` (spec chứa artifact ID
    của lượt, B16-R23);
  - D2: restore seq 2 (cursor step 784, epoch 9, item 4672), D3: CHECKPOINT_CORRUPT seq 2 →
    restore seq 1 (step 332, epoch 4); cả hai 13/13 đại lượng tolerance trong giới hạn, chênh 0,
    bằng baseline từng byte. Cursor khác vòng 1 vì thời điểm kill/checkpoint theo thời gian thực;
  - D4: 4 checkpoint, 40 chunk, prediction counts `[54, 169, 151, 338, 355, 429, 83, 37, 346, 38]`
    giống vòng 1; summary `sha256:a8795553…c3d4` (chunk nhúng `spec_checksum`, nên khác vòng 1);
  - D5: restore seq 2 (step 16, item 800), carry-forward 16 + tính 24, summary bằng D4 từng byte;
  - D6: CHECKPOINT_BLOB_MISSING → fallback seq 1, attempt 2 FAILED INTERNAL/`RUNNER_PROTOCOL_ERROR`,
    job FAILED, 0 result, recognized 16 → 16. Fail closed; phân loại lại khác lần trước
    (B16-R21, residual);
  - D7: 6 child, 2 ACCEPTED SUCCEEDED, 4 REJECTED `quota_exceeded`; counter trước → sau như vòng 1
    (GLOBAL `(0,19)` → `(2,22)`, TENANT/USER `(0,17)` → `(2,20)`, bucket 4 → 6); replay giống hệt,
    không tiêu thêm;
  - D8: mọi snapshot hợp lệ có runner UID 1000, workload UID 1001, relay UID 1000, Seccomp 2,
    NoNewPrivs 1, CapEff = CapBnd = 0.
- **RAM đỉnh vòng 2** (`memory.peak`, 1 GiB): D1 356,1; D2 320,4 / 363,5; D3 323,9 / 355,1; D7
  354,8 / 354,2; D4 280,9; D5 281,2 / 281,0; D6 280,5 / 280,4 MiB.

### Vòng 2 P: Mac

- Ruff: `ruff check .` "All checks passed!", `ruff format --check .` "456 files already formatted";
  `git diff --check` sạch.
- Pytest mặc định: **1588 passed, 523 skipped, 2 warnings**, 38,85 s; lặp lại có `-rs` cùng số
  đếm (39,54 s). Skip: 510 cần `--run-postgres`, 11 Docker B09 opt-in, 1 module thiếu `torch`,
  1 relay B11 opt-in.
- Pytest PostgreSQL (`nexa_b05_test_b16`): **2089 passed, 22 skipped, 2 warnings**, 547,97 s.
  Lượt này bắt đầu trước sửa R28, nên các test sweep và runner failure được chạy lại sau sửa:
  `test_sweep_expansion_b16.py`, `test_sweep_commands_b16.py`, `test_sweep_b16.py`,
  `tests/integration/test_runner_failure_b16.py`, `tests/worker/test_runner_failure_b16.py`,
  `test_runner_oom_b16.py`: **63 passed**, 35,90 s. `-rs` trên `tests/docker tests/workloads`
  (437 passed, 22 skipped): 22 skip = `test_real_runner` 9, B16 3, `test_b11_vertical` 2, B15 2,
  `test_real_executor` 2, B10 1, B11 IPC 1, B14 1 (opt-in Docker) và 1 module thiếu `torch`.
- Docker regression Mac (image CPU `nexa/cpu-iterative@sha256:95f89f11…4b6b`, worker
  `nexa/b16-worker:r2-local`; mỗi lần một file, kiểm áp lực trước từng file):

  | File | Kết quả | Thời gian | Trước khi chạy (mức, % free, PID caddy) |
  |---|---|---|---|
  | `test_real_runner.py` | 9 passed | 118,20 s | 2, 32 %, 26.137 |
  | `test_real_executor.py` | 2 passed | 2,93 s | 2, 32 %, 26.161 |
  | `test_b11_worker_ipc.py` | 1 passed | 8,84 s | 2, 33 %, 26.162 |
  | `test_b11_vertical.py` | 2 passed | 97,52 s | 2, 36 %, 26.164 |
  | `test_b14_checkpoint_restore.py` | 1 passed | 469,98 s | 2, 35 %, 26.183 |
  | `test_b15_control_recovery.py` | 2 passed | 903,26 s | 2, 33 %, 26.276 |

  Tổng **17 passed, 0 failed**. `test_b10_worker_restart.py` và `test_b16_workloads.py` không
  chạy lại trên Mac ở vòng 2 (vòng 1: skip đúng thiết kế). Swap khoảng 6,9–7,0/8 GiB, không lượt
  nào chạm mức 4. Không có container/volume B16 còn lại trên Mac; `nexa_b10_*` không bị động tới.
- P-smoke PyTorch trên Mac: **not-run** (áp lực mức 2); không có claim PyTorch arm64.

## Kết quả kiểm chứng

### Môi trường

- **P (Mac + PostgreSQL thật).** Pytest trên host Mac với `nexa_b13_pg` (PostgreSQL 17, cổng
  15439, DB `nexa_b05_test_b16` và `nexa_b05_test_b16docker`). Docker trên Mac là Docker Desktop
  VM (engine 29.8.0, kernel 7.0.12-linuxkit, aarch64, 8 CPU, khoảng 3,8 GiB), **không phải L**.
  Trên Mac chỉ chạy regression CPU/worker arm64. Không cài torch, numpy, pyarrow hay safetensors
  lên host Mac và không chạy PyTorch trên host Mac.
- **L (VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04)).** Ubuntu 24.04.5 LTS, 8 vCPU, khoảng 15 GiB,
  kernel 7.0.0-1013-aws, linux/amd64, Docker Engine 29.8.1, cgroup v2 (systemd), seccomp builtin,
  AppArmor. VM EC2 chạy Linux thật, **không phải** bare metal, GPU (G) hay release (R). PostgreSQL
  test (`nexa_b16_pg`, PostgreSQL 17) và API test chỉ nghe trên 127.0.0.1. Lệnh dài chạy trong
  tmux `nexa-b16`. Không dùng sudo.
- Evidence này **không thay thế**: ACC-22 đầy đủ (B15), GC (B19), tải lớn (B22), demo (B24),
  GPU/CUDA (B23).

### Revision, source và image

- Revision: `05ed7f4` cộng diff chưa commit của B16 (51 file tracked đổi, 79 file mới, gồm
  evidence/raw). Sau đóng băng chỉ `tests/docker/*` và tài liệu đổi. Product source và image
  PyTorch giữ nguyên.
  Vòng 2: 51 file tracked đổi, 85 file mới (thêm 3 file test và 3 raw JSON); product source và
  image đổi theo R24–R26/R28, listing hash và image mới ở §Vòng 2. Số liệu dưới đây là vòng 1.
- Listing hash của `src/nexa`: sha256 của các dòng `sha256  <đường dẫn tương đối>` cho mọi
  `*.py`, sắp theo đường dẫn. Kết quả: 149 file, `0f684a4dd4bb98e38d197459a183b24077321891d185074adadbe9e97165c610`.
  Giá trị này giống hệt trên:
  - source trên Mac và bản rsync lên VPS1;
  - `nexa/pytorch-cifar10@sha256:bd06f8a7…f9bb` và `nexa/batch-inference@sha256:2f6d0d43…64f8`;
  - CPU amd64 `nexa/cpu-iterative@sha256:20fd58cb…d89e` và worker amd64 `sha256:ee004e09…e1e7`;
  - CPU arm64 `nexa/cpu-iterative@sha256:519a8697…7d45` và worker arm64 `nexa/b16-worker:local`
    `sha256:5eb661af…4835`.
- `tests/`, `scripts/`, `deploy/` và `migrations/` trên VPS1 cũng khớp Mac (hash của danh sách
  sha256 là `484b3780…59c0`).

| Image | Kiến trúc | Digest / ID | Vai trò |
|---|---|---|---|
| `nexa/pytorch-cifar10:b16` | linux/amd64 | `sha256:bd06f8a7b433264c68ed1ea0a2e8b6a6d29ac6cb499310c9e603fa04b016f9bb` | adapter `pytorch.cifar10`, D1–D3, D7 |
| `nexa/batch-inference:b16` | linux/amd64 | `sha256:2f6d0d433992a652b3d7c0134a689f5aa4b4a7afbc4cc9705662f77ed0f564f8` | adapter `batch.inference`, D4–D6 |
| `nexa/cpu-iterative:b16-amd64` | linux/amd64 | `sha256:20fd58cb53885085338454394653d67d78f0a9ab16a1984898eefd2fa3d8d89e` | CPU adapter trong harness L |
| `nexa/b16-worker:amd64` | linux/amd64 | `sha256:ee004e09bc24769ea990cc2705513af225fafe579639f50b36a5aac7670ce1e7` | worker L |
| `nexa/cpu-iterative:b16` | linux/arm64 | `sha256:519a86971ea9d836c1c85fb8bda259302b9c74b342241c0bc6afad85d8757d45` | regression Docker P |
| `nexa/b16-worker:local` | linux/arm64 | `sha256:5eb661af7fdb7223437cb23958deb668673fa72bf2a1f1d0250fe3840ed54834` | regression Docker P |

- Base image của cả 6 image: `python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`.
- Framework: PyTorch 2.12.1 CPU.
- Không image PyTorch arm64 nào được build.
- Không push image lên registry.
- Lịch sử image PyTorch: digest cuối chính là digest lúc đóng băng, `image.history` trong
  fixture.json rỗng. Các bản build trước đóng băng (`nexa/pytorch-cifar10:dev` cùng image test
  `nexa/b16-torch-tests:dev`) chỉ dùng để phát triển và không nằm trong bất kỳ phép đo nghiệm
  thu nào.
- `docker save`: `$HOME/nexa-b16/images/pytorch-cifar10-b16-amd64.tar` (sha256 `1be2829f…5000`)
  và `batch-inference-b16-amd64.tar` (sha256 `18b68773…1c6f`). `index.json` trong mỗi tar trỏ
  đúng digest `bd06f8a7…` và `2f6d0d43…`.
- Digest được ghi ở `final-save-digests.jsonl` trước khi save. Máy khác chạy `docker load` rồi
  so `docker image inspect --format '{{.Id}}'` với bảng trên.

### L: Docker trên VPS1

Lệnh chạy (`$HOME/nexa-b16/tools/run_docker.sh`, không bao giờ in password):

```
NEXA_RUN_DOCKER=1 \
NEXA_B16_PYTORCH_IMAGE_REF=nexa/pytorch-cifar10@sha256:bd06f8a7b433264c68ed1ea0a2e8b6a6d29ac6cb499310c9e603fa04b016f9bb \
NEXA_B16_INFERENCE_IMAGE_REF=nexa/batch-inference@sha256:2f6d0d433992a652b3d7c0134a689f5aa4b4a7afbc4cc9705662f77ed0f564f8 \
NEXA_B09_IMAGE_REF=nexa/cpu-iterative@sha256:20fd58cb53885085338454394653d67d78f0a9ab16a1984898eefd2fa3d8d89e \
NEXA_B11_WORKER_IMAGE=sha256:ee004e09bc24769ea990cc2705513af225fafe579639f50b36a5aac7670ce1e7 \
NEXA_B16_DATA_DIR=$HOME/nexa-b16/data/final NEXA_B16_EVIDENCE_OUT=<run dir> \
NEXA_TEST_DATABASE_URL=postgresql+psycopg://postgres:<file 0600>@127.0.0.1:15441/nexa_b05_test_b16docker \
PYTHONPATH=src:. uv run --no-sync pytest --run-postgres -q -p no:cacheprovider -x tests/docker/test_b16_workloads.py
```

- Lần chạy cuối 20260927T234124Z: **2 passed, 0 failed, 0 skipped, 539,10 s**:
  - `test_b16_pytorch_training_crash_resume_corruption_and_sweep` (D1, D2, D3, D7 và D8 cho
    image training);
  - `test_b16_chunked_inference_carry_forward_and_blob_loss` (D4, D5, D6 và D8 cho image
    inference).
- Torch unit test (7.B) chạy trong image test (`deploy/pytorch-cpu/test.Dockerfile`, `--network none`,
  `--memory 4g`, read-only, source cuối mount read-only, `data/final`) với
  `$HOME/nexa-b16/tools/torch_tests.sh`: `tests/workloads` **111 passed**, 8,55 s. **Đính chính
  vòng 2:** lượt này chạy image test cũ (xem §Nhật ký M6), không chứng minh cho source cuối vòng 1;
  số hợp lệ vòng 2 là 153 passed (§Vòng 2).
- D9 (worker restart giữa chu kỳ checkpoint PyTorch): **not-run** (tùy chọn, không đủ thời gian).
- Kiểm JournalCorruption/virtiofs trên VPS1: **not-run** (tùy chọn).

### Timeline D1–D6

Mọi scenario dùng coordinator epoch 1. Mỗi file test có một worker incarnation (ID rút gọn 4 ký
tự cuối; ID đầy đủ nằm trong raw). Lease hết hiệu lực bằng revoke. Mọi allocation được release
bằng `VERIFIED_CLEANUP`, kể cả allocation quarantine. Container dừng và được verify. Mỗi job có
1 idempotency record submit.

| Scenario | Coordinator epoch | Worker incarnation | Job fence (job) | Attempts (nr: fence, state, failure) | Leases (fence, revoked) | Allocations (quarantined → release) | Checkpoints (ID seq, corrupt) | Restore per attempt | Events (last seq) | Ledger segments (charged) | Final |
|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 | 1 | …e3a0 | 1 | 1: 1, SUCCEEDED | 1, yes | – → RELEASED VERIFIED_CLEANUP | …2884 1; …97ff 2; …3259 3; …125f 4; …d12f 5 | none | 10 (10) | 12 (8.88875000) | SUCCEEDED, 1 result |
| D2 | 1 | …e3a0 | 3 | 1: 1, FAILED, INFRASTRUCTURE/RUNNER_UNAVAILABLE; 2: 3, SUCCEEDED | 1, yes; 3, yes | Q → RELEASED VERIFIED_CLEANUP; – → RELEASED VERIFIED_CLEANUP | …8210 1; …c6de 2; …dc90 3; …6eda 4; …7f7a 5 | none; …c6de seq 2 | 16 (16) | 15 (11.13578125) | SUCCEEDED, 1 result |
| D3 | 1 | …e3a0 | 3 | 1: 1, FAILED, INFRASTRUCTURE/RUNNER_UNAVAILABLE; 2: 3, SUCCEEDED | 1, yes; 3, yes | Q → RELEASED VERIFIED_CLEANUP; – → RELEASED VERIFIED_CLEANUP | …5db1 1; …fed5 2 CHECKPOINT_CHECKSUM_MISMATCH; …ff03 3; …686b 4; …4494 5; …6a7a 6 | none; …5db1 seq 1 | 18 (18) | 20 (13.53890625) | SUCCEEDED, 1 result |
| D4 | 1 | …6dfd | 1 | 1: 1, SUCCEEDED | 1, yes | – → RELEASED VERIFIED_CLEANUP | …ee93 1; …52d8 2; …31ab 3; …777c 4 | none | 9 (9) | 11 (8.35734375) | SUCCEEDED, 1 result |
| D5 | 1 | …6dfd | 3 | 1: 1, FAILED, INFRASTRUCTURE/RUNNER_UNAVAILABLE; 2: 3, SUCCEEDED | 1, yes; 3, yes | Q → RELEASED VERIFIED_CLEANUP; – → RELEASED VERIFIED_CLEANUP | …874b 1; …4f57 2; …ea4f 3; …4ad8 4 | none; …4f57 seq 2 | 15 (15) | 17 (11.11937500) | SUCCEEDED, 1 result |
| D6 | 1 | …6dfd | 4 | 1: 1, FAILED, INFRASTRUCTURE/RUNNER_UNAVAILABLE; 2: 3, FAILED, TIMEOUT/RUNTIME_LIMIT_REACHED | 1, yes; 3, yes | Q → RELEASED VERIFIED_CLEANUP; Q → RELEASED VERIFIED_CLEANUP | …417d 1; …f094 2 CHECKPOINT_BLOB_MISSING | none; …417d seq 1 | 14 (14) | – | FAILED, 0 result |

Chú thích cột:
- Q = allocation bị quarantine khi attempt thất bại bị fence. Chỉ release sau khi cleanup
  hoặc reconcile container đúng identity.
- Job fence 3 ở D2/D3/D5:
  - dispatch attempt 1 → 1;
  - worker báo `ATTEMPT_FAILED` → 2 (`execution_cleanup`: fence, revoke lease, quarantine);
  - dispatch attempt 2 → 3.
- Job fence 4 ở D6: attempt 2 cũng thất bại và bị fence thêm một lần.
- Ledger: số segment và tổng `charged_amount` (cộng bằng Decimal) do `reconcile` ghi lại. Test
  chỉ ghi nhận hai số này, không assert giá trị. D6 không đi qua `reconcile` nên raw không có
  ledger.

Các kiểm tra được assert:
- **Scenario thành công** (D1–D5 và hai child ACCEPTED của D7) dùng `reconcile` (helper B14
  dùng chung). Helper này chờ mọi allocation RELEASED rồi assert:
  - job SUCCEEDED, desired_state RUNNING, 1 idempotency record submit;
  - đúng số attempt và allocation; đúng 1 result, thuộc attempt cuối;
  - event sequence liên tục từ 1 đến `jobs.event_sequence`;
  - không còn reservation `RESERVED`, và `checkpoint_sequence` bằng sequence lớn nhất;
  - số `ALLOCATION_RELEASED` bằng số attempt;
  - container stop/verify, lease revoke, grant kết thúc, không còn container của attempt;
  - `admission_counters` là `{(0, 0)}`.
- **Scenario không thành công** (D6) dùng `_settled`: allocation RELEASED; container, lease,
  grant và counter như trên; không có result.

- **D1** (training liền mạch, 51,9 s): 5 checkpoint, 2370 step / 30 epoch, `eval_accuracy`
  0,427.
- **D2** (SIGKILL workload sau checkpoint 2):
  - attempt 1 FAILED INFRASTRUCTURE/`RUNNER_UNAVAILABLE` → `RETRY_READY`;
  - attempt 2 restore checkpoint seq 2 (cursor step 997, epoch 12, item 3136) → SUCCEEDED;
  - 1 result;
  - `model.safetensors` và `metrics.json` giống baseline từng byte;
  - result provenance giống hệt.
- **D3** (làm hỏng blob của checkpoint mới nhất, seq 2, trước restore):
  - `CHECKPOINT_CORRUPT` `CHECKPOINT_CHECKSUM_MISMATCH` rồi `CHECKPOINT_RESTORE_SELECTED` seq 1
    (cursor step 336, epoch 4);
  - kết quả giống baseline từng byte.
- **D4** (inference liền mạch, 48,3 s): 4 checkpoint, 40 chunk, summary
  `sha256:2af47cef…58c5`. Số dự đoán theo lớp: `[54, 169, 151, 338, 355, 429, 83, 37, 346, 38]`
  (tổng 2000).
- **D5** (kill sau 16 chunk được nhận diện và ≥ 2 checkpoint):
  - attempt 2 restore checkpoint seq 2 (cursor step 16, item 800);
  - carry-forward 16 chunk, chỉ tính 24 chunk còn lại;
  - summary giống baseline từng byte.
- **D6** (xóa blob chunk `chunk-00000008` mà checkpoint mới nhất seq 2 tham chiếu, trước
  restore):
  - `CHECKPOINT_CORRUPT` `CHECKPOINT_BLOB_MISSING` → fallback về seq 1 (cursor step 8);
  - attempt 2 FAILED TIMEOUT/`RUNTIME_LIMIT_REACHED` (runtime limit 90 s);
  - job FAILED, 0 result;
  - recognized chunk 16 → 16, không row nào bị thay hay gán lại;
  - hành vi này thuộc B16-R21 (mở); lần chạy 231234Z cùng scenario kết thúc
    INTERNAL/`RUNNER_PROTOCOL_ERROR`;
  - D6 không assert cách attempt kết thúc, chỉ assert fail closed.

### So sánh PyTorch theo tolerance

Tolerance: `tests/fixtures/workloads/pytorch-cifar10-v1/tolerance.json` (`bbfd3c25…9637`), không
đổi từ lúc đóng băng.

| Đại lượng | Giới hạn | D1 baseline | D2 resume | D3 fallback | Kết quả |
|---|---|---|---|---|---|
| steps / epochs / eval_items | exact | 2370 / 30 / 1000 | bằng | bằng | pass |
| epoch_sample_order_digests (30), input/spec checksum, architecture_id | exact | — | bằng | bằng | pass |
| train_loss | abs 0,001 | 0,12516860507624702 | chênh 0,0 | chênh 0,0 | pass |
| eval_loss | abs 0,001 | 6,254570941787402 | chênh 0,0 | chênh 0,0 | pass |
| eval_accuracy | abs 0,005 | 0,427 | chênh 0,0 | chênh 0,0 | pass |
| model_l2_norm | rel 1e-4 | 53,93611788857205 | rel 0,0 | rel 0,0 | pass |
| optimizer_momentum_l2_norm | rel 1e-4 | 8,235480399291982 | rel 0,0 | rel 0,0 | pass |
| parameter_l2_norms.* (8 tensor, cùng tên) | rel 1e-4 | — | rel 0,0 | rel 0,0 | pass |
| model.safetensors / metrics.json bitwise | thông tin | `f4363da9…cc8e` / `dc66870b…3351` | bằng | bằng | bằng từng byte |

Kết quả trên chỉ áp dụng cho cùng host, cùng image và cùng số thread (1). Khác host cùng kiến
trúc thì chưa đo. arm64 thì chưa build.

### Chunk dedup report

| | D4 baseline | D5 crash/resume |
|---|---|---|
| RecognizedChunk / chunk ID khác nhau | 40 / 40 | 40 / 40 |
| Chunk theo attempt nguồn | attempt 1: 40 | attempt 1: 16, attempt 2: 24 |
| ID đúng `chunk-%08d`, coverage `[0, 2000)` đúng, chunk 50 item | có | có |
| Xung đột / chunk lệch checksum so với baseline | 0 / 0 | 0 / 0 |
| Checksum từng chunk bằng baseline | — | có (40/40) |

### Sweep report (D7)

- Request: `tests/fixtures/workloads/hyperparameter-sweep-v1/request.json`, gồm `learning_rate`
  `[0.1, 0.01, 0.01, 0.001]` × `seed` `[1, 2]`. Sau dedup RFC 8785 còn 6 child.
- Tenant quota: outstanding 2.
- `POST /v1/sweeps` trả 207 với `child_count` 6, `accepted_count` 2, `rejected_count` 4.

| child_index | Outcome | Job | Kết quả job |
|---|---|---|---|
| 0 | ACCEPTED | `…d1d5` | SUCCEEDED, 1 attempt fence 1, 32 step, 1 result, allocation RELEASED `VERIFIED_CLEANUP` |
| 1 | ACCEPTED | `…1a7d` | SUCCEEDED, 1 attempt fence 1, 32 step, 1 result, allocation RELEASED `VERIFIED_CLEANUP` |
| 2–5 | REJECTED `quota_exceeded` ("Tenant outstanding quota exceeded") | — | không tạo job |

- Counter trước → sau admission (scope, outstanding, version):
  - GLOBAL `(0, 19)` → `(2, 22)`;
  - TENANT `(0, 17)` → `(2, 20)`;
  - USER `(0, 17)` → `(2, 20)`.
- Rate bucket version: TENANT 4 → 6, USER 4 → 6.
- Replay cùng key:
  - response giống hệt (`replay_identical` true);
  - counter và rate bucket không đổi;
  - không tạo job mới.
- Parent không có allocation, slot hay counter riêng. `GET /v1/sweeps/{id}` trả cùng dữ liệu.
- Sau khi cả hai child release: counter về `(0, 0)` (`_settled`).
- Latency 100 child (P, report-only, `test_one_hundred_child_sweep_latency`): xem §P.

### RAM đỉnh (VPS1)

- **Qua worker, có runner và checkpoint** (sampler đọc `memory.peak` của cgroup scope mỗi
  0,25 s; `memory_bytes` 1 GiB, 1 CPU):
  - training fixture: D1 383,8 MiB; D2 323,7 / 356,7 MiB; D3 319,6 / 356,6 MiB; D7 child
    352,4 / 352,0 MiB;
  - inference fixture: D4 280,3 MiB; D5 280,2 / 280,1 MiB; D6 280,1 / 280,0 MiB.
- **Cấu hình lớn, chỉ để sizing** (workload chạy trực tiếp không có runner, 1 thread;
  `docs/evidence/raw/B16-ram.json`; script `$HOME/nexa-b16/tools/ram.sh`):
  - training batch 64 / subset 5000 / 1 epoch: 315,8 MiB;
  - training batch 512 / subset 5000 / 3 epoch: 408,7 MiB;
  - inference chunk 50 / batch 50: 242,4 MiB;
  - inference chunk 2000 / batch 2000: 506,3 MiB.
- **not-run:**
  - `subset_size` 50000, vì dataset fixture chỉ có 5000 mẫu training;
  - inference `batch_size` 4096, vì N = 2000 chặn batch thực tế ở 2000.
- Khuyến nghị theo cấu hình nằm trong [submit](../submit.md).
- Mức áp lực bộ nhớ Mac lúc chạy Docker trên Mac: xem §P.

### Hardening (D8, docker inspect + /proc)

Container training (D1, và attempt restore của D2) và container inference (D4) có cùng cấu hình:
- `User` `1000:1000`;
- `NetworkMode` `none`;
- `ReadonlyRootfs` true, `Privileged` false;
- `CapDrop` `["ALL"]`, `CapAdd` rỗng;
- `SecurityOpt` `["no-new-privileges:true"]` (seccomp mặc định, không có `unconfined`);
- `PidsLimit` 128;
- `Memory` = `MemorySwap` = 1073741824;
- `NanoCpus` 1e9;
- restart policy `no`;
- log json-file `max-size` 1048576 / `max-file` 1;
- tmpfs `/output`, `/run/nexa`, `/tmp`.

Mount và tiến trình:
- Bind mount đều read-only:
  - `/input/dataset.arrow` và `/run/nexa-input`;
  - thêm `/input/model.safetensors` ở inference;
  - thêm 4 file `/input/restore/{model,optimizer,rng}.safetensors` và `training-state.json` ở
    attempt restore D2.
- Không có Docker socket.
- Tiến trình trong cgroup: runner UID 1000, workload UID 1001 và control relay UID 1000. Cả ba
  có Seccomp 2, NoNewPrivs 1, CapEff = CapBnd = 0 (B16-R22).

### P: regression trên Mac

- **Pytest mặc định** (`uv run --no-sync pytest -q`, `PYTHONPATH=src:.`): **1522 passed, 519
  skipped, 2 warnings**, 38,79 s. Lượt lặp lại có `-rs` cho cùng số đếm (41,26 s). Lý do skip:
  - 506 test cần `--run-postgres`;
  - 11 test Docker B09 opt-in;
  - 1 module không import được `torch` (`test_pytorch_workloads_b16.py`, host Mac không có
    torch; test này đã chạy trong image trên VPS1);
  - 1 test relay B11 opt-in.

  Skip không được tính là pass.
- **Pytest PostgreSQL** (`--run-postgres`, DB `nexa_b05_test_b16`): **2020 passed, 21 skipped,
  2 warnings**, 544,86 s. Lượt lặp lại có `-rs` cho cùng số đếm (553,11 s). Lý do skip:
  - 20 test Docker opt-in, gồm `test_real_runner` 9, `test_real_executor` 2, B16 2, B15 2,
    `test_b11_vertical` 2, B14 1, `test_b11_worker_ipc` 1, B10 1. Các file này được chạy riêng
    ở bảng Docker bên dưới;
  - 1 module không import được `torch` (`test_pytorch_workloads_b16.py`).
- **Ruff**: `ruff check .` báo "All checks passed!", `ruff format --check .` báo "453 files already
  formatted".
- **UI** (không sửa trong B16): `pnpm --dir web install --frozen-lockfile`,
  `pnpm --dir web run typecheck` (`tsc -b`) và `pnpm --dir web run build` đều thoát mã 0. `web/dist`
  bị ignore. Đây chỉ là lệnh bootstrap, không phải test sản phẩm UI.
- **Latency sweep 100 child** (report-only, `test_one_hundred_child_sweep_latency`, 5 lượt đo,
  percentile nearest rank): p50 3,410 s, p95 3,867 s, min 2,780 s. Test chỉ assert tính đúng: 207,
  100 ACCEPTED mỗi lượt, 500 job.
  - Đây là số của lần gọi thứ hai. Lần gọi đầu có pass nhưng output bị lọc mất dòng số.
  - Đo trên Docker Desktop VM của Mac với PostgreSQL chung, không phải benchmark.
- **Regression Docker B09–B15 trên Mac** (Docker Desktop linux/arm64).
  - Image: CPU `nexa/cpu-iterative@sha256:519a8697…7d45`, worker `nexa/b16-worker:local`
    (`sha256:5eb661af…4835`).
  - Env: `NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 NEXA_B10_RUNTIME_EVIDENCE=1`,
    `NEXA_B09_IMAGE_REF`, `NEXA_B11_WORKER_IMAGE`, DB `nexa_b05_test_b16docker`.
  - Mỗi lần một file, không xdist. Trước mỗi file đều ghi mức áp lực bộ nhớ và PID của caddy.

  | File | Kết quả | Thời gian | Áp lực trước khi chạy (mức, % free, PID caddy) |
  |---|---|---|---|
  | `test_real_runner.py` | 9 passed | 124,92 s | 1, 44 %, 24.502 |
  | `test_real_executor.py` | 2 passed | 1,78 s | 2, 32 %, 24.526 |
  | `test_b10_worker_restart.py` | 1 skipped ("worker and runner must share a Linux monotonic clock domain") | 2,05 s | 2, 32 %, 24.527 |
  | `test_b11_worker_ipc.py` | 1 passed | 8,40 s | 2, 32 %, 24.528 |
  | `test_b11_vertical.py` | 2 passed | 94,33 s | 2, 33 %, 24.530 |
  | `test_b14_checkpoint_restore.py` | 1 passed (S1–S6) | 488,97 s | 2, 34 %, 24.549 |
  | `test_b15_control_recovery.py` | 2 passed (C0–C10) | 900,68 s | 2, 32 %, 24.646 |
  | `test_b16_workloads.py` | 2 skipped (thiếu `NEXA_B16_*`, đúng thiết kế trên Mac) | 2,30 s | 2, 33 %, 24.821 |

  - Tổng: **17 passed, 3 skipped, 0 failed**. Các test bị skip không được tính là pass.
  - `test_b14_checkpoint_restore.py` được chạy lại một lần có `NEXA_B14_EVIDENCE_OUT` để lấy
    checksum: 1 passed, 496,75 s. Cả 7 kết quả (R0 và 6 scenario crash/corrupt/fallback/kill/
    adoption) đều là `sha256:f55bf404707d98b13ec910fa63ccac0d229f57d1c3d49233523fed5baf4d842e`,
    tức là bằng R0 của chính lượt đó. Giá trị khác R0 của B14 (`7de722a0…`) và B15
    (`8065fa80…`) là đúng thiết kế, xem B16-R23.
  - Swap trong suốt phiên khoảng 6,5/7 GiB. Không lượt nào chạm mức 4. Không dừng hay xóa
    container `nexa_b10_*`.
  - Sau khi chạy, không còn container hay volume nào do B16 tạo trên Mac.

### Acceptance matrix

Status chỉ nói về phần B16. Mọi gate trong `docs/acceptance.md` vẫn giữ `specified` cho đến Task
Review, và tài liệu đó không bị sửa.
- Env P = Mac + PostgreSQL 17. "P/Docker" = Docker Desktop linux/arm64 trên Mac.
- Env L = VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04), linux/amd64.
- "PG" là lượt `--run-postgres` đầy đủ ở §P.

| Gate | Tiêu chí B16 | Test | Evidence | Env | Applicability | Status | Giới hạn |
|---|---|---|---|---|---|---|---|
| ACC-18 | Checkpoint training 4 file: publish kiểm từng file và canonical manifest; restore kiểm mọi blob; newest corrupt → fallback | `test_pytorch_checkpoint_b16.py` (6 test), `test_checkpoint_validation_b16.py` (17), `test_training_state_b16.py`, `test_training_flow_b16.py`, `test_workloads_and_scripts_never_unpickle_or_execute_code` | PG | P | B16 | pass | Server không đọc byte tensor (`test_server_never_interprets_tensor_bytes`) |
| ACC-18 | Inference carry-forward chỉ dùng recognized chunk của đúng job, với source attempt/fence chính xác; chunk được tham chiếu mà corrupt hoặc mất → fallback; checkpoint kế thừa → input | `test_inference_chunks_b16.py` (8), `test_inference_validation_b16.py` (16), `test_b16_inference_extent_is_fenced_bounded_and_immutable`, `test_inference_flow_b16.py` | PG | P | B16 | pass | B16-R20 (restore theo job), B16-R21 mở |
| ACC-18 | Timeline crash/corrupt thật cho training | D2, D3 | raw/B16-training.json | L | B16 | pass | Một host; D9 not-run |
| ACC-18 | Timeline crash/missing-blob thật cho inference | D5, D6 | raw/B16-inference.json | L | B16 | pass | D6 fail closed, nhưng phân loại lỗi attempt khác nhau giữa các lần chạy (B16-R21) |
| ACC-19 | PyTorch resume/fallback nằm trong tolerance định trước; model/optimizer/RNG/step/cursor được khôi phục | D1–D3 (vòng 1 và vòng 2), torch unit test trong image vòng 2 (153 passed; số 111 của vòng 1 chạy image test cũ, đã đính chính) | raw/B16-training.json, raw/B16-r2-training.json, §Tolerance, §Vòng 2 | L | B16 | pass | Cùng host, cùng image, 1 thread; khác host chưa đo; arm64 chưa build; không CUDA |
| ACC-19 | Sweep ≤ 100 child; mỗi child tự admission/dedup/quota; parent không có slot; replay 207 giống hệt | `test_sweep_b16.py` (11), `test_sweep_expansion_b16.py` (8), `test_sweep_commands_b16.py` (5) | PG, fixture `expansion.json` | P | B16 | pass | Latency chỉ report-only |
| ACC-19 | Sweep thật: partial accept/reject, replay, child chạy xong | D7 | raw/B16-training.json | L | B16 | pass | Quota 2, 6 child |
| ACC-19 | Chunk inference không có output trùng | `test_inference_chunks_b16.py`, `test_chunk_manifest_b16.py`, D4/D5 | §Chunk dedup report | P + L | B16 | pass | — |
| ACC-19 | CPU crash-resume khớp chính xác (regression B14 trên image CPU B16) | `test_b14_checkpoint_restore.py` | §P Docker | P/Docker | B16 regression | pass | Docker Desktop VM |
| ACC-03 | Sweep child, input, template, chunk và restore không vượt tenant/job | `test_a_foreign_input_rejects_every_child_without_a_job`, `test_get_sweep_pages_by_child_index_within_the_tenant`, `test_template_reads_are_tenant_scoped_and_contract_shaped`, `test_inherited_inference_checkpoint_falls_back_to_input` | PG | P | B16 | pass | UI (W) thuộc B17/B18 |
| ACC-06 | 429/quota từng child; 422 cho lỗi request-level và GPU/capability; operational mode chặn sweep mới | `test_quota_and_rate_limits_reject_individual_children`, `test_request_level_violations_persist_nothing`, `test_gpu_request_is_rejected_by_every_cpu_template`, `test_operational_modes_gate_new_sweeps_and_resumed_children` | PG, D7 | P + L | B16 | pass | — |
| ACC-07 | Replay sweep trả cùng response và không tiêu counter/rate; replay đồng thời; crash giữa các child không tạo trùng | `test_sweep_admits_each_child_and_replays_the_same_mapping`, `test_concurrent_replays_admit_each_child_once`, `test_a_crash_between_children_resumes_without_duplicates`, D7 | PG, raw/B16-training.json | P + L | B16 | pass | Retention sweep là diễn giải B16-R05 |
| ACC-13 | Runner tách theo adapter vẫn giữ deadline/lease; runtime limit có hiệu lực | Regression `test_real_runner.py`, `test_b15_control_recovery.py` (vòng 1 và vòng 2 trên P/Docker); D6 (runtime 90 s). `test_b10_worker_restart.py` bị skip trên Mac (cần clock domain Linux dùng chung), trên VPS1 not-run | §P Docker, §Vòng 2 P, raw/B16-inference.json | P/Docker + L | B16 regression | pass | Không đo lại compute overlap (B15); `test_real_runner.py` trên L fail do oracle B09 (B16-R29), không tính là evidence L |
| ACC-14 | Mỗi job đúng 1 final result; publish chunk/checkpoint theo fence; extent bất biến | `test_complete_binds_model_and_metrics_once`, `test_complete_requires_full_coverage_and_binds_the_chunk_graph`, `test_b16_inference_extent_is_fenced_bounded_and_immutable`, D1–D7 (`reconcile`) | PG, raw | P + L | B16 | pass | — |
| ACC-17 | Media type/kind theo adapter; upload chunk theo cửa sổ W=8 | `test_upload_media_follows_job_adapter`, `test_chunk_uploads_follow_the_chunked_windows` | PG | P | B16 | pass | Fault injection fsync/rename không chạy lại (B07) |
| ACC-21 | Image hoặc adapter chưa verify thì không được advertise và không dispatch | `test_label_mismatch_keeps_image_unverified_and_adapter_unadvertised`, `test_unregistered_or_mismatched_adapter_is_never_compatible`, regression `test_b11_vertical.py` | U, P, §P Docker | P | B16 | pass | Không có partition test mới |
| ACC-22 | Kill container → retry trong budget và restore; timeout không retry mù; failure typed, fence/quarantine trước release | D2, D3, D5 (INFRASTRUCTURE → RETRY_READY), D6 (FAILED, không retry), `test_workload_exit_codes_map_to_failure_classes` | raw, U | P + L | B16 | pass | ACC-22 đầy đủ (log flood…) thuộc B15 |
| ACC-22 | Vòng 2: OOM và INVALID_INPUT của workload AI có kiểu, không retry; quarantine rồi release sau cleanup | `test_runner_oom_b16.py` (27), `test_runner_failure_b16.py` worker (5) và integration (3), R24/R25 torch test, `test_b16_workload_oom_is_typed_and_not_retried` | raw/B16-oom.json, PG, §Vòng 2 | P + L | B16 | pass | OOM thật chỉ ở 256 MiB trên VPS1; INVALID_INPUT thật trên L chỉ qua torch test trong container, không qua worker |
| ACC-23 | Giới hạn file/tensor/chunk/manifest khi stream | `test_oversized_tensor_file_is_rejected`, `test_corrupt_or_oversized_snapshot_is_rejected`, `test_chunk_manifest_b16.py` | PG, U | P | B16 | pass | GC/disk-full thuộc B19 |
| ACC-25 | Container PyTorch/inference hardened đúng như lúc thiết kế | D8 (inspect + /proc cho mọi PID), vòng 1 và vòng 2 | §Hardening, raw/B16-r2-*.json | L | B16 | pass | Vòng 2 thêm OOM thật (memory bound → OOM typed); không có abuse test PID/scratch mới; EC2 VM |
| ACC-26 | Không log byte chunk/tensor/input; không thêm metric label | Không có test riêng | — | — | B16 (một phần) | specified | Giữ cho B19 |
| ACC-27 | CLI `sweep submit/show`, `template list/show`; route contract | `test_sweep_commands_b16.py`, `test_template_commands.py`, `test_api_route_contract.py` | U, PG | P | B16 (CLI/API) | pass | UI thuộc B17/B18 |
| ACC-28 | Migration 0020 additive; downgrade có kiểm; ràng buộc và index khớp metadata | `test_migration_b16.py` (7) | PG | P | B16 | pass | Upgrade/restore khi bảo trì thuộc B21 |
| ACC-39 | Ruff, pytest, PostgreSQL, UI typecheck/build, build image amd64, lockfile không có thư viện ML | §P, §Vòng 2 P, `test_ml_libraries_stay_out_of_the_control_plane_lock` | §P, §Revision, §Vòng 2 | P + L | B16 | pass | Không scan image và không push; CI không chạy (không commit) |
| ACC-34 | GPU | — | — | — | không áp dụng cho B16 (B23) | specified | Không claim CUDA |
| ACC-36 | Demo | — | — | — | không áp dụng cho B16 (B24) | specified | — |
| ACC-38 | Project DoD | — | — | — | không áp dụng cho B16 (B24/B25) | specified | — |

### Giới hạn

- **Not-run:**
  - `subset_size` 50000, vì fixture chỉ có 5000 mẫu;
  - inference `batch_size` 4096, vì N = 2000;
  - D9 (worker restart giữa chu kỳ checkpoint PyTorch);
  - P-smoke PyTorch trên Mac, vì áp lực bộ nhớ ở mức 2 suốt phiên;
  - JournalCorruption/virtiofs.
- **Chưa build hay test image PyTorch arm64**, nên không có claim nào về PyTorch arm64. Mac chỉ
  chạy regression CPU/worker arm64.
- **L là EC2 VM** trên một host, không phải bare metal, GPU hay release.
- **Tolerance** chỉ được chứng minh cho cùng host/image/thread. Việc D2/D3 giống từng byte chỉ là
  thông tin, không phải yêu cầu.
- **Latency sweep** là số report-only trên Docker Desktop, không phải benchmark (B22).
- **B16-R21 vẫn mở.** Sau fallback, attempt mới tính lại chunk đã được nhận diện và bị 409. Job
  fail closed, nhưng phân loại lỗi của attempt khác nhau giữa các lần chạy (vòng 2: INTERNAL).
- **B16-R29 (vòng 2).** `test_real_runner.py` fail trên L vì oracle UID của test B09 dựa vào tên
  user host; regression runner chỉ được chứng minh trên P/Docker arm64.
- **Vòng 2 UI** không chạy lại (không sửa `web/`); lệnh UI ở §P là của vòng 1.
- **Để lại cho task sau:**
  - B17: UI user cho sweep và template;
  - B19: metrics, log audit, GC chunk/checkpoint;
  - B21: bảo trì migration 0020 (upgrade/restore);
  - B23: CUDA/GPU;
  - B22/B24: tải lớn và demo.

### Findings

- **B16-R23 (erratum fixture, ghi nhận).**
  - `tests/fixtures/workloads/cpu-iterative-v1/fixture.json` ghi `expected_result_checksum`
    `sha256:7de722a0…6f35` (R0 của B14), kèm `source` "every crash/resume run must return these
    exact result bytes".
  - Câu đó chỉ đúng trong phạm vi một lượt chạy. Byte kết quả CPU chứa `spec_checksum`
    (`workloads/cpu_iterative.py`). Spec của harness lại chứa `input_artifact_id`, là UUIDv7 mới
    ở mỗi lượt. Vì vậy mỗi lượt có R0 riêng: B14 `7de722a0…`, B15 `8065fa80…`, lượt regression
    B16 `f55bf404…`.
  - Oracle đúng, và cũng là oracle mà test B14/B15 assert: mọi scenario crash/resume bằng R0 của
    cùng lượt.
  - Fixture không bị sửa, để giữ SHA-256 ở bảng đóng băng. Muốn sửa câu `source` thì cần lượt
    đóng băng mới.

| ID | Loại | Trạng thái |
|---|---|---|
| B16-R01 | Diễn giải: 4 template = 3 Template row + API sweep | ghi nhận |
| B16-R02 | Diễn giải: metadata dataset trong schema metadata Arrow | ghi nhận |
| B16-R03 | Diễn giải: 2 image PyTorch dùng chung 1 Dockerfile | ghi nhận |
| B16-R04 | Diễn giải: phân loại lỗi sweep (request-level 422, lỗi từng child) | ghi nhận |
| ↳ B16-R04 | Remediation B01–B16: xem mục `B16-R04` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |
| B16-R05 | Diễn giải: retention của `submitSweep` | ghi nhận, cần xác nhận ở review |
| ↳ B16-R05 | Remediation B01–B16: xem mục `B16-R05` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |
| B16-R06 | Diễn giải: nguồn N và pinning `inference_extents` | ghi nhận |
| B16-R07 | Diễn giải: server kiểm lại chunk lúc chọn restore | ghi nhận |
| B16-R08 | 422 cho path/query sai định dạng (handler toàn app) | ghi nhận, không sửa |
| ↳ B16-R08 | Remediation B01–B16: xem mục `B16-R08` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |
| B16-R09 | `template show` là alias của `get` | ghi nhận |
| B16-R10 | Dedup RFC 8785 khác `uniqueItems` | ghi nhận |
| ↳ B16-R10 | Remediation B01–B16: xem mục `B16-R10` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). Fixture `hyperparameter-sweep-v1/request.json` đổi sha256 (`2aa967a8…` → `6e196e7d…`), khác bảng đóng băng ở trên. | xem mục remediation (chờ Task Review) |
| B16-R11 | Regression M1: adapter binding B11 cho template ngoài họ adapter | đã sửa, có test |
| B16-R12 | — | không dùng |
| B16-R13 | Flow control cửa sổ chunk | ghi nhận |
| B16-R14 | Prefetch result manifest trong `complete_attempt` | thay đổi hành vi, ghi nhận |
| B16-R15 | `decode_json_object` giải mã số | đã sửa, có test |
| B16-R16 | `CheckpointFlow._expected_manifest` | đã sửa, có test |
| B16-R17 | Giới hạn số lượng/kích thước chunk | ghi nhận |
| B16-R18 | File restore của inference | ghi nhận |
| B16-R19 | Media của upload chunk | đã sửa, có test |
| B16-R20 | Restore inference theo job | ghi nhận |
| B16-R21 | Chunk conflict sau khi tính lại | **mở**, cần quyết định user/contract |
| ↳ B16-R21 | Remediation B01–B16: xem mục `B16-R21` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |
| B16-R22 | Oracle hardening và runc exec init | diễn giải, oracle đã sửa |
| B16-R23 | Erratum cho `source` của fixture `cpu-iterative-v1` | ghi nhận, fixture không sửa sau đóng băng |
| B16-R24 | Chunk plan vượt `MAX_CHUNKS`/`MAX_CHUNK_FILE_BYTES` → exit 65 INVALID_INPUT trước chunk đầu | đã sửa (vòng 2), có test |
| B16-R25 | Lỗi kiểu/parse metadata dataset và header safetensors → `WorkloadInputError`, exit 65 | đã sửa (vòng 2), có test |
| B16-R26 | OOM cgroup → FAILED OOM/CONTAINER_OOM, không retry | đã sửa (vòng 2), có test + chạy thật trên L |
| B16-R27 | Tài liệu idempotency key của `sweep submit` | đã sửa (vòng 2) |
| B16-R28 | Mode của request sweep đọc `FOR SHARE` | đã sửa (vòng 2) |
| B16-R29 | Oracle UID `_wait_for_isolated_processes` (test B09) dùng tên user host | **mở**, không sửa (ngoài phạm vi) |
| ↳ B16-R29 | Remediation B01–B16: xem mục `B16-R29` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |
| B16-DOC-01 (audit B1–B16, không có hàng gốc) | Remediation B01–B16: xem mục `B16-DOC-01` trong [B01-B16-findings-remediation.md](B01-B16-findings-remediation.md). | xem mục remediation (chờ Task Review) |

- **B16-R29 (vòng 2, mở).** `tests/docker/test_real_runner.py::_wait_for_isolated_processes`
  (test B09, không đổi trong B16) đọc `docker top -eo pid,user,args` và chờ chuỗi `nexa-runner`
  hoặc `1000` và `nexa-workload` hoặc `1001`. `docker top` đổi UID sang tên theo `/etc/passwd` của
  host. Trên VPS1 UID 1000 là `ubuntu` và 1001 là `nexa`, nên output chỉ có `ubuntu`/`nexa` dù
  tiến trình đúng UID; test hết 30 s rồi fail. Trên Docker Desktop không có entry đó nên hiện
  UID số và test pass. Đã điều tra 2 lượt (run 11, 12, cùng nguyên nhân) rồi dừng. Hướng đóng
  (cần user quyết định vì là test đã duyệt của B09): đọc `pid,uid,args`, hoặc chạy trên host không
  có user 1000/1001.

Finding cũ không sửa trong B16, trạng thái giữ nguyên:
- B13-R12 (Decimal search_path);
- B14-R04;
- B15-R11, B15-R32, B15-R33, B15-R39 (race callback worker);
- `_result_once` JournalCorruption.

### Tự review

- **Phạm vi đã đọc:**
  - `git diff` toàn bộ file tracked;
  - từng file untracked, đọc riêng vì `git diff` không bao gồm chúng;
  - `git diff --check` sạch;
  - `.env` bị ignore;
  - không có IP, secret, tên key hay đường dẫn `/Users` trong diff và file untracked. Chỉ có
    comment subnet đã có từ trước trong `.env.example` và đoạn trích RFC.
- **Đã sửa trong lượt tự review:**
  - chú giải fence: fence 2 đến từ `ATTEMPT_FAILED`, không phải revoke/retry;
  - phạm vi check: `reconcile` áp dụng cho D1–D5 và D7, `_settled` chỉ áp dụng cho D6;
  - ledger chỉ được ghi nhận, không assert tick ≤ 1 s;
  - tên test ở §L;
  - tên lệnh CLI là `sweep submit/show`;
  - lý do skip ở §P được thay bằng số đếm chính xác từ `-rs`.
- **Đã kiểm, không sửa:**
  - stderr của hai workload chỉ in chuỗi lỗi cố định, không in byte input, tensor hay chunk;
  - fixture `cpu-iterative-v1` giữ nguyên SHA-256 đóng băng; câu `source` sai được ghi thành
    B16-R23.
- **Còn mở:** B16-R21, B16-R05 (cần xác nhận ở review), ACC-26 (specified, để cho B19).
- **Vòng 2:** đọc lại `git diff` các file tracked và từng file untracked đổi trong vòng 2; sửa
  trong lượt này: danh sách class được worker chuyển tiếp trong `docs/worker-agent.md` (bản đầu
  sai, sửa theo `worker/execution.py`); phát hiện số torch test vòng 1 chạy image cũ và đính chính.
  Còn mở thêm: B16-R29.

### Reproduction

- **Fixture**:
  - `scripts/b16_prepare_fixtures.py` dựng từ archive CIFAR-10 offline; checksum ghi trong
    `tests/fixtures/workloads/*/fixture.json`;
  - dữ liệu nằm ngoài repo (`NEXA_B16_DATA_DIR`).
- **Image**: `scripts/b16_build_image.sh`, dùng `deploy/pytorch-cpu/`. Sau khi build, so ID với
  bảng image.
- **L** (script VPS1 `$HOME/nexa-b16/tools/`, nằm ngoài repo):
  - `run_docker.sh` export đúng các biến ở §L, đọc password PostgreSQL từ file 0600 và ghi
    `pytest.out` cùng raw vào `evidence/run-<UTC>/`;
  - `torch_tests.sh` build `deploy/pytorch-cpu/test.Dockerfile`, rồi chạy `tests/workloads -rs`
    trong container (`--network none --memory 4g --cpus 2 --read-only`, source và data mount
    read-only); từ vòng 2 script truyền `--build-arg PRODUCT_IMAGE_REF=<tag>` sau khi kiểm ID image
    sản phẩm, dừng nếu build lỗi, và ghi ID image test cùng SHA-256 file test;
  - `ram.sh` chạy trực tiếp module workload trong container hardened (`--memory 6g --cpus 1
    --user 1001:1000`, `--threads 1`) và poll `memory.peak` mỗi 0,2 s. Kết quả đã chép vào
    `raw/B16-ram.json`.
- **Biến env B16**:
  - `NEXA_B16_PYTORCH_IMAGE_REF`, `NEXA_B16_INFERENCE_IMAGE_REF`: ref có digest;
  - `NEXA_B16_DATA_DIR`;
  - `NEXA_B16_EVIDENCE_OUT`: tùy chọn, nơi ghi raw JSON.
  - Nếu thiếu các biến này hoặc không bật `NEXA_RUN_DOCKER=1`, `test_b16_workloads.py` sẽ skip.
    Skip không được tính là pass.
- **P**: các lệnh ở §P. DB test luôn có prefix `nexa_b05_test_` và được truyền qua
  `NEXA_TEST_DATABASE_URL`.
