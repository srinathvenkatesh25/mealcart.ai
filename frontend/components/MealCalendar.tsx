"use client";

import type { DayTotals, MealPlan, MealSpec, Slot } from "@/lib/types";

export const SLOT_ORDER: Slot[] = ["breakfast", "mid_morning", "lunch", "evening", "dinner"];
export const SLOT_LABEL: Record<Slot, string> = {
  breakfast: "Breakfast", mid_morning: "Morning snack", lunch: "Lunch", evening: "Evening snack", dinner: "Dinner",
};

const kcal = (n: number) => Math.round(n).toLocaleString();

/** The validator's tolerances: calories within 5% and protein within 10 g of the daily target. */
export function onTarget(t: DayTotals, spec: MealSpec): boolean {
  return Math.abs(t.calories_delta) <= spec.macros.calories * 0.05 && Math.abs(t.protein_g_delta) <= 10;
}

/** Cells are small: drop filler words and the cuisine (already in the header). The pop-up shows the full name. */
export function shortName(title: string, cuisines: string[]): string {
  let out = title.replace(/^(Quick|Easy|Simple|One-Pot|One Pot|Sheet Pan)\s+/i, "");
  for (const c of cuisines) {
    const word = c.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (word) out = out.replace(new RegExp(`(^|\\s)${word}(?=\\s)`, "i"), "$1");
  }
  out = out.replace(/\s+/g, " ").trim();
  return out ? out.charAt(0).toUpperCase() + out.slice(1) : title;
}

/**
 * The week as a colour-coded calendar of meal names. Wide screens: a column per day and a row per
 * meal. Phones: a row per day. Every number lives in the meal pop-up; day headers carry the day's
 * totals and a check when the day is within the targets.
 */
export default function MealCalendar({ plan, perDay, spec, selected, onOpen }: {
  plan: MealPlan; perDay: Record<string, DayTotals> | null; spec: MealSpec | null;
  selected: { day: string; slot: Slot } | null; onOpen: (day: string, slot: Slot, opener: HTMLElement) => void;
}) {
  const slots = SLOT_ORDER.filter((s) => plan.days.some((d) => d.meals.some((m) => m.slot === s)));
  const cuisines = spec?.cuisines ?? [];
  const totals = perDay ? Object.values(perDay) : [];
  const week = totals.reduce((a, t) => ({ c: a.c + t.calories, p: a.p + t.protein_g }), { c: 0, p: 0 });
  // Grid placement for both layouts: --mc/--mr on phones, --dc/--dr on wide screens.
  const at = (mc: number, mr: number, dc: number, dr: number) =>
    ({ "--mc": mc, "--mr": mr, "--dc": dc, "--dr": dr } as React.CSSProperties);

  return (
    <>
      <div className="cal" role="table" aria-label="Meal plan for the week"
        style={{ "--days": plan.days.length, "--slots": slots.length } as React.CSSProperties}>
        <span className="cal-corner" style={at(1, 1, 1, 1)} />
        {slots.map((slot, s) => (
          <span key={slot} className={`cal-slot slot-${slot}`} role="columnheader" style={at(s + 2, 1, 1, s + 2)}>
            {SLOT_LABEL[slot]}
          </span>
        ))}
        {plan.days.map((d, i) => {
          const t = perDay?.[d.day];
          return [
            <div key={`${d.day}-h`} className="cal-day" role="rowheader" style={at(1, i + 2, i + 2, 1)}>
              <b>
                {d.day.slice(0, 3)}
                {t && spec && (onTarget(t, spec)
                  ? <span className="cal-ok" title="Within your targets" aria-label="within your targets">✓</span>
                  : <span className="cal-off" title="Outside your targets" aria-label="outside your targets">!</span>)}
              </b>
              {t && (
                <span className="cal-totals">
                  {kcal(t.calories)}<span className="unit"> kcal</span><br />
                  {Math.round(t.protein_g)} g<span className="unit"> protein</span>
                </span>
              )}
            </div>,
            ...slots.map((slot, s) => {
              const meal = d.meals.find((m) => m.slot === slot);
              const on = selected?.day === d.day && selected.slot === slot;
              return (
                <div key={`${d.day}-${slot}`} className={`cal-cell slot-${slot}`} role="cell" style={at(s + 2, i + 2, i + 2, s + 2)}>
                  {meal ? (
                    <button type="button" aria-pressed={on} aria-label={`${d.day} ${SLOT_LABEL[slot].toLowerCase()}: ${meal.title}`}
                      onClick={(e) => onOpen(d.day, slot, e.currentTarget)}>
                      <span className="cal-title">{shortName(meal.title, cuisines)}</span>
                    </button>
                  ) : <span className="cal-empty">—</span>}
                </div>
              );
            }),
          ];
        })}
      </div>
      <div className="cal-legend">
        {slots.map((slot) => <span key={slot} className={`slot-${slot}`}><i aria-hidden="true" />{SLOT_LABEL[slot]}</span>)}
        {spec && <span>✓ = within ±5% of {spec.macros.calories.toLocaleString()} kcal and ±10 g of {spec.macros.protein_g} g protein</span>}
        {totals.length > 0 && <span>Week: <b>{kcal(week.c)} kcal</b> · <b>{Math.round(week.p)} g</b> protein</span>}
      </div>
    </>
  );
}
