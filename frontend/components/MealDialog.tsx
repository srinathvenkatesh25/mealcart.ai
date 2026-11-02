"use client";

import { useEffect, useRef, useState } from "react";
import type { DayPlan, Meal, MealNutrition, Slot, SwapRequest } from "@/lib/types";
import { SLOT_LABEL, SLOT_ORDER } from "./MealCalendar";

const name = (s: string) => s.replace(/_/g, " ");
const kcal = (n: number) => Math.round(n).toLocaleString();
const g = (n: number) => `${Math.round(n)} g`;
const hasPrep = (p: string) => p && !/^(none|n\/a|-)$/i.test(p.trim());

function SwapForm({ day, meal, onSwap, onCancel }: {
  day: string; meal: Meal; onSwap: (req: SwapRequest) => void; onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [avoid, setAvoid] = useState<string[]>([]);
  return (
    <form className="swap" onSubmit={(e) => { e.preventDefault(); onSwap({ day, slot: meal.slot, reason, avoid }); }}>
      <label className="field">
        <span>What would you rather have? <em>optional</em></span>
        <input autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder="something lighter, no dal" />
      </label>
      <div className="field">
        <span id="avoid-label">Never use again this week</span>
        <div className="chips" role="group" aria-labelledby="avoid-label">
          {meal.ingredients.map((i) => (
            <label key={i.name} className="chip">
              <input type="checkbox" checked={avoid.includes(i.name)}
                onChange={() => setAvoid((a) => a.includes(i.name) ? a.filter((x) => x !== i.name) : [...a, i.name])} />
              {name(i.name)}
            </label>
          ))}
        </div>
      </div>
      <div className="row">
        <button className="btn btn-primary" type="submit">Swap this meal</button>
        <button className="btn btn-quiet" type="button" onClick={onCancel}>Keep it</button>
      </div>
    </form>
  );
}

function MealBody({ day, meal, nutrition, canSwap, onSwap }: {
  day: string; meal: Meal; nutrition: MealNutrition | undefined; canSwap: boolean; onSwap: (req: SwapRequest) => void;
}) {
  const [view, setView] = useState<"ingredients" | "recipe">("ingredients");
  const [swapping, setSwapping] = useState(false);
  const rows = new Map((nutrition?.ingredients ?? []).map((r) => [r.name, r]));
  return (
    <div className="dialog-meal">
      <div className="dialog-meal-top">
        <h3>{meal.title}</h3>
        <span className="fine">{meal.prep_minutes} min{meal.equipment_used.length ? ` · ${meal.equipment_used.map(name).join(", ")}` : ""}</span>
      </div>
      {nutrition && (
        <dl className="macro-strip" aria-label={`Nutrition for ${meal.title}`}>
          <div><dt>Calories</dt><dd>{kcal(nutrition.calories)}</dd></div>
          <div><dt>Protein</dt><dd>{g(nutrition.protein_g)}</dd></div>
          <div className="minor"><dt>Carbs</dt><dd>{g(nutrition.carbs_g)}</dd></div>
          <div className="minor"><dt>Fat</dt><dd>{g(nutrition.fat_g)}</dd></div>
        </dl>
      )}
      <div className="spec-tabs dialog-view" role="tablist" aria-label="Meal details">
        <button type="button" role="tab" aria-selected={view === "ingredients"} onClick={() => setView("ingredients")}>Ingredients</button>
        <button type="button" role="tab" aria-selected={view === "recipe"} onClick={() => setView("recipe")}>Recipe</button>
      </div>
      {view === "ingredients" ? (
        <>
          <table className="ing">
            <thead>
              <tr><th scope="col">Ingredient</th><th scope="col" className="num">Grams</th><th scope="col" className="num">kcal</th>
                <th scope="col" className="num">Protein</th><th scope="col" className="num ing-cf">Carbs · fat</th></tr>
            </thead>
            <tbody>
              {meal.ingredients.map((i) => {
                const r = rows.get(i.name);
                return (
                  <tr key={i.name}>
                    <td>{name(i.name)}{hasPrep(i.preparation) && <span className="muted">, {i.preparation}</span>}</td>
                    <td className="num mono">{Math.round(i.grams)}</td>
                    <td className="num mono">{r ? kcal(r.calories) : "—"}</td>
                    <td className="num mono">{r ? g(r.protein_g) : "—"}</td>
                    <td className="num mono ing-cf">{r ? `${g(r.carbs_g)} · ${g(r.fat_g)}` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="fine">Raw weights. Values are rounded, so a total can differ by a point or two.</p>
        </>
      ) : meal.steps.length ? (
        <ol className="steps">{meal.steps.map((s, n) => <li key={n}>{s.replace(/^\s*\d+[.)]\s*/, "")}</li>)}</ol>
      ) : <p className="muted">No steps for this meal.</p>}
      {canSwap && (swapping
        ? <SwapForm day={day} meal={meal} onSwap={onSwap} onCancel={() => setSwapping(false)} />
        : (
          <div className="dialog-foot">
            <span className="fine">Don&apos;t like it? Swap only this meal; the day is re-balanced and re-checked.</span>
            <button type="button" className="btn btn-quiet" onClick={() => setSwapping(true)}>Swap this meal</button>
          </div>
        ))}
    </div>
  );
}

/** One day's meals in a pop-up: a tab per meal, its numbers, ingredients or recipe, and Swap while approving. */
export default function MealDialog({ day, slot, totals, nutrition, canSwap, onSlot, onSwap, onClose }: {
  day: DayPlan | null; slot: Slot | null; totals: { calories: number; protein_g: number } | undefined;
  nutrition: MealNutrition[] | undefined; canSwap: boolean;
  onSlot: (slot: Slot) => void; onSwap: (req: SwapRequest) => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const open = Boolean(day && slot);

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const meals = day ? SLOT_ORDER.flatMap((s) => {
    const i = day.meals.findIndex((m) => m.slot === s);
    return i >= 0 ? [{ meal: day.meals[i], nutrition: nutrition?.[i] }] : [];
  }) : [];
  const current = meals.find((m) => m.meal.slot === slot);

  return (
    <dialog ref={dialog} className="meal-dialog" aria-labelledby="meal-dialog-title" onClose={onClose}
      onClick={(e) => { if (e.target === dialog.current) onClose(); }}>
      <button type="button" className="dialog-close" aria-label="Close" onClick={onClose}>×</button>
      {day && current && (
        <>
          <div className="dialog-head">
            <h2 id="meal-dialog-title">{day.day}</h2>
            {totals && <span className="fine">{kcal(totals.calories)} kcal · {g(totals.protein_g)} protein for the day</span>}
          </div>
          <div className="meal-tabs" role="tablist" aria-label="Meals">
            {meals.map(({ meal, nutrition: n }) => (
              <button key={meal.slot} type="button" role="tab" className={`slot-${meal.slot}`}
                aria-selected={meal.slot === slot} onClick={() => onSlot(meal.slot)}>
                {SLOT_LABEL[meal.slot]}{n && <span className="mono"> {kcal(n.calories)}</span>}
              </button>
            ))}
          </div>
          <MealBody key={`${day.day}|${current.meal.slot}|${current.meal.title}`} day={day.day} meal={current.meal}
            nutrition={current.nutrition} canSwap={canSwap} onSwap={(req) => { onSwap(req); onClose(); }} />
        </>
      )}
    </dialog>
  );
}
