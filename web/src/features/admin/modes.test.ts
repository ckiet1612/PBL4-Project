import { describe, expect, it } from "vitest";
import { MODE_LABELS, NOT_REOPENABLE_WARNING, modeOptions } from "./modes";

describe("operational mode options (docs/web-ui.md A7, domain/policy.py)", () => {
  it("offers only the transitions validate_mode_transition allows", () => {
    expect(modeOptions("NORMAL").map((o) => o.target)).toEqual(["ADMISSION_OFF"]);
    expect(modeOptions("ADMISSION_OFF").map((o) => o.target)).toEqual(["NORMAL", "WRITE_FROZEN"]);
    expect(modeOptions("WRITE_FROZEN").map((o) => o.target)).toEqual(["ADMISSION_OFF"]);
  });

  it("states each consequence; reopening NORMAL depends on readiness, freeze/restore wait for B21", () => {
    const [off] = modeOptions("NORMAL");
    expect(off.consequence).toBe("Ngừng nhận job mới từ mọi tenant; job đã nhận vẫn chạy.");
    expect(off.note).toBe("Mở lại NORMAL cần cơ sở dữ liệu, lưu trữ và worker sẵn sàng.");
    expect(off.reopenWarning).toBe(false);

    const [normal, frozen] = modeOptions("ADMISSION_OFF");
    expect(normal.consequence).toBe("Nhận job và điều phối trở lại.");
    expect(normal.note).toBe(
      "Chỉ mở lại được khi cơ sở dữ liệu, lưu trữ và worker đều sẵn sàng. Nếu chưa, máy chủ từ chối và giữ nguyên chế độ.",
    );
    expect(normal.reopenWarning).toBe(false);
    expect(frozen.consequence).toBe(
      "Khóa mọi thay đổi quản trị và nhận job. Cần mọi container đã dừng và allocation đã reconcile.",
    );
    expect(frozen.reopenWarning).toBe(true);

    const [unfreeze] = modeOptions("WRITE_FROZEN");
    expect(unfreeze.consequence).toBe("Mở lại thay đổi quản trị sau khi khôi phục đã được xác minh.");
    expect(unfreeze.reopenWarning).toBe(true);
    expect(NOT_REOPENABLE_WARNING).toBe("Bản hiện tại chưa hỗ trợ đóng băng/khôi phục qua API (B21).");
  });

  it("labels every mode", () => {
    expect(Object.keys(MODE_LABELS).sort()).toEqual(["ADMISSION_OFF", "NORMAL", "WRITE_FROZEN"]);
  });
});
