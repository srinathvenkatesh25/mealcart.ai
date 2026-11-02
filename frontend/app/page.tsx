"use client";

import dynamic from "next/dynamic";
import { useRef, useState } from "react";
import ActivityDrawer from "@/components/ActivityDrawer";
import CartReview from "@/components/CartReview";
import GroceryList from "@/components/GroceryList";
import MealCalendar from "@/components/MealCalendar";
import MealDialog from "@/components/MealDialog";
import QuestionCard from "@/components/QuestionCard";
import ShoppingProgress from "@/components/ShoppingProgress";
import StepTabs, { type Step } from "@/components/StepTabs";
import UsageLine from "@/components/UsageLine";
import { ApiError } from "@/lib/api";
import type { MealSpec, Slot, SwapRequest } from "@/lib/types";
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
  LLMQuotaError: "Every AI model in your list is out of free quota, busy, or not answering right now. Try again in a few minutes.",
  APITimeoutError: "An AI model stopped answering. Start a new plan; MealCart now moves to the next model when this happens.",
  APIConnectionError: "MealCart couldn't reach the AI service. Check your internet connection and start a new plan.",
};

export default function Home() {
  const { state, start, answer, newRun } = useRun();
  const [formError, setFormError] = useState<string | null>(null);
  // Items unticked on the grocery list. Tied to the list itself, so a rebuilt list
  // (e.g. after a swap) starts with everything ticked again.
  const [unticked, setUnticked] = useState<{ list: unknown; items: Set<string> }>({ list: null, items: new Set() });
  const removed = unticked.list === state.groceryList ? unticked.items : new Set<string>();

  const [step, setStep] = useState<Step>("meals");
  const [mealsConfirmed, setMealsConfirmed] = useState(false);
  const [approvedCount, setApprovedCount] = useState<number | null>(null);
  const [open, setOpen] = useState<{ day: string; slot: Slot } | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  const approving = state.question?.kind === "plan_approval";
  const otherQuestion = state.question && !approving ? state.question : null;
  const finished = state.status === "cart_ready" || state.status === "needs_attention";
  const working = state.status === "running" || state.status === "starting";
  const stage = finished ? 3 : state.approved ? 3 : approving ? 2 : state.runId ? 1 : 0;

  // Move between steps when the run moves on (React's "adjust state when props change" pattern):
  // a new approval question (first plan, or after a swap) starts at Meals; approving or a cart goes to Cart.
  const questionId = approving ? state.question?.event_id ?? null : null;
  const [seen, setSeen] = useState({ runId: state.runId, questionId, approved: state.approved, report: state.report });
  if (seen.runId !== state.runId || seen.questionId !== questionId || seen.approved !== state.approved || seen.report !== state.report) {
    setSeen({ runId: state.runId, questionId, approved: state.approved, report: state.report });
    if (seen.runId !== state.runId) { setStep("meals"); setMealsConfirmed(false); setApprovedCount(null); }
    if (questionId && questionId !== seen.questionId) { setStep("meals"); setMealsConfirmed(false); }
    if ((state.approved && !seen.approved) || (state.report && !seen.report)) setStep("cart");
  }

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

  const list = state.groceryList;
  const kept = list?.items.filter((i) => !removed.has(i.name)) ?? [];
  // Estimate from prices seen in earlier carts; only shown when every kept item has one.
  const est = kept.length && kept.every((i) => i.est_price_usd !== null)
    ? kept.reduce((sum, i) => sum + (i.est_price_usd ?? 0), 0) : null;
  const budget = state.spec?.budget_weekly_usd ?? null;

  function confirmList() {
    if (!list) return;
    const sent = removed.size
      ? answer("plan_approval", { edit: { ...list, items: kept } })
      : answer("plan_approval", "approved");
    if (sent) setApprovedCount(kept.length);
  }

  const shopping = state.approved && !state.report && !state.failure;
  const total = approvedCount ?? kept.length;
  const addedSoFar = state.progress.filter((l) => /^(Added|Already in cart)\b/.test(l.message)).length;
  const targets = state.spec?.macros;
  const openDay = open ? state.plan?.days.find((d) => d.day === open.day) ?? null : null;

  if (!state.runId && state.status !== "starting") {
    return (
      <div className="shell">
        <header className="masthead">
          <p className="wordmark">MealCart</p>
          <ol className="stages" aria-label="Progress">
            {STAGES.map((s, i) => <li key={s} aria-current={i === 0 ? "step" : undefined}>{s}</li>)}
          </ol>
        </header>
        <main className="layout">
          <div className="content">
            <section className="hero">
              <h1>A week of meals that hits your numbers.</h1>
              <p>
                Set your daily calories and protein. MealCart plans the meals, checks every day against USDA
                nutrition data, and fills your Instacart cart. You approve the list first, and you always check
                out yourself.
              </p>
            </section>
            <SpecForm busy={false} error={formError} onSubmit={submit} />
          </div>
          <aside className="rail">
            {state.notice && <p className="notice" role="status">{state.notice}</p>}
            <section className="how" aria-labelledby="how-heading">
              <h2 id="how-heading" className="eyebrow">How a run goes</h2>
              <ol>
                <li><b>Meals.</b> An AI picks meals; software sizes every portion to your targets and checks it against USDA data.</li>
                <li><b>Grocery list.</b> Swap meals you don&apos;t like, untick what you have, and confirm the list.</li>
                <li><b>Instacart cart.</b> Chrome fills your cart and checks it against the list. You check out.</li>
              </ol>
            </section>
          </aside>
        </main>
      </div>
    );
  }

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
        <button className="btn btn-quiet" onClick={newRun} disabled={working && !finished && stage < 2}>
          New plan
        </button>
      </header>

      <main className="run">
        {targets && (
          <section className="hero hero-run">
            <h1>
              <span className="mono-num">{targets.calories.toLocaleString()}</span> kcal ·{" "}
              <span className="mono-num">{targets.protein_g}</span> g protein
            </h1>
            <p>a day, for {state.spec?.days} days{state.spec?.cuisines.length ? `, ${state.spec.cuisines.join(", ")}` : ""}</p>
          </section>
        )}

        {state.notice && <p className="notice" role="status">{state.notice}</p>}
        {state.runId && !state.connected && !finished && state.status !== "failed" && (
          <p className="notice" role="status">Reconnecting to the MealCart server…</p>
        )}

        {otherQuestion && (
          <QuestionCard question={otherQuestion} onAnswer={(kind, value) => answer(kind, value)} />
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

        <StepTabs value={step} onChange={setStep} tabs={[
          {
            id: "meals", label: "Meals",
            note: !state.plan ? "Planning…" : state.approved || mealsConfirmed ? "✓ confirmed" : "Review the week",
          },
          {
            id: "list", label: "Grocery list", disabled: !list || (approving && !mealsConfirmed),
            note: state.approved ? `✓ ${total} items confirmed` : list ? `${kept.length} items to buy` : "After the meals",
          },
          {
            id: "cart", label: "Instacart cart", disabled: !state.approved && !state.report,
            note: state.report ? (state.status === "cart_ready" ? "✓ ready to check out" : "Needs a look")
              : shopping ? `Filling… ${Math.min(addedSoFar, total)}/${total}` : "After you confirm the list",
          },
        ]} />

        <div role="tabpanel" id={`step-panel-${step}`} aria-labelledby={`step-${step}`} className="step-panel">
          {step === "meals" && (state.plan ? (
            <section aria-labelledby="week-heading">
              <div className="section-head">
                <h2 id="week-heading">The week</h2>
                <p>Click a meal for its calories, protein, ingredients and recipe{approving ? ", or to swap it" : ""}.</p>
              </div>
              <MealCalendar plan={state.plan} perDay={state.perDay} spec={state.spec} selected={open}
                onOpen={(day, slot, el) => { opener.current = el; setOpen({ day, slot }); }} />
              {approving && (
                <div className="step-foot">
                  <span className="fine">Swap any meal first if you like. Nothing is bought yet.</span>
                  <div className="row">
                    <button type="button" className="btn btn-quiet" onClick={() => answer("plan_approval", "rejected")}>
                      Cancel this plan
                    </button>
                    <button type="button" className="btn btn-primary"
                      onClick={() => { setMealsConfirmed(true); setStep("list"); }}>
                      Confirm meals →
                    </button>
                  </div>
                </div>
              )}
            </section>
          ) : !state.failure && (
            <div className="placeholder" aria-busy="true">
              <div className="label label-ghost" />
              <div className="label label-ghost" />
              <div className="label label-ghost" />
              <p>Planning your week. This takes a minute or two; Activity on the right shows each step.</p>
            </div>
          ))}

          {step === "list" && list && (
            <>
              <GroceryList list={list} editable={approving} removed={removed}
                onToggle={(item) => {
                  const next = new Set(removed);
                  if (next.has(item)) next.delete(item); else next.add(item);
                  setUnticked({ list: state.groceryList, items: next });
                }} />
              {approving && (
                <div className="step-foot">
                  <span className="list-sum">
                    <b>{kept.length} items</b>
                    {est !== null
                      ? <> · about <span className="mono">${est.toFixed(2)}</span> from your last prices
                        {budget !== null && (est > budget
                          ? <> · <span className="over">${(est - budget).toFixed(2)} over your ${budget.toFixed(0)} budget</span></>
                          : <> · within your ${budget.toFixed(0)} budget</>)}</>
                      : <> · the price is shown after shopping</>}
                  </span>
                  <div className="row">
                    <button type="button" className="btn btn-quiet" onClick={() => setStep("meals")}>← Back to meals</button>
                    <button type="button" className="btn btn-primary" onClick={confirmList}>
                      Confirm list and fill my Instacart cart
                    </button>
                  </div>
                </div>
              )}
            </>
          )}

          {step === "cart" && (state.report ? (
            <CartReview report={state.report} complete={state.status === "cart_ready"} budget={budget} usage={state.usage} />
          ) : shopping ? (
            <ShoppingProgress lines={state.progress} total={total} />
          ) : null)}
        </div>
      </main>

      <MealDialog day={openDay} slot={open?.slot ?? null}
        totals={open ? state.perDay?.[open.day] : undefined}
        nutrition={open ? state.perMeal?.[open.day] : undefined}
        canSwap={approving} onSlot={(slot) => setOpen((o) => (o ? { ...o, slot } : o))} onSwap={swap}
        onClose={() => { setOpen(null); opener.current?.focus(); }} />

      <ActivityDrawer lines={state.progress} working={working && !state.question} usage={state.usage} />
    </div>
  );
}
