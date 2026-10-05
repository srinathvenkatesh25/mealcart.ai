"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { eventsUrl, getRun, startRun } from "./api";
import type {
  CartReport, DayTotals, GroceryList, HitlKind, LlmUsage, MealPlan, MealSpec, Question, RunStatus, ServerEvent,
} from "./types";

const RUN_KEY = "mealcart.runId";
const TERMINAL: RunStatus[] = ["cart_ready", "needs_attention", "failed"];

export interface ProgressLine {
  seq: number;
  node: string;
  message: string;
}

export interface RunView {
  runId: string | null;
  status: RunStatus | "idle" | "starting";
  spec: MealSpec | null;
  progress: ProgressLine[];
  plan: MealPlan | null;
  perDay: Record<string, DayTotals> | null;
  groceryList: GroceryList | null;
  question: Question | null;
  report: CartReport | null;
  failure: { error: string; detail: string[] } | null;
  notice: string | null;
  connected: boolean;
  approved: boolean;
  usage: LlmUsage | null;
}

const initial: RunView = {
  runId: null, status: "idle", spec: null, progress: [], plan: null, perDay: null, groceryList: null,
  question: null, report: null, failure: null, notice: null, connected: false, approved: false, usage: null,
};

type Action =
  | { type: "reset" }
  | { type: "starting"; spec: MealSpec | null }
  | { type: "started"; runId: string; spec: MealSpec | null }
  | { type: "loaded"; row: NonNullable<Awaited<ReturnType<typeof getRun>>> }
  | { type: "event"; event: ServerEvent }
  | { type: "connected"; value: boolean }
  | { type: "answered"; kind: HitlKind; approved: boolean }
  | { type: "notice"; text: string | null };

function reduce(state: RunView, action: Action): RunView {
  switch (action.type) {
    case "reset":
      return initial;
    case "starting":
      return { ...initial, status: "starting", spec: action.spec };
    case "started":
      return { ...initial, runId: action.runId, status: "running", spec: action.spec };
    case "loaded": {
      const r = action.row;
      return {
        ...state,
        runId: r.id,
        status: r.status,
        spec: r.meal_spec_json,
        plan: state.plan ?? r.meal_plan_json,
        groceryList: state.groceryList ?? r.grocery_list_json,
        report: r.cart_report_json,
        failure: state.failure ?? (r.status === "failed" ? { error: r.error ?? "failed", detail: [] } : null),
        approved: Boolean(r.cart_report_json) || state.approved,
        usage: r.llm_usage ?? state.usage,
      };
    }
    case "connected":
      return { ...state, connected: action.value };
    case "answered":
      return { ...state, question: null, approved: state.approved || action.approved, notice: null };
    case "notice":
      return { ...state, notice: action.text };
    case "event": {
      const e = action.event;
      switch (e.type) {
        case "run.progress":
          return { ...state, progress: [...state.progress, { seq: e.seq, node: e.node, message: e.message }] };
        case "plan.ready":
          return { ...state, plan: e.meal_plan, perDay: e.per_day, groceryList: e.grocery_list };
        case "hitl.required":
          return {
            ...state, status: "awaiting_hitl",
            question: { event_id: e.event_id, kind: e.kind, prompt: e.prompt, screenshot_b64: e.screenshot_b64 },
          };
        case "hitl.resolved":
          return {
            ...state, status: "running",
            question: state.question?.event_id === e.event_id ? null : state.question,
          };
        case "hitl.rejected":
          return { ...state, notice: "That answer arrived after the question closed, so it was ignored." };
        case "cart.ready":
          return { ...state, status: "cart_ready", report: e.report, question: null, approved: true };
        case "run.needs_attention":
          return { ...state, status: "needs_attention", report: e.report, question: null, approved: true };
        case "run.failed":
          return { ...state, status: "failed", failure: { error: e.error, detail: e.detail }, question: null };
      }
    }
  }
  return state;
}

function notifyIfHidden(prompt: string) {
  if (typeof document === "undefined" || !document.hidden) return;
  try {
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification("MealCart needs an answer", { body: prompt });
    }
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.12, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch {
    // notifications are a convenience; the question is on the page regardless
  }
}

function readStoredRun(): string | null {
  try {
    return localStorage.getItem(RUN_KEY);
  } catch {
    return null;
  }
}

function storeRun(runId: string | null) {
  try {
    if (runId) localStorage.setItem(RUN_KEY, runId);
    else localStorage.removeItem(RUN_KEY);
  } catch {
    // private window: the run just won't be reopened after a reload
  }
}

export function useRun() {
  const [state, dispatch] = useReducer(reduce, initial);
  const ws = useRef<WebSocket | null>(null);
  const lastSeq = useRef(0);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const statusRef = useRef(state.status);
  useEffect(() => {
    statusRef.current = state.status;
  }, [state.status]);

  const connect = useCallback((runId: string) => {
    const open = (attempt: number) => {
      ws.current?.close();
      const socket = new WebSocket(eventsUrl(runId, lastSeq.current));
      ws.current = socket;
      socket.onopen = () => dispatch({ type: "connected", value: true });
      socket.onmessage = (msg) => {
        const event = JSON.parse(msg.data) as ServerEvent;
        if (event.seq) {
          if (event.seq <= lastSeq.current) return;
          lastSeq.current = event.seq;
        }
        if (event.type === "hitl.required") notifyIfHidden(event.prompt);
        dispatch({ type: "event", event });
        if (event.type === "cart.ready" || event.type === "run.needs_attention" || event.type === "run.failed") {
          getRun(runId).then((row) => row && dispatch({ type: "loaded", row })).catch(() => undefined);
        }
      };
      socket.onclose = () => {
        if (ws.current !== socket) return;
        dispatch({ type: "connected", value: false });
        if (TERMINAL.includes(statusRef.current as RunStatus)) return;
        const delay = Math.min(10_000, 500 * 2 ** attempt);
        retry.current = setTimeout(() => open(attempt + 1), delay);
      };
    };
    open(0);
  }, []);

  const disconnect = useCallback(() => {
    if (retry.current) clearTimeout(retry.current);
    const socket = ws.current;
    ws.current = null;
    socket?.close();
  }, []);

  // Reopen the last run after a page reload.
  useEffect(() => {
    const runId = readStoredRun();
    if (!runId) return;
    getRun(runId)
      .then((row) => {
        if (!row) return storeRun(null);
        dispatch({ type: "loaded", row });
        connect(runId);
      })
      .catch(() => dispatch({ type: "notice", text: "Can't reach the MealCart server. Is it running?" }));
    return disconnect;
  }, [connect, disconnect]);

  const start = useCallback(
    async (body: MealSpec | { text: string }) => {
      disconnect();
      lastSeq.current = 0;
      const spec = "text" in body ? null : body;
      dispatch({ type: "starting", spec });
      if ("Notification" in window && Notification.permission === "default") {
        Notification.requestPermission().catch(() => undefined);
      }
      try {
        const { run_id } = await startRun(body);
        storeRun(run_id);
        dispatch({ type: "started", runId: run_id, spec });
        connect(run_id);
        if (!spec) getRun(run_id).then((row) => row && dispatch({ type: "loaded", row }));
      } catch (err) {
        dispatch({ type: "reset" });
        throw err;
      }
    },
    [connect, disconnect],
  );

  const answer = useCallback(
    (kind: HitlKind, value: unknown) => {
      const q = state.question;
      if (!ws.current || ws.current.readyState !== WebSocket.OPEN) {
        dispatch({ type: "notice", text: "Reconnecting to the server… try again in a moment." });
        return false;
      }
      ws.current.send(JSON.stringify({ type: "hitl.response", kind, value, event_id: q?.event_id }));
      const approved = kind === "plan_approval" && (value === "approved" || (typeof value === "object" && value !== null && "edit" in value));
      dispatch({ type: "answered", kind, approved });
      return true;
    },
    [state.question],
  );

  const newRun = useCallback(() => {
    disconnect();
    storeRun(null);
    lastSeq.current = 0;
    dispatch({ type: "reset" });
  }, [disconnect]);

  return { state, start, answer, newRun };
}
