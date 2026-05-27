import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { User, Sparkles, Clock, ChevronDown, ChevronUp, BarChart3 } from "lucide-react";
import clsx from "clsx";
import type { ChatMessage } from "../types";
import ChartViewer from "./ChartViewer";

export default function Message({ m }: { m: ChatMessage }) {
  const isUser = m.role === "user";
  const [showSteps, setShowSteps] = useState(false);

  return (
    <div className={clsx("flex gap-3 animate-fade-in", isUser ? "justify-end" : "")}>
      {!isUser && <Avatar role="assistant" />}

      <div className={clsx(
        "max-w-[80%] pane",
        isUser
          ? "bg-accent-600/15 border-accent-600/30 px-4 py-2.5"
          : "px-4 py-3",
      )}>
        {!isUser && m.intent && (
          <div className="flex items-center gap-2 mb-2 text-[10.5px] text-chalk-400">
            <span className="chip">{m.intent}</span>
            {m.confidence ? (
              <span className="chip">{Math.round(m.confidence * 100)}% conf</span>
            ) : null}
            {m.elapsed ? (
              <span className="flex items-center gap-1"><Clock size={10} /> {m.elapsed.toFixed(1)}s</span>
            ) : null}
          </div>
        )}

        {m.pending ? (
          <Pending />
        ) : (
          <div className="prose-agent">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {m.content || (isUser ? "" : "_(no output)_")}
            </ReactMarkdown>
          </div>
        )}

        {/* Charts */}
        {m.charts && m.charts.length > 0 && (
          <div className="mt-3 space-y-2">
            {m.charts.map((p, i) => (
              <ChartViewer key={i} path={p} />
            ))}
          </div>
        )}

        {/* Step results disclosure */}
        {m.step_results && m.step_results.length > 0 && (
          <div className="mt-3">
            <button
              className="text-[11px] text-chalk-400 hover:text-chalk-200 flex items-center gap-1"
              onClick={() => setShowSteps(s => !s)}
            >
              {showSteps ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              {m.step_results.length} execution step{m.step_results.length > 1 ? "s" : ""}
            </button>
            {showSteps && (
              <div className="mt-2 space-y-2">
                {m.step_results.map((s, i) => (
                  <div key={i} className="pane p-2 bg-ink-850">
                    <div className="flex items-center gap-2 text-[11px]">
                      <span className={clsx("status-dot", s.error ? "err" : "ok")} />
                      <span className="font-mono text-chalk-200">{s.step_id}</span>
                      <span className="text-chalk-400 truncate">{s.description}</span>
                      <span className="ml-auto text-chalk-500">{s.elapsed.toFixed(1)}s</span>
                    </div>
                    {s.code && (
                      <pre className="mt-1.5 font-mono text-[10.5px] text-chalk-300 bg-ink-900 border border-ink-700 rounded p-1.5 overflow-x-auto">
                        {s.code}
                      </pre>
                    )}
                    {s.error && (
                      <div className="mt-1 text-[11px] text-signal-err">{s.error}</div>
                    )}
                    {s.chart_path && (
                      <div className="mt-2 flex items-center gap-1 text-[10.5px] text-accent-400">
                        <BarChart3 size={10} /> chart attached
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {isUser && <Avatar role="user" />}
    </div>
  );
}

function Avatar({ role }: { role: "user" | "assistant" }) {
  return (
    <div className={clsx(
      "w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5",
      role === "user"
        ? "bg-accent-600/20 text-accent-400 border border-accent-600/40"
        : "bg-ink-800 text-chalk-200 border border-ink-700",
    )}>
      {role === "user" ? <User size={13} /> : <Sparkles size={13} />}
    </div>
  );
}

function Pending() {
  return (
    <div className="flex items-center gap-1.5 text-chalk-400 text-[12px] py-1">
      <span className="status-dot info" />
      <span className="status-dot info" style={{ animationDelay: "200ms" }} />
      <span className="status-dot info" style={{ animationDelay: "400ms" }} />
      <span className="ml-2">thinking</span>
    </div>
  );
}
