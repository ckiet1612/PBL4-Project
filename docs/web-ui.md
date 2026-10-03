# Nexa Web UI cho user (B17) và quản trị (B18)

Trạng thái: **B17 đã được Task Review duyệt; B18 (khu quản trị) đã triển khai,
chờ Task Review độc lập**. Tài liệu này mô tả bộ khung UX chức năng
(low-fidelity) trong `web/` cho người dùng tenant (`MEMBER`, `TENANT_ADMIN`) và,
ở mục "Khu quản trị (B18)", cho `SYSTEM_ADMIN`. UI chỉ là client của REST `/v1`
chung với CLI: authorization, state machine, quota và scheduler thuộc backend.
Log pipeline thuộc B17-R01 (chưa có), GPU thuộc B23, Compose/UI image sản phẩm
thuộc B21/B25.

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
| Khu quản trị (B18) | Link `Quản trị` cuối global nav, chỉ khi session có `SYSTEM_ADMIN` | Chi tiết ở mục "Khu quản trị (B18)". |

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
  chỉ đọc; chỉ quản trị viên hệ thống được đăng nhập" — **thực tế không xảy ra**:
  server trả 401 "Invalid credentials" cho user không phải admin trước khi kiểm
  tra mật khẩu (không lộ user tồn tại, không ghi rate metadata), nên trang hiện
  thông báo sai thông tin đăng nhập chung (B18-R21, Playwright W1-admin-frozen
  quan sát được); phiên hết hạn hoặc bị thu
  hồi → thông báo "Phiên đăng nhập đã hết hạn. Đăng nhập lại để tiếp tục".
- Bước tiếp theo: về `next` (chỉ path nội bộ) hoặc Jobs của tenant mặc định.
  `next` bị bỏ nếu path **sau chuẩn hóa** bắt đầu bằng `//` (ví dụ `/.//x`,
  `/%2e%2e//x`, `/a/..//x`) hoặc là `/login` (không phân biệt hoa thường).

#### 3.2 Không có tenant (`/` khi memberships rỗng)
- Mục đích: giải thích cho user (kể cả `SYSTEM_ADMIN`) chưa thuộc tenant nào.
- Thông tin: "Tài khoản chưa thuộc tenant nào. Liên hệ quản trị viên để được
  thêm vào tenant." Với `SYSTEM_ADMIN`: thêm link `Mở khu quản trị` (B18).
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
| `AppLayout` (header, `GlobalNav`, `TenantPicker`, `AccountMenu`) | mọi trang sau đăng nhập | link `Quản trị` khi session có `SYSTEM_ADMIN` (B18-R13) |
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
khác tạo". TENANT_ADMIN không có link quản trị: quản trị user/tenant/quota/
worker/fairness ở khu `/admin` chỉ dành cho `SYSTEM_ADMIN` (B18); vào thẳng URL
thì thấy "Cần quyền quản trị hệ thống" và không có request `/v1/admin` nào.

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

## Khu quản trị (B18)

Khu quản trị dành cho `SYSTEM_ADMIN`, nằm trong cùng web app và gọi cùng REST
`/v1/admin/*` mà CLI `nexa admin` dùng. Backend quyết định authorization, state
machine và scheduler; UI chỉ hiện/ẩn và giải thích. Admin lần đầu phải trả lời
được: hệ thống có khỏe không, cần chú ý gì, làm được gì ở đây, mỗi thao tác có
hậu quả gì và hoàn tác được không.

**Mọi request `/v1/admin/*` (kể cả GET) ghi một dòng audit** cùng transaction.
Vì vậy trang quản trị tải dữ liệu một lần khi mở, có nút `Làm mới` và dòng
"Cập nhật lúc …"; không polling danh sách hay tổng quan (B18-R02). Ngoại lệ duy
nhất là chi tiết worker trong lúc chuyển trạng thái (mục 6).

### A1. Sitemap

```text
/admin                                  Tổng quan
/admin/workers                          Worker (danh sách)
/admin/workers/:workerId                Chi tiết worker: sức khỏe, inventory, sức chứa, thao tác
/admin/jobs                             Hàng chờ toàn hệ thống (chỉ xem)
                                        ?tenant_id=&user_id=&state=&waiting_reason=&created_after=&cursor=
/admin/jobs/:jobId                      Chi tiết job (chỉ xem, không control)
/admin/tenants                          Tenant ?cursor=
/admin/tenants/:tenantId                Chi tiết tenant ?tab=info|members|policy
/admin/users                            User ?cursor=
/admin/users/:userId                    Chi tiết user
/admin/policy                           Chính sách hệ thống: giới hạn toàn cục, chế độ vận hành
/admin/fairness                         Fairness ?from=&to=&bucket=&tenant_id=
/admin/recovery                         Sự kiện khôi phục ?from=&to=&cursor=
/admin/audit                            Audit ?from=&to=&action=&cursor=
/admin/*                                "Không tìm thấy trang" trong khung quản trị
```

- Filter, khoảng thời gian, tab và cursor nằm trên URL (chia sẻ link, nút Back
  đúng). Cursor hỏng hoặc đổi filter → về trang đầu kèm thông báo.
- Không có `/admin/memberships`: membership thuộc tenant (tab `Thành viên`).
  Không có API membership theo user (B18-R10).
- Tenant policy là tab `Chính sách` của tenant, không phải trang riêng.

### A2. Navigation

| Vùng | Nội dung | Ghi chú |
|---|---|---|
| Global nav (header) | `Jobs`, `Dữ liệu` khi có tenant gần nhất; `Quản trị` khi session có `SYSTEM_ADMIN` | `Quản trị` hiện cả khi admin không có membership (B18-R13): global nav không còn phụ thuộc tenant. |
| Khung quản trị | Tiêu đề "Quản trị hệ thống"; dòng cố định "Mọi thao tác và lượt xem trong khu này đều được ghi audit"; nút `Về khu tenant` khi user có membership | Nút về tenant gần nhất (memory) hoặc tenant mặc định. |
| Secondary nav | **Vận hành**: Tổng quan, Worker, Hàng chờ, Khôi phục · **Tổ chức**: Tenant, User · **Chính sách**: Hệ thống · **Giám sát**: Fairness, Audit | Nhóm là nhãn chữ nhỏ; mục đang mở có `aria-current="page"`. |
| Breadcrumb | `Worker › <id>`, `Hàng chờ › Job <id>`, `Tenant › <slug>`, `User › <username>` | Chỉ ở trang chi tiết. |
| Banner chế độ | Khi lần đọc global policy gần nhất có `operational_mode ≠ NORMAL`: "Hệ thống đang ở chế độ …" + link `Chính sách hệ thống` | Lấy từ lần đọc gần nhất (Tổng quan hoặc Chính sách), không gọi thêm API. |

Dưới 768 px: secondary nav là một thanh ngang tự xuống dòng ngay dưới tiêu đề,
nhóm vẫn có nhãn; không hamburger (như UX-A11). Bảng rộng nằm trong
`.table-wrap` cuộn ngang riêng; trang không cuộn ngang.

Route guard: khu `/admin` chỉ render khi `session.system_roles` có
`SYSTEM_ADMIN`; ngược lại hiện "Cần quyền quản trị hệ thống" và **không gửi**
request `/v1/admin/*` nào. Đây chỉ là hiển thị; backend vẫn trả 403.

### A3. Danh sách trang

Trạng thái chung mọi trang: `Đang tải…`; `ErrorPanel` (thông điệp tiếng Việt,
request_id, `Thử lại`); quyền bị từ chối → "Cần quyền quản trị hệ thống"; 503 có
Retry-After → "Thử lại sau khoảng N giây"; WRITE_FROZEN → banner chế độ, trang
đọc vẫn chạy, mutation hiện lý do 409 của server. "Admin lần đầu" = hệ thống mới
chỉ có tenant/user seed: mỗi danh sách trống có hướng dẫn bước tiếp theo.

Cột "Audit khi mở" = số request `/v1/admin/*` (= số dòng audit) khi mở trang
với tham số mặc định; `Làm mới` tốn đúng số đó lần nữa.

| Trang | Mục đích / việc chính | Primary · secondary action | Trạng thái riêng | Audit khi mở | Bước tiếp theo |
|---|---|---|---|---|---|
| Tổng quan `/admin` | Hệ thống khỏe không, cần chú ý gì | `Làm mới` · link tới Worker/Khôi phục/Chính sách | Chưa có worker: "Chưa có worker nào đăng ký; khởi động worker cục bộ"; mục "Cần chú ý": worker không READY, worker không ENABLED, có QUARANTINED, chế độ ≠ NORMAL, có sự kiện khôi phục 24 h | 5 (policy, workers, allocations HELD, allocations QUARANTINED, recovery 24 h) | Mở worker / sự kiện |
| Worker `/admin/workers` | Danh sách worker (single-node: thường 1) | Mở chi tiết · `Làm mới` | Trống: như trên | 1 | Chi tiết worker |
| Chi tiết worker | Sức khỏe, heartbeat, inventory, sức chứa, allocation đang giữ; drain/disable/enable | Thao tác theo ma trận mục A6 · `Làm mới` | Inventory null: "Worker chưa gửi inventory"; danh sách allocation trên 100: "chưa đầy đủ"; đang chuyển trạng thái: tự cập nhật | 3 (worker, HELD, QUARANTINED); mỗi lần tự cập nhật 3 | Chờ hoàn tất; Hàng chờ |
| Hàng chờ `/admin/jobs` | Xem job mọi tenant, lọc | Lọc · trang sau/trước/đầu · mở chi tiết | Trống: "Chưa có job nào"; không khớp filter: "Không có job khớp bộ lọc" + `Xóa bộ lọc` | 2 (jobs, tenants trang đầu ≤ 100 để hiện slug) | Chi tiết job |
| Chi tiết job | Thông tin job chỉ xem | `Mở trong khu tenant` (khi admin là thành viên tenant) | 404: "Không tìm thấy job"; ghi chú "Khu quản trị chỉ xem. Điều khiển job cần là thành viên của tenant" | 1 | Khu tenant |
| Tenant `/admin/tenants` | Danh sách, tạo tenant | `Tạo tenant` · mở chi tiết | Trống: "Chưa có tenant nào. Tạo tenant đầu tiên" | 1 | Chi tiết tenant |
| Chi tiết tenant | Thông tin, thành viên, chính sách | Info: `Đổi tên`, `Tắt`/`Bật` (một PATCH tenant tại một thời điểm: khi một nút đang gửi, nút kia disabled, B18-RV04) · Thành viên: `Thêm thành viên`, đổi vai trò, `Xóa` · Chính sách: `Lưu thay đổi` | Tenant tắt: nhãn "Đã tắt"; không có thành viên: hướng dẫn thêm; hạn mức tài nguyên = 0: cảnh báo | info 1; thành viên 2 (+1 users khi mở dialog thêm); chính sách 2 | Thêm thành viên → chính sách |
| User `/admin/users` | Danh sách, tạo user | `Tạo user` · mở chi tiết | Trống hiếm (luôn có admin) | 1 | Chi tiết user / thêm vào tenant |
| Chi tiết user | Đổi tên, đặt lại mật khẩu, bật/tắt, quyền hệ thống | Từng form riêng, mỗi form một xác nhận | User là admin cuối: server trả 409, hiện lý do | 1 | Tenant (membership quản lý ở tenant) |
| Chính sách hệ thống `/admin/policy` | Giới hạn job tồn đọng toàn cục, chế độ vận hành | `Lưu giới hạn` · `Chuyển chế độ` | WRITE_FROZEN: ô giới hạn bị khóa kèm lý do | 1 | Tổng quan |
| Fairness `/admin/fairness` | Thời gian tài nguyên trội theo tenant và bucket | `Xem báo cáo` | Kiểm tra trước khi gửi (mục A7); không có dữ liệu: "Không có phân bổ nào trong khoảng này" | 2 (fairness với khoảng mặc định 24 h/1 h, tenants trang đầu) | Tenant |
| Khôi phục `/admin/recovery` | Sự kiện khôi phục/fence/quarantine/checkpoint | Lọc khoảng · trang | Trống: "Không có sự kiện khôi phục trong khoảng này" | 1 | Chi tiết job (admin) |
| Audit `/admin/audit` | Ai làm gì, khi nào | Lọc khoảng + action · trang | Ghi chú "Danh sách gồm cả lượt xem của quản trị viên"; trống | 1 | – |

### A4. Component mới hoặc mở rộng

| Component | Mới/mở rộng | Hành vi |
|---|---|---|
| `GlobalNav` / `AppLayout` | mở rộng | Render khi có tenant **hoặc** là admin; slot `Quản trị` (B18-R13) |
| `AppErrorBoundary` | mới | Lỗi render bất kỳ → "Đã xảy ra lỗi hiển thị" + `Tải lại trang`; không lộ stack (B18-R15). Một boundary ngoài cùng và một boundary quanh trang trong layout (khóa theo `pathname`): navigation vẫn hiện, chuyển route khác thì xóa lỗi (B18-RV10) |
| `refreshCsrf` (`auth/csrfRefresh.ts`) | mở rộng | Gắn với user mà request **đã được gửi dưới tên**: các refresh đồng thời dùng chung một lần `GET /v1/auth/session`; chỉ gửi lại khi phiên đọc được thuộc đúng user đó; request gửi khi chưa biết user, hoặc phiên thuộc user khác → không gửi lại (`SESSION_SWITCHED`), người chờ đầu tiên xóa state, về `/`, "Phiên đăng nhập đã đổi sang tài khoản khác" (B18-R14, B18-RV03) |
| `IntentSlot` (`features/admin/intent.ts`) | mới | Intent + giá trị đã gửi sống ở trang, không ở dialog: hủy rồi mở lại dialog khi kết quả còn chưa chắc (mạng/timeout/5xx proxy) → điền lại giá trị và gửi lại cùng Idempotency-Key, cùng `If-Match` đầu; kết quả chắc chắn (thành công/412/409) → bỏ giá trị (kể cả mật khẩu) (B18-RV10) |
| `NoTenantPage` | mở rộng | Admin không membership: link `Mở khu quản trị` |
| `AdminGuard` | mới | Kiểm `SYSTEM_ADMIN` trước khi render khu quản trị |
| `AdminLayout` | mới | Tiêu đề, ghi chú audit, secondary nav, `Về khu tenant`, banner chế độ |
| `RefreshBar` | mới | "Cập nhật lúc …" + `Làm mới` (+ trạng thái tự cập nhật ở worker) |
| `RangeFields` | mới | from/to theo giờ địa phương (`datetime-local`), gửi RFC3339 UTC; preset 1 giờ/24 giờ/7 ngày; khoảng mặc định được ghi lên URL (`replace`) một lần khi mở Audit/Khôi phục/Fairness, nên tải lại và trang sau giữ đúng cận (`useUrlRange`, B18-RV10) |
| `CapacityTable` | mới | Allocatable (inventory) · đang giữ (HELD + QUARANTINED) · còn trống ước tính; cờ "chưa đầy đủ" |
| `WorkerActions` + `TransitionStatus` | mới | Ma trận A6, dialog có lý do, chữ chờ/hoàn tất theo dữ liệu API |
| `ConflictCompare` | mới | 412 ở form chính sách: giá trị server cạnh giá trị đang sửa |
| `DecimalInput` (hàm `parseDecimal`) | mới | Nhận `0,5` hoặc `0.5`; lỗi gắn vào field |
| `ConfirmDialog`, `ErrorPanel`, `Pager`, `ShortId`, `Time`, `StatusBadge`, `Tabs`, `Notice`, `EmptyState` | dùng lại | Như B17 |

### A5. Workflow quản trị (bước → trang → API)

| ID | Bước | Trang | API |
|---|---|---|---|
| A-01 | Đăng nhập; admin có membership vào tenant như B17 và thấy `Quản trị`; admin không membership thấy `Mở khu quản trị` (trang không tenant) và `Quản trị` trên nav | `/` → `/admin` | `GET /v1/auth/session` |
| A-02 | Xem tổng quan: chế độ, giới hạn, worker, sức chứa, cần chú ý, sự kiện 24 h | `/admin` | `GET /v1/admin/policy`, `/workers?page_size=10`, `/allocations?state=HELD&page_size=100`, `/allocations?state=QUARANTINED&page_size=100`, `/recovery-events?from&to&page_size=10` |
| A-03 | Drain: chi tiết worker → `Ngừng nhận job` → lý do → xác nhận → chờ "không còn job đang chạy" → `Bật lại` | chi tiết worker | `POST /v1/admin/workers/{id}/drain` (If-Match, Idempotency-Key, `{reason}`) → tự cập nhật `GET /workers/{id}` + allocations → `POST …/enable` |
| A-04 | Disable: `Tắt worker` → lý do → chờ worker xác nhận dọn dẹp → `Bật lại` (409 nếu còn QUARANTINED) | chi tiết worker | `POST …/disable` → tự cập nhật → `POST …/enable` |
| A-05 | Lọc hàng chờ theo tenant/user/trạng thái/lý do chờ/thời điểm → trang sau → chi tiết | `/admin/jobs` → chi tiết | `GET /v1/admin/jobs?…`, `GET /v1/admin/tenants?page_size=100`, `GET /v1/admin/jobs/{id}` |
| A-06 | Tạo tenant (slug, tên) → chi tiết → đổi tên / tắt / bật | Tenant → chi tiết | `POST /v1/admin/tenants`, `GET/PATCH /v1/admin/tenants/{id}` |
| A-07 | Tạo user (tên đăng nhập, tên, mật khẩu ×2, quyền hệ thống) → chi tiết: đổi tên, đặt lại mật khẩu, bật/tắt, quyền | User → chi tiết | `POST /v1/admin/users`, `GET/PATCH /v1/admin/users/{id}` |
| A-08 | Thêm thành viên (chọn user hoặc dán user ID, vai trò) → đổi vai trò → xóa; 412 → tải lại, giải thích trong dialog, giữ user/vai trò đã chọn (membership đích đã mất khi đổi vai trò/xóa → đóng dialog, báo trên trang) | tenant `?tab=members` | `GET /memberships` (ETag = MembershipSet), `POST /memberships`, `DELETE /memberships/{user_id}`; `GET /v1/admin/users?page_size=100` khi mở dialog |
| A-09 | Sửa trọng số/hạn mức → lưu (chỉ field đổi) → 412: so sánh; 409: lý do server + gợi ý | tenant `?tab=policy` | `GET/PATCH /v1/admin/tenants/{id}/policy` |
| A-10 | Sửa giới hạn toàn cục; chuyển chế độ theo ma trận A7 | `/admin/policy` | `GET/PATCH /v1/admin/policy` |
| A-11 | Chọn khoảng, bucket, tenant → xem bảng + tổng theo tenant | `/admin/fairness` | `GET /v1/admin/fairness?from&to&bucket_seconds[&tenant_id]` |
| A-12 | Xem sự kiện khôi phục theo khoảng → trang → mở job | `/admin/recovery` | `GET /v1/admin/recovery-events?from&to&cursor` |
| A-13 | Lọc audit theo khoảng + action → trang | `/admin/audit` | `GET /v1/admin/audit?from&to&action&cursor` |
| A-14 | Lỗi: 403, 409, 412, 503/Retry-After, WRITE_FROZEN, cursor hỏng | tại chỗ | mục "Ánh xạ lỗi" (phần quản trị) |

### A6. Ma trận thao tác worker

UI chỉ hiện/ẩn; server quyết định (`src/nexa/application/admin_workers.py`).
Mọi thao tác có ô lý do bắt buộc (1–256 ký tự), `If-Match` = ETag của lần
`GET worker` gần nhất, một Idempotency-Key cho mỗi lần xác nhận.

| admin_state | `Ngừng nhận job` (drain) | `Tắt worker` (disable) | `Bật lại` (enable) |
|---|---|---|---|
| ENABLED | hiện | hiện | ẩn |
| DRAINING | ẩn | hiện | hiện |
| DISABLED | ẩn | ẩn | hiện |

| Thao tác | Hậu quả (chữ trong dialog) | Hoàn tác | Chữ đang chờ | Chữ hoàn tất (chỉ khi API cho thấy) |
|---|---|---|---|---|
| Drain | "Worker ngừng nhận job mới. Job đang chạy tiếp tục tới khi kết thúc. Không dừng job nào. Hoàn tác bằng Bật lại." | `Bật lại` | "Đang chờ n job đang chạy kết thúc" | không còn allocation HELD → "Đã ngừng nhận job; không còn job đang chạy" |
| Disable | "Worker ngừng nhận job mới. Mọi lần chạy đang có quyền bị thu hồi (fence) và được yêu cầu dừng. Tài nguyên giữ ở trạng thái QUARANTINED, vẫn tính vào hạn mức, cho tới khi worker xác nhận dọn dẹp. Job sẽ được khôi phục ở lần chạy mới theo checkpoint nếu có." | `Bật lại` sau khi dọn xong | "Đang chờ worker xác nhận dọn dẹp" (còn n QUARANTINED) | không còn QUARANTINED → "Worker đã xác nhận dọn dẹp xong" |
| Enable | "Chỉ bật được khi worker có heartbeat mới đạt READY, đã reconcile và không còn phân bổ QUARANTINED." | Drain/Tắt | "Đã bật; đang chờ worker báo READY" | `health = READY` → "Worker sẵn sàng nhận job" |

- DISABLED và trang đầu có QUARANTINED: ghi chú "Còn n phân bổ chờ worker xác
  nhận dọn dẹp; máy chủ sẽ từ chối bật lại cho tới khi dọn xong". Nút `Bật lại`
  vẫn bấm được; 409 hiện lý do server.
- Không suy trạng thái từ 202 hay từ thời gian trôi qua.
- Trang HELD/QUARANTINED đọc **trước** thao tác không chứng minh drain/disable
  đã xong (job có thể vừa được đặt lên worker): sau drain/disable trang hiện
  "Đang kiểm tra lại phân bổ sau thao tác" và chỉ hiện chữ hoàn tất từ lần đọc
  sau thao tác (B18-R23).
- **Tự cập nhật khi chuyển trạng thái**: sau thao tác (hoặc khi mở trang thấy
  DRAINING còn HELD, DISABLED còn QUARANTINED, ENABLED chưa READY) đọc lại worker
  + HELD + QUARANTINED (3 dòng audit mỗi lần): bắt đầu 5 s, ×1,5, trần 60 s;
  dừng khi đạt chữ hoàn tất, khi rời trang, hoặc sau 10 phút ("Đã dừng tự cập
  nhật, bấm Làm mới"); tạm dừng khi tab ẩn; tôn trọng Retry-After.
- Sức chứa: allocatable lấy từ `inventory.allocatable`; đang giữ = tổng trang
  đầu HELD + trang đầu QUARANTINED (page_size 100 mỗi loại); có trang sau → "chưa
  đầy đủ" + link; "còn trống" ghi "ước tính trên giao diện; scheduler dùng dữ
  liệu của server". Allocation không lọc được theo worker (single-node).
- Sau mỗi thay đổi allocation (job bắt đầu, dọn dẹp xong) worker reconcile lại
  và heartbeat kế tiếp mới đạt READY (vòng 5 s). Trong khoảng đó server từ chối
  `Bật lại` với 409 "The latest worker heartbeat did not pass the READY checks"
  hoặc "The current worker incarnation is not reconciled"; dialog hiện lý do,
  admin bấm lại sau vài giây. Heartbeat làm đổi `health` hoặc `ready_at` (ví dụ
  STARTING → READY ở trên) cũng tăng `version`, nên thao tác gửi từ trang đọc
  trước đó có thể gặp 412: trang đọc lại worker, báo "Đối tượng vừa được thay
  đổi (vA → vB). Kiểm tra rồi gửi lại."; thao tác còn áp dụng với trạng thái mới
  → dialog **giữ nguyên** cùng lý do đã nhập và hiện câu đó trong dialog; không
  còn áp dụng (nút đã biến mất) → đóng dialog, câu đó hiện trên trang; không tự
  gửi lại (B18-RV05). Heartbeat đều đặn không đổi
  `version`: trước B18, `discovered_at` mới của inventory ở mỗi heartbeat tạo
  inventory version mới và tăng ETag mỗi 5 s; backend đã sửa để checksum
  inventory bỏ `discovered_at` (B18-R22, phát hiện ở Playwright W2-admin, có
  test PostgreSQL).

### A7. Ma trận chế độ vận hành và quy tắc form chính sách

Lựa chọn hiển thị theo `validate_mode_transition` (`src/nexa/domain/policy.py`);
server quyết định. Bản hiện tại dùng `FailClosedRecoveryProofProvider`: mọi bằng
chứng (freeze/restore/readiness) là `False`.

| Chế độ hiện tại | Lựa chọn | Hậu quả (dialog) | Kết quả thật ở bản hiện tại |
|---|---|---|---|
| NORMAL | ADMISSION_OFF | "Ngừng nhận job mới từ mọi tenant; job đã nhận vẫn chạy." | 200 |
| ADMISSION_OFF | NORMAL | "Nhận job trở lại. Cần bằng chứng sẵn sàng và worker đã reconcile." | 409 "Readiness and worker reconciliation are required" (B18-R05) |
| ADMISSION_OFF | WRITE_FROZEN | "Khóa mọi thay đổi quản trị và nhận job. Cần mọi container đã dừng và allocation đã reconcile." | 409 "Freeze requires stopped containers and reconciled unreleased allocations" (B18-R18) |
| WRITE_FROZEN | ADMISSION_OFF | "Mở lại thay đổi quản trị sau khi khôi phục đã được xác minh." | 409 "Restore verification is required before leaving frozen mode" (B18-R05) |

- Mọi lựa chọn rời ADMISSION_OFF/WRITE_FROZEN kèm cảnh báo cố định "Bản hiện tại
  chưa mở lại được chế độ qua API (cần bằng chứng khôi phục, B18-R05)". Dialog
  NORMAL → ADMISSION_OFF nói rõ: **"Trong bản hiện tại không quay lại NORMAL qua
  API được."**
- Giới hạn toàn cục: số nguyên 1–1.000.000; dưới số job đang tồn đọng → server
  409; WRITE_FROZEN → ô khóa kèm lý do. Giới hạn và chế độ là hai form, hai
  intent.
- Chính sách tenant: mọi field của `TenantPolicy`; `resource_limit` hiện và nhập
  bằng core/GiB/GPU, gửi millicore/byte/số nguyên; trọng số và tốc độ gửi nhận
  `0,5` hoặc `0.5`, gửi số JSON; ô hiển thị số ở dạng thập phân ngắn nhất đọc lại
  đúng giá trị (không làm tròn `toFixed`), nên field không sửa không bao giờ bị
  tính là đã đổi (B18-RV06); PATCH chỉ gồm field đã đổi (không đổi gì → nút
  lưu disabled); hạn mức tài nguyên có thành phần CPU hoặc RAM bằng 0 → cảnh báo
  "Tenant chưa chạy được job nào vì hạn mức tài nguyên bằng 0"; 412 →
  `ConflictCompare`; 409 → lý do server + "Hãy ngừng nhận job (drain) hoặc chờ
  job đang chạy kết thúc rồi thử lại".

### A8. Chức năng được gộp và không lên navigation

| Chức năng | Cách gộp / vào | Lý do |
|---|---|---|
| Membership | Tab `Thành viên` của tenant | MembershipSet có version theo tenant; không có API theo user (B18-R10) |
| Tenant policy | Tab `Chính sách` của tenant | Quota thuộc tenant |
| Allocation | Bảng trong chi tiết worker + số liệu tổng quan | Single-node; allocation chỉ có nghĩa cạnh sức chứa |
| Global policy + chế độ | Một trang `Hệ thống` | Cùng object, cùng ETag |
| Chi tiết job admin | Không lên nav; vào từ hàng chờ/khôi phục | Chỉ xem |
| Template | Không có trong UI | Đăng ký template qua CLI/maintenance; không có REST admin template |
| Log, metrics, storage, GC | Không có | B17-R01, B19 |

### A9. Giả định UX (tiếp theo UX-A15)

| ID | Giả định | Lý do | Cách đổi |
|---|---|---|---|
| UX-A16 | Khu quản trị ở `/admin/*`, không cần tenant trên URL | API admin không có tenant header; admin có thể không có membership | Bảng route `App.tsx` |
| UX-A17 | Trang quản trị tải một lần + `Làm mới`; chỉ chi tiết worker tự cập nhật khi chuyển trạng thái | Mọi GET admin ghi audit (B18-R02) | `features/admin/transitionPoll.ts`, `TRANSITION_POLL` |
| UX-A18 | Page size 25 cho bảng quản trị; 100 cho allocation (sức chứa), danh sách tenant/user dùng để hiện tên | Đủ mật độ; tên chỉ từ trang đã tải (B18-R09) | `PAGE_SIZE` trong `api/limits.ts` |
| UX-A19 | Khoảng mặc định 24 giờ cho fairness/khôi phục/audit, theo phút tròn (ô `datetime-local`), điểm cuối làm tròn **lên** phút kế tiếp để không mất dữ liệu của phút hiện tại; bucket mặc định 1 giờ | Câu hỏi thường gặp nhất là "hôm nay"; 24 bucket dễ đọc | `features/admin/fairness.ts`, `ranges.ts` (`defaultRange`) |
| UX-A20 | Tên tenant/user chỉ lấy từ danh sách đã tải trong memory; không có thì hiện ID rút gọn | Không có API tra tên hàng loạt; mỗi lần tra là một dòng audit | Thay `ShortId` khi contract có tên |
| UX-A21 | CPU nhập bằng core (tối đa 3 chữ số thập phân), RAM bằng GiB (làm tròn tới byte), GPU số nguyên; chấp nhận `,` và `.` | Admin nghĩ theo core/GiB; API dùng millicore/byte | `features/admin/units.ts` |
| UX-A22 | Chỉ thao tác worker có ô lý do (bắt buộc) | Contract chỉ worker action nhận `reason`; các mutation khác không có trường lý do để gửi | Khi contract thêm `reason`, thêm vào dialog |
| UX-A23 | Giới hạn toàn cục và chế độ là hai form, hai lần xác nhận | Hậu quả khác nhau, tránh gửi nhầm cả hai | `features/admin/policy/` |
| UX-A24 | Chi tiết job admin chỉ hiện object Job (không tiến trình/sự kiện/lần chạy) | Các route đó cần tenant header và membership | Thêm khi có API admin tương ứng |
| UX-A25 | Thêm thành viên: chọn trong 100 user đầu hoặc dán user ID | Không có tìm kiếm user | Khi có API tìm kiếm, thay ô chọn |
| UX-A26 | Admin có membership vào khu tenant như B17 (link `Quản trị` trên nav); admin không membership thấy `Mở khu quản trị` ở `/` | Không đổi trang chủ của admin đang dùng tenant | `HomeRedirect` / `NoTenantPage` |
| UX-A27 | Mật khẩu nhập hai lần, chỉ sống trong state của form, xóa ngay sau intent (thành công hay lỗi chắc chắn) | Không để mật khẩu ở URL/log/storage | `features/admin/users/` |
| UX-A28 | Fairness tự chạy khi mở với tham số trên URL (mặc định 24 h/1 h) | Một dòng audit, admin thấy ngay dữ liệu | Bỏ `autoRun` trong `FairnessPage` |

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

Phần quản trị (`web/src/features/admin/errors.ts`, `AdminErrorPanel`): dùng
chung bảng trên, khác ở các dòng sau.

| code (HTTP) | Nơi xử lý | Hành vi |
|---|---|---|
| `permission_denied` (403) | mọi trang `/admin` | "Cần quyền quản trị hệ thống", không gợi ý; route guard chặn trước khi gửi `/v1/admin` nếu session không có `SYSTEM_ADMIN` |
| `validation_failed` (400/422) | form tenant/user/chính sách | Gắn câu của server vào field nếu nhận ra tên field (`validationField`); không nhận ra → ErrorPanel với "Chi tiết từ máy chủ" |
| `version_conflict` (412) | tenant, user, membership, chính sách, worker | Đọc lại đối tượng, "Đối tượng vừa được thay đổi (vA → vB). Kiểm tra rồi gửi lại."; dialog membership/worker giữ nguyên giá trị đã nhập và hiện câu đó tại chỗ; chính sách hiện bảng so sánh giá trị đã nhập với giá trị server; lần gửi sau là intent mới với ETag mới; không tự gửi lại |
| `state_conflict` (409) | mọi thao tác quản trị | ErrorPanel tại form/dialog với "Chi tiết từ máy chủ: <lý do>" (ví dụ "Administrative mutations are disabled while writes are frozen", "The latest worker heartbeat did not pass the READY checks", "Restore verification is required before leaving frozen mode"); chính sách tenant thêm gợi ý drain |
| `invalid_cursor` (400) | Hàng chờ, Audit, Khôi phục, Tenant, User | Như B17: về trang đầu cùng bộ lọc, "Vị trí trang không còn hợp lệ, đã quay về trang đầu" |
| Khoảng thời gian sai (> 31 ngày, from > to) | Fairness, Khôi phục, Audit | Chặn trong form trước khi gửi; URL sai → "Không gửi báo cáo: …"; server vẫn trả 400 nếu gọi thẳng |

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
    features/admin/         B18: AdminLayout (guard, nav, banner chế độ), OverviewPage, workers/,
                            jobs/ (chỉ xem), tenants/, users/, policy/, monitor/ (fairness,
                            khôi phục, audit); logic thuần có test: actions.ts (ma trận worker),
                            modes.ts, policyForm.ts, forms.ts, ranges.ts, fairness.ts, intent.ts,
                            transitionPoll.ts, errors.ts, labels.ts, units.ts, overview.ts
    styles/                 tokens.css, layout.css, components.css
  tests/e2e/                fixture.ts, global-setup.ts, support.ts, admin-support.ts;
                            w1/ (desktop), admission/, mobile/, w2/; B18: admin/, admin-mode/,
                            admin-frozen/, w2-admin/, admin-mobile/
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
# B18 khu quản trị — mỗi lệnh một stack riêng:
$S run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admin
$S run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admin-mode   # để lại ADMISSION_OFF
$S run --tier w1 --operational-mode WRITE_FROZEN -- pnpm --dir web exec playwright test --project=w1-admin-frozen
NEXA_B17_CPU_IMAGE_REF=… NEXA_B17_WORKER_IMAGE=… \
  $S run --tier w2 -- pnpm --dir web exec playwright test --project=w2-admin --project=w2-admin-mobile
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
- GPU (B23), Compose/UI image sản phẩm (B21/B25), Playwright trong CI (B17-R14)
  chưa có. Giới hạn riêng của khu quản trị ở cuối mục này.

Khu quản trị:

- Không có API tổng sức chứa hay đếm hàng chờ (B18-R03/R04): sức chứa cộng
  trang đầu HELD + QUARANTINED, có cờ "chưa đầy đủ"; hàng chờ không hiện tổng.
- Rời ADMISSION_OFF/WRITE_FROZEN cần bằng chứng khôi phục chưa có (B18-R05);
  ADMISSION_OFF → WRITE_FROZEN luôn 409 vì proof provider fail closed (B18-R18).
  UI hiện lý do của server; trạng thái WRITE_FROZEN chỉ kiểm được trên stack
  test seed thẳng vào DB.
- Service hẹp hơn contract: slug 3–63 ký tự (contract 3–64, B18-R19), GPU của
  hạn mức tenant ≤ 1 (contract ≤ 64, B18-R20); UI hiện 422 của server tại field.
- Đăng nhập của user thường khi WRITE_FROZEN nhận 401 "Invalid credentials"
  (server không phân biệt), nên thông báo khóa ghi của B17 không xuất hiện
  (B18-R21).
- Sau thay đổi allocation, `Bật lại` có thể bị 409/412 trong vài giây (A6,
  B18-R22); admin bấm lại.
- Audit `artifact.upload.commit` của upload do worker gửi ghi `actor_type`
  USER với id của worker (backend B07), nên trang Audit hiện "User …<worker>"
  cho "Tải dữ liệu lên"; UI hiện đúng dữ liệu server (B18-R24).
- Mỗi GET admin là một dòng audit; danh sách không tự cập nhật (B18-R02).
- Tên tenant/user chỉ từ danh sách đã tải (B18-R09); membership chỉ quản lý
  theo tenant (B18-R10).
- Playwright W2-admin giữ cửa sổ QUARANTINED bằng `docker pause` container
  worker của chính harness (`nexa_b17_worker_*`); đây là kỹ thuật test, không
  phải thao tác vận hành.
