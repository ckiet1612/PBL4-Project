// Cursor pager: "Trang sau" follows next_cursor; "Trang trước" replays the in-memory trail.
interface PagerProps {
  hasPrevious: boolean;
  nextCursor: string | null;
  onFirst(): void;
  onPrevious(): void;
  onNext(cursor: string): void;
  disabled?: boolean;
}

export function Pager({ hasPrevious, nextCursor, onFirst, onPrevious, onNext, disabled = false }: PagerProps) {
  return (
    <nav className="pager" aria-label="Phân trang">
      <button type="button" onClick={onFirst} disabled={disabled || !hasPrevious}>
        Về trang đầu
      </button>
      <button type="button" onClick={onPrevious} disabled={disabled || !hasPrevious}>
        Trang trước
      </button>
      <button type="button" onClick={() => nextCursor && onNext(nextCursor)} disabled={disabled || nextCursor === null}>
        Trang sau
      </button>
    </nav>
  );
}
