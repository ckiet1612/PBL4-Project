// 412 on a policy form: the server's current values next to what the admin is editing.
import { POLICY_FIELD_LABELS, type PolicyDraft, type PolicyField } from "../policyForm";

interface ConflictCompareProps {
  server: PolicyDraft;
  draft: PolicyDraft;
  message: string;
  onUseServer(): void;
}

export function ConflictCompare({ server, draft, message, onUseServer }: ConflictCompareProps) {
  const fields = (Object.keys(POLICY_FIELD_LABELS) as PolicyField[]).filter((field) => server[field] !== draft[field]);
  return (
    <section className="warning-panel stack-tight" role="alert" aria-label="Xung đột phiên bản">
      <p className="warning">{message}</p>
      {fields.length === 0 ? (
        <p>Giá trị trên máy chủ trùng với giá trị đang sửa.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <caption>Khác nhau giữa máy chủ và form</caption>
            <thead>
              <tr>
                <th scope="col">Trường</th>
                <th scope="col">Trên máy chủ</th>
                <th scope="col">Đang sửa</th>
              </tr>
            </thead>
            <tbody>
              {fields.map((field) => (
                <tr key={field}>
                  <th scope="row">{POLICY_FIELD_LABELS[field]}</th>
                  <td>{server[field]}</td>
                  <td>{draft[field]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="actions">
        <button type="button" onClick={onUseServer}>
          Dùng giá trị máy chủ
        </button>
      </div>
    </section>
  );
}
