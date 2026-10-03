// Password entered twice; values live only in the owning form's state (UX-A27).
import { useId } from "react";

interface PasswordFieldsProps {
  password: string;
  confirm: string;
  errors: { password?: string; confirm?: string };
  show: boolean;
  label?: string;
  onChange(password: string, confirm: string): void;
}

export function PasswordFields({ password, confirm, errors, show, label = "Mật khẩu", onChange }: PasswordFieldsProps) {
  const ids = { password: useId(), confirm: useId() };
  const passwordError = show ? errors.password : undefined;
  const confirmError = show ? errors.confirm : undefined;
  return (
    <>
      <div className="field">
        <label htmlFor={ids.password}>{label}</label>
        <input
          id={ids.password}
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => onChange(event.target.value, confirm)}
          aria-invalid={passwordError !== undefined}
          aria-describedby={`${ids.password}-help`}
        />
        <p id={`${ids.password}-help`} className={passwordError ? "field-error" : "help"}>
          {passwordError ?? "12–1024 ký tự."}
        </p>
      </div>
      <div className="field">
        <label htmlFor={ids.confirm}>Nhập lại mật khẩu</label>
        <input
          id={ids.confirm}
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(event) => onChange(password, event.target.value)}
          aria-invalid={confirmError !== undefined}
          aria-describedby={confirmError ? `${ids.confirm}-error` : undefined}
        />
        {confirmError && (
          <p id={`${ids.confirm}-error`} className="field-error">
            {confirmError}
          </p>
        )}
      </div>
    </>
  );
}
