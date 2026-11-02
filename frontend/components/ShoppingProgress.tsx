"use client";

import type { ProgressLine } from "@/lib/useRun";

// Read from the shopper's own progress messages (backend/app/shopping/shopper.py).
const ADDED = /^(Added|Already in cart)(?: at ([^:]+))?: (.+?) \(/;
const STORE = /(?:^Shopping at |Looking at )(.+?)\.?$/;

/** While MealCart fills the cart: which store, how many items so far, and what it just added. */
export default function ShoppingProgress({ lines, total }: { lines: ProgressLine[]; total: number }) {
  const shop = lines.filter((l) => l.node === "shop").map((l) => l.message);
  const added = shop.flatMap((m) => { const x = ADDED.exec(m); return x ? [x[3]] : []; });
  // Stores in the order they were opened, each with how many items went into its cart so far.
  const stores: { name: string; added: number }[] = [];
  for (const m of shop) {
    const opened = STORE.exec(m)?.[1];
    if (opened && !stores.some((s) => s.name === opened)) stores.push({ name: opened, added: 0 });
    const at = ADDED.exec(m)?.[2];
    if (at) { const st = stores.find((s) => s.name === at); if (st) st.added += 1; }
  }
  const current = [...shop].reverse().map((m) => STORE.exec(m)?.[1]).find(Boolean);
  const done = Math.min(added.length, total);
  const pct = total ? (done / total) * 100 : 0;

  return (
    <section className="progress-card" aria-live="polite" aria-labelledby="progress-title">
      <p className="eyebrow">Filling your Instacart cart</p>
      <h2 id="progress-title">{current ? `Shopping at ${current}` : shop.length ? "Choosing a store…" : "Opening Instacart…"}</h2>
      <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}
        aria-label="Items added to your cart"><span style={{ width: `${pct}%` }} /></div>
      <p>
        <b>Added {done} of {total}</b> items
        {added.length > 0 && <> · last: {added[added.length - 1]}</>}
      </p>
      {stores.length > 1 && (
        <div className="store-chips">
          {stores.map((s) => (
            <span key={s.name} className={`store-chip${s.name === current ? " on" : ""}`}>
              {s.name} · {s.added} added
            </span>
          ))}
        </div>
      )}
      <p className="fine">Chrome runs hidden. Every step, including which AI model answered, is in Activity. MealCart never opens checkout.</p>
    </section>
  );
}
