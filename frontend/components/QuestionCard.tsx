"use client";

import { useState } from "react";
import type { GroceryList, HitlKind, Question } from "@/lib/types";

const TITLES: Record<HitlKind, string> = {
  plan_approval: "Approve the grocery list",
  login_email: "Sign in to Instacart",
  email_code: "Enter your Instacart code",
  login_password: "Enter your Instacart password",
  captcha: "Instacart wants to check you're human",
  substitution_approval: "Approve a substitute",
  cart_clear_approval: "Your cart already has items",
};

function TextAnswer({ q, onAnswer }: { q: Question; onAnswer: (v: string) => void }) {
  const [value, setValue] = useState("");
  const input = {
    login_email: { type: "email", autoComplete: "email", label: "Email", button: "Send email" },
    email_code: { type: "text", autoComplete: "one-time-code", label: "Code", button: "Send code" },
    login_password: { type: "password", autoComplete: "current-password", label: "Password", button: "Send password" },
  }[q.kind as "login_email" | "email_code" | "login_password"];
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) onAnswer(value.trim()); }}>
      <label className="field">
        <span>{input.label}</span>
        <input autoFocus type={input.type} autoComplete={input.autoComplete} value={value}
          inputMode={q.kind === "email_code" ? "numeric" : undefined} onChange={(e) => setValue(e.target.value)} />
      </label>
      <button className="btn btn-primary" type="submit">{input.button}</button>
      <p className="fine">Typed straight into Instacart in the Chrome window. MealCart doesn&apos;t save it.</p>
    </form>
  );
}

export default function QuestionCard({ question, groceryList, removed, budget, onAnswer }: {
  question: Question; groceryList: GroceryList | null; removed: Set<string>; budget: number | null;
  onAnswer: (kind: HitlKind, value: unknown) => void;
}) {
  const q = question;
  const answer = (value: unknown) => onAnswer(q.kind, value);

  let body: React.ReactNode;
  switch (q.kind) {
    case "plan_approval": {
      const keep = groceryList ? groceryList.items.length - removed.size : 0;
      // Estimate from prices seen in earlier carts; only shown when every kept item has one.
      const kept = groceryList?.items.filter((i) => !removed.has(i.name)) ?? [];
      const est = kept.length && kept.every((i) => i.est_price_usd !== null)
        ? kept.reduce((sum, i) => sum + (i.est_price_usd ?? 0), 0) : null;
      const approve = () => {
        if (groceryList && removed.size) {
          answer({ edit: { ...groceryList, items: groceryList.items.filter((i) => !removed.has(i.name)) } });
        } else {
          answer("approved");
        }
      };
      body = (
        <>
          <p>
            {groceryList ? <><b>{keep} items</b> go into your Instacart cart.</> : q.prompt}
            {removed.size > 0 && <> {removed.size} you already have are left out.</>}
          </p>
          {est !== null && (
            <p>
              About <span className="mono">${est.toFixed(2)}</span> from your last prices
              {budget !== null && (est > budget
                ? <>, <span className="over">${(est - budget).toFixed(2)} over your ${budget.toFixed(0)} budget</span>. Swap meals or untick items to bring it down.</>
                : <>, within your ${budget.toFixed(0)} budget.</>)}
            </p>
          )}
          <p className="fine">Nothing is bought. You check out yourself in the Instacart app.</p>
          <div className="row">
            <button className="btn btn-primary" onClick={approve}>Fill my cart</button>
            <button className="btn btn-quiet" onClick={() => answer("rejected")}>Cancel this plan</button>
          </div>
        </>
      );
      break;
    }
    case "login_email":
    case "email_code":
    case "login_password":
      body = <><p>{q.prompt}</p><TextAnswer q={q} onAnswer={answer} /></>;
      break;
    case "captcha":
      body = (
        <>
          {q.screenshot_b64 && (
            // eslint-disable-next-line @next/next/no-img-element
            <img className="shot" src={`data:image/png;base64,${q.screenshot_b64}`} alt="What Instacart is showing in Chrome" />
          )}
          <p>Solve it in the Chrome window MealCart opened, then come back here.</p>
          <button className="btn btn-primary" onClick={() => answer("done")}>I&apos;ve solved it</button>
        </>
      );
      break;
    case "substitution_approval":
      body = (
        <>
          <p>{q.prompt.replace(/\s*\(yes\/no\)\s*$/i, "")}</p>
          <div className="row">
            <button className="btn btn-primary" onClick={() => answer("yes")}>Use the substitute</button>
            <button className="btn btn-quiet" onClick={() => answer("no")}>Skip this item</button>
          </div>
        </>
      );
      break;
    case "cart_clear_approval":
      body = (
        <>
          <p>{q.prompt.replace(/\s*Clear it first\? \(clear\/keep\)\s*$/i, "")}</p>
          <div className="row">
            <button className="btn btn-primary" onClick={() => answer("clear")}>Empty it first</button>
            <button className="btn btn-quiet" onClick={() => answer("keep")}>Keep those items</button>
          </div>
        </>
      );
      break;
  }

  return (
    <section className="question" aria-live="assertive" aria-labelledby="question-title">
      <p className="eyebrow">Your answer is needed</p>
      <h2 id="question-title">{TITLES[q.kind]}</h2>
      {body}
    </section>
  );
}
