import { describe, expect, it } from "vitest";
import { globalNavItems } from "./nav";

describe("global nav (B18-R13: no longer needs a tenant)", () => {
  it("shows tenant areas with a tenant and Quản trị for system admins", () => {
    expect(globalNavItems("t1", true)).toEqual([
      { to: "/t/t1/jobs", label: "Jobs" },
      { to: "/t/t1/data", label: "Dữ liệu" },
      { to: "/admin", label: "Quản trị" },
    ]);
    expect(globalNavItems(null, true)).toEqual([{ to: "/admin", label: "Quản trị" }]);
    expect(globalNavItems("t1", false).map((i) => i.label)).toEqual(["Jobs", "Dữ liệu"]);
    expect(globalNavItems(null, false)).toEqual([]);
  });
});
