// Allocatable (inventory) · held (first HELD + QUARANTINED pages) · estimated free (A6).
import type { ResourceCapacityVector } from "../../../api/types";
import { CliHint } from "../../../components/CliHint";
import { formatBytes, formatCores } from "../../../components/format";
import type { CapacityRows } from "../actions";

function cells(vector: ResourceCapacityVector | null) {
  if (vector === null) {
    return (
      <>
        <td>—</td>
        <td>—</td>
        <td>—</td>
      </>
    );
  }
  return (
    <>
      <td>{formatCores(vector.cpu_millis)}</td>
      <td>{formatBytes(vector.memory_bytes)}</td>
      <td>{vector.gpu_count}</td>
    </>
  );
}

export function CapacityTable({ rows, caption }: { rows: CapacityRows; caption: string }) {
  return (
    <div className="stack-tight">
      <div className="table-wrap">
        <table>
          <caption>{caption}</caption>
          <thead>
            <tr>
              <th scope="col">Loại</th>
              <th scope="col">CPU</th>
              <th scope="col">RAM</th>
              <th scope="col">GPU</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Có thể cấp phát</th>
              {cells(rows.allocatable)}
            </tr>
            <tr>
              <th scope="row">Đang giữ (HELD + QUARANTINED){rows.incomplete ? " — chưa đầy đủ" : ""}</th>
              {cells(rows.held)}
            </tr>
            <tr>
              <th scope="row">Còn trống (ước tính)</th>
              {cells(rows.free)}
            </tr>
          </tbody>
        </table>
      </div>
      {rows.allocatable === null && <p className="muted">Worker chưa gửi inventory.</p>}
      {rows.incomplete && (
        <>
          <p className="warning">Chưa đầy đủ: có hơn 100 phân bổ ở một trạng thái; số đang giữ chỉ tính trang đầu.</p>
          <CliHint text="Xem toàn bộ phân bổ bằng CLI:" command="nexa admin allocations --state HELD" />
        </>
      )}
      <p className="muted">Còn trống là ước tính trên giao diện; scheduler dùng dữ liệu của server.</p>
    </div>
  );
}
