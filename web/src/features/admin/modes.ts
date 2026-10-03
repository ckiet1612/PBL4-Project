// Operational mode choices, mirroring validate_mode_transition (src/nexa/domain/policy.py, A7).
import type { OperationalMode } from "../../api/types";

export const MODE_LABELS: Record<OperationalMode, string> = {
  NORMAL: "Bình thường",
  ADMISSION_OFF: "Ngừng nhận job",
  WRITE_FROZEN: "Khóa ghi",
};

export const NOT_REOPENABLE_WARNING = "Bản hiện tại chưa mở lại được chế độ qua API (cần bằng chứng khôi phục, B18-R05)";

export interface ModeOption {
  target: OperationalMode;
  consequence: string;
  /** Leaving ADMISSION_OFF/WRITE_FROZEN needs recovery proof the current release cannot give. */
  reopenWarning: boolean;
  note?: string;
}

const OPTIONS: Record<OperationalMode, ModeOption[]> = {
  NORMAL: [
    {
      target: "ADMISSION_OFF",
      consequence: "Ngừng nhận job mới từ mọi tenant; job đã nhận vẫn chạy.",
      reopenWarning: false,
      note: "Trong bản hiện tại không quay lại NORMAL qua API được.",
    },
  ],
  ADMISSION_OFF: [
    {
      target: "NORMAL",
      consequence: "Nhận job trở lại. Cần bằng chứng sẵn sàng và worker đã reconcile.",
      reopenWarning: true,
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
