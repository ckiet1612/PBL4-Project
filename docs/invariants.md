# System invariants, concurrency và recovery

Dẫn xuất từ [PLAN.md](../PLAN.md) §2–§9, §14; **PLAN được ưu tiên nếu có mâu thuẫn**. Đây là constraint cho implementation và review; [contract](contracts.md) định nghĩa hành vi bên ngoài, [acceptance](acceptance.md) định nghĩa evidence cần chứng minh. Các ID INV dưới đây dùng để tham chiếu, không thay đổi thuật toán đã duyệt.

## Tenant, resource và fairness

| ID | Invariant và hệ quả kiểm chứng |
|---|---|
| INV-01 | Tenant ownership kiểm trên mọi query/download/composite reference; user có role/membership hợp lệ mới được thao tác. Worker credential chỉ cho identity local; client không cung cấp filesystem path |
| INV-02 | Tổng allocation **chưa release** ≤ capacity/quota; GPU UUID độc quyền trên allocation chưa release. Quarantine vẫn giữ tài nguyên và vẫn được charge. Lease expiry, process exit report hoặc đổi leader riêng lẻ không đủ chứng minh capacity rảnh |
| INV-03 | Capacity từ host thật trừ CPU reserve ≥max(1 core, 20% host CPU), RAM ≥max(2 GiB, 20% host RAM); không cấp âm/vượt thực tế. Thiếu capability bị chặn; configured pool tạm offline không đồng nghĩa pool bất khả thi |
| INV-04 | Fairness charge dominant share của allocation đang giữ theo thời gian/weight, không charge utilization tự khai báo. Account khi allocation/release và tick ≤1 giây; restart khôi phục từ timeline không double-charge. Tenant mới có nhu cầu có virtual score ít nhất bằng floor hiện tại |
| INV-05 | Chọn tenant theo virtual score tăng dần; hòa theo dominant share/weight rồi ready sequence cũ nhất. Trong tenant: effective priority giảm dần, rồi FIFO. Priority 0/1/2, default 1; mỗi 60 giây chờ **đủ điều kiện** tăng 1, tối đa 2 |
| INV-06 | Sau 120 giây đủ điều kiện, bảo vệ oldest feasible job của tenant được chọn bằng cùng fairness score. Chỉ một reservation local; chưa fit thì dừng dispatch mới để drain, đủ thì dispatch và bỏ reservation. Bỏ reservation mất hiệu lực phải ghi reason; không đổi thành global FIFO hoặc starvation vô hạn |
| INV-07 | Admission/quota/rate/counter bền vững, atomic và replay-safe; paused vẫn chiếm outstanding. Giảm policy xuống dưới allocation/counter đang giữ bị từ chối; phải drain trước, không vay vượt hard quota |

Fairness theo PLAN §5: `d_i = max(A_i,r / C_r)` trên resource có capacity dương; tăng `V_i` bằng `d_i × elapsed / w_i`, với `w_i > 0`. Candidate tick giới hạn 16/tenant **cộng** oldest eligible cho reservation; head theo tenant/priority, indexed aging/retry và cursor công bằng, heap tái dựng từ DB. Không scan/sort full queue trên mỗi quyết định; độ phức tạp mục tiêu khoảng `O(TK + T log T)` cộng index lookup.

| Policy mặc định (có version) | Giá trị |
|---|---|
| Outstanding | Global 100.000, tenant 2.000, user 2.000; gồm paused |
| Attempt đồng thời | ≤2/tenant, ≤1/user và tiếp tục bị capacity/request giới hạn |
| Tenant resource | CPU/RAM ≤50% capacity cấp job; GPU ≤1 nếu host có GPU; admin điều chỉnh theo bài đo |
| Submit rate | Tenant 5/s burst 20; user 2/s burst 10; durable token bucket, replay không tiêu thêm |

Không suy số container thực thi từ 100.000 queue hay 100 client. Reservation chống starvation dưới điều kiện worker/storage khỏe, weight dương, quota ổn định, request vừa máy và runtime hữu hạn; không tự tuyên bố upper bound chờ chưa chứng minh.

## Quyền thực thi và transaction

| ID | Invariant và điểm linearization cần giữ |
|---|---|
| INV-08 | Job/session logic và immutable spec tồn tại qua recovery; tối đa một attempt được cấp quyền/job. Job fence tăng đơn điệu; terminal bất biến. Manual retry tạo identity mới, khác automatic retry/resume |
| INV-09 | Coordinator leadership lease/epoch, worker incarnation và job fence là ba miền khác nhau. Commit dispatch recheck leader lease/epoch, policy/job version, quota/capacity/GPU UUID/fence dưới khóa; tạo attempt/allocation nguyên tử. Đổi coordinator không vô hiệu hóa attempt khỏe |
| INV-10 | State, event sequence, counter và idempotency cùng transaction; unique keys/CAS/row locks làm một transition thắng khi callback/reaper/control đua nhau. Response `202` sau commit; không gọi Docker trong transaction |
| INV-11 | Mọi publish công nhận metadata phải kiểm epoch/attempt/fence/lease/desired state; unique final result/job và callback dedup. Cancel commit thắng mọi completion đến sau; compute có thể lặp nhưng catalog không công nhận result trùng |
| INV-12 | DB time quyết lease expiry; worker/runner lấy deadline monotonic từ thời điểm gửi renew trừ margin, response chậm không kéo deadline. Runner độc lập agent; workload không sửa runner/lease channel. Singleton local và reconcile/dừng container cũ trước worker READY |
| INV-13 | Hết lease → revoke/fence/attempt lost/recovering và quarantine; cleanup xác minh identity + container đã dừng mới release. Host không phản hồi thì chờ, không cấp lại capacity bị giữ; fail closed khi DB/renew không commit được |

| Thời gian/ngân sách mặc định | Giá trị theo PLAN §9 |
|---|---|
| Worker heartbeat / suspect / unavailable | 5 / 15 / 30 giây |
| Attempt lease / renew / safety margin | 45 / 5 / 5 giây |
| Coordinator lease / renew / recovery scan | 15 / 5 / 1 giây |
| Startup / runtime / graceful stop | Tối đa 30 / 300 / 5 giây mỗi attempt; hết grace kill |
| Checkpoint interval | Default 30 giây, template cho phép 5–60 giây; thêm checkpoint khi pause |
| Retry hạ tầng | 2 lần sau attempt đầu; backoff `min(30, 2^(retry_no-1)) + jitter[0,1]` giây |

Timeout, invalid input và OOM không auto retry cùng spec; resume không dùng retry lỗi hạ tầng và không kéo dài runtime attempt đang chạy. Drain ngừng allocation mới và chờ; disable ngừng allocation mới, yêu cầu dừng/fence và giữ quarantine đến cleanup. UI không nói “đã dừng” trước bằng chứng cleanup.

## Blob, checkpoint và recovery

| ID | Invariant và failure boundary |
|---|---|
| INV-14 | Bounded stream vào staging theo tenant (và attempt với worker upload) → verify checksum/size → file fsync → atomic rename → directory fsync → metadata transaction; publish từ attempt phải fenced. Blob durable trước khi metadata được công nhận; crash trước commit chỉ có orphan, không có committed partial blob |
| INV-15 | Giữ ≥2 checkpoint committed gần nhất. Restore chọn bản mới nhất khớp checksum/input/schema/adapter/image/environment; corrupt thì thử bản trước. Không còn bản hợp lệ chỉ restart input nếu adapter restart-safe, ghi event; không deserialize pickle tùy ý |
| INV-16 | Watermark/byte quota được kiểm trước admission/dispatch/upload và trong stream; request body, log, artifact/file/tổng tenant, scratch đều có hard limit. GC chỉ xóa staging/orphan đủ tuổi, không còn upload hợp lệ và không được metadata tham chiếu |
| INV-17 | Recovery giữ accepted job/session/ledger/counter; DB là nguồn state, heap/cache tái dựng. Backup/relocation cần snapshot DB và artifact nhất quán sau drain/pause, stop execution và khóa ghi; host cũ giữ execution tắt. Restore reconcile/incarnation mới, kiểm checksum/compatibility rồi mới mở admission |

RPO=0 cho metadata đã xác nhận **trong failure scope** khi durability PostgreSQL/storage được giữ. Một server tắt không có availability; retry không bảo vệ khỏi mất ổ/corruption. Di chuyển/upgrade có downtime; job không vừa capacity/quota mới hoặc không tương thích bị chặn có reason. Không dùng snapshot cũ để hứa rollback không mất dữ liệu mới.

## Security, observability và fault tolerance

| ID | Constraint |
|---|---|
| INV-18 | Admin allowlist image digest/template/adapter; input read-only chuẩn bị offline; không code/shell/mount/image tùy ý hoặc external side effects. Container UID non-root, rootfs read-only, network disabled, capabilities dropped, no-new-privileges/seccomp, không Docker socket/host namespace/control-plane credential |
| INV-19 | CPU quota, hard RAM, PID/log/runtime/scratch bounds có hiệu lực trên Linux; scratch tmpfs tính trong RAM. Worker đặc quyền Docker là trusted component; workload restart policy `no`, worker agent có thể restart qua Compose |
| INV-20 | Browser password Argon2id + opaque server session `HttpOnly/Secure/SameSite`, CSRF, expiry/revocation; không credential localStorage. CLI scoped expiring opaque token hash trong DB; bootstrap admin/worker từ secret, không default password. Chặn traversal/symlink/forged worker/replay/oversize. Caddy/TLS phục vụ UI/proxy API; DB/metrics/Docker socket chỉ trong mạng quản trị |
| INV-21 | JSON log có request/job/attempt/worker ID + reason nhưng không token/input/checkpoint; không dùng job ID làm metric label. Metrics gồm admission/reject, queue/age, schedule/allocation/reservation, resource-time/Jain, resource/lease/retry/checkpoint/restore/stale/quarantine/storage/checksum. Audit/event atomic; Prometheus hỏng không đổi correctness |

Ma trận lỗi bắt buộc nằm tại PLAN §9 và gates ACC-15–ACC-31: crash compute/upload/commit; API/coordinator kill quanh commit/response; old leader quay lại; DB partition/delayed heartbeat; cancel/pause/complete/reaper race; timeout/OOM/log flood; disk full/corrupt checkpoint; toàn stack reboot/backup restore. Evidence cần timeline quyền/compute overlap/allocation, rejected stale writes, accepted-ID và counter reconciliation, không chỉ log “restarted successfully”.
