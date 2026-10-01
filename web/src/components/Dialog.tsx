// Native modal <dialog>: the browser keeps focus inside and Esc fires "cancel".
import { useEffect, useId, useRef, type FormEvent, type ReactNode } from "react";

interface DialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  /** Esc, the close button and "Bỏ qua". Ignored while busy. */
  onClose(): void;
  busy?: boolean;
}

export function Dialog({ open, title, children, onClose, busy = false }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const busyRef = useRef(busy);
  busyRef.current = busy;

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const onCancel = (event: Event) => {
      event.preventDefault();
      if (!busyRef.current) onClose();
    };
    dialog.addEventListener("cancel", onCancel);
    return () => dialog.removeEventListener("cancel", onCancel);
  }, [onClose]);

  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId}>
      {open && (
        <>
          <h2 id={titleId}>{title}</h2>
          {children}
        </>
      )}
    </dialog>
  );
}

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  danger?: boolean;
  busy?: boolean;
  /** Blocks confirm (e.g. invalid reason) without closing. */
  confirmDisabled?: boolean;
  onConfirm(): void;
  onCancel(): void;
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  busyLabel = "Đang gửi…",
  danger = false,
  busy = false,
  confirmDisabled = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!busy && !confirmDisabled) onConfirm();
  };
  return (
    <Dialog open={open} title={title} onClose={onCancel} busy={busy}>
      <form onSubmit={submit} className="dialog-body">
        {children}
        <div className="actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            Bỏ qua
          </button>
          <button type="submit" className={danger ? "danger" : "primary"} disabled={busy || confirmDisabled}>
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
