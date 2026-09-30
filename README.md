# nexa / PBL4

Nền tảng single-node, self-hosted và hardware-portable chạy batch AI cho nhiều tenant trên một Linux server, phân phối CPU/RAM/GPU công bằng và tiếp tục job từ application checkpoint.

**Trạng thái: B01–B16 và đợt B1–B16 Findings Remediation đã được Task Review duyệt.** B04 có policy thuần weighted dominant resource-time, aging, reservation và evidence simulator lớp D. B05 cung cấp PostgreSQL schema/migration/constraint và transaction helpers. B06 bổ sung API thật cho identity, browser session, CLI token, SYSTEM_ADMIN, tenant/membership, versioned policy, bootstrap worker và audit. B07 bổ sung filesystem artifact store, bounded upload, durable commit order, tenant counter/reservation và artifact REST API. B08 bổ sung submit atomic, idempotency replay/conflict, durable queue, quota/rate backpressure và REST job/session/event query. B09 bổ sung discovery/capability, Docker executor, journal/cleanup proof, trusted runner và image CPU deterministic; evidence gồm unit/protocol/fault tests và mười một scenario executor/runner trên Docker Desktop Linux VM. B10 bổ sung worker local, bootstrap/singleton, heartbeat, reconciliation/adoption, lease renewal, replay bền vững và Compose bootstrap; đã có evidence API/PostgreSQL và worker kill/restart với runner thật trên Docker Desktop Linux VM. B11 bổ sung coordinator leadership, policy-based allocation/dispatch, worker claim/start, CPU result upload và fenced result/release; luồng CPU hai tenant, mất response sau commit và restart trước cleanup đã được kiểm thử trên Docker Desktop Linux VM. B12 cung cấp product CLI REST-only cho config, token, artifact, core job/session/event/result và admin identity/policy/audit; command surface chỉ đăng ký route đã wire và giới hạn backend hiện tại được ghi tại [CLI guide](docs/cli.md) và [B12 evidence](docs/evidence/B12-cli.md). B13 đưa weighted fairness, hard quota, aging, reservation và ledger vào coordinator thật; evidence gồm queue 100.000 Job trên PostgreSQL 17. B14 bổ sung CPU checkpoint/restore: reserve/publish checkpoint có fence, chọn checkpoint lúc claim với đánh dấu `CORRUPT` và fallback, retry có restore sau khi container crash, cùng API/CLI xem danh sách checkpoint; sáu scenario Docker thật cho kết quả giống từng byte với run không bị ngắt. B15 bổ sung cancel, pause/resume, manual retry, lease reaper và admin drain/disable/enable worker; các scenario Docker thật C0–C10 cho kết quả sau pause/resume, reaper, network loss và restart toàn stack giống từng byte với run không bị ngắt ([B15 evidence](docs/evidence/B15-control-recovery.md)). B16 bổ sung template PyTorch CPU training (`pytorch-cifar10-cnn`) có checkpoint model/optimizer/RNG/sampler, batch inference theo chunk (`batch-inference`) có carry-forward không tạo output trùng và hyperparameter sweep tối đa 100 child; scenario Docker thật chạy trên VPS1 (Linux amd64, môi trường L, là EC2 VM) ([B16 evidence](docs/evidence/B16-pytorch-sweep-inference.md)). Triển khai bare Linux, portability hai máy, Web UI nghiệp vụ, GPU và release acceptance vẫn thuộc các chặng sau; các thông số hiệu năng ngoài evidence đã ghi vẫn là mục tiêu nghiệm thu, chưa phải kết quả.

Đợt **B1–B16 Findings Remediation** xử lý 31 finding còn mở mà audit độc lập B01–B16 đã xác nhận. Đợt này đã được Task Review duyệt theo xác nhận của user ngày 30/09/2026. Kết quả: 23 finding `CLOSED`, 5 `ENVIRONMENT BLOCKED` (chưa chạy lại được trên Docker Desktop của Mac), 3 `NEEDS OWNER DECISION` và 0 `BLOCKED`. Chi tiết ở mục B1–B16 Findings Remediation bên dưới và trong [evidence remediation](docs/evidence/B01-B16-findings-remediation.md).

[PLAN.md](PLAN.md) bản duyệt ngày 16/09/2026 là nguồn sự thật về phạm vi, kiến trúc, thuật toán, backlog và nghiệm thu; PLAN được ưu tiên khi tài liệu dẫn xuất này mâu thuẫn. Yêu cầu trực tiếp mới nhất của user có ưu tiên cao nhất; không tự sửa PLAN để hợp thức hóa thay đổi thiết kế.

## Phạm vi đã khóa

- Một deployment quản lý một Linux server; API/CLI/Web UI user và admin đều bắt buộc. Một bản release sản phẩm `v1.0.0`.
- Weighted dominant resource-time scheduling + hard quota + aging + một reservation local. FIFO/RR/WRR/DRR/DRF chỉ phục vụ so sánh trong simulator.
- Bốn workload được quản lý: CPU deterministic, PyTorch training nhỏ, sweep hữu hạn, inference theo chunk. Admin khóa template/image digest/adapter; không chạy code, shell, image hoặc host mount tùy ý.
- Recovery theo application checkpoint; compute at-least-once trong retry budget, chỉ một final result/job được công nhận. Chịu process crash, gián đoạn mạng và reboot khi dữ liệu bền vững còn nguyên; backup cần thiết cho mất ổ đĩa/corruption. Không phục hồi heap/socket/terminal/CUDA context tùy ý.
- Cùng release cài độc lập trên hai cấu hình Linux; di chuyển deployment có dừng và chỉ resume khi tương thích. GPU cần gate NVIDIA thật trước khi công bố hỗ trợ.
- Multi-server, shared object storage, cross-node recovery, GPU pools, interactive inference, Kubernetes và ≥1 triệu job là Future Work ngoài backlog hiện tại (PLAN §15).

## Đọc tài liệu

| Tài liệu | Vai trò |
|---|---|
| [PLAN.md](PLAN.md) | Quyết định đã duyệt, requirements chi tiết, 6 giai đoạn, 25 task B01–B25, DoD |
| [AGENTS.md](AGENTS.md) | Quy tắc ngắn cho agent, invariant và điều kiện hoàn tất task |
| [Project structure](docs/project-structure.md) | Cây hiện tại, target placement, kiến trúc, ownership và chiều dependency |
| [Authentication and administration](docs/authentication.md) | Cấu hình, bootstrap, session/CSRF, token scopes, RBAC, policy và handoff B06 |
| [Artifact storage](docs/artifacts.md) | Storage layout, upload headers, checksum, durable commit, quota, replay và download |
| [Submit and durable queue](docs/submit.md) | Submit nguyên tử, admission/quota/rate, idempotency, job/session/event query và accepted-ID reconciliation |
| [Worker executor](docs/worker-executor.md) | B09 discovery, Docker executor, identity/journal, cleanup proof và giới hạn trách nhiệm |
| [Trusted runner](docs/trusted-runner.md) | B09 IPC, deadline/watchdog boundary, CPU adapter và result/checkpoint handoff |
| [B09 evidence](docs/evidence/B09-docker-executor-trusted-runner.md) | Lệnh tái lập, digest/config, raw reports và gate status có giới hạn môi trường |
| [B10 worker agent](docs/worker-agent.md) | Entrypoint, bootstrap, heartbeat, reconciliation/adoption và giới hạn acceptance |
| [B10 evidence](docs/evidence/B10-worker-heartbeat-reconcile.md) | API/PostgreSQL, worker/runner restart evidence và các gate còn mở |
| [Coordinator](docs/coordinator.md) | B11 leadership, allocation/dispatch, CPU result và cách chạy luồng hoàn chỉnh |
| [B11 evidence](docs/evidence/B11-coordinator-dispatch-result.md) | HTTP/PostgreSQL, CPU end-to-end hai tenant, replay, cleanup và giới hạn kiểm chứng |
| [CLI guide](docs/cli.md) | B12 CLI `nexa`, config/profile, token, upload/submit/query/download, admin và giới hạn command hiện tại |
| [B12 evidence](docs/evidence/B12-cli.md) | CLI/API/PostgreSQL, response-loss replay, luồng CLI đến Docker CPU result và giới hạn kiểm chứng |
| [B13 evidence](docs/evidence/B13-production-fairness.md) | Queue 100.000 Job, query plan, ledger cadence, weighted fairness, reservation trace, fault DB và gate matrix |
| [B14 evidence](docs/evidence/B14-cpu-checkpoint-restore.md) | Checkpoint reserve/publish, restore selection/fallback, retry promotion, Docker S1–S6 timeline/checksum, findings và acceptance matrix |
| [Contracts](docs/contracts.md) | Điểm vào contract `1.0.0-b01`: OpenAPI `/v1`, domain/state, internal interfaces, workload/checkpoint và concurrency/recovery |
| [Invariants](docs/invariants.md) | Tenant/resource accounting, concurrency, fencing, checkpoint, recovery, security |
| [Acceptance](docs/acceptance.md) | Gate ID, điều kiện pass, evidence, môi trường; testing/benchmark objectives |
| [ADR](docs/adr.md) | Khi nào ghi quyết định và nội dung tối thiểu; không thay PLAN |
| [Traceability](docs/requirements-traceability.md) | PLAN → contract → INV → ACC → backlog → phép kiểm chứng |
| [Environment inventory](docs/environment-inventory.md) | Công cụ/phần cứng/CI/nhân lực đã quan sát hoặc chưa xác nhận |
| [Agent setup / Skill Creator handoff](docs/agent-setup.md) | Kiểm kê instructions/skills, candidate skills và giới hạn setup |

Stack và module boundaries được tổng hợp tại tài liệu cấu trúc; quy tắc sản phẩm không nằm trong skill. OpenAPI và JSON Schema trong `docs/` là specification, không phải runtime implementation; kết quả review B01 nằm tại [contract review evidence](docs/evidence/B01-contract-review.md).

## Project skills hiện có

- [benchmarking-scheduler-fairness](.agents/skills/benchmarking-scheduler-fairness/SKILL.md): dùng khi chạy hoặc review evidence về fairness, aging, reservation, queue scalability hoặc load benchmark; không dành cho scheduler implementation hoặc generic unit testing.
- [verifying-recovery-fencing](.agents/skills/verifying-recovery-fencing/SKILL.md): dùng khi kiểm thử hoặc tổng hợp evidence về lease expiry, fencing, stale callback, cancel/reaper race, quarantine, checkpoint recovery hoặc reconciliation; không dành cho generic debugging, implementation planning hoặc documentation review đơn thuần.

Skill hợp lệ về cấu trúc/hướng dẫn không chứng minh runtime acceptance. Status và evidence sản phẩm vẫn theo [acceptance](docs/acceptance.md); PLAN tiếp tục là nguồn sự thật chính.

## B02 bootstrap workspace

Prerequisite đã chọn cho bootstrap là Python 3.12, `uv` 0.12.15, Node.js 24 LTS và `pnpm` 11.9.0. Evidence B02 dùng Python 3.12.13, isolated `uv` 0.12.15 và isolated Node 24.21.0; không cài tool global hoặc sửa cấu hình máy. Node 26.4.0 vẫn là system runtime quan sát được, không phải support target.

Python workspace dùng các command reproducible sau:

```sh
uv sync --frozen --all-groups --no-editable
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -q
```

`uv.lock` được tạo bằng `uv` 0.12.15; lock check, frozen non-editable sync, Ruff và pytest đều pass trên Python 3.12.13. `--no-sync` giữ các quality command trên đúng môi trường vừa cài, không để `uv run` tự đổi lại project sang editable install.

UI workspace đã được kiểm tra bằng lockfile hiện có:

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web run typecheck
pnpm --dir web run build
```

Các command UI trên pass bằng selected target Node 24.21.0 LTS với checksum lockfile không đổi. Shell web chỉ hiển thị trạng thái bootstrap; nó không gọi API, không mô phỏng authorization và không triển khai user/admin flow. [CI workflow](.github/workflows/ci.yml) dùng quyền `contents: read` và gọi đúng các command Python/UI này.

### Cấu hình bootstrap

Runtime lấy cấu hình trực tiếp từ process environment. [`.env.example`](.env.example) chỉ là mẫu an toàn cho local/deployment tooling và không được Nexa tự động load. Không có `.env`, credential hoặc path máy phát triển mặc định; bootstrap CI/test dùng fixture an toàn và không cần biến `NEXA_*` thật.

| Biến | Kiểu và trạng thái | Ví dụ an toàn / mặc định |
|---|---|---|
| `NEXA_DATABASE_URL` | PostgreSQL URL, bắt buộc | `postgresql+psycopg://nexa@localhost/nexa`; credential thật phải được secret injection cung cấp |
| `NEXA_ARTIFACT_ROOT` | Absolute path, bắt buộc | `/srv/nexa/artifacts`; không có default |
| `NEXA_ENVIRONMENT` | `development\|test\|production`, tùy chọn | `development` |
| `NEXA_API_JSON_MAX_BYTES` | Integer 65,536–16,777,216, tùy chọn | `1048576` theo contract |
| `NEXA_LOG_LEVEL` | `DEBUG\|INFO\|WARNING\|ERROR\|CRITICAL`, tùy chọn | `INFO` |

Biến `NEXA_*` chưa khai báo làm config validation fail; biến process không thuộc namespace này được bỏ qua. B06 bổ sung public HTTPS origin, secret-file paths, deployment/worker identity, maintenance/trusted proxy CIDR, Argon2, session/token/bootstrap TTL, login-rate, cursor và idempotency bounds. Danh sách đầy đủ cùng hướng dẫn tạo secret nằm tại [authentication and administration](docs/authentication.md). Lỗi chỉ nêu tên biến và rule an toàn, không phản chiếu giá trị. Log/error không được chứa token, password, credential, input hay checkpoint content.

## B03 simulator và baseline

B03 hiện cung cấp virtual clock deterministic, resource/job model bất biến, trace có version/checksum, simulator engine, shared `SchedulerPolicy` snapshot và năm baseline `fifo`, `rr`, `wrr`, `drr`, `drf`. Code nằm hoàn toàn dưới `benchmarks/`; đây là evidence lớp D, không phải scheduler sản phẩm và không chứng minh PostgreSQL/API/Docker/Linux/GPU.

Chạy một baseline:

```sh
uv run --no-sync python -m benchmarks.simulator.cli run \
  --trace benchmarks/fixtures/small-trace.json \
  --seed 7 \
  --baseline fifo \
  --output benchmarks/tmp/fifo-seed-7.json
```

Tái tạo bundle, bảng và biểu đồ B03 đã chọn:

```sh
uv run --no-sync python -m benchmarks.simulator.cli compare \
  --trace benchmarks/fixtures/standard-trace.json \
  --seeds 7,11,19,23,29 \
  --output benchmarks/results/b03-baselines.json

uv run --no-sync python -m benchmarks.simulator.cli report \
  --input benchmarks/results/b03-baselines.json \
  --csv benchmarks/plots/b03-comparison.csv \
  --svg benchmarks/plots/b03-comparison.svg
```

Để kiểm tra byte-equivalent replay, chạy lại hai command vào `benchmarks/tmp/` rồi dùng `cmp` cho JSON/CSV/SVG. Báo cáo cấu hình, checksum, remediation, focused review và giới hạn nằm tại [B03 simulator evidence](docs/evidence/B03-simulator.md). B03 đã được focused Task Review trả lời `Duyệt`; ACC-09/10/11 cùng mọi gate runtime vẫn chưa được tuyên bố pass.

## B04 fairness, aging và reservation

B04 triển khai policy sản phẩm thuần dưới `src/nexa/domain/` và `src/nexa/scheduler/`. Harness riêng dưới `benchmarks/b04/` nối policy này với trace/simulator B03, giữ năm baseline làm mốc so sánh và tạo artifact B04 riêng. Policy không gọi DB/Docker, không đọc đồng hồ hệ thống, không giữ global state và chỉ trả proposal có version; coordinator/runtime vẫn thuộc task sau.

Chạy fixed suite gồm sáu profile, năm seed và sáu policy:

```sh
uv run --no-sync python -m benchmarks.b04.cli compare \
  --suite benchmarks/fixtures/b04-suite.json \
  --output benchmarks/results/b04-fairness.json

uv run --no-sync python -m benchmarks.b04.cli report \
  --input benchmarks/results/b04-fairness.json \
  --csv benchmarks/plots/b04-fairness.csv \
  --svg benchmarks/plots/b04-fairness.svg
```

Để replay, đổi ba output sang `benchmarks/tmp/` rồi dùng `cmp` với artifact đã chọn. Cả 20 run fairness hợp lệ của policy Nexa đạt ngưỡng cố định `J >= 0,95`; profile giới hạn giữ Jain ở `null/N/A`. Năm trace reservation dùng release lệch mốc, ghi trực tiếp các job nhỏ đang fit nhưng bị drain giữ lại, dispatch job lớn ở 190 giây trước năm baseline ở 270 giây, và tiếp tục có arrival sau dispatch. Chi tiết seed, hash, arithmetic, gate lớp D và giới hạn nằm tại [B04 fairness evidence](docs/evidence/B04-fairness.md). `ROADMAP.md` ghi nhận B04 đã được Task Review duyệt ngày 19/09/2026; báo cáo B04 cũ vẫn giữ nguyên bối cảnh remediation trước lần duyệt cuối.

## B05 PostgreSQL schema và migration

B05 triển khai physical schema generation `1` bằng SQLAlchemy 2/Alembic cho 52 bảng, gồm identity, job/session/attempt, worker/inventory/allocation, policy/fairness, event/idempotency, catalog artifact/checkpoint/result và guard nội bộ cho reference artifact. Initial revision `20260919_0001` dùng snapshot `schema_v1` bất biến, serialize migration runner bằng PostgreSQL advisory lock và có schema guard fail-closed. Domain/scheduler không import ORM; worker vẫn không truy cập DB.

Transaction layer cung cấp engine/session lifecycle, row lock theo ID ổn định, optimistic CAS, DB timestamps và retry toàn transaction có giới hạn cho deadlock/serialization conflict. Remediation hiện dùng generated composite FK để tuần tự hóa UploadSession owner/reference, committed-artifact guard + FK cho mọi đường JobSpec/checkpoint/result/chunk/log/reference, Decimal text canonical với cùng miền giải mã Python gồm giới hạn raw/adjusted exponent, và score index chỉ giữ prefix hữu hạn để không làm hẹp significand hợp lệ. PostgreSQL 17 integration tests dùng database riêng có tên bắt đầu bằng `nexa_b05_test_`; fixture từ chối production URL, host không an toàn và sai major version trước khi cleanup.

Chạy đầy đủ Python unit/property/PostgreSQL integration:

```sh
uv sync --frozen --all-groups --no-editable --reinstall-package nexa

NEXA_TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/nexa_b05_test_NAME' \
  uv run --no-sync pytest -q --run-postgres
```

Mapping physical, cách dùng helpers, migration command và nghĩa vụ B06+ nằm tại [database mapping](docs/database.md). Kết quả local và giới hạn gate nằm tại [B05 evidence](docs/evidence/B05-postgresql.md). B05 đã được Task Review duyệt theo xác nhận mới nhất của user ngày 20/09/2026; hosted CI chưa được quan sát.

## B06 identity, token và RBAC

B06 cung cấp FastAPI app factory và 24 operation `/v1` đã duyệt cho admin bootstrap, browser login/session/logout, CLI token của chính caller, tenant/user/membership/SYSTEM_ADMIN, global/tenant policy, worker credential bootstrap và audit pagination. Application service recheck user, grant, membership, scope, expiry/revocation và operational mode trong transaction; browser mutation dùng cookie `Secure` cùng Origin/Host/CSRF, còn bearer dùng exact scope không phân cấp. Password dùng Argon2id; session/token/worker credential chỉ lưu hash; secret một lần không được persist vào idempotency snapshot.

Migration `20260920_0002` nâng schema generation lên `2`, thêm auth-control singleton, login-rate state bền vững và uniqueness cho worker credential hiện hành. API startup chỉ kiểm schema và durable deployment identity, không auto-migrate hoặc seed principal. Global policy version 1 là seed migration fail-closed; tenant mới có resource limits bằng zero cho đến khi task inventory/runtime sau cung cấp capacity đã xác minh.

Chạy API sau khi migration và secret/config đã được operator chuẩn bị:

```sh
uv run --no-sync uvicorn nexa.api.main:app --host 127.0.0.1 --port 8000
```

Worker bootstrap window hết hạn chỉ được mở lại bằng maintenance command local có audit:

```sh
uv run --no-sync nexa-maintenance reopen-worker-bootstrap
```

Luồng và boundary chi tiết nằm tại [authentication and administration](docs/authentication.md); evidence B06 nằm tại [B06 identity/token/RBAC](docs/evidence/B06-identity-token-rbac.md). `/v1/auth/session` vẫn browser-cookie-only theo OpenAPI hiện hành; mâu thuẫn mô tả CLI introspection chưa được tự ý sửa contract.

## B07 artifact store và upload bền vững

B07 cung cấp filesystem ArtifactStore một máy, upload bounded qua `application/octet-stream`, kiểm tra checksum/kích thước/media type, tenant storage reservation và artifact REST API. Bytes được ghi vào staging, kiểm tra rồi `fsync` file, atomic rename cùng filesystem và `fsync` thư mục trước khi PostgreSQL commit metadata, UploadSession, counter, audit và idempotency. Blob staging/orphan không được download hoặc reference; client không bao giờ nhận filesystem path hay blob key nội bộ.

API public hỗ trợ upload các kind `INPUT`, `DATASET` và `MODEL` theo allowlist media type; các kind checkpoint/result/log dành cho worker flow ở các task sau. Upload cùng idempotency key và metadata đã commit sẽ replay cùng Artifact, còn request khác payload trả conflict. List/metadata/download chỉ trả artifact `COMMITTED` đúng tenant, dùng cursor ký và hỗ trợ một byte range với checksum ETag.

Mỗi tenant có counter committed/reserved bytes; upload bị giới hạn theo file, quota tenant và disk watermark. B07 có primitive staging expiry và cleanup an toàn, nhưng metrics/alert, reachability claim, full GC, worker authority/fencing, checkpoint/restore, Linux portability và load/release evidence vẫn thuộc các task sau. Chi tiết nằm tại [artifact storage](docs/artifacts.md) và [B07 evidence](docs/evidence/B07-artifact-store.md). B07 đã được Task Review duyệt theo xác nhận mới nhất của user ngày 20/09/2026.

## B08 submit, idempotency và durable queue

B08 nhận spec/template đã allowlist cùng artifact `COMMITTED`, kiểm tra tenant ownership,
parameter/resource/capability feasibility và ghi `Job`, `LogicalSession`, `JobSpec`, artifact references, event,
audit, counters, durable rate buckets và idempotency snapshot nguyên tử trên PostgreSQL.
`POST /v1/jobs` chỉ trả `202` sau commit; retry cùng key/payload replay đúng snapshot, khác
payload trả `409`. `GET /v1/jobs`, job detail, logical session và events dùng tenant scope,
keyset/signed cursor và persisted state. Queue này là durable accepted-job ledger, chưa phải
dispatch/executor/scheduler runtime. Chi tiết tại [submit](docs/submit.md) và [B08 evidence](docs/evidence/B08-submit-durable-queue.md).

Evidence B08 ghi nhận kiểm thử PostgreSQL 17.11 cho concurrent submit, queue/rate race,
rollback và tình huống API process sập sau commit nhưng trước khi gửi response. API process
mới truy vấn và replay đúng Job ID cùng snapshot đã lưu; accepted-ID reconciliation không
có missing/duplicate/mismatch trong các scenario đã kiểm tra. B08 đã được Task Review duyệt
theo xác nhận của user ngày 20/09/2026. Worker/coordinator restart, host reboot, terminal
retention lifecycle, load/soak/chaos và release gates vẫn thuộc các chặng sau.

## B09 Docker executor và trusted runner

B09 có `ResourceProvider` đọc runtime Docker thực nơi workload sẽ chạy, tính capacity sau reserve floor và trả compatibility reason đóng. `DockerExecutor` chỉ nhận execution context bất biến đã được ủy quyền, materialize input đã xác minh theo luồng hữu hạn, ghi journal cục bộ quanh side effect, dùng full container ID cùng runtime identity digest ổn định, và không tự thay đổi Job, allocation, quota hay ledger. Container chạy PID 1 bằng `1000:1000`, drop toàn bộ capability và không add capability; worker khởi chạy supervisor `1001:1000` bằng exact-container-ID `docker exec`. Rootfs/input read-only, network none, seccomp, CPU/RAM/PID/tmpfs/log bounds và restart policy `no` đều được cấu hình và kiểm hành vi.

Trusted runner dùng frame JSON length-prefixed tối đa 64 KiB, payload đóng theo từng type, durable sequence/ACK replay và watchdog độc lập với socket receive. CPU iterative giữ oracle deterministic, phát progress, đi hết result handshake tới final manifest có checksum/provenance và chỉ trả bốn trường kết quả workload theo contract. B09 chưa triển khai HTTP renewal, checkpoint publish/restore, worker reconcile hay server-side result/release; evidence hiện là Docker Desktop Linux VM, không phải pass cho bare Linux, hai-host, GPU hoặc release. Chi tiết ở [worker executor](docs/worker-executor.md), [trusted runner](docs/trusted-runner.md) và [B09 evidence](docs/evidence/B09-docker-executor-trusted-runner.md).

B09 đã được Task Review duyệt theo xác nhận của user ngày 21/09/2026, trong phạm vi implementation và evidence nêu trên. Báo cáo triển khai được lập trước xác nhận này có thể còn ghi chờ review. B10 tái sử dụng các primitive discovery, executor/journal, IPC/deadline và cleanup proof để xây worker local.

## B10 Worker local, heartbeat và reconcile

B10 đã triển khai worker local với singleton OS, bootstrap credential bền vững,
incarnation server-generated, heartbeat/inventory theo DB time, bounded
reconciliation, exact container identity, adoption/rebind journal, renewal và
runner deadline ACK. Worker chạy các vòng heartbeat, reconcile, renewal, poll
và runner IPC độc lập; callback heartbeat chưa rõ outcome được replay bằng
đúng callback ID/payload trước khi lấy inventory mới. Allocation chưa được xác
minh cleanup vẫn bị giữ, không được coi là đã release.

Cleanup của authority đã revoked không gửi failure stale; khi restart, worker
phục hồi proof từ journal và giữ pending cho đến khi cleanup được xác minh.
Operation đã kết thúc bằng timeout được retry, còn operation đang chạy không
bị tạo chồng. Các tình huống này có unit/fault evidence dùng executor/journal
thật với Docker backend và cleanup peer giả lập; chưa chứng minh transaction
release/reaper phía server của B11/B15.

Evidence API/PostgreSQL và scenario worker bị kill rồi restart với runner B09
thật đã pass trong Docker Desktop Linux VM. Đây chưa phải bằng chứng bare-Linux,
GPU, clean-host portability hoặc release acceptance. **B10 đã hoàn thành và được
Task Review duyệt theo xác nhận của user ngày 22/09/2026**, trong phạm vi
implementation và evidence đã ghi nhận. Báo cáo triển khai được lập trước xác
nhận này có thể còn ghi chờ review. Chi tiết ở [B10 worker agent](docs/worker-agent.md)
và [B10 evidence](docs/evidence/B10-worker-heartbeat-reconcile.md).

## B11 coordinator, dispatch và CPU result

B11 bổ sung process coordinator dùng B04 policy để cấp allocation và offer qua PostgreSQL; worker nhận offer qua API, claim execution graph, tải input theo Authority, start runner, renew lease và xử lý result handshake. API chỉ công nhận manifest/binding hợp lệ từ attempt còn quyền, trả Result đã công nhận và chỉ release tài nguyên sau cleanup proof khớp identity.

Kiểm thử HTTP/PostgreSQL tập trung cho offer replay, graph input, result provenance, counter, event, accounting và cleanup đã có. Luồng upload/submit → coordinator → worker Linux → Docker runner → download cho hai tenant, mất response sau commit, restart trước cleanup và đối soát cuối đã chạy cục bộ trên Docker Desktop Linux VM. Claim cũ không có container sau khi worker đổi incarnation vẫn giữ allocation và chặn READY; vòng reaper/fence tự động thuộc B15. **B11 đã được Task Review duyệt theo xác nhận của user ngày 24/09/2026**, trong phạm vi implementation và evidence đã ghi nhận. Xem [hướng dẫn coordinator](docs/coordinator.md) và [evidence B11](docs/evidence/B11-coordinator-dispatch-result.md). Bare-Linux portability, GPU, checkpoint recovery hoàn chỉnh và release acceptance chưa được chứng minh.

## B12 CLI

B12 bổ sung CLI sản phẩm `nexa` gọi REST API chung cho cấu hình/profile, token, upload/download artifact, submit, job/session/event/result và quản trị tenant/user/membership/policy/audit. CLI giữ token tách khỏi cấu hình thường với quyền file hạn chế, gửi tenant context, giữ idempotency key khi retry và dùng ETag/If-Match cho mutation có yêu cầu. Backend tiếp tục kiểm tra mọi quyền; `nexa-maintenance` vẫn là CLI bảo trì riêng.

[Evidence B12](docs/evidence/B12-cli.md) ghi nhận kiểm thử CLI, cài entrypoint từ lockfile, API/PostgreSQL thật cho isolation/scope/ETag và mất response sau commit của submit/token. Luồng cùng một job từ CLI upload/submit đến coordinator, worker, Docker CPU workload rồi CLI đọc trạng thái/sự kiện và tải kết quả đã được kiểm thử; nhánh worker restart/cleanup cũng có kiểm thử hồi quy. Evidence container hiện giới hạn ở Docker Desktop Linux arm64 VM trên macOS.

**B12 đã được Task Review duyệt theo xác nhận của user ngày 25/09/2026**, trong phạm vi implementation và evidence nêu trên. [CLI guide](docs/cli.md) và báo cáo triển khai được lập trước xác nhận này có thể còn ghi chờ review. Các lệnh template, attempts/checkpoints/logs/progress, admin job/worker/allocation/fairness/recovery chưa được đăng ký vì backend tương ứng chưa có; việc bổ sung cần backend và evidence riêng. Job controls thuộc B15, sweep thuộc B16. Mô tả này là surface tại thời điểm duyệt B12: B15 đã đăng ký thêm `job cancel|pause|resume|retry|attempts`, `admin worker`, `admin allocations` và `admin recovery-events` (xem [CLI guide](docs/cli.md)). Phê duyệt B12 không thay nghiệm thu bare Linux, checkpoint recovery đầy đủ, Web UI, GPU, tải lớn, portability hoặc release.

## B13 fairness production, quota và ledger

B13 đưa policy B04 (weighted dominant resource-time + hard quota + aging 60 giây + một reservation 120 giây) vào coordinator thật. Queue head/submitter được duy trì trong PostgreSQL; mỗi tenant chỉ lấy tối đa 16 normal candidates cộng một oldest qua index, min-heap tenant theo Decimal. Event eligibility chỉ phát sinh từ thay đổi admin (capacity, inventory, bật/tắt tenant) và được replay tối đa 64 Job mỗi tick. Headroom quota đang giữ là staircase theo tenant (migration `0017`), nên cấp phát/release không ghi lại dòng Job hay event (sửa finding B13-R13 của Task Review vòng 1). Ledger được một accounting heartbeat riêng commit tối đa mỗi 250 ms, charge cả allocation QUARANTINED, fail closed khi DB lỗi và catch-up từ mốc đã commit. Evidence trên source cuối, PostgreSQL 17.11 trong Docker Desktop Linux VM gồm:

- 100.000 Job nộp qua production HTTP API trong một lần chạy, 0 lỗi; accepted ID, idempotency và counter đối chiếu được.
- Query plan trên queue 100.000 Job không Seq Scan bảng `jobs`, tối đa 16 dòng mỗi loop; tick median 223,5 ms (drain), 448,8 ms (dispatch) và 478,6 ms ở quota mặc định 50%, mỗi quyết định và mỗi release ghi 1 dòng `jobs`, 0 event.
- Tám phép đo cadence ledger commit, gồm hai lần 720 giây có rebuild 100 tenant, gap tối đa 498,1 ms.
- Ba run weighted fairness ở quota mặc định 50% có Jain tối thiểu 0,9994, không tick nào bỏ trống capacity vì replay; ba run đối chứng 6.000m trọng số 1:2:4 có Jain tối thiểu 0,9845.
- Trace reservation tạo sau 121,3 giây và hai thứ tự cleanup/tick.
- Fault DB tiêm vào có rollback và catch-up.
- Suite PostgreSQL 947 passed; Docker CPU vertical 2 passed.

Finding B13-R13 của Task Review vòng 1 đã được Task Review vòng 2 chạy lại độc lập và đóng. Finding B13-R12 (hàm Decimal của B05 gọi nhau không qualify schema nên `ANALYZE`/autoanalyze `fairness_ledgers` lỗi và `pg_restore` cần workaround dưới `search_path` hạn chế của PostgreSQL 17) **vẫn mở**, ngoài phạm vi B13 và chờ user quyết định; nó liên quan trực tiếp đến backup/restore của B21. Task Review vòng 2 ghi thêm một nhận xét không chặn: khi replay event do thay đổi admin (inventory, policy, bật tenant), tenant đó tạm không được chọn cho tới khi replay xong; hành vi này đã có từ vòng 1, không phải regression. Xem [B13 evidence](docs/evidence/B13-production-fairness.md) để chạy lại và xem gate matrix.

**B13 đã được Task Review duyệt theo xác nhận của user ngày 26/09/2026**, trong phạm vi implementation và evidence nêu trên. Báo cáo triển khai được lập trước xác nhận này có thể còn ghi chờ review. Phê duyệt B13 không thay nghiệm thu tải B22 (100 accepted/s trong 15 phút, soak 8 giờ), bare Linux, portability, GPU hoặc release.

## B14 CPU checkpoint/restore

B14 cho workload CPU `cpu-iterative` lưu checkpoint định kỳ và chạy tiếp từ checkpoint hợp lệ mới nhất sau khi container crash:

- **Reserve/publish có fence.** Worker reserve `checkpoint_id` + sequence đơn điệu theo Job; mất response thì replay trả lại đúng cặp. Publish kiểm authority/fence/lease theo DB time. Manifest là RFC 8785 canonical với schema đóng; provenance và compatibility được kiểm từng trường; file phải là artifact đã commit cùng tenant/attempt. Lỗi manifest xác định trả 422 và reservation chuyển `REJECTED`; sequence không bao giờ dùng lại.
- **Restore selection lúc claim.** Blob, checksum và provenance được kiểm ngoài transaction; lựa chọn được ghi bất biến vào `execution_context`. Bản hỏng bị đánh dấu `CORRUPT` một chiều qua bảng insert-only `checkpoint_corruptions` (migration `20260926_0018`, `schema_v15`), rồi hệ thống thử bản cũ hơn. Khi hết bản hợp lệ:
  - Job `restart_safe` chạy lại từ input, kèm event `CHECKPOINT_FALLBACK_TO_INPUT`;
  - Job không `restart_safe` kết thúc `FAILED` có reason, không chạy lại âm thầm.
- **Retry có restore.** Cleanup chuyển `RECOVERING → RETRY_WAIT` khi còn retry budget và có checkpoint đã commit hoặc template `restart_safe`. Coordinator leader promote `RETRY_WAIT → QUEUED` đúng một lần, theo batch có giới hạn. Reaper hết lease, cancel/pause và ma trận recovery đầy đủ thuộc B15 (xem mục B15 bên dưới).
- **Worker/runner.** Checkpoint cycle được ghi journal (reserve → chụp trạng thái tại ranh giới step → upload → bind → finalize → publish). Adoption sau worker restart không tạo reservation hay artifact trùng. Adapter resume đúng cursor `(step, accumulator)`. File restore được mount read-only, hardening B09 giữ nguyên.
- **Xem danh sách checkpoint.** `GET /v1/jobs/{job_id}/checkpoints` (keyset ≤100, có hiển thị `CORRUPT`) và lệnh `nexa job checkpoints`. B14 không xóa hay prune checkpoint; GC thuộc B19.

[Evidence B14](docs/evidence/B14-cpu-checkpoint-restore.md) chạy trên Docker Desktop Linux VM (linux/arm64) với PostgreSQL 17:

- **Image.** Image CPU mới `nexa/cpu-iterative@sha256:e0e6222eb899498c447eeee9f44ac053a62af271da2d1b95316b09117a999b6f` và image worker mới, build local, không push.
- **Sáu scenario Docker S1–S6:**
  - chạy liền mạch;
  - kill container sau checkpoint thứ hai;
  - làm hỏng checkpoint mới nhất;
  - làm hỏng mọi checkpoint rồi fallback về input;
  - kill ở nhiều thời điểm compute;
  - SIGKILL worker khi reservation còn mở.

  Trong cùng một lần chạy, kết quả R1–R6 giống R0 từng byte. Checksum giữa các lần chạy khác nhau vì `spec_checksum` chứa `input_artifact_id` của từng lần chạy.
- **Kiểm thử:**
  - 43 test PostgreSQL của B14 pass;
  - suite PostgreSQL đầy đủ 1238 passed, 16 skipped;
  - suite mặc định 900 passed, 354 skipped;
  - regression Docker B09/B10/B11 trên image mới 14 passed, 1 skipped (test B10 chỉ chạy trên Linux host).
- **Chưa chạy.** Chưa ai dùng `docker inspect` để xem cấu hình thật của container checkpoint có mount restore, nên dòng ACC-25 tương ứng là `not-run`; phần này mới có unit test.

Task Review vòng 1 không duyệt vì các finding B14-R01, R02, R06, R08, R09. Vòng 2 sửa hết, rồi Task Review chạy lại độc lập PostgreSQL và Docker S1–S6 và đóng mọi finding chặn. Finding không chặn còn mở:

- **B14-R03:** `listJobEvents` trả `created_at` với 6 chữ số thập phân. Lỗi thuộc B08.
- **B14-R04:** contract cần làm rõ trường hợp mọi checkpoint đều không tương thích với worker nhận việc.
- **B14-R05:** file `input.json` tải dở sau khi worker crash vẫn được tin. Lỗi thuộc B11/B15.
- **B14-R10:** checkpoint đua với runner đang dừng bị gắn nhãn `CHECKPOINT_PROTOCOL_ERROR`.
- **B14-R11:** một câu trong [worker agent](docs/worker-agent.md) chưa đúng với trường hợp Job không `restart_safe`.
- **Hai ghi chú:**
  - Biến thể còn lại của R08: phản hồi 2xx không phải object khi reserve/publish checkpoint. Lỗi tự hết khi replay callback.
  - Blob `not_found` bị đánh `CORRUPT` ngay cả khi storage được mount muộn.
- **B13-R12:** vẫn mở.

Test realtime benchmark của B13 từng flaky một lần khi chạy ngay sau Docker test; lần chạy lại thì pass.

**B14 đã được Task Review duyệt theo xác nhận của user ngày 26/09/2026**, trong phạm vi implementation và evidence nêu trên. Evidence và tài liệu được lập trước xác nhận này có thể còn ghi chờ review. Chưa có template seed nào checkpointable. Muốn dùng checkpoint, admin phải đăng ký template version có `checkpointable = true` và digest image B14. Phê duyệt B14 không thay ACC-22 đầy đủ (B15), PyTorch/sweep/inference (B16), GC và reference protection (B19), tải B22, bare Linux, portability, GPU hoặc release.

## B15 cancel, pause/resume, retry và recovery

B15 bổ sung job control dưới `If-Match` và idempotency: cancel ở mọi state chưa terminal, pause qua checkpoint-for-pause (hoặc dừng ngay khi job không checkpointable nhưng `restart_safe`), resume, manual retry thành job/session mới với `retry_of_job_id`. Lease reaper của coordinator leader thu hồi lease hết hạn theo DB time bằng CAS, fence attempt đúng một lần và để allocation `QUARANTINED` (vẫn bị tính quota/ledger) đến khi cleanup có proof. Admin drain/disable/enable worker: drain giữ offer đã commit, disable fence mọi lease sống, enable chỉ khi heartbeat gần nhất đã qua kiểm tra READY và worker đã reconcile. Migration `20260926_0019` cho phép job checkpoint-for-pause vào queue B13, thêm `worker_incarnations.ready_checked_at` và backfill `terminal_at`.

Evidence ở [B15 evidence](docs/evidence/B15-control-recovery.md): kiểm thử runner/worker, PostgreSQL cho race cancel/reaper/complete/disable có thứ tự lock xác định và mutation check, cùng scenario Docker thật C0–C10. Evidence container giới hạn ở Docker Desktop Linux VM trên macOS; không thay bare Linux, GPU, tải lớn hoặc release.

- **Image build local, không đẩy lên registry:**
  - CPU `nexa/cpu-iterative:b15` = `nexa/cpu-iterative@sha256:c30fa52a0e0dd7ecdc25bf4ceae4ffe2ef711c1c4630029d60dd8b7535342024`;
  - worker `nexa/b15-worker:local` = `sha256:bcefa221f16a392b97ec8defe2fa16c066a5e1ce37dd6c4f1a25fe05257aeae3`.
- **Scenario Docker trên source cuối (run 19):**
  - C0 chạy liền mạch; C1 pause → resume; C2 cancel khi RUNNING;
  - C3 SIGKILL worker quá lease; C4 và C5 pause-crash sau/trước checkpoint đầu (C5b: template không `restart_safe` → `FAILED`);
  - C6 drain/disable/enable; C7 manual retry có và không có checkpoint; C8 timeout không retry;
  - C9 mất mạng khi renew, không có compute chồng lấn; C10 restart API, coordinator và worker cùng lúc.

  Trong cùng một lần chạy, kết quả C1, C3, C4, C6, C9 và C10 giống R0 từng byte. C7 cho cùng kết quả tính toán dưới spec của job nguồn.
- **Kiểm thử:**
  - suite mặc định 978 passed, 443 skipped;
  - suite PostgreSQL đầy đủ 1403 passed, 18 skipped (18 test Docker opt-in);
  - regression Docker B09/B10/B11/B14 trên image mới 15 passed, 1 skipped, 2 deselected (test B10 chỉ chạy trên Linux host);
  - Docker B15 (C0–C10, C5b/C7b) 2 passed.
- **Chưa chạy.** Reboot host (ACC-20) là `not-run`. OOM và log-flood không có trong scenario B15; dòng ACC-22 dẫn evidence container thật của B09, B11 và B14. Race cancel/complete chỉ được chứng minh trên PostgreSQL.

B15 sửa luôn B14-R03 (timestamp 3 chữ số), B14-R05, B14-R10 và câu sai trong [worker agent](docs/worker-agent.md) (B14-R11). Task Review vòng 1 không duyệt. Vòng 2 sửa các finding chặn, rebuild hai image và chạy lại PostgreSQL cùng Docker trên source cuối. Còn mở, không chặn:

- **B15-R11:** container chết sau start ACK nhưng trước khi runner nhận deadline bị báo `TIMEOUT/STARTUP_TIMEOUT` thay vì lỗi hạ tầng retry được. Lỗi gốc B10/B11.
- **B15-R32:** promote retry về `QUEUED` vẫn khóa `KEY SHARE` dòng `users` qua trigger `queue_submitters` của B13. Promote thất bại được thử lại ở lần probe sau; sửa cần đổi trigger/FK đã duyệt của B13.
- **B15-R33:** callback cũ bị từ chối và kết quả reconcile của worker chưa được lưu thành event, chỉ thấy trong response API và log worker.
- **B15-R39 (Task Review vòng 2 phát hiện, gốc B10/B11):** `reconcile_once` gửi lại callback failure/cleanup qua `_send_resolution` mà không giữ khóa journal của attempt, nên đua với vòng gửi kết quả. Log có `/cleanup` gửi hai lần, một `KeyError` đã được bắt và worker tạm mất trạng thái sẵn sàng (poll trả 409 `state_conflict`).
- **Ghi chú:** thỉnh thoảng có cảnh báo `JournalCorruption` trong `_result_once` ngay sau khi checkpoint commit. Không làm đổi kết quả nhưng chưa được chẩn đoán. Offer commit ngay trước `ADMISSION_OFF` có thể bị reaper thu hồi vì poll từ chối khi mode khác bình thường.
- **Diễn giải contract đã chọn cách an toàn:**
  - B15-R02: lỗi non-retryable thắng desired `PAUSED` → `FAILED`;
  - B15-R05: provenance session của checkpoint kế thừa khi manual retry;
  - B15-R06: manual retry lần hai với key mới được phép;
  - B15-R34: cancel job đã bị reaper fence giữ attempt `LOST`.
- **B14-R04 và B13-R12:** vẫn mở.

**B15 đã được Task Review duyệt theo xác nhận của user ngày 27/09/2026**, trong phạm vi implementation và evidence nêu trên. Evidence và tài liệu được lập trước xác nhận này có thể còn ghi chờ review. Muốn dùng pause qua checkpoint, admin phải đăng ký template version có `checkpointable = true` và digest image B15. Phê duyệt B15 không thay PyTorch/sweep/inference (B16), Web UI (B17/B18), metrics/GC (B19), race/security (B20), reboot, tải lớn và chaos (B22), bare Linux, portability, GPU hoặc release.

## B16 PyTorch, sweep và chunk inference

B16 thay các chỗ so sánh cứng `cpu.iterative` bằng registry adapter tĩnh `nexa.domain.workload_adapters` (server, coordinator và worker không import PyTorch), cho worker quảng bá capability theo từng image đã kiểm label và thêm ba phần:

- **Template.** Template version được admin đăng ký bằng `nexa-maintenance register-template` từ file định nghĩa `deploy/templates/*.json` cùng image digest; cùng nội dung thì idempotent, khác nội dung thì bị từ chối. Người dùng xem qua `GET /v1/templates`, `GET /v1/templates/{template_id}` và `nexa template list|show`. Mọi template B16 chỉ chạy CPU; yêu cầu GPU bị từ chối.
- **PyTorch CPU training (`pytorch-cifar10-cnn`).** Dataset CIFAR-10 subset chuẩn bị offline bằng `scripts/b16_prepare_fixtures.py` (bản binary, không pickle); checkpoint gồm model/optimizer bằng safetensors, RNG Python/NumPy/Torch CPU và sampler/cursor. Fixture và tolerance ở `tests/fixtures/workloads/pytorch-cifar10-v1/` được đóng băng trước phép đo nghiệm thu.
- **Hyperparameter sweep.** `POST /v1/sweeps`, `GET /v1/sweeps/{sweep_id}` và `nexa sweep submit|show`: tối đa 100 child sau dedup RFC 8785, mỗi child đi qua cùng use case submit (quota/rate/admission riêng), parent không giữ slot, replay trả mapping giống hệt và không tiêu thêm counter.
- **Batch inference theo chunk (`batch-inference`).** Chunk `chunk-%08d` bất biến, RecognizedChunk unique theo job, carry-forward chỉ nhận chunk đã được công nhận của cùng job với đúng source attempt/fence, restore kiểm lại chunk được tham chiếu và completion yêu cầu phủ đủ `[0, N)`.
- **Migration `20260928_0020`** lưu ordered expansion của sweep và số item của batch inference.

Evidence ở [B16 evidence](docs/evidence/B16-pytorch-sweep-inference.md):

- **Image build local, không đẩy lên registry** (base `python:3.12-slim` theo digest, PyTorch 2.12.1 CPU):
  - linux/amd64 trên VPS1: training `nexa/pytorch-cifar10:b16r2` = `sha256:fcad40287b78e8a7f3208168c739aaadc0c2e2004290876950c1f2ae4391e797`, inference `nexa/batch-inference:b16r2` = `sha256:db50610aa8c93896202638ca7e731b6a0595ead3b6ea62ca331c313c4f42c4ae`, CPU `sha256:6d6d0635…49d5`, worker `sha256:5defa941…bfe5`;
  - linux/arm64 trên Mac: CPU `nexa/cpu-iterative@sha256:95f89f118eb808ced3e475929378c40997ad0b47d6ffe27c3c9b3d0f2e194b6b`, worker `nexa/b16-worker:r2-local` (`sha256:788a8fc8…1625`);
  - chưa build image PyTorch arm64.
- **Scenario Docker trên VPS1 (môi trường L):** D1 training liền mạch; D2 SIGKILL container rồi restore; D3 checkpoint mới nhất hỏng → fallback bản trước; D4 inference liền mạch; D5 crash rồi carry-forward (16 chunk giữ lại, 24 chunk tính tiếp, 0 xung đột); D6 blob checkpoint mất → fallback, job fail closed; D7 sweep 6 child với quota cho 2 child; D8 kiểm hardening container. Thêm OOM thật ở 256 MiB cho cả training và inference: `OOM/CONTAINER_OOM`, không retry. D2/D3 nằm trong tolerance đã đóng băng, chênh 0 so với baseline.
- **Kiểm thử trên source cuối:**
  - suite mặc định 1588 passed, 523 skipped; suite PostgreSQL đầy đủ 2089 passed, 22 skipped;
  - test cần torch chạy trong image test dựng từ image PyTorch trên VPS1: 153 passed;
  - regression Docker B09–B15 trên Mac với image mới: 17 passed.
- **Task Review chạy lại độc lập** suite mặc định và PostgreSQL (khớp số trên). Test torch, scenario Docker trên VPS1 và Docker trên Mac được duyệt dựa trên evidence của Task Code, không chạy lại.
- **Chưa chạy:** D9 (worker restart giữa chu kỳ checkpoint PyTorch), `subset_size` 50000, inference `batch_size` 4096, smoke PyTorch trên Mac. Tolerance chỉ được chứng minh cho cùng host, image và số thread; khác host cùng kiến trúc chưa đo.

Task Review vòng 1 không duyệt vì ba finding chặn. Vòng 2 sửa hết: tham số inference vượt giới hạn và metadata dataset/header safetensors hỏng dừng với `INVALID_INPUT` (B16-R24/R25), OOM của container dừng với `OOM/CONTAINER_OOM` thay vì `INTERNAL` (B16-R26), cả hai không retry; kèm B16-R27 (tài liệu) và B16-R28 (khóa `FOR SHARE`). Còn mở, không chặn:

- **B16-R21:** sau fallback về checkpoint cũ hơn, attempt mới tính lại chunk đã được công nhận và bị 409. Job fail closed nhưng phân loại lỗi attempt khác nhau giữa các lần chạy. Cần quyết định của user/contract.
- **B16-R29:** test B09 `tests/docker/test_real_runner.py` so UID theo tên user mà `docker top` hiển thị, nên fail trên VPS1 (UID 1000/1001 có tên trên host). Hướng sửa gợi ý: đọc `pid,uid,args`. Regression runner hiện chỉ được chứng minh trên Docker Desktop.
- **Ghi chú của Task Review:** [trusted runner](docs/trusted-runner.md) ghi exit 65 xảy ra "trước khi ghi output", không đúng với chunk k>0 vượt kích thước; phân loại `INVALID_INPUT` vẫn đúng.
- **Diễn giải đã chấp nhận:** B16-R05 (retention idempotency của `submitSweep`) và các diễn giải B16-R01–R07 ghi trong evidence.
- **B13-R12, B14-R04, B15-R11/R32/R33/R39** và ghi chú `JournalCorruption` giữ nguyên.

**B16 đã được Task Review duyệt theo xác nhận của user ngày 28/09/2026**, trong phạm vi implementation và evidence nêu trên. Evidence và tài liệu được lập trước xác nhận này có thể còn ghi chờ review. Muốn chạy workload AI, admin phải đăng ký template version bằng `nexa-maintenance register-template` với digest image B16 đúng kiến trúc máy. Phê duyệt B16 không thay Web UI (B17/B18), metrics/GC (B19), race/security (B20), bare Linux, portability và backup (B21), tải lớn (B22), GPU/CUDA (B23), demo (B24) hoặc release.

## B1–B16 Findings Remediation

Đợt này đóng các finding mà audit độc lập B01–B16 xác nhận còn mở. Phạm vi gồm cả những finding đã ghi "còn mở, không chặn" ở các mục B13–B16 phía trên. Các danh sách "còn mở" ở mục B11–B16 là ảnh chụp tại lúc từng chặng được duyệt; trạng thái hiện tại của các finding đó ghi ở mục này. Task không dùng Superpowers, không sửa PLAN và không thêm dependency (lockfile không đổi).

**Kết quả trên 31 finding đầu vào:**

- 23 `CLOSED`.
- 5 `ENVIRONMENT BLOCKED`.
- 3 `NEEDS OWNER DECISION`.
- 0 `BLOCKED`.

Task còn tự phát hiện thêm 10 vấn đề mới (REM-R01..R10): 9 `CLOSED`, 1 `BLOCKED` (REM-R08).

**Các thay đổi chính:**

- **PostgreSQL:**
  - Migration `20260928_0021` (`schema_v18`, B13-R12) qualify lời gọi nội bộ của 6 helper Decimal và ghim `search_path` cho 39 hàm. Nhờ đó `ANALYZE`, `REINDEX` và `pg_dump` → `pg_restore` chạy được mà không cần workaround.
  - Migration `20260929_0022` (`schema_v19`, B14-OBS-01) thêm bảng insert-only `artifact_store_identity`. Checkpoint chỉ bị đánh `CORRUPT` khi store đã được xác minh đúng; store sai hoặc bị dựng lại thì API trả 503 và không đánh dấu gì.
  - Migration head là `0022`, không sửa migration đã phát hành.
- **Worker và recovery:**
  - B11-H01: worker chết giữa lúc claim đã commit và lúc journal gắn container giờ được xử lý bằng tombstone rồi `NoContainerProof`. Container tạo muộn bị dọn.
  - B15-R39: resolution được gửi dưới khóa journal của attempt, nên không còn `/cleanup` kép.
  - B15-OBS-01: đọc journal có khóa; `JournalCorruption` mang chẩn đoán an toàn.
  - REM-R05/R06: không đường nào (renewal, scan, IPC, result, failure, offer) còn hành động trên attempt đã verify cleanup.
  - B14-R08: body 2xx không phải object ở reserve/publish checkpoint được xử lý như lỗi protocol.
- **Phân loại lỗi runner:**
  - B15-R11: runner chết sau start ACK được báo `RUNNER_UNAVAILABLE` và được retry, thay vì `STARTUP_TIMEOUT`.
  - B15-R10: startup limit có lý do `STARTUP_LIMIT`, ra `TIMEOUT/STARTUP_TIMEOUT`.
  - B15-R14: exit status 90–96 giữ lý do dừng; watchdog stop không xác nhận thì fail closed.
  - B14-K5: lỗi disk/I/O khi stage checkpoint ra `INTERNAL/CHECKPOINT_STORAGE_FAILED`, không retry.
  - REM-R01: reset kết nối supervisor không còn bị coi là stop đã xác nhận.
- **Batch inference (B16-R21).** Claim mang `recognized_chunks`. Worker tải các chunk đã công nhận và mount read-only; runner bỏ qua chúng và carry-forward đúng source attempt/fence. Job restart-safe giờ hoàn tất sau fallback mà không tính lại hay công nhận trùng. Xung đột thật cho `INTERNAL` với reason an toàn; `ErrorResponse` có thêm trường `reason`.
- **Coordinator:**
  - B15-OBS-02: trong `ADMISSION_OFF`, offer đã commit trước đó vẫn được poll/claim, và không có offer mới.
  - B15-R32: khóa principal dùng `FOR NO KEY UPDATE`, không còn vòng khóa với FK `queue_submitters`.
  - B14-K1: probe retry chỉ lấy khóa khi có việc.
  - B15-R18: retention sweep có giới hạn; walk trên 100.000 record giảm từ 267,9 ms xuống 0,11 ms (EXPLAIN ANALYZE).
- **API và contract:**
  - B16-R08: path/query sai định dạng trả 400.
  - B16-R10: giá trị trùng trong một dimension của sweep bị từ chối 422.
  - B15-R05: server kiểm lineage và provenance của checkpoint kế thừa khi manual retry.
  - B15-R33: callback stale bị từ chối và kết quả reconcile được ghi thành log JSON có giới hạn, không thêm event type. Counter metric thuộc B19.
  - Làm rõ contract cho B14-R04, B15-R02, B15-R34, B16-R04 và B16-R05.
- **Compose (ENV-01).** Healthcheck `wget` của BusyBox để lại tiến trình `ssl_client` mồ côi, và caddy ở vị trí PID 1 không thu dọn chúng, nên cứ mỗi lần healthcheck (5 giây) lại rò một zombie. Service caddy nay có `init: true`, thêm `pids_limit: 512` làm lưới an toàn. Soak 8 giờ 30 phút trên VPS1: 0 zombie, pids ổn định ở 15.
- **Tài liệu:** sửa câu trạng thái lỗi thời trong `AGENTS.md`, `docs/acceptance.md` và header evidence B04/B05 (AUD-01), chỉ đổi câu trạng thái, không đổi quy tắc hay gate. Sửa câu exit 65 trong [trusted runner](docs/trusted-runner.md) (B16-DOC-01).

**Breaking change có chủ đích:**

- B16-R08: 422 → 400 cho path/query sai định dạng, cùng code `validation_failed`.
- B16-R10: sweep có giá trị trùng trong một dimension trước đây được gộp im lặng; nay bị từ chối 422. Sweep đã lưu vẫn replay nguyên trạng.

**Thay đổi additive:**

- `ErrorResponse.reason`.
- `ExecutionContext.recognized_chunks`.
- Lý do IPC `STARTUP_LIMIT` và exit status 90–96.
- Mã `CHECKPOINT_STORAGE_FAILED`.

Runner, worker và image workload phải dựng từ cùng source.

**Evidence.** Mọi số liệu dưới đây chạy trên cây cuối (hash `src/nexa` `27857313…`):

- Suite mặc định trên Mac: 1772 passed, 599 skipped.
- Suite PostgreSQL trên VPS1: 2343 passed, 4 skipped.
- Toàn bộ `tests/docker` trên VPS1: 24 passed.
- Test torch trong image test: 162 passed.
- Ruff và `git diff --check` sạch.

Image cuối chỉ build cho linux/amd64 trên VPS1, không đẩy lên registry:

- CPU `sha256:8ecfe240000348e492fbda2774acd78f214416a228bf7399632db686e179645e`;
- worker `sha256:897a7219b0610c9fab5e4f954791e7979f23030411fdd10277efc64ae236ac8f`;
- PyTorch `sha256:fb7331027009ab223d0e361e1ba50c563ce6e033e7eacdee72960f010901f2ec`;
- inference `sha256:32e963d043bab5b07952cc90034985208eb601e9236b06cf9bd04b6efa0e3449`.

Chưa build image arm64. Không có lượt Docker hay PostgreSQL nào chạy được trên Docker Desktop (P) trong task. VPS1 là EC2 VM không GPU, nên không gate ACC nào được nâng lên pass nhờ các lượt này.

Task Review vòng 1 không duyệt vì bốn điểm chặn (RV01–RV04):

- REM-R05 chưa đóng;
- D6/D6b chưa chạy lại với image cuối;
- chunk đã công nhận vẫn bị tính lại;
- evidence chưa đủ.

Vòng 2 đã sửa hết. Theo đúng quy tắc đóng, vòng 2 cũng chuyển bốn finding mà điều kiện đóng đòi chạy cả trên P từ `CLOSED` sang `ENVIRONMENT BLOCKED`.

**Chưa đóng:**

- **`ENVIRONMENT BLOCKED`:**
  - B11-H01, B15-R39, B15-R11 và B16-R29 đã đạt mọi điều kiện trên VPS1; chỉ còn thiếu lượt chạy trên P.
  - B15-OBS-01 có sửa phòng thủ và chẩn đoán, và VPS1 không còn cảnh báo. Tuy vậy cơ chế gây lỗi chưa được chứng minh bằng dữ liệu.
  - Hành động tối thiểu:
    1. dừng `nexa_b10_smoke3-caddy-1` (hoặc khởi động lại VM của Docker Desktop);
    2. build image arm64 từ cây hiện tại;
    3. chạy `tests/docker` trên P theo evidence;
    4. với B15-OBS-01, đếm số cảnh báo `JournalCorruption`.
- **`NEEDS OWNER DECISION`:**
  - B13-OBS-01 (OD-2): số đo trên VPS1 ở 100.000 Job là khoảng 375,8 giây sau khi đổi capability và khoảng 42,9 giây sau khi bật/tắt tenant, trước lần dispatch đầu. Owner chọn chấp nhận limit (docs đã sẵn) hoặc đặt bound để tối ưu.
  - B15-R06 (OD-1): có cho manual retry lần hai với key mới không.
  - AUD-02 (OD-3): file `benchmarks/results/b04-fairness.json` nặng 93 MB đang được track: giữ kèm guard, hoặc ngừng track.
- **`BLOCKED`:** REM-R08. Precondition của test Docker K5 fail một lần và không tái hiện trong 7 lần chạy lại; chưa có dữ liệu chẩn đoán. Không ảnh hưởng B14-K5.
- **Giới hạn đã ghi:**
  - danh sách recognized tối đa 2048 mục, chưa đo ở quy mô lớn nhất;
  - tải chunk carry-forward nằm trong budget claim 30 giây;
  - mất frame của B14-K5 chỉ còn lý do chung;
  - đường result của K5 giữ phân loại cũ;
  - store identity không tự khởi tạo lại;
  - `CREATE_IN_FLIGHT` cùng incarnation giữ worker không READY đến khi restart;
  - còn cửa sổ vài chục ms reserve checkpoint sau cleanup, bị server fence trả 409;
  - blob dùng chung theo tenant.

  Xem mục "Giới hạn" của evidence.

**B1–B16 Findings Remediation đã được Task Review duyệt theo xác nhận của user ngày 30/09/2026**, trong phạm vi implementation và evidence nêu trên. Evidence được lập trước xác nhận này còn ghi chờ Task Review. Phê duyệt này:

- không biến các finding `ENVIRONMENT BLOCKED`, `NEEDS OWNER DECISION` và `BLOCKED` thành đã đóng;
- không nâng gate ACC nào;
- không thay các chặng B17–B25.

Muốn chạy image mới, admin đăng ký template version mới bằng `nexa-maintenance register-template` với digest image cuối đúng kiến trúc máy; không sửa template version đã đăng ký.

## Thứ tự triển khai

Theo PLAN §11/§13: **contract + simulator → vertical slice → fairness → recovery → Web UI → nghiệm thu/release**. Contract `1.0.0-b01` đã đóng R-03, R-05 và R-09 qua focused rereview cùng verification mới; ACC-01 là `pass`. B01–B16 đã được duyệt (B13 và B14 theo xác nhận của user ngày 26/09/2026, B15 ngày 27/09/2026, B16 ngày 28/09/2026), và đợt B1–B16 Findings Remediation được duyệt ngày 30/09/2026. Các chặng tiếp theo là **B17 — Web UI cho user**, **B18 — Web UI cho admin** và **B19 — Metrics, audit, storage limits**; cả ba đã đủ điều kiện và có thể làm song song. Owner còn ba quyết định OD-1..3 và một lượt chạy lại trên Docker Desktop cho năm finding `ENVIRONMENT BLOCKED`, nêu ở mục remediation phía trên. Evidence container B10–B15 giới hạn ở Docker Desktop Linux VM. B16 có thêm evidence Linux thật (môi trường L) trên VPS1, một EC2 VM; đợt remediation chạy lại toàn bộ `tests/docker` trên đó. Các gate bare-Linux/portability/release vẫn thuộc các chặng sau. B23 GPU có điều kiện: bắt đầu được khi B16 và B19 xong và có GPU thật; thiếu GPU không chặn lõi CPU nhưng chặn claim GPU verified.

[Environment inventory](docs/environment-inventory.md) ghi nhận Git, Python 3.12, Docker/Compose, Node.js và `pnpm` trên máy macOS hiện tại; system PATH vẫn thiếu `uv` và `psql`, nhưng B02/B05 đã dùng isolated `uv`, psycopg và Docker PostgreSQL 17 để hoàn tất local evidence tương ứng. GitHub-hosted run chưa được quan sát. macOS hỗ trợ development và PostgreSQL integration, không thay evidence Linux/cgroups. Các command B05 chỉ chứng minh persistence trên PostgreSQL 17, không phải product runtime.

## Kiểm tra repository hiện tại

```sh
git status --short --untracked-files=all
git diff
git diff --check
git ls-files --others --exclude-standard
git check-ignore -v --no-index .env
```

Đọc riêng nội dung file untracked vì `git diff` chưa hiển thị chúng. Với `git check-ignore`, exit 1 cho đường dẫn không bị ignore là kết quả mong đợi khi bảo vệ file nguồn/evidence. Không commit/push tự động.
