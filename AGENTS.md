# Agent instructions — nexa / PBL4

## Phạm vi và nguồn sự thật

Nexa là nền tảng single-node, self-hosted, hardware-portable cho batch AI nhiều tenant trên **một Linux server/deployment**, phân phối CPU/RAM/GPU nguyên chiếc và application checkpoint recovery. API, CLI và Web UI user/admin đều bắt buộc; một release `v1.0.0`. Future Work tại PLAN §15 không thuộc backlog hiện tại.

Thứ tự ưu tiên: **yêu cầu trực tiếp mới nhất của user → [PLAN.md](PLAN.md) đã duyệt → contract/tài liệu đã duyệt → code/tests → ghi chú cũ**. Tài liệu này dẫn xuất từ PLAN; PLAN ưu tiên nếu mâu thuẫn. Không sửa PLAN để hợp thức hóa quyết định tự chọn. Thay quyết định đã khóa cần user duyệt, cập nhật PLAN trước rồi đồng bộ contract, ADR, gates và task phụ thuộc.

Repo đã có contract B01 và bootstrap B02 hoàn chỉnh: Python/UI manifests và lockfiles, package/config/tests, React/TypeScript/Vite shell và CI read-only đã tồn tại. Chưa có product implementation, runtime, migration hoặc runtime acceptance evidence. Đọc [README](README.md), [cấu trúc](docs/project-structure.md), [contract](docs/contracts.md), [invariant](docs/invariants.md) và [acceptance](docs/acceptance.md) theo phạm vi task. Không coi bootstrap/CI xanh, target placement hoặc gate `specified` là product acceptance đã đạt.

## Kiến trúc và boundary đã khóa

- Modular monolith: API và coordinator là process riêng; worker local chỉ trao đổi control plane qua API và là thành phần duy nhất truy cập Docker socket. UI/CLI dùng cùng REST `/v1`; authorization/state machine thuộc backend.
- Python 3.12, FastAPI, SQLAlchemy 2/psycopg, Typer, `uv`; PostgreSQL 17/Alembic; Docker/Compose; React/TypeScript/Vite, Node.js LTS/`pnpm`; Caddy/TLS; Prometheus metrics/JSON logs; GitHub Actions/GHCR. Kiểm thử bằng Pytest/Hypothesis/Playwright, Ruff và TypeScript typecheck. Giữ dependency lockfile.
- Interface: `SchedulerPolicy`, `ResourceProvider`, `Executor`, `WorkloadAdapter`, `ArtifactStore`. Domain/policy không phụ thuộc transport, ORM, Docker hay PyTorch; adapter hạ tầng phụ thuộc contract. Scheduler không import PyTorch. Module ownership và chiều dependency ở tài liệu cấu trúc.
- PostgreSQL là nguồn sự thật cho state, queue, allocation, quota, lease, ledger/counter, idempotency và metadata; filesystem bền vững giữ blob bất biến. Heap/cache phải tái dựng được; metrics không quyết định correctness.

## Invariant không được phá

- Mọi query/download/composite reference phải kiểm tra tenant ownership, membership/role và principal scope. Không nhận filesystem path từ client; UI không thay authorization backend.
- Allocation **chưa release**, kể cả quarantine, phải nằm trong capacity/quota; mỗi GPU UUID chỉ thuộc tối đa một allocation chưa release. Trừ reserve CPU ≥ max(1 core, 20% host), RAM ≥ max(2 GiB, 20% host); không hardcode hardware/đường dẫn máy phát triển. Không suy concurrency từ queue size.
- Chỉ dùng weighted dominant resource-time + hard quota + aging + một reservation local. Charge allocation đang giữ, không lấy utilization; ledger tick ≤1 giây, không double-charge/reset sau restart. Aging 60 giây, reservation 120 giây đủ điều kiện; tenant selection vẫn theo weighted score. RR/WRR/DRR/DRF/FIFO chỉ là baseline simulator.
- Một job/một session logic; spec/input/digest bất biến. Tối đa một attempt được cấp quyền/job; fence tăng đơn điệu. Coordinator epoch, worker incarnation và job fence khác nhau; đổi leader không hủy attempt khỏe.
- Lease expiry dùng DB time; deadline worker/runner dùng monotonic từ lúc **gửi** renewal, trừ safety margin. Hết lease không chứng minh container đã dừng: revoke/fence, quarantine, chỉ release sau cleanup/reconcile đúng identity.
- Publish kiểm tra worker epoch, attempt, job fence, lease và desired state; unique final result/job. Cancel đã commit chặn completion sau đó. Terminal bất biến; recovery/resume giữ job/session và tạo attempt mới; manual retry tạo job/session mới với `retry_of_job_id`.
- State/event sequence/counter/idempotency commit cùng transaction; dedup submit/callback, replay trước `If-Match`. Không gọi Docker trong transaction. Khóa/CAS và uniqueness phải chặn stale leader, reaper/callback đồng thời và oversubscription.
- Blob: bounded staging → checksum/fsync/atomic rename/fsync directory → metadata commit; publish từ attempt phải fenced. Giữ ≥2 checkpoint committed; restore kiểm provenance/compatibility, fallback có event; GC không xóa blob còn được tham chiếu. At-least-once compute, chỉ một final result được công nhận; không hứa exactly-once tổng quát.

## Security, durability và quan sát

Chỉ template/image digest/adapter admin cho phép; không code/shell/image/mount tùy ý hay side effect ra hệ thống ngoài. Container non-root, read-only rootfs/input, network disabled, drop capabilities, seccomp, no-new-privileges, CPU/RAM/PID/log/scratch bounds; workload không có Docker socket hoặc worker credential. Session browser/CSRF, token scope/expiry/revocation và secret handling theo contract; không log credential/input/checkpoint. DB/schema/storage không khỏe phải fail closed; runner deadline độc lập worker, container workload restart policy `no`, reconcile trước READY. Failure scope là process/network/reboot khi storage bền vững còn nguyên, không bao gồm mất ổ đĩa/corruption. Ghi audit/event cùng transaction; metrics cardinality có giới hạn, không dùng job ID làm label.

## Cách làm việc và hoàn thành

**hiểu → phân tích → lập kế hoạch → xin approval khi cần → implement → test → review → verification → báo cáo**. Dùng Superpowers phù hợp với task: brainstorming/writing-plans khi thiết kế; TDD cho code; systematic-debugging khi lỗi; review và verification-before-completion trước claim hoàn tất. Không sao chép workflow vào docs dự án. Skill không được mở rộng quyền hoặc vượt PLAN/yêu cầu user; authorization đã có thì tiếp tục trong phạm vi.

Đọc Git status/diff trước sửa; giữ thay đổi user, không reset/ghi đè phần không thuộc task. Không tự commit/push, tạo branch hay đổi lịch sử Git nếu chưa được yêu cầu. Không tự cài/sửa skill. Cập nhật tài liệu cùng thay đổi contract; ADR theo [quy trình](docs/adr.md), không dùng ADR để vượt PLAN.

Hoàn thành task cần tất cả gate áp dụng trực tiếp pass **với evidence**, không phá invariant/gate hiện có, review diff và kiểm chứng thực tế. Release gate chưa đến giai đoạn chạy ghi không áp dụng cho task, không gán pass. Không tuyên bố Linux/GPU/benchmark đạt bằng simulator hoặc tài liệu. Test/lint/build/migration/Playwright khi có implementation theo PLAN §10/§13; cập nhật command sau khi manifest/script thực sự tồn tại.

Command bootstrap Python: `uv sync --frozen --all-groups --no-editable`, `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, `uv run --no-sync pytest -q`. Command bootstrap UI: `pnpm --dir web install --frozen-lockfile`, `pnpm --dir web run typecheck`, `pnpm --dir web run build`. Command hygiene: `git status --short --untracked-files=all`, `git diff`, `git diff --check`, `git ls-files`, `git ls-files --others --exclude-standard`, `git check-ignore -v --no-index .env`. `git diff` không gồm file untracked: phải đọc chúng riêng. Các command bootstrap không phải test/build sản phẩm.
