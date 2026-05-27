import { useEffect, useRef, useState } from "react";
import {
  Send, MessageSquare, FileText, Globe, Wrench, Sparkles, Square,
} from "lucide-react";
import clsx from "clsx";
import { useStore } from "../store";
import type { Mode, SessionSchema } from "../types";
import Message from "./Message";
import FileDropZone from "./FileDropZone";

const MODE_OPTIONS: { value: Mode; label: string; icon: React.ReactNode; hint: string }[] = [
  { value: "chat",   label: "Chat",   icon: <MessageSquare size={13} />, hint: "Conversational analysis" },
  { value: "report", label: "Report", icon: <FileText      size={13} />, hint: "Full multi-step report" },
  { value: "verify", label: "Verify", icon: <Globe         size={13} />, hint: "Internet-assisted verification" },
  { value: "mutate", label: "Mutate", icon: <Wrench        size={13} />, hint: "Modify the spreadsheet" },
];

export default function ChatPanel() {
  const {
    activeSessionId, activeSchema, messages, sending, mode, setMode, sendQuery,
  } = useStore();
  const [input, setInput] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, sending]);

  function onSubmit(e?: React.FormEvent) {
    e?.preventDefault();
    if (!input.trim() || sending) return;
    void sendQuery(input);
    setInput("");
    requestAnimationFrame(() => taRef.current?.focus());
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSubmit();
    }
  }

  // No session loaded → drop zone / welcome
  if (!activeSessionId) {
    return (
      <main className="flex flex-col min-h-0 bg-ink-950">
        <Header schemaSummary={null} />
        <FileDropZone />
      </main>
    );
  }

  return (
    <main className="flex flex-col min-h-0 bg-ink-950">
      <Header schemaSummary={activeSchema} />

      {/* messages */}
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-4">
        {messages.length === 0 && <EmptyChat />}
        {messages.map(m => <Message key={m.id} m={m} />)}
        <div ref={endRef} />
      </div>

      {/* composer */}
      <form
        onSubmit={onSubmit}
        className="border-t border-ink-800 px-4 py-3 bg-ink-900"
      >
        <div className="flex items-center gap-1 mb-2">
          {MODE_OPTIONS.map(opt => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setMode(opt.value)}
              className={clsx(
                "chip cursor-pointer transition-colors",
                mode === opt.value
                  ? "bg-accent-600/20 border-accent-600/50 text-accent-400"
                  : "hover:border-ink-500",
              )}
              title={opt.hint}
            >
              {opt.icon}
              {opt.label}
            </button>
          ))}
          <div className="flex-1" />
          <div className="text-[10.5px] text-chalk-500">
            <Sparkles size={11} className="inline -mt-0.5 mr-1" />
            Local model · streaming
          </div>
        </div>

        <div className="flex items-end gap-2">
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder={modePlaceholder(mode)}
            className="input font-sans resize-none flex-1 min-h-[44px] max-h-[180px]"
            disabled={sending}
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className={clsx("btn-primary h-[44px] px-4", sending && "opacity-70")}
            title="Send  (Enter)"
          >
            {sending ? <Square size={14} /> : <Send size={14} />}
            {sending ? "Working" : "Send"}
          </button>
        </div>
        <div className="mt-1.5 text-[10.5px] text-chalk-500 flex gap-3">
          <span><span className="kbd">Enter</span> send</span>
          <span><span className="kbd">Shift+Enter</span> newline</span>
        </div>
      </form>
    </main>
  );
}

// ----- helpers -------------------------------------------------------------

function Header({ schemaSummary }: { schemaSummary: SessionSchema | null }) {
  const session = useStore(s => s.sessions.find(x => x.id === s.activeSessionId));
  return (
    <header className="h-12 px-6 flex items-center gap-3 border-b border-ink-800 select-none">
      {session ? (
        <>
          <div className="text-sm font-semibold tracking-tight">{session.name}</div>
          <span className="chip">{session.file_name}</span>
          {schemaSummary && (
            <>
              <span className="chip">{schemaSummary.shape.rows.toLocaleString()} rows</span>
              <span className="chip">{schemaSummary.shape.columns} cols</span>
              <span className="chip">{schemaSummary.domain}</span>
            </>
          )}
        </>
      ) : (
        <div className="text-sm text-chalk-300">Open a spreadsheet to begin.</div>
      )}
    </header>
  );
}

function EmptyChat() {
  return (
    <div className="max-w-2xl mx-auto mt-12 text-center select-none">
      <div className="text-[15px] text-chalk-200 font-medium mb-1">Ask anything about your spreadsheet.</div>
      <div className="text-[12.5px] text-chalk-400 mb-6">
        Try a question, request a report, verify dates against the web, or mutate a column.
      </div>
      <div className="grid grid-cols-2 gap-2 text-left">
        {EXAMPLE_QUERIES.map((q, i) => (
          <SampleCard key={i} title={q.title} body={q.body} />
        ))}
      </div>
    </div>
  );
}

const EXAMPLE_QUERIES = [
  { title: "Quick stats",    body: "How many wins vs losses, broken down by Sport?" },
  { title: "Full report",    body: "Generate a performance report with charts." },
  { title: "Verify dates",   body: "Verify the dates of pending matches against the web." },
  { title: "Add a column",   body: "Add a Risk Level column based on Stake and Result." },
];

function SampleCard({ title, body }: { title: string; body: string }) {
  const { sendQuery } = useStore();
  return (
    <button
      onClick={() => void sendQuery(body)}
      className="pane p-3 hover:border-accent-600/40 hover:bg-ink-850 transition-colors text-left"
    >
      <div className="text-[12.5px] font-medium text-accent-400 mb-1">{title}</div>
      <div className="text-[12px] text-chalk-300 leading-snug">{body}</div>
    </button>
  );
}

function modePlaceholder(mode: Mode) {
  switch (mode) {
    case "report":  return "Describe the report you want…";
    case "verify":  return "Describe what should be verified against the web…";
    case "mutate":  return "Describe the column or change you want…";
    default:        return "Ask a question or describe an analysis…";
  }
}
