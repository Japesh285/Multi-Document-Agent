import { Loader2, AlertTriangle } from "lucide-react";

export default function BootSplash({ error }: { error: string | null }) {
  return (
    <div className="h-screen w-screen flex flex-col items-center justify-center bg-ink-950 text-chalk-100 select-none">
      <div className="flex items-center gap-3 text-accent-400 text-lg font-semibold">
        <div className="w-8 h-8 rounded-md bg-accent-600/20 border border-accent-600/40 flex items-center justify-center">
          <span className="font-mono">SA</span>
        </div>
        Spreadsheet Agent
      </div>

      <div className="mt-12 text-sm text-chalk-300 flex items-center gap-2">
        {error ? (
          <>
            <AlertTriangle size={16} className="text-signal-err" />
            <span>{error}</span>
          </>
        ) : (
          <>
            <Loader2 size={16} className="animate-spin text-accent-400" />
            <span>Starting backend…</span>
          </>
        )}
      </div>

      {error && (
        <div className="mt-6 text-[12px] text-chalk-400 max-w-md text-center">
          Check that Python is installed and dependencies are present
          (<span className="font-mono text-chalk-200">pip install -r requirements.txt</span>).
        </div>
      )}

      <div className="mt-auto mb-6 text-[10px] text-chalk-500 font-mono">
        local-first &nbsp;·&nbsp; ollama &nbsp;·&nbsp; pandas &nbsp;·&nbsp; fastapi
      </div>
    </div>
  );
}
