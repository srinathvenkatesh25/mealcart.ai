"use client";

import type { ProgressLine } from "@/lib/useRun";

const STEP: Record<string, string> = {
  intake: "Targets", plan: "Planning", solve: "Portions", validate: "Checks", repair: "Fixing",
  consolidate: "List", swap: "Swap", shop: "Instacart", model: "AI model",
};

// Newest first, so new lines appear at the top without scrolling the page.
export default function ActivityFeed({ lines, working }: { lines: ProgressLine[]; working: boolean }) {
  return (
    <section className="feed" aria-labelledby="feed-heading">
      <h2 id="feed-heading" className="eyebrow">Activity</h2>
      <ol aria-live="polite">
        {working && <li className="feed-working"><span className="feed-step">Now</span><span>Working…</span></li>}
        {[...lines].reverse().map((l) => (
          <li key={l.seq}>
            <span className="feed-step">{STEP[l.node] ?? l.node}</span>
            <span>{l.message}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
