// One place that turns every API error code into Vietnamese text and a suggested action.
import { ApiError, type ClientErrorCode, type ContractErrorCode } from "./client";

export interface ErrorMessage {
  title: string;
  hint?: string;
  /** Show the server's (English) message as secondary detail. */
  showServerMessage?: boolean;
}

const INTERFACE_BUG = "Đây là lỗi của giao diện. Tải lại trang rồi thử lại; nếu vẫn lỗi, gửi mã yêu cầu cho quản trị viên.";
const WORKER_ONLY = "Mã lỗi này chỉ dành cho worker, không áp dụng cho giao diện.";

export const ERROR_MESSAGES: Record<ClientErrorCode, ErrorMessage> = {
  authentication_required: { title: "Phiên đăng nhập đã hết hạn. Đăng nhập lại để tiếp tục" },
  invalid_csrf: { title: "Phiên làm việc không còn hợp lệ", hint: "Tải lại trang rồi thử lại." },
  permission_denied: {
    title: "Bạn không có quyền thực hiện thao tác này",
    hint: "Liên hệ quản trị viên tenant nếu cần thêm quyền.",
  },
  resource_not_found: { title: "Không tìm thấy hoặc bạn không có quyền xem" },
  validation_failed: {
    title: "Dữ liệu gửi lên không hợp lệ",
    hint: "Sửa các trường được đánh dấu rồi gửi lại.",
    showServerMessage: true,
  },
  invalid_cursor: { title: "Vị trí trang không còn hợp lệ, đã quay về trang đầu" },
  idempotency_conflict: { title: "Yêu cầu trùng khóa chống gửi lặp nhưng khác nội dung", hint: INTERFACE_BUG },
  idempotency_in_progress: {
    title: "Yêu cầu trước đó vẫn đang được xử lý",
    hint: "Đợi vài giây rồi gửi lại; yêu cầu sẽ không bị tạo trùng.",
  },
  one_time_secret_unavailable: {
    title: "Token chỉ hiển thị một lần và không thể xem lại",
    hint: "Thu hồi token này rồi tạo token mới.",
  },
  precondition_required: { title: "Thiếu điều kiện phiên bản (If-Match)", hint: INTERFACE_BUG },
  version_conflict: { title: "Dữ liệu vừa thay đổi", hint: "Kiểm tra trạng thái mới rồi thử lại." },
  state_conflict: { title: "Thao tác không phù hợp với trạng thái hiện tại", showServerMessage: true },
  infeasible_request: {
    title: "Tài nguyên yêu cầu vượt khả năng của hệ thống",
    hint: "Giảm CPU hoặc RAM, hoặc kiểm tra giới hạn của template.",
    showServerMessage: true,
  },
  rate_limited: { title: "Bạn gửi yêu cầu quá nhanh", hint: "Đợi hết thời gian chờ rồi gửi lại." },
  quota_exceeded: {
    title: "Tenant đã chạm hạn mức",
    hint: "Đợi các job hiện có hoàn tất, hoặc liên hệ quản trị viên tenant để nâng hạn mức. Yêu cầu không được gửi lại tự động.",
    showServerMessage: true,
  },
  queue_full: { title: "Hệ thống tạm thời quá tải: hàng đợi đã đầy", hint: "Thử lại sau ít phút." },
  dependency_unavailable: { title: "Hệ thống tạm thời không sẵn sàng", hint: "Thử lại sau ít phút." },
  checksum_mismatch: {
    title: "Checksum không khớp: nội dung tệp khác với lúc tính SHA-256",
    hint: "Chọn lại tệp rồi tải lên lại.",
  },
  payload_too_large: {
    title: "Tệp hoặc yêu cầu vượt giới hạn kích thước của máy chủ",
    hint: "Dùng CLI cho tệp lớn hoặc liên hệ quản trị viên.",
  },
  storage_pressure: {
    title: "Hệ thống tạm thời ngừng nhận dữ liệu vì bộ nhớ lưu trữ sắp đầy",
    hint: "Thử lại sau hoặc liên hệ quản trị viên.",
  },
  stale_authority: { title: "Lỗi quyền của worker", hint: WORKER_ONLY },
  lease_expired: { title: "Lease của worker đã hết hạn", hint: WORKER_ONLY },
  callback_replayed: { title: "Callback của worker bị gửi lặp", hint: WORKER_ONLY },
  internal_error: {
    title: "Lỗi hệ thống",
    hint: "Thử lại; nếu vẫn lỗi, gửi mã yêu cầu cho quản trị viên.",
  },
  network_error: {
    title: "Không kết nối được máy chủ",
    hint: "Kiểm tra kết nối rồi thử lại. Gửi lại một thao tác không tạo trùng.",
  },
  timeout: { title: "Máy chủ không phản hồi kịp", hint: "Thử lại sau ít phút." },
  unexpected_response: {
    title: "Máy chủ trả phản hồi không đúng định dạng",
    hint: "Thử lại; nếu vẫn lỗi, gửi mã yêu cầu cho quản trị viên.",
  },
};

const CLIENT_ONLY = new Set<ClientErrorCode>(["network_error", "timeout", "unexpected_response"]);

export const CONTRACT_ERROR_CODES = (Object.keys(ERROR_MESSAGES) as ClientErrorCode[]).filter(
  (code): code is ContractErrorCode => !CLIENT_ONLY.has(code),
);

export interface ErrorView {
  code: ClientErrorCode;
  status: number;
  title: string;
  hint: string | null;
  detail: string | null;
  requestId: string | null;
  retryAfterSeconds: number | null;
}

export function describeError(error: unknown): ErrorView {
  if (!(error instanceof ApiError)) {
    // Programming errors and unknown throwables: never show their message or stack.
    const message = ERROR_MESSAGES.internal_error;
    return {
      code: "internal_error",
      status: 0,
      title: message.title,
      hint: message.hint ?? null,
      detail: null,
      requestId: null,
      retryAfterSeconds: null,
    };
  }
  const message = ERROR_MESSAGES[error.code] ?? ERROR_MESSAGES.unexpected_response;
  return {
    code: error.code,
    status: error.status,
    title: message.title,
    hint: message.hint ?? null,
    detail: message.showServerMessage ? error.serverMessage : null,
    requestId: error.requestId,
    // Quota frees up when jobs finish, not after the server's short Retry-After hint.
    retryAfterSeconds: error.code === "quota_exceeded" ? null : error.retryAfterSeconds,
  };
}

export function isApiError(error: unknown, ...codes: ClientErrorCode[]): error is ApiError {
  return error instanceof ApiError && (codes.length === 0 || codes.includes(error.code));
}

/**
 * A cursor taken from the URL that the server refuses: expired or forged (invalid_cursor), or
 * outside the 16–2048 character query contract (validation_failed). Lists go back to page one.
 */
export function isBadCursorError(error: unknown, cursor: string | null): boolean {
  return cursor !== null && isApiError(error, "invalid_cursor", "validation_failed");
}
