// Operational mode choices, mirroring validate_mode_transition (src/nexa/domain/policy.py, A7).
import type { OperationalMode } from "../../api/types";

export const MODE_LABELS: Record<OperationalMode, string> = {
  NORMAL: "Bình thường",
  ADMISSION_OFF: "Ngừng nhận job",
  WRITE_FROZEN: "Khóa ghi",
};

/** Freeze/restore proofs still fail closed on the server (B21); reopening NORMAL does not (B19-R02). */
export const NOT_REOPENABLE_WARNING = "Bản hiện tại chưa hỗ trợ đóng băng/khôi phục qua API (B21).";

export interface ModeOption {
  target: OperationalMode;
  consequence: string;
  /** Entering/leaving WRITE_FROZEN needs a recovery proof the current release cannot give. */
  reopenWarning: boolean;
  note?: string;
}

const OPTIONS: Record<OperationalMode, ModeOption[]> = {
  NORMAL: [
    {
      target: "ADMISSION_OFF",
      consequence: "Ngừng nhận job mới từ mọi tenant; job đã nhận vẫn chạy.",
      reopenWarning: false,
      note: "Mở lại NORMAL cần cơ sở dữ liệu, lưu trữ và worker sẵn sàng.",
    },
  ],
  ADMISSION_OFF: [
    {
      target: "NORMAL",
      consequence: "Nhận job và điều phối trở lại.",
      reopenWarning: false,
      note: "Chỉ mở lại được khi cơ sở dữ liệu, lưu trữ và worker đều sẵn sàng. Nếu chưa, máy chủ từ chối và giữ nguyên chế độ.",
    },
    {
      target: "WRITE_FROZEN",
      consequence: "Khóa mọi thay đổi quản trị và nhận job. Cần mọi container đã dừng và allocation đã reconcile.",
      reopenWarning: true,
    },
  ],
  WRITE_FROZEN: [
    {
      target: "ADMISSION_OFF",
      consequence: "Mở lại thay đổi quản trị sau khi khôi phục đã được xác minh.",
      reopenWarning: true,
    },
  ],
};

export function modeOptions(current: OperationalMode): ModeOption[] {
  return OPTIONS[current];
}
