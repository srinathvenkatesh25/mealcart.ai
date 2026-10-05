"use client";

import { useState } from "react";
import type { DayPlan, DayTotals, Meal, MealNutrition, MealPlan, MealSpec, SwapRequest } from "@/lib/types";

const SLOT_LABEL: Record<string, string> = {
  breakfast: "Breakfast", mid_morning: "Snack", lunch: "Lunch", evening: "Snack", dinner: "Dinner",
};

const name = (s: string) => s.replace(/_/g, " ");
const signed = (n: number, unit = "") => {
  const r = Math.round(n); // sign after rounding, so -0.4 reads "±0", not "−0"
  return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r)}${unit}`;
};
const kcal = (n: number) => Math.round(n).toLocaleString();
const grams = (n: number) => `${Math.round(n)} g`;
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
        <span>Never use again this week</span>
        <div className="chips">
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

function MealRow({ day, meal, nutrition, canSwap, onSwap }: {
  day: string; meal: Meal; nutrition: MealNutrition | undefined; canSwap: boolean; onSwap: (req: SwapRequest) => void;
}) {
  const [swapping, setSwapping] = useState(false);
  const rows = new Map((nutrition?.ingredients ?? []).map((r) => [r.name, r]));
  return (
    <li className="meal">
      <details>
        <summary>
          <span className="meal-slot">{SLOT_LABEL[meal.slot]}</span>
          <span className="meal-title">{meal.title}</span>
          <span className="meal-time">{meal.prep_minutes} min</span>
          {nutrition && (
            <span className="meal-macros">
              {kcal(nutrition.calories)} kcal · {grams(nutrition.protein_g)} protein
            </span>
          )}
        </summary>
        <div className="meal-body">
          {nutrition && (
            <dl className="macro-strip" aria-label={`Nutrition for ${meal.title}`}>
              <div><dt>Calories</dt><dd>{kcal(nutrition.calories)}</dd></div>
              <div><dt>Protein</dt><dd>{grams(nutrition.protein_g)}</dd></div>
              <div className="minor"><dt>Carbs</dt><dd>{grams(nutrition.carbs_g)}</dd></div>
              <div className="minor"><dt>Fat</dt><dd>{grams(nutrition.fat_g)}</dd></div>
            </dl>
          )}
          <ul className="ingredients">
            {meal.ingredients.map((i) => {
              const r = rows.get(i.name);
              return (
                <li key={i.name}>
                  <span className="grams">{Math.round(i.grams)} g</span>
                  <span>{name(i.name)}{hasPrep(i.preparation) && <em>, {i.preparation}</em>}</span>
                  {r && <span className="ing-macros">{kcal(r.calories)} kcal · {grams(r.protein_g)}</span>}
                </li>
              );
            })}
          </ul>
          {meal.steps.length > 0 && (
            <ol className="steps">{meal.steps.map((s, n) => <li key={n}>{s}</li>)}</ol>
          )}
          {meal.equipment_used.length > 0 && <p className="equipment">Uses {meal.equipment_used.map(name).join(", ")}</p>}
        </div>
      </details>
      {canSwap && !swapping && (
        <button className="btn btn-link swap-trigger" onClick={() => setSwapping(true)}
          aria-label={`Swap ${day} ${SLOT_LABEL[meal.slot].toLowerCase()}: ${meal.title}`}>
          Swap
        </button>
      )}
      {swapping && (
        <SwapForm day={day} meal={meal} onCancel={() => setSwapping(false)}
          onSwap={(req) => { setSwapping(false); onSwap(req); }} />
      )}
    </li>
  );
}

function DayLabel({ day, totals, meals, spec, canSwap, onSwap }: {
  day: DayPlan; totals: DayTotals | undefined; meals: MealNutrition[] | undefined; spec: MealSpec | null;
  canSwap: boolean; onSwap: (req: SwapRequest) => void;
}) {
  const kcalTarget = spec?.macros.calories;
  const proteinTarget = spec?.macros.protein_g;
  return (
    <article className="label" aria-label={`${day.day} plan`}>
      <h3 className="label-day">{day.day}</h3>
      <div className="label-rule-thick" />
      <div className="label-row label-row-big">
        <span>Calories</span>
        <strong>{totals ? Math.round(totals.calories).toLocaleString() : "—"}</strong>
      </div>
      {totals && kcalTarget && (
        <div className="label-target">target {kcalTarget.toLocaleString()} · {signed(totals.calories_delta)}</div>
      )}
      <div className="label-rule-mid" />
      <div className="label-row">
        <span><b>Protein</b></span>
        <strong>{totals ? `${Math.round(totals.protein_g)} g` : "—"}</strong>
      </div>
      {totals && proteinTarget !== undefined && (
        <div className="label-target">target {proteinTarget} g · {signed(totals.protein_g_delta, " g")}</div>
      )}
      {totals && (
        <div className="label-row label-row-info">
          <span>Carbs {Math.round(totals.carbs_g)} g</span>
          <span>Fat {Math.round(totals.fat_g)} g</span>
        </div>
      )}
      <div className="label-rule-thick" />
      <ul className="meals">
        {day.meals.map((m, i) => (
          <MealRow key={m.slot} day={day.day} meal={m} nutrition={meals?.[i]} canSwap={canSwap} onSwap={onSwap} />
        ))}
      </ul>
    </article>
  );
}

export default function PlanView({ plan, perDay, perMeal, spec, canSwap, onSwap }: {
  plan: MealPlan; perDay: Record<string, DayTotals> | null; perMeal: Record<string, MealNutrition[]> | null;
  spec: MealSpec | null; canSwap: boolean; onSwap: (req: SwapRequest) => void;
}) {
  return (
    <section className="plan" aria-labelledby="plan-heading">
      <div className="section-head">
        <h2 id="plan-heading">The week</h2>
        <p>
          Calories and protein for every meal add up to the day on its label (shown rounded, so a total can differ by a
          point or two). Open a meal for its carbs, fat and per-ingredient numbers.
          {canSwap && " Don't like a meal? Swap it before you approve."}
        </p>
      </div>
      <div className="labels">
        {plan.days.map((d) => (
          <DayLabel key={d.day} day={d} totals={perDay?.[d.day]} meals={perMeal?.[d.day]} spec={spec}
            canSwap={canSwap} onSwap={onSwap} />
        ))}
      </div>
    </section>
  );
}
