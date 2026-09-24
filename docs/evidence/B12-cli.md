# B12 CLI evidence

## Trạng thái và phạm vi

**Đã triển khai, chờ Task Review độc lập.** Đây là evidence local cho product
CLI REST-only và tài liệu contract. Nó không phải phê duyệt B12, project
acceptance hay release acceptance. Không claim B15 controls, scheduler
production, GPU, multi-server, load, portability, recovery, Web UI hoặc release.

Revision được kiểm tra khi lập report:

```text
cffa8de7ebe5cab9f0436ecee42fbcfbec884e51
```

Worktree vẫn uncommitted theo yêu cầu. Các thay đổi gồm product CLI B12,
focused tests và tài liệu B12; không sửa maintenance CLI behavior.

## TDD và test evidence

Smoke test được thêm tại `tests/cli/test_cli_smoke.py`. Test chạy product module
`python -m nexa.cli.app --help` và maintenance entry point (hoặc gọi
`nexa.cli.main.app()` khi executable chưa được cài); product phải hiện `config`
và `artifact`, maintenance phải hiện `reopen-worker-bootstrap`. Đây là kiểm tra
entry-point separation, không phải API integration.

Evidence từ Tasks 1–4 được giữ nguyên:

| Lớp | Command/result | Ý nghĩa |
|---|---|---|
| Foundation/config/transport | `PYTHONPATH=src .venv/bin/pytest tests/cli/test_config.py tests/cli/test_storage.py tests/cli/test_errors_output.py tests/cli/test_client.py tests/cli/test_app.py -q` → 21 passed ở fix round cuối Task 1 | local config, secret separation, retry/error/output và REST transport mocks |
| Config/token | `PYTHONPATH=src .venv/bin/pytest tests/cli/test_config_commands.py tests/cli/test_token_commands.py tests/integration/test_maintenance_cli.py -q` → 5 passed, 2 skipped | command mapping; maintenance PostgreSQL tests guarded/skipped |
| Template/artifact/job | `PYTHONPATH=src .venv/bin/pytest tests/cli -q` → 44 passed ở Task 3 fix round | bounded I/O, checksum, strict spec JSON, job/artifact transport mocks |
| Admin | `PYTHONPATH=src .venv/bin/pytest tests/cli -q` → 69 passed ở Task 4 fix round | operation descriptors, ETag, idempotency, pagination, body validation |
| Post-review CLI | `PYTHONPATH=src .venv/bin/pytest tests/cli -q` → 102 passed | redirect fail-closed, admin password prompt refuses echo fallback before reading or sending a request, empty-response ETag, config-write error regressions, route-surface contract, plus prior CLI coverage |
| Full local suite | `PYTHONPATH=src /tmp/nexa-b12-frozen-env/bin/pytest -q` → 646 passed, 257 skipped, 2 dependency deprecation warnings | fresh regression run from the frozen non-editable environment; PostgreSQL tests are opt-in |
| Guarded API/PostgreSQL | `PYTHONPATH=src NEXA_TEST_DATABASE_URL=<dedicated guarded URL> .venv/bin/pytest --run-postgres tests/integration/test_cli_b12_vertical.py tests/integration/test_execution_closure_b11.py::test_http_graph_result_recognition_cleanup_and_replay -q` → 3 passed, 2 dependency deprecation warnings | real CLI subprocess to Uvicorn/FastAPI and migrated PostgreSQL 17; detail below |
| Same-job Docker vertical | `NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 NEXA_B09_IMAGE_REF=<pinned CPU digest> NEXA_B11_WORKER_IMAGE=nexa/b11-local:review NEXA_TEST_DATABASE_URL=<fresh guarded URL> PYTHONPATH=src .venv/bin/pytest --run-postgres 'tests/docker/test_b11_vertical.py::test_two_tenant_api_to_docker_result_and_release[False]' -q` → 1 passed, 2 dependency deprecation warnings in 58.22 s | CLI upload/submit/status/events/result-download for the same job through real coordinator, Linux worker container and Docker CPU workload |
| Docker restart regression | Same guarded command with `test_two_tenant_api_to_docker_result_and_release[True]` and a separate fresh test database → 1 passed, 2 dependency deprecation warnings in 49.70 s | pre-existing worker restart/cleanup arm still passes after adding the CLI path |
| Static | `/tmp/nexa-b12-frozen-env/bin/ruff check .`; `/tmp/nexa-b12-frozen-env/bin/ruff format --check .`; `git diff --check` → pass | lint/format/whitespace after R02/R05 changes |

Task 5 smoke command:

```text
PYTHONPATH=src .venv/bin/pytest tests/cli/test_cli_smoke.py -q
```

Kết quả ghi nhận sau khi thêm test: `1 passed`. Direct module help cũng đã
chạy thành công và hiện năm group `config`, `token`, `artifact`, `job`,
`admin`; `template` không được đăng ký vì route/service tương ứng chưa có.

## Frozen verification sequence

Chuỗi frozen verification ban đầu được yêu cầu trong checkout:

```bash
git status --short --untracked-files=all
git diff
git diff --check
git ls-files --others --exclude-standard
uv sync --frozen --all-groups --no-editable
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -q
uv run --no-sync nexa --help
uv run --no-sync nexa --output json config show
```

`uv` ban đầu không có trong PATH. Đã cài `uv==0.9.27` vào virtual environment
tạm ngoài repo và chạy `uv lock --check` → exit 0, rồi đặt
`UV_PROJECT_ENVIRONMENT=/tmp/nexa-b12-frozen-env` để chạy
`uv sync --frozen --all-groups --no-editable` → exit 0, cài 41 package gồm
`nexa==0.1.0`. Trong environment này, không đặt `PYTHONPATH`,
`/tmp/nexa-b12-frozen-env/bin/nexa --help` → exit 0 với năm group product,
và `/tmp/nexa-b12-frozen-env/bin/nexa --output json config show` → exit 0,
JSON có profile `default`, endpoint `http://127.0.0.1:8000`, tenant/token
`null`. `XDG_CONFIG_HOME` được chuyển sang thư mục tạm. Đây là installed
entrypoint thực trên macOS arm64 từ lockfile, không phải bằng chứng Linux.

## PostgreSQL/API integration

Repository dùng fixture guarded sau:

```bash
NEXA_TEST_DATABASE_URL='postgresql+psycopg://...@127.0.0.1:55432/nexa_b05_test_<name>' \
PYTHONPATH=src .venv/bin/pytest --run-postgres tests/api/test_http_contract.py tests/api/test_operation_matrix.py -q
```

Fixture chỉ chạy khi có `--run-postgres`, URL `postgresql+psycopg`, host
loopback/CI, database bắt đầu bằng `nexa_b05_test_`, PostgreSQL major 17; nó
drop/recreate schema public nên phải serialize và không dùng `NEXA_DATABASE_URL`.
Run B12 dùng PostgreSQL 17 container cục bộ qua `127.0.0.1:15437`, tạo mới
database riêng `nexa_b05_test_b12_cli_r05`, từ chối tái sử dụng nếu đã tồn tại,
chạy hai target pytest tuần tự, rồi `DROP DATABASE ... WITH (FORCE)` trong
`finally`. Credential được đọc cục bộ và chỉ truyền qua environment cho pytest;
không in trong report.

`tests/integration/test_cli_b12_vertical.py` chạy product CLI ở subprocess
qua Uvicorn/FastAPI production và database đã migrate. Nó kiểm tra upload replay
cùng artifact identity/checksum, submit replay cùng job identity/ETag, job và
session status, event sequence, tải
input artifact với checksum đúng/sai, admin tenant/user/membership, admin
read/write scope, tenant isolation, stale `If-Match`, ETag của membership sau
delete 204, page-size bound, và submit post-commit/pre-response crash/replay.
Test token response loss xác nhận server commit 201 trước khi process crash;
replay trả 409 `one_time_secret_unavailable`, không tạo token thứ hai. Test
`test_http_graph_result_recognition_cleanup_and_replay` của B11 tạo completed
result qua API callback có authority/fencing, rồi gọi CLI `job result` và
`job result-download` qua HTTP để so exact manifest bytes và checksum.
Kết quả guarded run: **3 passed, 2 dependency deprecation warnings**.

Để xác nhận một chuỗi cùng job, nhánh `[False]` của
`tests/docker/test_b11_vertical.py` đã được mở rộng trên database test riêng
`nexa_b05_test_b12_cli_docker_r05`. CLI upload input và submit job tenant đầu
tiên; coordinator, Linux worker container và Docker CPU workload chạy job đó.
Sau completion, CLI `job get`, `job events`, `job result`, `job result-download`
và `artifact download` đọc chính job/artifact này. Manifest provenance trỏ tới
đúng job; manifest và output tải bằng CLI khớp exact bytes/checksum với API và
CPU adapter độc lập. Run guarded tuần tự **1 passed, 2 dependency deprecation
warnings in 58.22 s**; launcher tạo mới rồi drop database test trong `finally`.
Nhánh restart `[True]` chạy trên database riêng
`nexa_b05_test_b12_cli_docker_restart_r05` và đạt **1 passed, 2 dependency
deprecation warnings in 49.70 s**; database cũng được drop trong `finally`.
Đây là Docker Desktop Linux arm64 VM trên máy Mac, không phải bare Linux/GPU,
portability hay load acceptance.
Trước các run, Docker VM cạn PID do các Caddy smoke container cũ tăng process;
đã tạm dừng `nexa_b10_smoke2-caddy-1` đang unhealthy để PostgreSQL và Docker
có thể tạo process. Container đó được giữ ở trạng thái stopped sau kiểm chứng;
không thay đổi image hay dữ liệu database của các smoke deployment.

R02: `tests/cli/test_admin_commands.py::test_admin_user_create_refuses_getpass_echo_fallback_before_reading`
dùng fallback thật của `getpass` và terminal input giả lập. `GetPassWarning`
được chuyển thành lỗi exit 2 trước khi đọc; test xác nhận secret không xuất
hiện trong output và không có request HTTP. Test này đã đỏ trước sửa và xanh
sau sửa.

## FastAPI route coverage and gaps

`src/nexa/api/app.py` hiện include auth, admin, bootstrap, artifacts, jobs và
worker routers. Những route CLI có backend wiring trong snapshot gồm:

- token list/create/revoke;
- artifact list/upload/metadata/download;
- job list/submit/get, logical session, events và result;
- admin tenant/user/membership, global/tenant policy và audit operations.

CLI surface đã được thu hẹp về các route có wiring trong FastAPI snapshot.
Template list/get, job attempts/checkpoints/logs/progress, admin job reads,
worker, allocations, fairness và recovery-events không được đăng ký nên không
thể âm thầm trả 404; các command này chỉ quay lại sau khi backend route/service
và API integration evidence tồn tại. Test `tests/cli/test_api_route_contract.py`
gọi `create_app(...).openapi()` và kiểm tra method/path contract đã khai báo cho
CLI được đăng ký trong FastAPI, đồng thời giữ danh sách surface bị loại khỏi
CLI. Các test transport của từng command kiểm tra callback thực tế forward đúng
path; `docs/contracts/openapi.yaml` vẫn là mapping source cho các phần đã đăng ký.

## Security and evidence limits

Docs/tests không chứa production secret, artifact content, database URL thật
hoặc server path; test fixtures có thể dùng chuỗi giả lập để kiểm tra redaction.
CLI gửi exact tenant/scope/idempotency/ETag headers nhưng
backend vẫn là authority. Local tests không chứng minh real camera/GPU,
Docker/multi-server portability, load/soak/chaos, checkpoint recovery,
production scheduler, Web UI, bare Linux hoặc `v1.0.0` release. B12 chỉ có
thể chuyển sang trạng thái khác sau independent Task Review với evidence phù
hợp.
