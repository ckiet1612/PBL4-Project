// WAI-ARIA tabs with roving focus; the active tab is owned by the caller (URL ?tab=).
import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem<K extends string> {
  id: K;
  label: string;
}

interface TabsProps<K extends string> {
  label: string;
  tabs: TabItem<K>[];
  active: K;
  onSelect(id: K): void;
  children: ReactNode;
}

export function Tabs<K extends string>({ label, tabs, active, onSelect, children }: TabsProps<K>) {
  const baseId = useId();
  const refs = useRef(new Map<K, HTMLButtonElement>());

  const onKeyDown = (event: KeyboardEvent) => {
    const index = tabs.findIndex((tab) => tab.id === active);
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    const id = tabs[next].id;
    onSelect(id);
    refs.current.get(id)?.focus();
  };

  return (
    <div className="tabs">
      <div role="tablist" aria-label={label} className="tab-list" onKeyDown={onKeyDown}>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            ref={(element) => {
              if (element) refs.current.set(tab.id, element);
              else refs.current.delete(tab.id);
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${tab.id}`}
            aria-selected={tab.id === active}
            aria-controls={`${baseId}-panel`}
            tabIndex={tab.id === active ? 0 : -1}
            onClick={() => onSelect(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`${baseId}-panel`} aria-labelledby={`${baseId}-tab-${active}`} className="tab-panel">
        {children}
      </div>
    </div>
  );
}
