import { createFileRoute, Link } from "@tanstack/react-router";
import { History, Play, RefreshCw } from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel } from "@/components/app/Card";
import { StatusBadge } from "@/components/app/StatusBadge";
import { usePolledJson } from "@/hooks/use-polled-json";
import { formatDate, formatDuration, toneFor, formatNumber } from "@/lib/helpers";
import type { RunHistoryResponse, RunHistoryRow } from "@/lib/types";

export const Route = createFileRoute("/runs")({
  component: RunsPage,
});

function computeDuration(run: RunHistoryRow): number | null {
  if (!run.started_at || !run.completed_at) return null;
  const start = new Date(run.started_at).getTime();
  const end = new Date(run.completed_at).getTime();
  if (isNaN(start) || isNaN(end)) return null;
  return Math.round((end - start) / 1000);
}

function RunsPage() {
  const { data: runsData, refresh } = usePolledJson<RunHistoryResponse>("/api/runs/history", 8000);
  const list = runsData?.items ?? [];

  return (
    <AppShell>
      <PageHeader
        title="Run History"
        subtitle="Complete log of every pipeline execution."
        actions={
          <div className="flex gap-2">
            <button onClick={() => refresh()}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[13px] font-medium hover:bg-muted">
              <RefreshCw className="h-4 w-4" /> Refresh
            </button>
            <Link to="/autopilot"
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[13px] font-medium text-primary-foreground shadow-sm hover:bg-primary/90">
              <Play className="h-4 w-4" /> New run
            </Link>
          </div>
        }
      />
      {list.length > 0 ? (
        <Panel padded={false}>
          <ul className="max-h-[70vh] divide-y divide-border overflow-y-auto scrollbar-thin">
            {list.map((run) => (
              <li key={run.run_id} className="flex items-center gap-3 px-5 py-3.5 transition-colors hover:bg-muted/50">
                <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary/10">
                  <History className="h-4 w-4 text-primary" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-foreground">{run.run_type ?? "run"}</span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                    <span>{run.started_at ? formatDate(run.started_at) : "�"}</span>
                    {(() => { const d = computeDuration(run); return d != null ? <><span>�</span><span>{formatDuration(d)}</span></> : null; })()}
                    {run.processed_count != null && <><span>�</span><span>{formatNumber(run.processed_count)} processed</span></>}
                    {run.submitted_count != null && run.submitted_count > 0 && <><span>�</span><span>{formatNumber(run.submitted_count)} submitted</span></>}
                  </div>
                </div>
                <StatusBadge tone={toneFor(run.status)}>{run.status ?? "unknown"}</StatusBadge>
              </li>
            ))}
          </ul>
        </Panel>
      ) : (
        <Panel>
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <History className="mb-3 h-8 w-8 text-muted-foreground" />
            <div className="text-[14px] font-semibold text-foreground">No runs yet</div>
            <p className="mt-1 text-[12.5px] text-muted-foreground">
              Start a pipeline run from the <Link to="/autopilot" className="text-primary hover:underline">Autopilot</Link> page to see history here.
            </p>
          </div>
        </Panel>
      )}
    </AppShell>
  );
}
