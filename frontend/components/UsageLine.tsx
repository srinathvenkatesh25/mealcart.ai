import type { LlmUsage } from "@/lib/types";

const short = (model: string) => model.replace(/^groq:/, "Groq ").replace(/^openai\//, "");

/** What this run asked of the free AI quota: calls, tokens, and which models answered. */
export default function UsageLine({ usage }: { usage: LlmUsage | null }) {
  if (!usage || usage.calls === 0) return null;
  const tokens = usage.input_tokens + usage.output_tokens;
  return (
    <p className="fine usage">
      AI used for this plan: {usage.calls} call{usage.calls === 1 ? "" : "s"} · {tokens.toLocaleString()} tokens ·{" "}
      {usage.cost_usd > 0 ? `$${usage.cost_usd.toFixed(2)}` : "free tier"}
      {usage.models.length > 0 && <> · {usage.models.map((m) => `${short(m.model)} ×${m.calls}`).join(", ")}</>}
    </p>
  );
}
