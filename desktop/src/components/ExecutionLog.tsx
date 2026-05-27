import { useState } from "react";
import { ChevronDown, ChevronRight, CheckCircle2, XCircle } from "lucide-react";
import clsx from "clsx";
import type { StepResult } from "../types";
import { chartFileUrl } from "../lib/api";

export default function ExecutionLog({ steps }: { steps: StepResult[] }) {
  return (
    <div className="space-y-2">
      {steps.map((s, i) => <StepCard key={i} step={s} idx={i} />)}
    </div>
  );
}

function StepCard({ step, idx }: { step: StepResult; idx: number }) {
  const [open, setOpen] = useState(false);
  const failed = !!step.error;

  return (
    <div className="pane">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 p-2 text-left hover:bg-ink-850 transition-colors rounded-lg"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {failed
          ? <XCircle size={13} className="text-signal-err" />
          : <CheckCircle2 size={13} className="text-signal-ok" />}
        <span className="font-mono text-[11.5px] text-chalk-200 truncate">
          {idx + 1}. {step.step_id}
        </span>
        <span className="ml-auto text-[10.5px] text-chalk-500">
          {step.elapsed.toFixed(1)}s
        </span>
      </button>

      {open && (
        <div className="px-2 pb-2 space-y-1.5 border-t border-ink-800">
          <div className="text-[11.5px] text-chalk-300 pt-2">{step.description}</div>

          {step.code && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-chalk-500 mb-0.5">code</div>
              <pre className="font-mono text-[10.5px] text-chalk-200 bg-ink-900 border border-ink-700 rounded p-2 overflow-x-auto whitespace-pre-wrap">
                {step.code}
              </pre>
            </div>
          )}

          {step.output && !step.error && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-chalk-500 mb-0.5">output</div>
              <pre className="font-mono text-[10.5px] text-chalk-300 bg-ink-850 border border-ink-700 rounded p-2 overflow-x-auto max-h-[180px]">
                {step.output}
              </pre>
            </div>
          )}

          {step.error && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-signal-err mb-0.5">error</div>
              <pre className="font-mono text-[10.5px] text-signal-err bg-ink-850 border border-signal-err/30 rounded p-2 overflow-x-auto">
                {step.error}
              </pre>
            </div>
          )}

          {step.chart_path && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-chalk-500 mb-0.5">chart</div>
              <img
                src={chartFileUrl(step.chart_path)}
                alt="chart"
                className="rounded border border-ink-700 max-w-full"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
