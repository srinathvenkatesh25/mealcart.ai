"use client";

import type { GroceryList as GL } from "@/lib/types";

const name = (s: string) => s.replace(/_/g, " ");

export default function GroceryList({ list, editable, removed, onToggle }: {
  list: GL; editable: boolean; removed: Set<string>; onToggle: (item: string) => void;
}) {
  return (
    <section className="groceries" aria-labelledby="groceries-heading">
      <div className="section-head">
        <h2 id="groceries-heading">Grocery list</h2>
        <p>
          {list.items.length} items for the week
          {list.est_total_usd !== null && <> · about <span className="mono">${list.est_total_usd.toFixed(2)}</span></>}
          {editable && " · untick anything you already have"}
        </p>
      </div>
      <ul className="grocery-items">
        {list.items.map((item) => {
          const off = removed.has(item.name);
          return (
            <li key={item.name} className={off ? "is-removed" : undefined}>
              {editable ? (
                <label>
                  <input type="checkbox" checked={!off} onChange={() => onToggle(item.name)} />
                  <span className="grocery-name">{name(item.name)}</span>
                </label>
              ) : (
                <span className="grocery-name">{name(item.name)}</span>
              )}
              <span className="grocery-qty mono">{item.purchase_qty}</span>
              {item.substitutes.length > 0 && (
                <span className="grocery-subs">or {item.substitutes.slice(0, 2).map(name).join(", ")}</span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
