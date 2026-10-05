"use client";

import { useEffect, useRef } from "react";
import type { CartReport, LlmUsage } from "@/lib/types";
import UsageLine from "./UsageLine";

const name = (s: string) => s.replace(/_/g, " ");
const usd = (n: number) => `$${n.toFixed(2)}`;

export default function CartReview({ report, complete, budget, usage }: {
  report: CartReport; complete: boolean; budget: number | null; usage: LlmUsage | null;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  // The cart appears at the top of the page; bring it into view once, when it arrives.
  useEffect(() => {
    heading.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    heading.current?.focus({ preventScroll: true });
  }, []);
  const missing = report.coverage.filter((c) => !c.covered);
  const lineFor = (item: string) => report.lines.find((l) => l.grocery_item === item);
  return (
    <section className="cart" aria-labelledby="cart-heading">
      <div className="section-head">
        <h2 id="cart-heading" ref={heading} tabIndex={-1}>{complete ? "Your cart is ready" : "Your cart needs a look"}</h2>
        <p>
          At <b>{report.store}</b> · subtotal <span className="mono">{usd(report.subtotal_usd)}</span>
          {budget !== null && (report.over_budget_by_usd
            ? <> · <span className="over">{usd(report.over_budget_by_usd)} over your {usd(budget)} budget</span></>
            : <> · within your {usd(budget)} budget</>)}
        </p>
      </div>

      {!complete && missing.length > 0 && (
        <p className="callout">
          {missing.length} item{missing.length > 1 ? "s aren't" : " isn't"} fully in the cart:{" "}
          {missing.map((m) => name(m.item)).join(", ")}. Add {missing.length > 1 ? "them" : "it"} yourself in Instacart,
          or start a new plan.
        </p>
      )}

      <table className="receipt">
        <thead>
          <tr><th scope="col">For</th><th scope="col">In your cart</th><th scope="col" className="num">Need / have</th>
            <th scope="col" className="num">Price</th></tr>
        </thead>
        <tbody>
          {report.coverage.map((c) => {
            const line = lineFor(c.item);
            return (
              <tr key={c.item} className={c.covered ? undefined : "is-missing"}>
                <td>
                  <span className="tick" aria-label={c.covered ? "covered" : "not covered"}>{c.covered ? "✓" : "✗"}</span>
                  {name(c.item)}
                </td>
                <td>
                  {line ? <>{line.product_name} <span className="muted">({line.pack_size}) × {line.packs}</span></> : "—"}
                  {line?.substituted_for && <span className="sub-note">substitute</span>}
                </td>
                <td className="num mono">{Math.round(c.grams_needed)} / {Math.round(c.grams_in_cart)} g</td>
                <td className="num mono">{line ? usd(line.line_total_usd) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr><th scope="row" colSpan={3}>Subtotal before fees</th><td className="num mono">{usd(report.subtotal_usd)}</td></tr>
        </tfoot>
      </table>

      {report.notes.length > 0 && (
        <ul className="notes">{report.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      )}

      <UsageLine usage={usage} />

      <div className="checkout">
        <p>MealCart never checks out. Review the cart and pay in the Instacart app or your usual browser.</p>
        <a className="btn btn-primary" href="https://www.instacart.com/store/" target="_blank" rel="noreferrer">
          Open Instacart
        </a>
      </div>
    </section>
  );
}
