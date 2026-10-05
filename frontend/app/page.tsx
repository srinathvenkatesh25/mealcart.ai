"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import ActivityFeed from "@/components/ActivityFeed";
import CartReview from "@/components/CartReview";
import GroceryList from "@/components/GroceryList";
import PlanView from "@/components/PlanView";
import QuestionCard from "@/components/QuestionCard";
import UsageLine from "@/components/UsageLine";
import { ApiError } from "@/lib/api";
import type { MealSpec, SwapRequest } from "@/lib/types";
import { useRun } from "@/lib/useRun";

// Client-only: the form starts from preferences saved in this browser.
const SpecForm = dynamic(() => import("@/components/SpecForm"), { ssr: false });

const STAGES = ["Targets", "Plan", "Approve", "Cart"] as const;

const FAILURES: Record<string, string> = {
  plan_failed_validation: "The planner couldn't meet every rule after 3 tries.",
  llm_quota_exceeded: "Every AI model in your list is out of free quota or busy right now.",
  llm_bad_output: "The AI model kept returning an unreadable plan.",
  llm_too_large: "The AI model on its free tier can't take a request this big, even split up.",
  cancelled_by_user: "You cancelled this plan. Nothing was added to your cart.",
  invalid_edit: "The edited grocery list couldn't be read.",
  HitlTimeoutError: "Nobody answered a question within 15 minutes, so the run stopped.",
  LoginFailedError: "Signing in to Instacart didn't finish.",
  LoginRequiredError: "You're not signed in to Instacart. Run python scripts/login.py once, then try again.",
  HumanCheckError: "Instacart asked for a human check. Set BROWSER_MODE=visible in .env, restart the server, and solve it in the Chrome window.",
  ProfileBusyError: "Another run is already using the Instacart browser. Close it and try again.",
};

export default function Home() {
  const { state, start, answer, newRun } = useRun();
  const [formError, setFormError] = useState<string | null>(null);
  // Items unticked on the grocery list. Tied to the list itself, so a rebuilt list
  // (e.g. after a swap) starts with everything ticked again.
  const [unticked, setUnticked] = useState<{ list: unknown; items: Set<string> }>({ list: null, items: new Set() });
  const removed = unticked.list === state.groceryList ? unticked.items : new Set<string>();

  const approving = state.question?.kind === "plan_approval";
  const finished = state.status === "cart_ready" || state.status === "needs_attention";
  const working = state.status === "running" || state.status === "starting";
  const stage = finished ? 3 : state.approved ? 3 : approving ? 2 : state.runId ? 1 : 0;

  async function submit(body: MealSpec | { text: string }) {
    setFormError(null);
    try {
      await start(body);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Couldn't start the plan.");
    }
  }

  function swap(req: SwapRequest) {
    answer("plan_approval", { swap: req });
  }

  const targets = state.spec?.macros;

  return (
    <div className="shell">
      <header className="masthead">
        <p className="wordmark">MealCart</p>
        <ol className="stages" aria-label="Progress">
          {STAGES.map((s, i) => (
            <li key={s} aria-current={i === stage ? "step" : undefined} className={i < stage ? "done" : undefined}>
              {s}
            </li>
          ))}
        </ol>
        {state.runId && (
          <button className="btn btn-quiet" onClick={newRun} disabled={working && !finished && stage < 2}>
            New plan
          </button>
        )}
      </header>

      <main className="layout">
        <div className="content">
          {!state.runId && state.status !== "starting" ? (
            <>
              <section className="hero">
                <h1>A week of meals that hits your numbers.</h1>
                <p>
                  Set your daily calories and protein. MealCart plans the meals, checks every day against USDA
                  nutrition data, and fills your Instacart cart. You approve the list first, and you always check
                  out yourself.
                </p>
              </section>
              <SpecForm busy={false} error={formError} onSubmit={submit} />
            </>
          ) : (
            <>
              {targets && (
                <section className="hero hero-run">
                  <h1>
                    <span className="mono-num">{targets.calories.toLocaleString()}</span> kcal ·{" "}
                    <span className="mono-num">{targets.protein_g}</span> g protein
                  </h1>
                  <p>a day, for {state.spec?.days} days{state.spec?.cuisines.length ? `, ${state.spec.cuisines.join(", ")}` : ""}</p>
                </section>
              )}

              {state.failure && (
                <section className="failure" role="alert">
                  <h2>This plan stopped</h2>
                  <p>{FAILURES[state.failure.error] ?? state.failure.error}</p>
                  {state.failure.detail.length > 0 && (
                    <ul>{state.failure.detail.slice(0, 6).map((d) => <li key={d}>{d}</li>)}</ul>
                  )}
                  <UsageLine usage={state.usage} />
                  <button className="btn btn-primary" onClick={newRun}>Start a new plan</button>
                </section>
              )}

              {state.report && (
                <CartReview report={state.report} complete={state.status === "cart_ready"}
                  budget={state.spec?.budget_weekly_usd ?? null} usage={state.usage} />
              )}

              {!state.plan && !state.failure && (
                <div className="placeholder" aria-busy="true">
                  <div className="label label-ghost" />
                  <div className="label label-ghost" />
                  <div className="label label-ghost" />
                  <p>Planning your week. This takes about a minute.</p>
                </div>
              )}

              {state.plan && (
                <PlanView plan={state.plan} perDay={state.perDay} perMeal={state.perMeal} spec={state.spec}
                  canSwap={approving} onSwap={swap} />
              )}

              {state.groceryList && !state.report && (
                <GroceryList list={state.groceryList} editable={approving} removed={removed}
                  onToggle={(item) => {
                    const next = new Set(removed);
                    if (next.has(item)) next.delete(item); else next.add(item);
                    setUnticked({ list: state.groceryList, items: next });
                  }} />
              )}
            </>
          )}
        </div>

        <aside className="rail">
          {state.question && (
            <QuestionCard question={state.question} groceryList={state.groceryList} removed={removed}
              budget={state.spec?.budget_weekly_usd ?? null} onAnswer={(kind, value) => answer(kind, value)} />
          )}
          {state.notice && <p className="notice" role="status">{state.notice}</p>}
          {state.runId && !state.connected && !finished && state.status !== "failed" && (
            <p className="notice" role="status">Reconnecting to the MealCart server…</p>
          )}
          {state.runId ? (
            <ActivityFeed lines={state.progress} working={working && !state.question} />
          ) : (
            <section className="how" aria-labelledby="how-heading">
              <h2 id="how-heading" className="eyebrow">How a run goes</h2>
              <ol>
                <li><b>Plan.</b> An AI picks meals; software sizes every portion to your targets.</li>
                <li><b>Check.</b> Each day is verified against USDA data and your rules, and fixed if it misses.</li>
                <li><b>Approve.</b> You see the week and the grocery list. Swap meals or untick items you have.</li>
                <li><b>Fill.</b> Chrome opens Instacart, picks a store and fills your cart. You check out.</li>
              </ol>
            </section>
          )}
        </aside>
      </main>
    </div>
  );
}
