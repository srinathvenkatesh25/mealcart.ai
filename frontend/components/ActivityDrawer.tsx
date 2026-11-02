"use client";

import { useEffect, useRef, useState } from "react";
import type { LlmUsage } from "@/lib/types";
import type { ProgressLine } from "@/lib/useRun";
import ActivityFeed from "./ActivityFeed";
import UsageLine from "./UsageLine";

/** Activity as a pull-out tab on the right edge: every step, every AI model switch, and AI usage. */
export default function ActivityDrawer({ lines, working, usage }: {
  lines: ProgressLine[]; working: boolean; usage: LlmUsage | null;
}) {
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <>
      <button type="button" className="activity-tab" aria-haspopup="dialog" onClick={() => setOpen(true)}>
        {working && <span className="activity-dot" aria-hidden="true" />}
        Activity <span className="activity-count">{lines.length}</span>
      </button>
      <dialog ref={dialog} className="activity-drawer" aria-label="Activity" onClose={() => setOpen(false)}
        onClick={(e) => { if (e.target === dialog.current) setOpen(false); }}>
        <button type="button" className="dialog-close" aria-label="Close" onClick={() => setOpen(false)}>×</button>
        <ActivityFeed lines={lines} working={working} />
        <UsageLine usage={usage} />
      </dialog>
    </>
  );
}
