# nexa / PBL4

Nền tảng single-node, self-hosted và hardware-portable chạy batch AI cho nhiều tenant trên một Linux server, phân phối CPU/RAM/GPU công bằng và tiếp tục job từ application checkpoint.

**Trạng thái: B01–B14 đã được Task Review duyệt.** B04 có policy thuần weighted dominant resource-time, aging, reservation và evidence simulator lớp D. B05 cung cấp PostgreSQL schema/migration/constraint và transaction helpers. B06 bổ sung API thật cho identity, browser session, CLI token, SYSTEM_ADMIN, tenant/membership, versioned policy, bootstrap worker và audit. B07 bổ sung filesystem artifact store, bounded upload, durable commit order, tenant counter/reservation và artifact REST API. B08 bổ sung submit atomic, idempotency replay/conflict, durable queue, quota/rate backpressure và REST job/session/event query. B09 bổ sung discovery/capability, Docker executor, journal/cleanup proof, trusted runner và image CPU deterministic; evidence gồm unit/protocol/fault tests và mười một scenario executor/runner trên Docker Desktop Linux VM. B10 bổ sung worker local, bootstrap/singleton, heartbeat, reconciliation/adoption, lease renewal, replay bền vững và Compose bootstrap; đã có evidence API/PostgreSQL và worker kill/restart với runner thật trên Docker Desktop Linux VM. B11 bổ sung coordinator leadership, policy-based allocation/dispatch, worker claim/start, CPU result upload và fenced result/release; luồng CPU hai tenant, mất response sau commit và restart trước cleanup đã được kiểm thử trên Docker Desktop Linux VM. B12 cung cấp product CLI REST-only cho config, token, artifact, core job/session/event/result và admin identity/policy/audit; command surface chỉ đăng ký route đã wire và giới hạn backend hiện tại được ghi tại [CLI guide](docs/cli.md) và [B12 evidence](docs/evidence/B12-cli.md). B13 đưa weighted fairness, hard quota, aging, reservation và ledger vào coordinator thật; evidence gồm queue 100.000 Job trên PostgreSQL 17. B14 bổ sung CPU checkpoint/restore: reserve/publish checkpoint có fence, chọn checkpoint lúc claim với đánh dấu `CORRUPT` và fallback, retry có restore sau khi container crash, cùng API/CLI xem danh sách checkpoint; sáu scenario Docker thật cho kết quả giống từng byte với run không bị ngắt. Triển khai bare Linux, portability hai máy, recovery đầy đủ (cancel/pause/reaper, B15), workload AI (B16), Web UI nghiệp vụ, GPU và release acceptance vẫn thuộc các chặng sau; các thông số hiệu năng ngoài evidence đã ghi vẫn là mục tiêu nghiệm thu, chưa phải kết quả.

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

**B12 đã được Task Review duyệt theo xác nhận của user ngày 25/09/2026**, trong phạm vi implementation và evidence nêu trên. [CLI guide](docs/cli.md) và báo cáo triển khai được lập trước xác nhận này có thể còn ghi chờ review. Các lệnh template, attempts/checkpoints/logs/progress, admin job/worker/allocation/fairness/recovery chưa được đăng ký vì backend tương ứng chưa có; việc bổ sung cần backend và evidence riêng. Job controls thuộc B15, sweep thuộc B16. Phê duyệt B12 không thay nghiệm thu bare Linux, checkpoint recovery đầy đủ, Web UI, GPU, tải lớn, portability hoặc release.

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
- **Retry có restore.** Cleanup chuyển `RECOVERING → RETRY_WAIT` khi còn retry budget và có checkpoint đã commit hoặc template `restart_safe`. Coordinator leader promote `RETRY_WAIT → QUEUED` đúng một lần, theo batch có giới hạn. Reaper hết lease, cancel/pause và ma trận recovery đầy đủ thuộc B15.
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

## Thứ tự triển khai

Theo PLAN §11/§13: **contract + simulator → vertical slice → fairness → recovery → Web UI → nghiệm thu/release**. Contract `1.0.0-b01` đã đóng R-03, R-05 và R-09 qua focused rereview cùng verification mới; ACC-01 là `pass`. B01–B14 đã được duyệt (B13 và B14 theo xác nhận của user ngày 26/09/2026). Chặng tiếp theo là **B15 — Cancel, pause/resume, retry, recovery**; B15 đã đủ điều kiện vì B12, B13 và B14 đều hoàn thành. Evidence container B10–B14 hiện giới hạn ở Docker Desktop Linux VM; các gate bare-Linux/portability/release vẫn thuộc các chặng sau. B23 GPU có điều kiện; thiếu GPU không chặn lõi CPU nhưng chặn claim GPU verified.

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
