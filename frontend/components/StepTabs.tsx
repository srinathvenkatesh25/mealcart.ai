"use client";

export type Step = "meals" | "list" | "cart";

export interface StepTab {
  id: Step;
  label: string;
  note: string;
  disabled?: boolean;
}

/** The run as three steps: Meals, Grocery list, Instacart cart. */
export default function StepTabs({ tabs, value, onChange }: { tabs: StepTab[]; value: Step; onChange: (s: Step) => void }) {
  return (
    <div className="step-tabs" role="tablist" aria-label="Steps">
      {tabs.map((t, i) => (
        <button key={t.id} type="button" role="tab" id={`step-${t.id}`} aria-selected={value === t.id}
          aria-controls={`step-panel-${t.id}`} disabled={t.disabled} onClick={() => onChange(t.id)}
          onKeyDown={(e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            const dir = e.key === "ArrowRight" ? 1 : -1;
            for (let n = 1; n < tabs.length; n++) {
              const next = tabs[(i + dir * n + tabs.length) % tabs.length];
              if (!next.disabled) { onChange(next.id); document.getElementById(`step-${next.id}`)?.focus(); break; }
            }
          }}>
          <span><span className="step-num">{i + 1}</span>{t.label}</span>
          <small>{t.note}</small>
        </button>
      ))}
    </div>
  );
}
