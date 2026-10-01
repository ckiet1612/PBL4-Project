# Nexa Web UI cho user (B17)

Trạng thái: **B17 đã triển khai, chờ Task Review độc lập**. Tài liệu này mô tả
bộ khung UX chức năng (low-fidelity) cho người dùng tenant (`MEMBER`,
`TENANT_ADMIN`) trong `web/`. UI chỉ là client của REST `/v1` chung với CLI:
authorization, state machine, quota và scheduler thuộc backend. Admin UI thuộc
B18, log pipeline thuộc B17-R01 (chưa có), GPU thuộc B23, Compose/UI image sản
phẩm thuộc B21/B25.

Thứ tự ưu tiên khi thiết kế: Rõ ràng > Mật độ thông tin > Tiện lợi > Hình thức.

## Kiến trúc thông tin

### 1. Sitemap

```text
/login                                  Đăng nhập (?next=<path nội bộ>)
/                                       Chuyển tới /t/<tenant mặc định>/jobs;
                                        không có membership → trang giải thích
/t/:tenantId/jobs                       Jobs (trang chủ) ?state=&template=&after=&cursor=
/t/:tenantId/jobs/new                   Tạo job ?template=
/t/:tenantId/jobs/:jobId                Chi tiết job ?tab=progress|result|events|logs|config
/t/:tenantId/sweeps/:sweepId            Kết quả sweep (không có trên nav)
/t/:tenantId/data                       Dữ liệu (artifact) ?kind=&cursor=
/account/tokens                         Token CLI (menu tài khoản)
*                                       "Không tìm thấy trang"
```

- Tenant mặc định là membership đầu tiên theo thứ tự server trả về
  (`GET /v1/auth/session`).
- Tenant nằm trên URL để link chia sẻ và nhiều tab không lẫn tenant, và không
  cần lưu tenant vào storage (UX-A01).
- `/account/tokens` không có tenant vì token CLI thuộc user, không thuộc tenant.
  Nav trên trang này quay về tenant đang dùng gần nhất trong phiên SPA (chỉ giữ
  trong memory); sau khi tải lại trang thì về tenant mặc định (UX-A02).
- Tenant trên URL không có trong memberships → trang "Không có quyền truy cập
  tenant này" ngay trên route đó, không gọi API tenant. Backend vẫn là nơi kiểm
  tra thật (403/404).

### 2. Navigation

| Vùng | Nội dung | Ghi chú |
|---|---|---|
| Global nav | `Jobs`, `Dữ liệu` | Không có Dashboard: user không có API tổng hợp và không được đếm bằng cách tải toàn bộ queue. |
| Header trái | Tên sản phẩm `Nexa` (link về `/`) | Không logo, không branding. |
| Bộ chọn tenant | Chỉ hiện khi có ≥ 2 membership; mỗi mục là ID rút gọn + vai trò | Session không có tên tenant (B17-R05). Đổi tenant giữ nguyên khu (Jobs/Dữ liệu), bỏ filter/cursor. |
| Menu tài khoản | Nút `Tài khoản` → vai trò trong tenant hiện tại, user ID rút gọn, `Token CLI`, `Đăng xuất` | Menu thả xuống, đóng bằng Esc. |
| Breadcrumb | `Jobs › Tạo job`, `Jobs › Job <id>`, `Jobs › Sweep <id>`, `Tài khoản › Token CLI` | Chỉ ở trang con. |
| Contextual action | `Tạo job` (primary, trang Jobs), `Tải lên tệp` (primary, trang Dữ liệu), control trên header chi tiết job | Không đặt control trên dòng danh sách. |
| Khu quản trị (B18) | Chỗ trống sau `Dữ liệu` trong global nav (component `GlobalNav` có một slot cuối) | B17 không render link hay trang giữ chỗ nào. |

Dưới 768 px: global nav thành một thanh ngang ngay dưới header (tự xuống dòng),
bộ chọn tenant và menu tài khoản xuống dòng thứ hai của header. Không dùng
hamburger để tránh thêm trạng thái ẩn (UX-A11).

### 3. Danh sách trang

Mọi trang có vùng lỗi chung (`ErrorPanel`: thông điệp tiếng Việt, request_id
sao chép được, nút `Thử lại` khi thao tác đọc) và trạng thái `Đang tải…`.

#### 3.1 Đăng nhập (`/login`)
- Mục đích: vào hệ thống bằng tài khoản do quản trị viên tạo.
- Nhiệm vụ chính: nhập username/password.
- Thông tin chính: một câu giới thiệu Nexa ("Nền tảng chạy batch AI trên
  container cho nhiều tenant"), form.
- Primary: `Đăng nhập`. Secondary: không.
- Trạng thái: loading (nút khóa, "Đang đăng nhập…"); lỗi chung "Tên đăng nhập
  hoặc mật khẩu không đúng" (không tiết lộ user tồn tại); 429 đếm ngược theo
  Retry-After, nút khóa đến hết; 403 khi WRITE_FROZEN "Hệ thống đang ở chế độ
  chỉ đọc; chỉ quản trị viên hệ thống được đăng nhập"; phiên hết hạn hoặc bị thu
  hồi → thông báo "Phiên đăng nhập đã hết hạn. Đăng nhập lại để tiếp tục".
- Bước tiếp theo: về `next` (chỉ path nội bộ) hoặc Jobs của tenant mặc định.
  `next` bị bỏ nếu path **sau chuẩn hóa** bắt đầu bằng `//` (ví dụ `/.//x`,
  `/%2e%2e//x`, `/a/..//x`) hoặc là `/login` (không phân biệt hoa thường).

#### 3.2 Không có tenant (`/` khi memberships rỗng)
- Mục đích: giải thích cho user (kể cả `SYSTEM_ADMIN`) chưa thuộc tenant nào.
- Thông tin: "Tài khoản chưa thuộc tenant nào. Liên hệ quản trị viên để được
  thêm vào tenant." Với `SYSTEM_ADMIN`: thêm câu "Khu quản trị hệ thống sẽ có ở
  bản sau." (B18).
- Primary: không; Secondary: `Token CLI`, `Đăng xuất` (trong menu tài khoản).

#### 3.3 Jobs (`/t/:tenantId/jobs`)
- Mục đích: xem job của tenant và đi tới job cần theo dõi.
- Nhiệm vụ chính: tìm job, mở chi tiết, tạo job mới.
- Thông tin chính: bảng `Job` (ID rút gọn, nút sao chép ID đầy đủ), `Template`
  (display_name + version), `Trạng thái` (badge chữ + lý do chờ), `Người tạo`
  ("Bạn" hoặc ID rút gọn), `Tạo lúc`. Job là retry có nhãn "Chạy lại của <id>".
  Ghi rõ "Mới nhất trước" (server cố định `created_at DESC`).
- Filter (trên URL): trạng thái (một giá trị), template, "Tạo sau" (thời điểm
  địa phương → UTC). Nút `Xóa bộ lọc`.
- Primary: `Tạo job`. Secondary: `Làm mới`, `Trang sau`, `Trang trước`,
  `Về trang đầu`.
- Trạng thái:
  - loading: bảng giữ khung, dòng "Đang tải…";
  - empty (tenant chưa có job, không filter) = người dùng lần đầu: hướng dẫn hai
    bước "1. Chuẩn bị dữ liệu (trang Dữ liệu) → 2. Tạo job";
  - không có kết quả lọc: "Không có job khớp bộ lọc" + `Xóa bộ lọc`;
  - error: ErrorPanel; cursor bị server từ chối (`invalid_cursor`, hoặc
    `validation_failed` khi URL có `cursor=` sai độ dài 16–2048) → về trang đầu
    kèm thông báo "Vị trí trang không còn hợp lệ, đã quay về trang đầu";
  - permission denied: "Bạn không có quyền xem job của tenant này";
  - disabled: `Trang trước` disabled ở trang đầu, `Trang sau` disabled khi hết.
- Cập nhật: chỉ trang đầu tự làm mới khi tab hiển thị (mục Polling); trang sau
  không tự làm mới.
- Bước tiếp theo: mở chi tiết job, hoặc `Tạo job`.

#### 3.4 Tạo job (`/t/:tenantId/jobs/new`)
- Mục đích: gửi một job (hoặc một sweep) cho template đã đăng ký.
- Nhiệm vụ chính: chọn template → dữ liệu → tham số → tài nguyên → gửi.
- Nhóm theo thứ tự: `1. Template` (radio; display_name, version, "CPU", "Hỗ trợ
  tạm dừng/tiếp tục" nếu checkpointable) → `2. Dữ liệu đầu vào` (picker lọc theo
  kind/media type phù hợp template, B17-R06; `Tải lên tệp mới` ngay trong form;
  batch-inference thêm `Model`) → `3. Tham số` (sinh từ `parameter_schema`) →
  `4. Tài nguyên` (CPU theo core, RAM theo GiB) → `5. Nâng cao` (thu gọn:
  priority, giới hạn thời gian, chu kỳ checkpoint, chế độ sweep cho
  pytorch-cifar10-cnn).
- Primary: `Gửi job` (hoặc `Gửi sweep (N job)`). Secondary: `Hủy` (về Jobs).
- Trạng thái:
  - loading template: "Đang tải template…";
  - empty: không có template enabled → "Chưa có template nào được bật. Liên hệ
    quản trị viên." (không có form);
  - empty dữ liệu: picker ghi "Chưa có tệp phù hợp" + `Tải lên tệp mới`;
  - lỗi validate inline dưới từng field (required, min/max, kiểu);
  - lỗi server: gắn vào nhóm nếu xác định được (tham số/tài nguyên/dữ liệu),
    không thì ở đầu form; luôn kèm request_id; form giữ nguyên dữ liệu;
  - disabled: nút gửi khóa khi đang gửi ("Đang gửi…"), khi đang đếm ngược
    Retry-After sau `rate_limited` (429), và khi sweep > 100 tổ hợp. Còn lỗi inline thì bấm
    gửi không gửi request: form hiện mọi lỗi, mở `Nâng cao` nếu lỗi nằm ở đó và
    đưa focus tới tóm tắt lỗi;
  - permission denied: ErrorPanel "Không có quyền tạo job trong tenant này".
- Bước tiếp theo: 202 → chi tiết job + thông báo "Đã nhận job"; 207 sweep →
  trang sweep.

#### 3.5 Chi tiết job (`/t/:tenantId/jobs/:jobId`)
Thứ bậc: tiêu đề/ID → trạng thái → primary actions → tóm tắt → nội dung chính
(tab) → metadata → hành động nguy hiểm.
- Mục đích: theo dõi và điều khiển một job, lấy kết quả.
- Header: `display_name` của template + job ID (sao chép được); badge trạng thái;
  khi `desired_state` khác `state` thì thêm dòng giải thích (ví dụ "Đã yêu cầu
  hủy, chờ hệ thống xác nhận dừng"); lý do chờ bằng lời; primary actions theo
  ma trận (mục 8): `Tạm dừng`, `Tiếp tục`, `Chạy lại`. `Hủy job` đặt tách riêng
  (vùng "Hành động nguy hiểm").
- Tóm tắt: tạo lúc/cập nhật lúc, người tạo, template@version, CPU (core), RAM
  (GiB), giới hạn thời gian, số lần thử lại `retry_count/max_retries`, link
  `Chạy lại của <job>`; tiến độ (fraction %, step, epoch, item_cursor khi có,
  thời điểm báo cáo, "báo cáo bởi lần chạy #n"); "Chưa có dữ liệu tiến độ" khi
  `available=false`.
- Tab (deep link `?tab=`), mặc định: `Kết quả` khi SUCCEEDED, còn lại `Tiến trình`.
  - `Tiến trình`: tiến độ chi tiết; bảng lần chạy (số thứ tự, trạng thái, bắt
    đầu/kết thúc, failure_class; ID kỹ thuật trong `<details>`); bảng checkpoint
    (sequence, state, thời điểm, lần chạy nguồn); checkpoint đang dùng để khôi
    phục (`restore_checkpoint_id`) được đánh dấu.
  - `Kết quả`: có result → bảng tệp (logical_name, media type, kích thước,
    checksum rút gọn, `Tải xuống`) + metrics dạng key/value văn bản; chưa có →
    giải thích theo trạng thái ("Job chưa hoàn tất", "Job đã hủy nên không có kết
    quả", "Job thất bại; xem Sự kiện và Lần chạy").
  - `Sự kiện`: bảng theo sequence (loại, lý do, tác nhân, thời điểm);
    `Tải thêm sự kiện` bằng `after_sequence` (tối đa 100 mỗi lần).
  - `Log`: "Hệ thống chưa hỗ trợ xem log của job" (B17-R01); không gọi API,
    không lỗi đỏ.
  - `Cấu hình`: spec chỉ đọc (tham số, link dữ liệu vào/model, priority,
    chu kỳ checkpoint), `spec_checksum`.
- Metadata (`<details>` thu gọn): job_id, session_id, version/ETag, job_fence,
  event_sequence.
- Trạng thái: loading; 404 → "Không tìm thấy job hoặc bạn không có quyền xem"
  (không phân biệt tenant khác); lỗi đọc từng khu vực hiện tại chỗ; MEMBER xem job
  của người khác → không có control, một dòng "Chỉ người tạo job hoặc quản trị
  viên tenant được điều khiển job này"; disabled `Tạm dừng` kèm lý do khi
  template không checkpointable; 412 → tải lại job, thông báo "Job vừa thay đổi
  (trạng thái mới: …). Kiểm tra rồi thử lại."
- Bước tiếp theo: chờ/điều khiển; SUCCEEDED → tải kết quả; FAILED → `Chạy lại`.

#### 3.6 Kết quả sweep (`/t/:tenantId/sweeps/:sweepId`)
- Mục đích: xem job con của một sweep được nhận hay bị từ chối.
- Thông tin: sweep ID, tạo lúc, tổng số job con, số được nhận/bị từ chối; bảng
  job con (index, parameter_hash rút gọn, trạng thái nhận, lỗi, link job).
  API không trả giá trị tham số của từng job con (B17-R10/B17-R16); tham số xem
  ở tab `Cấu hình` của job con.
- Primary: không; Secondary: link job con, `Trang sau`/`Trang trước`.
- Trạng thái: loading; 404; empty không xảy ra (child_count ≥ 1).
- Không có trên nav và không có trang liệt kê sweep (API không có). Chỉ vào
  được ngay sau khi gửi hoặc qua link/URL đã lưu.

#### 3.7 Dữ liệu (`/t/:tenantId/data`)
- Mục đích: quản lý tệp đầu vào (INPUT/DATASET/MODEL) và tải artifact.
- Thông tin: bảng `Loại`, `Media type`, `Kích thước`, `Trạng thái`, `Checksum`
  (rút gọn), `Tạo lúc`, `ID`; filter theo loại (trên URL); phân trang cursor.
- Primary: `Tải lên tệp` (mở dialog). Secondary: `Tải xuống` trên từng dòng,
  `Trang sau`/`Trang trước`/`Về trang đầu`.
- Trạng thái: empty/lần đầu "Chưa có dữ liệu. Tải lên tệp đầu vào để tạo job";
  không có kết quả lọc; lỗi; permission denied; tệp > 256 MiB → hướng dẫn lệnh
  CLI, không đọc tệp; "Đang tính SHA-256…", "Đang tải lên…".
- Bước tiếp theo: sau khi tải lên → `Tạo job với tệp này`.

#### 3.8 Token CLI (`/account/tokens`)
- Mục đích: tạo và thu hồi token cho CLI.
- Thông tin: bảng tên, quyền, tạo lúc, hết hạn, trạng thái (Còn hiệu lực /
  Hết hạn / Đã thu hồi); hướng dẫn dùng token với CLI.
- Primary: `Tạo token` (form trên trang: tên, quyền checkbox, hạn 1/7/30 ngày).
  Secondary: `Thu hồi` (qua ConfirmDialog).
- Trạng thái: empty "Chưa có token"; raw token hiện đúng một lần trong khung
  cảnh báo + `Sao chép`, mất khi rời/tải lại trang; `one_time_secret_unavailable`
  → giải thích và gợi ý thu hồi rồi tạo token mới; quyền `admin:*` chỉ hiện với
  `SYSTEM_ADMIN`.

#### 3.9 Không tìm thấy trang (`*`) và không có quyền tenant
- "Không tìm thấy trang" + link về Jobs.
- "Không có quyền truy cập tenant này" + link về tenant mặc định.

### 4. Component chính

Chỉ gồm component dùng ≥ 2 chỗ hoặc có hành vi phức tạp.

| Component | Dùng ở | Hành vi |
|---|---|---|
| `AppLayout` (header, `GlobalNav`, `TenantPicker`, `AccountMenu`) | mọi trang sau đăng nhập | slot quản trị cho B18 không render |
| `StatusBadge` | Jobs, chi tiết, sweep | nhãn chữ từ `labels.ts`, class theo nhóm ngữ nghĩa |
| `ErrorPanel` | mọi trang/khu vực | thông điệp ánh xạ, request_id sao chép, `Thử lại` |
| `EmptyState` | Jobs, Dữ liệu, Token, Kết quả | tiêu đề + hướng dẫn + action |
| `Pager` | Jobs, Dữ liệu, Token, Sweep | stack cursor trong memory: sau/trước/về đầu |
| `ConfirmDialog` | hủy job, thu hồi token, control | `<dialog>` modal, giữ focus, Esc đóng, ô lý do |
| `ShortId` / `CopyButton` | mọi bảng ID, request_id, token | rút gọn 8 ký tự, sao chép đầy đủ |
| `Time` | mọi bảng | giờ địa phương có múi giờ, UTC gốc trong `title` |
| `ArtifactPicker` + `UploadPanel` | Tạo job, Dữ liệu | lọc kind/media; SHA-256; giới hạn 256 MiB |
| `DownloadButton` | Kết quả, Dữ liệu | fetch → Blob → kiểm ETag/checksum → object URL |
| `Tabs` | chi tiết job | tab trên URL `?tab=`, điều khiển bằng phím mũi tên |

### 5. User workflow (bước → trang → API)

| ID | Bước | Trang | API |
|---|---|---|---|
| W-01 | Mở URL → chưa có phiên → đăng nhập → quay lại đúng trang | `/login?next=` → trang cũ | `GET /v1/auth/session` (401) → `POST /v1/auth/login` → `GET /v1/auth/session` |
| W-01b | Đăng xuất / phiên hết hạn | menu tài khoản → `/login` | `POST /v1/auth/logout` (204/401 đều về login); bất kỳ 401 → `/login?next=` |
| W-02 | Người dùng mới: Jobs trống → `Dữ liệu` → tải lên → `Tạo job với tệp này` | Jobs → Dữ liệu → Tạo job | `GET /v1/jobs` → `POST /v1/artifacts` |
| W-03 | Chọn template → dữ liệu (hoặc tải lên trong form) → tham số → tài nguyên → nâng cao → gửi → chi tiết (lý do chờ) | Tạo job → chi tiết | `GET /v1/templates`, `GET /v1/artifacts?kind=`, `POST /v1/artifacts`, `POST /v1/jobs` |
| W-04 | Lọc danh sách → mở chi tiết → theo dõi tới terminal | Jobs → chi tiết | `GET /v1/jobs?state=&template_id=&created_after=`; `GET /v1/jobs/{id}`, `/progress`, `/events?after_sequence=`, `/attempts`, `/checkpoints`, `/result` |
| W-05 | Tạm dừng/tiếp tục/hủy (xác nhận) → thấy PAUSING/CANCELLING tới khi backend xác nhận | chi tiết | `POST /v1/jobs/{id}/pause|resume|cancel` (If-Match, Idempotency-Key) |
| W-06 | Job FAILED → `Chạy lại` → chọn từ đầu hoặc checkpoint COMMITTED → job mới | chi tiết cũ → chi tiết mới | `GET /checkpoints` → `POST /v1/jobs/{id}/retry` (body job mới) |
| W-07 | SUCCEEDED → tab Kết quả → tải tệp | chi tiết `?tab=result` | `GET /result` → `GET /v1/artifacts/{manifest}/content` → `GET /v1/artifacts/{file}/content` |
| W-08 | Xem/lọc artifact → tải lên → tải xuống | Dữ liệu | `GET /v1/artifacts?kind=`, `POST /v1/artifacts`, `GET /content` |
| W-09 | Tạo job pytorch → Nâng cao → bật sweep → thêm chiều → thấy số job con → gửi → trang sweep | Tạo job → sweep | `POST /v1/sweeps` (207) → `GET /v1/sweeps/{id}` |
| W-10 | Tạo token → sao chép một lần → danh sách → thu hồi | Token CLI | `GET/POST /v1/tokens`, `DELETE /v1/tokens/{id}` |
| W-11 | Đổi tenant → danh sách theo tenant mới; mở link chia sẻ → đúng tenant | mọi trang | header `X-Nexa-Tenant-Id` lấy từ URL |
| W-12 | TENANT_ADMIN mở job của thành viên khác → điều khiển | chi tiết | như W-05 |
| W-13 | Lỗi hệ thống: quota, tạm ngừng nhận job, quá tải, không tìm thấy, xung đột phiên bản | tại chỗ | ánh xạ lỗi (mục Ánh xạ lỗi) |

### 6. Workflow của TENANT_ADMIN trong B17

TENANT_ADMIN dùng cùng các trang với MEMBER. Khác biệt duy nhất: trên chi tiết
job của thành viên khác, control hiện như với job của chính mình (W-12). Nhãn
`Người tạo` cho biết job của ai; dialog xác nhận ghi thêm "Job này do người
khác tạo". Quản trị user/tenant/quota/worker/fairness thuộc B18; B17 không hiện
link quản trị nào.

### 7. Chức năng được gộp và không lên navigation

| Chức năng | Cách gộp / vào | Lý do |
|---|---|---|
| Template | Chọn trong bước 1 của Tạo job | Template chỉ có display_name, capability và tham số; trang catalog riêng không thêm thông tin. |
| Tạo job | Primary action của Jobs và empty state | Tạo job bắt đầu từ danh sách. |
| Upload trong form | `Tải lên tệp mới` trong bước 2 | Không rời trang, giữ giá trị đã nhập. |
| Sweep | Bật trong `Nâng cao` của Tạo job (pytorch) | Sweep = base spec + chiều tham số; dùng lại form. |
| Session, attempt, checkpoint, progress | Tab `Tiến trình` của chi tiết job | Cùng câu hỏi "job đang ở đâu". Session chỉ là metadata. |
| Result manifest | Tab `Kết quả` | User cần tệp và metrics, không cần manifest thô. |
| Kết quả sweep | Không lên nav; vào sau khi gửi sweep | Không có API liệt kê sweep (B17-R10). |
| Token CLI | Menu tài khoản | Việc làm một lần, thuộc user. |
| Log | Tab `Log` với trạng thái chưa hỗ trợ | B17-R01. |

### 8. Ma trận hành động

UI chỉ dùng ma trận để hiện/ẩn; backend quyết định (docs/contracts/state-machines.md).
"Được điều khiển" = vai trò `TENANT_ADMIN` trong tenant hiện tại **hoặc**
`job.user_id == session.user_id`. Khi không được điều khiển: ẩn mọi control,
hiện dòng "Chỉ người tạo job hoặc quản trị viên tenant được điều khiển job này".

| state | desired_state | Hủy | Tạm dừng | Tiếp tục | Chạy lại |
|---|---|---|---|---|---|
| QUEUED | RUNNING | hiện (→ CANCELLED) | ẩn | ẩn | ẩn |
| DISPATCHING | RUNNING | hiện (→ CANCELLING) | ẩn | ẩn | ẩn |
| RUNNING | RUNNING | hiện (→ CANCELLING) | hiện nếu checkpointable; không thì **disabled** "Template này không hỗ trợ tạm dừng" | ẩn | ẩn |
| PAUSING | PAUSED | hiện (→ CANCELLING) | ẩn ("Đã yêu cầu tạm dừng, chờ checkpoint") | ẩn | ẩn |
| PAUSED | PAUSED | hiện (→ CANCELLED) | ẩn | hiện | ẩn |
| RECOVERING | RUNNING/PAUSED | hiện (→ CANCELLING) | ẩn | ẩn | ẩn |
| RETRY_WAIT | RUNNING | hiện (→ CANCELLED) | ẩn | ẩn | ẩn |
| bất kỳ đang hoạt động | CANCELLED | ẩn ("Đã yêu cầu hủy, chờ xác nhận dừng") | ẩn | ẩn | ẩn |
| CANCELLING | CANCELLED | ẩn | ẩn | ẩn | ẩn |
| SUCCEEDED / CANCELLED | – | ẩn | ẩn | ẩn | ẩn |
| FAILED | – | ẩn | ẩn | ẩn | hiện (chọn từ đầu hoặc checkpoint COMMITTED) |

- `Tiếp tục` luôn hiện khi PAUSED; nếu template không restart_safe và không có
  checkpoint tương thích, server trả 422 và UI hiện thông điệp đó (không đoán).
- Pause với template không checkpointable dùng **disabled kèm lý do** thay vì
  ẩn, để user biết vì sao không có (UX-A07). Ba template v1 đều checkpointable.
- Hủy luôn qua ConfirmDialog: "Job đang chạy sẽ được yêu cầu dừng; kết quả của
  lần chạy này không được công nhận; không hoàn tác được."
- Mỗi control: một Idempotency-Key cho một lần xác nhận; `If-Match` là ETag của
  lần `GET job` mới nhất; trong một tab chỉ một mutation/job tại một thời điểm.

### 9. Giả định UX

| ID | Giả định | Lý do | Cách đổi |
|---|---|---|---|
| UX-A01 | Tenant trên URL `/t/:tenantId/...` | Link chia sẻ, nhiều tab không lẫn; không cần storage | Đổi bảng route trong `App.tsx` và `useTenant()` (`auth/guards.tsx`) |
| UX-A02 | Tenant mặc định = membership đầu tiên; tenant gần nhất chỉ nhớ trong memory | Không dùng storage (khuyến nghị của task) | Thêm key storage không nhạy cảm và ghi vào mục Storage |
| UX-A03 | Page size cố định 25 cho Jobs/Dữ liệu/Sweep, 50 cho Token, 100 cho sự kiện, lần chạy và checkpoint | Đủ mật độ, không vượt trần 100 | Hằng số `PAGE_SIZE` trong `api/limits.ts` |
| UX-A04 | Tài nguyên mặc định 1 core, 1 GiB RAM; priority 1, giới hạn 300 s, checkpoint 30 s (theo default của OpenAPI) | Vừa với bounds của cả 3 template v1; server không tự điền default | `features/submit/defaults.ts` |
| UX-A05 | Tham số template không có default (`default: null` trong cả 3 template) → field trống, placeholder ghi giới hạn | Không bịa default phía client | Khi template có default, form tự dùng |
| UX-A06 | Retry mặc định "Chạy lại từ đầu" (`checkpoint_id: null`); checkpoint COMMITTED là lựa chọn thêm | Luôn hợp lệ; checkpoint phải qua proof của server (422 nếu không) | `features/jobs/useJobControls.tsx` |
| UX-A07 | Pause không hỗ trợ → disabled kèm lý do | Rõ ràng hơn ẩn | Ma trận `actions.ts` |
| UX-A08 | Tab mặc định theo trạng thái (SUCCEEDED → Kết quả) | Ít click nhất cho việc chính của từng giai đoạn | `defaultTab()` |
| UX-A09 | Không tự đi hết các trang, không tổng hợp số liệu | PLAN/contract cấm tải toàn bộ queue | – |
| UX-A10 | Giới hạn upload/download qua trình duyệt 256 MiB | SHA-256 phải tính trước; blob nằm trong RAM trình duyệt | `api/limits.ts` |
| UX-A11 | Mobile dùng nav ngang xuống dòng, không hamburger | Ít trạng thái ẩn, dễ test | CSS `layout.css` |
| UX-A12 | Sweep chỉ bật cho `pytorch-cifar10-cnn`; giá trị mỗi chiều cách nhau bởi `;` (nếu có) hoặc `,` — ví dụ `0,1; 0,01` hoặc `1, 2, 3` | Contract chỉ nhận TrainingJobSpec; `;` giữ được dấu phẩy thập phân | `features/submit/sweep.ts` |
| UX-A13 | Trang Dữ liệu mặc định hiện mọi loại artifact server trả (kể cả checkpoint/result) | API không có "chỉ tệp của user"; lọc theo loại có sẵn | Đặt `kind` mặc định trên URL |
| UX-A14 | Người tạo hiển thị "Bạn" hoặc user ID rút gọn | Session/Job không có tên (B17-R05) | Khi contract có tên, thay trong `ShortId` |
| UX-A15 | Sau khi gửi control, UI hiện trạng thái trả về từ response rồi polling nhịp nhanh | Không suy trạng thái từ thời gian hay từ 202 | `usePolled().refresh()` → `Poller.kick()` (`api/polling.ts`) |

Khi phân vân, chọn phương án ít gây nhầm lẫn nhất, ít bước nhất, dễ học, dễ
scan, khớp workflow và dễ redesign.

## Ánh xạ lỗi

Mọi lỗi đi qua `describeError()` (`web/src/api/errors.ts`): tiêu đề tiếng Việt
theo `code` của `Error` envelope, gợi ý, `request_id` sao chép được. Thông điệp
thô của server chỉ hiện với `state_conflict` (lý do nghiệp vụ có ích cho user);
lỗi lập trình không bao giờ lộ message/stack.

| code (HTTP) | Nơi xử lý | Hành vi |
|---|---|---|
| `authentication_required` (401) | client chung | Về `/login?next=<trang hiện tại>` với "Phiên đăng nhập đã hết hạn…" |
| `invalid_csrf` (403) | client chung | Lấy lại CSRF bằng `GET /v1/auth/session` và gửi lại **một** lần cùng request (cùng Idempotency-Key, cùng body); không lấy được phiên hoặc lần hai vẫn lỗi → về đăng nhập |
| `permission_denied` (403) | từng trang | Thông điệp theo ngữ cảnh ("Không có quyền tạo job trong tenant này", …) |
| `resource_not_found` (404) | chi tiết job/sweep | "Không tìm thấy job hoặc bạn không có quyền xem" — không phân biệt tenant khác |
| `validation_failed` (400/422) | form | Gắn vào nhóm field nếu xác định được; form giữ dữ liệu |
| `invalid_cursor` (400), hoặc `validation_failed` khi URL có `cursor` | Jobs, Dữ liệu | Bỏ `cursor` khỏi URL, về trang đầu, thông báo "Vị trí trang không còn hợp lệ…"; không hiện ErrorPanel |
| `version_conflict` (412) | control | Tải lại job, "Job vừa thay đổi (trạng thái mới: …). Kiểm tra rồi thử lại."; không tự gửi lại |
| `state_conflict` (409) | control; Tạo job | Control: hiện lý do của server. Tạo job: "Hệ thống đang tạm ngừng nhận job mới" (ADMISSION_OFF/WRITE_FROZEN) |
| `infeasible_request` (422) | Tạo job | "Tài nguyên yêu cầu vượt khả năng của hệ thống" tại nhóm Tài nguyên |
| `quota_exceeded` (429) | Tạo job | "Tenant đã chạm hạn mức"; **không** đếm ngược theo Retry-After và không tự gửi lại (hạn mức chỉ trống khi job khác xong) |
| `rate_limited` (429) | Đăng nhập, Tạo job | Đếm ngược theo Retry-After, khóa nút đến hết |
| `queue_full`/`dependency_unavailable` (503) và lỗi tạm thời khác | ErrorPanel mọi nơi; polling | ErrorPanel ghi "Thử lại sau khoảng N giây" khi có Retry-After; polling chờ `max(backoff, Retry-After)` |
| `idempotency_in_progress` (409) | form/control | "Yêu cầu trước đó vẫn đang được xử lý"; gửi lại dùng cùng key nên không trùng |
| `checksum_mismatch`, `payload_too_large`, `storage_unavailable` | Upload | Thông điệp riêng; tệp > 256 MiB bị chặn trước khi đọc, kèm lệnh CLI |
| `one_time_secret_unavailable` (409) | Token | Giải thích, gợi ý thu hồi rồi tạo mới |
| mạng/timeout/phản hồi sai định dạng | client chung | "Không kết nối được máy chủ" / "Máy chủ không phản hồi kịp" / "…không đúng định dạng"; form/control giữ Idempotency-Key cho lỗi mạng, timeout và 5xx không có envelope (do proxy sinh, ví dụ 502 rỗng khi API chết sau commit), nên gửi lại replay kết quả đã commit thay vì tạo trùng; 5xx có envelope là câu trả lời server đã lưu theo key → intent mới |

## Polling

Một `Poller` (`web/src/api/polling.ts`) cho mỗi tài nguyên đang xem: chỉ một
request mỗi lúc, dừng khi rời trang.

| Lịch | Ban đầu | Hệ số | Trần | Dừng khi |
|---|---|---|---|---|
| `DETAIL_POLL` (chi tiết job: job + progress/attempts/checkpoints + sự kiện mới + result) | 2 s | ×1,5 khi không đổi | 30 s | job terminal (`SUCCEEDED/FAILED/CANCELLED`); tab ẩn |
| `LIST_POLL` (chỉ trang đầu của Jobs) | 10 s | ×1,5 | 60 s | tab ẩn; trang sau không tự làm mới |

- Dữ liệu đổi (fingerprint khác) → về nhịp ban đầu. Lỗi tạm thời → backoff,
  tôn trọng Retry-After. Lỗi chắc chắn (401/403/404/cursor/validation) → dừng.
- Sau một control, `usePolled().refresh()` gọi `Poller.kick()`: đọc ngay, lịch
  bắt đầu lại từ 2 s. Tab hiện lại → đọc ngay.
- Vitest kiểm bằng fake timers (`polling.test.ts`); W2 kịch bản 15 kiểm bằng
  Playwright clock rằng job terminal không còn request nào trong 120 s giả lập.

## Storage phía trình duyệt

Không có. UI không ghi `localStorage`, `sessionStorage`, IndexedDB hay cookie do
JavaScript tạo. Cookie phiên do server đặt (`HttpOnly`, `Secure`,
`SameSite=Lax`); CSRF token, tenant gần nhất, liên kết "Đã chạy lại thành" và
raw token CLI mới tạo chỉ nằm trong memory của tab. Playwright W1 kịch bản 1
kiểm storage rỗng sau đăng nhập.

## Bảo mật phía client và origin

- Một origin TLS: Caddy (`deploy/web/Caddyfile`) phục vụ `web/dist` và proxy
  `/v1/*`; `NEXA_PUBLIC_ORIGIN` phải đúng origin trình duyệt thấy (Origin/Host
  được backend kiểm).
- Header trên đường UI: CSP `default-src 'self'; script-src 'self'; style-src
  'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`, không header `Server`. `index.html`
  `no-cache`; `/assets/*` (tên có hash) `immutable` và 404 khi thiếu (không SPA
  fallback).
- Không phục vụ `/docs`, `/redoc`, `/openapi.json` và `/v1/internal/*` qua
  origin này (404). Lý do `/v1/internal/*`: Docker Desktop chuyển request đã
  proxy tới API với nguồn 127.0.0.1, nằm trong CIDR bảo trì mặc định.
- Không `dangerouslySetInnerHTML`, không inline script/style; tên tệp tải xuống
  được làm sạch (`features/data/files.ts`); download kiểm checksum trước khi lưu.
- Không có credential, CSRF hay token trong URL, log, console.

## Kiến trúc thư mục web

```text
web/
  index.html, vite.config.ts, vitest.config.ts, playwright.config.ts
  tools/openapi-gen/        openapi-typescript (pin) → src/api/generated.ts; --check cho drift
  src/
    main.tsx, App.tsx       mount + bảng route
    api/                    client.ts (fetch, header tenant/CSRF/Idempotency-Key/If-Match),
                            endpoints.ts, errors.ts, polling.ts, idempotency.ts, limits.ts,
                            types.ts, generated.ts (sinh, commit)
    auth/                   session.tsx (session + CSRF trong memory), guards.tsx
                            (RequireSession, TenantScope, useTenant), LoginPage.tsx, next.ts
    app/                    AppLayout.tsx (header, nav, tenant, tài khoản), SimplePages.tsx, labels.ts
    components/             bits.tsx, Dialog.tsx, ErrorPanel.tsx, Pager.tsx, Tabs.tsx,
                            DownloadButton.tsx, CliHint.tsx, usePolled.ts, useCountdown.ts,
                            format.ts, pageTrail.ts
    features/jobs/          JobsPage, JobDetailPage, useJobDetail, useJobControls
                            (dialog pause/resume/cancel/retry), actions.ts (ma trận), retryLinks.ts
    features/submit/        SubmitPage, SweepPage, ArtifactPicker, params.ts, spec.ts, sweep.ts, defaults.ts
    features/data/          DataPage, UploadPanel, files.ts
    features/account/       TokensPage
    styles/                 tokens.css, layout.css, components.css
  tests/e2e/                fixture.ts, global-setup.ts, support.ts;
                            w1/ (desktop), admission/, mobile/, w2/
```

Unit test (`*.test.ts`) nằm cạnh mã. Không có UI kit, CSS framework, CSS-in-JS,
thư viện state/form/data-fetching, icon hay i18n.

## Chạy và kiểm thử

Yêu cầu: Node 24 LTS, pnpm 11.9.0; Playwright dùng Chromium tải bằng
`pnpm --dir web exec playwright install chromium` (không `install-deps`).

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web run typecheck        # check:api (drift OpenAPI → generated.ts) + tsc -b
pnpm --dir web run build
pnpm --dir web run test             # Vitest
pnpm --dir web run gen:api          # chỉ khi contract OpenAPI đổi
```

Vite dev server (`pnpm --dir web run dev`) không proxy API; để thử tay trên
backend thật dùng harness: `... b17_e2e_stack.py up --tier w1` (giữ stack đến
Ctrl-C) rồi mở URL harness in ra.

Playwright chạy qua harness `scripts/b17_e2e_stack.py`: reset + migrate DB test
(chỉ nhận `NEXA_TEST_DATABASE_URL` trỏ tới DB `nexa_b05_test_*`), chạy API,
Caddy `nexa_b17_*`, seed user/tenant/artifact với mật khẩu ngẫu nhiên, ghi
fixture 0600 ngoài repo rồi chạy lệnh sau `--`. API bind 127.0.0.1 (trên Linux
bridge thì 0.0.0.0 trong thời gian test); process API không thấy biến
`NEXA_TEST_*`, `NEXA_ENDPOINT`, `NEXA_TOKEN`, `NEXA_TENANT_ID`, `NEXA_PROFILE`.

```sh
pnpm --dir web run build
export NEXA_TEST_DATABASE_URL=<postgresql+psycopg://…/nexa_b05_test_…>   # không ghi password vào log
S="PYTHONPATH=src:. uv run --no-sync python scripts/b17_e2e_stack.py"
# W1: backend thật, không worker
$S run --tier w1 -- pnpm --dir web exec playwright test --project=w1-desktop --project=w1-mobile
# Chế độ vận hành: stack riêng, vì ADMISSION_OFF không trả về NORMAL qua API được (B17-R18)
$S run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admission
# W2: thêm coordinator + worker Docker + image cpu-iterative build từ source hiện tại
NEXA_B17_CPU_IMAGE_REF=nexa/cpu-iterative@sha256:<digest> NEXA_B17_WORKER_IMAGE=<worker image> \
  $S run --tier w2 -- pnpm --dir web exec playwright test --project=w2
# Dọn phần còn lại sau khi bị ngắt giữa chừng
$S cleanup
```

`run` luôn dọn process, container `nexa_b17_*` và thư mục state khi kết thúc.
`web/test-results/` (trace/screenshot khi fail) bị git ignore và không được
commit; storageState nằm trong thư mục state của harness.

## Hướng dẫn redesign

- Mọi giá trị hình thức (font, màu, khoảng cách, bo góc, độ rộng) nằm trong
  `web/src/styles/tokens.css` (≤ 20 biến). Đổi giao diện = đổi tệp này, sau đó
  `components.css` (hình dạng component) và `layout.css` (lưới, breakpoint
  768 px). Không sửa trang hay logic.
- Màu trạng thái lấy theo nhóm ngữ nghĩa (`STATE_TONE` trong `app/labels.ts` →
  class `tone-*`), không theo từng state.
- Test Playwright bám role/label/text tiếng Việt, không bám class hay màu, nên
  redesign không làm vỡ test trừ khi đổi câu chữ; đổi câu chữ thì sửa
  `app/labels.ts`/component và test cùng lúc.
- Có thể thay component trình bày (bảng, badge, dialog) mà giữ nguyên hook
  (`usePolled`, `useJobControls`, `useJobDetail`) và API client.

## Giới hạn đã biết

- Log job chưa có (B17-R01); tab Log chỉ hiện trạng thái chưa hỗ trợ.
- Liên kết "Đã chạy lại thành" chỉ có trong phiên trình duyệt đã tạo retry
  (B17-R17): contract không có liên kết ngược bền vững; chiều "Chạy lại của"
  luôn có vì đọc từ `retry_of_job_id`.
- Không có tên user/tenant (B17-R05), không liệt kê sweep (B17-R10), sweep child
  không có giá trị tham số (B17-R16).
- Admin UI (B18), GPU (B23), Compose/UI image sản phẩm (B21/B25), Playwright
  trong CI (B17-R14) chưa thuộc B17.
