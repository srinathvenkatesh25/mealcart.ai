"use client";

import { useState } from "react";
import type { CookingSkill, EffortLevel, MealSpec } from "@/lib/types";

const SPEC_KEY = "mealcart.lastSpec";

export const DEFAULT_SPEC: MealSpec = {
  days: 7,
  macros: { calories: 2200, protein_g: 140 },
  cuisines: ["Indian"],
  budget_weekly_usd: 60,
  zip_code: "",
  include_snacks: true,
  allergies: [],
  dietary_restrictions: [],
  dislikes: ["mushrooms", "mayonnaise"],
  protein_source: "chicken",
  max_prep_minutes: 30,
  effort_level: "low_moderate",
  cooking_skill: "basic",
  equipment: ["stove", "microwave", "oven", "rice_cooker"],
  pantry: [],
};

const EQUIPMENT = ["stove", "microwave", "oven", "rice_cooker", "air_fryer", "pressure_cooker", "blender"];
const RESTRICTIONS = ["vegetarian", "vegan", "pescatarian", "halal", "no_beef", "no_pork"];
const EFFORT: [EffortLevel, string][] = [
  ["low", "Low"], ["low_moderate", "Low to moderate"], ["moderate", "Moderate"], ["high", "High"],
];
const SKILL: [CookingSkill, string][] = [["basic", "Basic"], ["intermediate", "Intermediate"], ["advanced", "Advanced"]];

const label = (s: string) => s.replace(/_/g, " ");
const toList = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

function loadSpec(): MealSpec {
  try {
    const saved = localStorage.getItem(SPEC_KEY);
    return saved ? { ...DEFAULT_SPEC, ...JSON.parse(saved) } : DEFAULT_SPEC;
  } catch {
    return DEFAULT_SPEC;
  }
}

interface Props {
  busy: boolean;
  error: string | null;
  onSubmit: (body: MealSpec | { text: string }) => void;
}

export default function SpecForm({ busy, error, onSubmit }: Props) {
  // Rendered only in the browser (see app/page.tsx), so saved preferences can seed the state.
  const [spec, setSpec] = useState<MealSpec>(loadSpec);
  const [mode, setMode] = useState<"form" | "words">("form");
  const [words, setWords] = useState("");
  // Text fields are kept as typed and split into lists on submit.
  const [text, setText] = useState(() => ({
    cuisines: spec.cuisines.join(", "), dislikes: spec.dislikes.join(", "),
    allergies: spec.allergies.join(", "), pantry: spec.pantry.join(", "),
  }));

  const set = <K extends keyof MealSpec>(key: K, value: MealSpec[K]) => setSpec((s) => ({ ...s, [key]: value }));
  const toggle = (key: "equipment" | "dietary_restrictions", value: string) =>
    setSpec((s) => ({ ...s, [key]: s[key].includes(value) ? s[key].filter((v) => v !== value) : [...s[key], value] }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mode === "words") return onSubmit({ text: words });
    const full: MealSpec = {
      ...spec,
      cuisines: toList(text.cuisines), dislikes: toList(text.dislikes),
      allergies: toList(text.allergies), pantry: toList(text.pantry),
    };
    try {
      localStorage.setItem(SPEC_KEY, JSON.stringify(full));
    } catch {
      // not remembered next time; fine
    }
    onSubmit(full);
  }

  return (
    <form className="spec" onSubmit={submit}>
      <div className="spec-tabs" role="tablist" aria-label="How to describe your week">
        <button type="button" role="tab" aria-selected={mode === "form"} onClick={() => setMode("form")}>
          Set targets
        </button>
        <button type="button" role="tab" aria-selected={mode === "words"} onClick={() => setMode("words")}>
          Describe in words
        </button>
      </div>

      {mode === "words" ? (
        <label className="field field-wide">
          <span>Your week, in a sentence or two</span>
          <textarea
            rows={5}
            value={words}
            onChange={(e) => setWords(e.target.value)}
            placeholder="2200 calories and 140 g protein a day, Indian food with chicken, no mushrooms, 30 minutes per meal, ZIP 12345, $60 a week"
            required
          />
          <small>Include your ZIP code. Anything you leave out uses the defaults.</small>
        </label>
      ) : (
        <>
          <fieldset className="targets">
            <legend>Daily targets</legend>
            <label className="field target">
              <span>Calories</span>
              <input type="number" min={800} max={6000} step={50} required value={spec.macros.calories}
                onChange={(e) => set("macros", { ...spec.macros, calories: Number(e.target.value) })} />
              <small>kcal</small>
            </label>
            <label className="field target">
              <span>Protein</span>
              <input type="number" min={0} max={400} step={5} required value={spec.macros.protein_g}
                onChange={(e) => set("macros", { ...spec.macros, protein_g: Number(e.target.value) })} />
              <small>g</small>
            </label>
          </fieldset>

          <fieldset className="grid">
            <legend>The week</legend>
            <label className="field">
              <span>Days</span>
              <input type="number" min={1} max={7} required value={spec.days}
                onChange={(e) => set("days", Number(e.target.value))} />
            </label>
            <label className="field">
              <span>ZIP code</span>
              <input inputMode="numeric" pattern="[0-9]{5}" maxLength={5} required value={spec.zip_code}
                placeholder="5 digits" autoComplete="postal-code" title="A 5-digit US ZIP code"
                onChange={(e) => set("zip_code", e.target.value.replace(/\D/g, ""))} />
              <small>We check Instacart delivers here.</small>
            </label>
            <label className="field">
              <span>Weekly budget</span>
              <input type="number" min={0} step={5} value={spec.budget_weekly_usd ?? ""}
                onChange={(e) => set("budget_weekly_usd", e.target.value ? Number(e.target.value) : null)} />
              <small>USD</small>
            </label>
            <label className="field">
              <span>Cuisines</span>
              <input value={text.cuisines} placeholder="Indian, Mexican"
                onChange={(e) => setText({ ...text, cuisines: e.target.value })} />
            </label>
            <label className="field">
              <span>Main protein</span>
              <input value={spec.protein_source ?? ""} placeholder="chicken"
                onChange={(e) => set("protein_source", e.target.value || null)} />
            </label>
            <label className="field check">
              <input type="checkbox" checked={spec.include_snacks}
                onChange={(e) => set("include_snacks", e.target.checked)} />
              <span>Include two snacks a day</span>
            </label>
          </fieldset>

          <fieldset className="grid">
            <legend>Never use</legend>
            <label className="field">
              <span>Dislikes</span>
              <input value={text.dislikes} placeholder="mushrooms, mayonnaise"
                onChange={(e) => setText({ ...text, dislikes: e.target.value })} />
            </label>
            <label className="field">
              <span>Allergies</span>
              <input value={text.allergies} placeholder="dairy, tree nuts"
                onChange={(e) => setText({ ...text, allergies: e.target.value })} />
            </label>
            <div className="field field-wide">
              <span id="restrictions-label">Dietary restrictions</span>
              <div className="chips" role="group" aria-labelledby="restrictions-label">
                {RESTRICTIONS.map((r) => (
                  <label key={r} className="chip">
                    <input type="checkbox" checked={spec.dietary_restrictions.includes(r)}
                      onChange={() => toggle("dietary_restrictions", r)} />
                    {label(r)}
                  </label>
                ))}
              </div>
            </div>
          </fieldset>

          <fieldset className="grid">
            <legend>Your kitchen</legend>
            <label className="field">
              <span>Max time per meal</span>
              <input type="number" min={5} max={180} step={5} required value={spec.max_prep_minutes}
                onChange={(e) => set("max_prep_minutes", Number(e.target.value))} />
              <small>min</small>
            </label>
            <label className="field">
              <span>Effort</span>
              <select value={spec.effort_level} onChange={(e) => set("effort_level", e.target.value as EffortLevel)}>
                {EFFORT.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="field">
              <span>Cooking skill</span>
              <select value={spec.cooking_skill} onChange={(e) => set("cooking_skill", e.target.value as CookingSkill)}>
                {SKILL.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <div className="field field-wide">
              <span id="equipment-label">Equipment</span>
              <div className="chips" role="group" aria-labelledby="equipment-label">
                {EQUIPMENT.map((eq) => (
                  <label key={eq} className="chip">
                    <input type="checkbox" checked={spec.equipment.includes(eq)} onChange={() => toggle("equipment", eq)} />
                    {label(eq)}
                  </label>
                ))}
              </div>
            </div>
            <label className="field field-wide">
              <span>Already in your pantry</span>
              <input value={text.pantry} placeholder="salt, turmeric, cumin seeds, oil"
                onChange={(e) => setText({ ...text, pantry: e.target.value })} />
              <small>These are never bought.</small>
            </label>
          </fieldset>
        </>
      )}

      {error && <p className="form-error" role="alert">{error}</p>}
      <button className="btn btn-primary btn-large" type="submit" disabled={busy}>
        {busy ? "Starting…" : "Plan my week"}
      </button>
    </form>
  );
}
