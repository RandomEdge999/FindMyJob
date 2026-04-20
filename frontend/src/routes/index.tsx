import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import {
  Activity,
  ArrowUpRight,
  ChevronDown,
  CircleAlert,
  CircleCheck,
  Hourglass,
  Play,
  TrendingUp,
} from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel, PanelHeader } from "@/components/app/Card";
import { StatusBadge } from "@/components/app/StatusBadge";
import { usePolledJson } from "@/hooks/use-polled-json";
import { useLiveConsole } from "@/hooks/use-live-console";
import { deriveOperatorState, formatNumber, formatDate, toneFor, toneForStream } from "@/lib/helpers";
import { STAGE_LABELS } from "@/lib/constants";
import type { DailyInboxResponse, RunHistoryResponse } from "@/lib/types";

export const Route = createFileRoute("/")({
  component: DashboardPage,
});

function DashboardPage() {
  const { data: runsData } = usePolledJson<RunHistoryResponse>("/api/runs/history", 9000);
  const { data: dailyData } = usePolledJson<DailyInboxResponse>("/api/daily/inbox?limit=8", 10000);
  const runs = runsData?.items ?? [];
  const inboxItems = dailyData?.items ?? [];
  const [showInbox, setShowInbox] = useState(false);
  const { snapshot, connection, lastSnapshotAt } = useLiveConsole();
  const op = deriveOperatorState(snapshot, connection, lastSnapshotAt);

  return (
    <AppShell>
      <PageHeader
        title="Dashboard"
        subtitle="Pipeline health and queue at a glance."
        actions={
          <div className="flex items-center gap-2">
            <Link
              to="/autopilot"
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-xl bg-primary px-4 text-[13px] font-medium text-primary-foreground shadow-sm hover:bg-primary/90"
            >
              <Play className="h-4 w-4" />
              New Run
            </Link>
          </div>
        }
      />

      {/* ── Stat cards row ── */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <DashStatCard label="Discovered" value={op.counters.discovered} featured />
        <DashStatCard label="Submitted" value={op.counters.submitted} delta="pipeline" />
        <DashStatCard label="Ready to Apply" value={op.counters.readyToApply} delta="pipeline" />
        <DashStatCard label="Blocked" value={op.counters.blockedByQuestions} />
      </div>

      {/* ── Main grid: 3 columns like Donezo ── */}
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">

        {/* Run Status - spans 1 col */}
        <Panel className="flex flex-col">
          <PanelHeader
            title="Run Status"
            actions={
              <StatusBadge tone={toneForStream(op.streamHealth)} dot>
                {op.streamHealth}
              </StatusBadge>
            }
          />
          <div className="grid grid-cols-2 gap-2">
            <MiniStat icon={<Hourglass className="h-3.5 w-3.5" />} label="Queue" value={String(op.queue.depth)} />
            <MiniStat icon={<Activity className="h-3.5 w-3.5" />} label="Stage" value={STAGE_LABELS[op.stage] ?? op.stage} />
            <MiniStat icon={<CircleCheck className="h-3.5 w-3.5" />} label="Submitted" value={formatNumber(op.counters.submitted)} />
            <MiniStat
              icon={<CircleAlert className="h-3.5 w-3.5" />}
              label="Errors"
              value={formatNumber(op.counters.failed)}
              tone={op.counters.failed > 0 ? "warning" : undefined}
            />
          </div>
          <div className="mt-2 flex-1 rounded-lg border border-border bg-surface p-2.5">
            <div className="text-[10.5px] text-muted-foreground">Current target</div>
            <div className="mt-0.5 truncate text-[12px] font-medium text-foreground">{op.currentTitle || "\u2014"}</div>
            <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <span>Elapsed {op.elapsed}</span>
              <span>&middot;</span>
              <span>Last {op.lastSeen}</span>
            </div>
          </div>
          {op.warningNotice && (
            <div className="mt-2 rounded-lg border border-border bg-surface p-2 text-[11px] text-[oklch(0.45_0.18_27)]">
              {op.warningNotice}
            </div>
          )}
        </Panel>

        {/* Recent Events - spans 1 col */}
        <Panel className="flex flex-col">
          <PanelHeader title="Recent Events" />
          {op.eventsDescending.length > 0 ? (
            <ul className="flex-1 -mx-1 max-h-56 divide-y divide-border overflow-y-auto scrollbar-thin px-1">
              {op.eventsDescending.slice(0, 12).map((ev, i) => (
                <li key={ev.id ?? i} className="flex items-start gap-2 py-1.5">
                  <span className={"mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full " + dotColor(ev)} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[11.5px] font-medium text-foreground">{ev.message ?? "Event"}</div>
                    <div className="truncate text-[10px] text-muted-foreground">
                      {[ev.company, ev.role].filter(Boolean).join(" \u00b7 ") || ev.source || ""}
                    </div>
                  </div>
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                    {ev.created_at ? formatDate(ev.created_at) : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="flex flex-1 items-center justify-center text-[11.5px] text-muted-foreground">
              No recent events
            </div>
          )}
        </Panel>

        {/* Pipeline Stages + Source Mix - spans 1 col */}
        <Panel className="flex flex-col">
          <PanelHeader title="Pipeline" />
          <div className="flex flex-wrap gap-1.5">
            {op.stageTrail.map((s) => (
              <span
                key={s.key}
                className={
                  "inline-flex items-center justify-center rounded-md border px-2 py-0.5 text-[10.5px] font-medium " +
                  (s.active
                    ? "border-primary bg-primary/10 text-primary"
                    : s.done
                      ? "border-border bg-muted text-foreground"
                      : "border-border bg-surface text-muted-foreground")
                }
              >
                {s.label}
              </span>
            ))}
          </div>
          {op.sourceMix.length > 0 && (
            <div className="mt-2">
              <div className="mb-1 text-[10.5px] font-medium text-muted-foreground">Source mix</div>
              <div className="flex flex-wrap gap-1.5">
                {op.sourceMix.map(([source, count]) => (
                  <span key={source} className="inline-flex items-center rounded-md border border-border bg-surface px-1.5 py-0.5 text-[10.5px] text-foreground">
                    {source} <span className="ml-0.5 tabular-nums text-muted-foreground">{count}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </Panel>
      </div>

      {/* ── Bottom grid: 2 columns ── */}
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">

        {/* Counters - always visible, compact */}
        <Panel>
          <PanelHeader title="Counters" />
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-4">
            {([
              ["Discovered", op.counters.discovered],
              ["Screened", op.counters.screenedOut],
              ["Evaluated", op.counters.evaluated],
              ["Drafted", op.counters.drafted],
              ["Ready", op.counters.readyToApply],
              ["Submitted", op.counters.submitted],
              ["Failed", op.counters.failed],
              ["Blocked", op.counters.blockedByQuestions],
            ] as const).map(([label, value]) => (
              <div key={label} className="flex items-center justify-between py-0.5 text-[11.5px]">
                <span className="text-muted-foreground">{label}</span>
                <span className="font-semibold tabular-nums text-foreground">{formatNumber(value)}</span>
              </div>
            ))}
          </div>
        </Panel>

        {/* Run History - always visible, compact */}
        <Panel className="flex flex-col">
          <PanelHeader
            title="Run History"
            actions={
              runs.length > 0 ? (
                <Link to="/runs" className="text-[11px] font-medium text-primary hover:underline">View all</Link>
              ) : null
            }
          />
          {runs.length > 0 ? (
            <ul className="flex-1 max-h-48 -mx-1 divide-y divide-border overflow-y-auto scrollbar-thin px-1">
              {runs.slice(0, 8).map((run) => (
                <li key={run.run_id} className="flex items-center gap-2 py-1.5">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[11.5px] font-medium text-foreground">
                      {run.run_type ?? "run"}
                    </div>
                    <div className="truncate text-[10px] text-muted-foreground">
                      {run.started_at ? formatDate(run.started_at) : "-"}
                      {run.submitted_count ? ` \u00b7 ${run.submitted_count} submitted` : ""}
                    </div>
                  </div>
                  <StatusBadge tone={toneFor(run.status)}>{run.status ?? "unknown"}</StatusBadge>
                </li>
              ))}
            </ul>
          ) : (
            <div className="flex flex-1 items-center justify-center py-6 text-[11.5px] text-muted-foreground">
              No runs yet.{" "}
              <Link to="/autopilot" className="ml-1 text-primary hover:underline">Start one</Link>
            </div>
          )}
        </Panel>
      </div>

      <Panel className="mt-3">
        <PanelHeader
          title="Inbox"
          description="Optional view of the current inbox without leaving the dashboard."
          actions={
            <button
              type="button"
              onClick={() => setShowInbox((value) => !value)}
              className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted"
            >
              {showInbox ? "Hide" : "Show"}
              <ChevronDown className={"h-4 w-4 transition-transform " + (showInbox ? "rotate-180" : "")} />
            </button>
          }
        />
        {showInbox ? (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <StatusBadge tone="neutral">New {formatNumber(dailyData?.counts?.new_matching ?? 0)}</StatusBadge>
              <StatusBadge tone="warning">Needs Input {formatNumber(dailyData?.counts?.needs_user_input ?? 0)}</StatusBadge>
              <StatusBadge tone="success">Review Ready {formatNumber(dailyData?.counts?.ready_for_review ?? 0)}</StatusBadge>
              <StatusBadge tone="info">Applied {formatNumber(dailyData?.counts?.approved_pending_submit ?? 0)}</StatusBadge>
            </div>
            {inboxItems.length > 0 ? (
              <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
                {inboxItems.map((item) => (
                  <li key={item.job_id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[12.5px] font-medium text-foreground">{item.title ?? "Untitled"}</div>
                      <div className="truncate text-[11px] text-muted-foreground">
                        {item.company ?? "-"} {item.source ? `\u00b7 ${item.source}` : ""}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusBadge tone={toneFor(item.workflow_state)}>{item.workflow_state ?? "unknown"}</StatusBadge>
                      {item.status && item.status !== item.workflow_state ? (
                        <StatusBadge tone={toneFor(item.status)}>{item.status}</StatusBadge>
                      ) : null}
                      {item.submission_status ? (
                        <StatusBadge tone={toneFor(item.submission_status)}>{item.submission_status}</StatusBadge>
                      ) : null}
                      {item.application_id ? (
                        <Link
                          to="/review"
                          search={{ id: item.application_id, section: "summary" }}
                          className="inline-flex h-7 items-center justify-center rounded-lg border border-border bg-card px-2.5 text-[11px] font-medium hover:bg-muted"
                        >
                          Open
                        </Link>
                      ) : (
                        <Link
                          to="/autopilot"
                          className="inline-flex h-7 items-center justify-center rounded-lg border border-border bg-card px-2.5 text-[11px] font-medium hover:bg-muted"
                        >
                          Queue
                        </Link>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[11.5px] text-muted-foreground">No inbox items are available yet.</p>
            )}
          </div>
        ) : (
          <p className="text-[11.5px] text-muted-foreground">
            Hidden by default. Expand it when you want the current inbox counts and quick review links.
          </p>
        )}
      </Panel>
    </AppShell>
  );
}

/* ── Stat card matching Donezo design ── */
function DashStatCard({ label, value, featured, delta }: {
  label: string; value: string | number; featured?: boolean; delta?: string;
}) {
  return (
    <div
      className={
        "relative flex flex-col rounded-xl border p-3.5 shadow-card " +
        (featured
          ? "border-transparent bg-primary text-primary-foreground"
          : "border-border bg-card text-foreground")
      }
    >
      <div className="flex items-start justify-between">
        <span className={"text-[11px] font-medium " + (featured ? "text-primary-foreground/80" : "text-muted-foreground")}>
          {label}
        </span>
        <div className={"grid h-6 w-6 place-items-center rounded-md " + (featured ? "bg-primary-foreground/15" : "border border-border")}>
          <ArrowUpRight className="h-3 w-3" />
        </div>
      </div>
      <div className="mt-1.5 text-[24px] font-bold leading-none tracking-tight tabular-nums">
        {value}
      </div>
      {delta && (
        <div className={"mt-1.5 flex items-center gap-1 text-[10px] " + (featured ? "text-primary-foreground/70" : "text-muted-foreground")}>
          <TrendingUp className="h-3 w-3" />
          <span>From pipeline</span>
        </div>
      )}
    </div>
  );
}

function MiniStat({ icon, label, value, tone }: {
  icon: React.ReactNode; label: string; value: string; tone?: "warning";
}) {
  return (
    <div className="rounded-lg border border-border bg-surface px-2.5 py-1.5">
      <div className="flex items-center gap-1 text-[10px] text-muted-foreground">{icon} {label}</div>
      <div className={"mt-0.5 text-[14px] font-semibold tabular-nums leading-tight " + (tone === "warning" ? "text-[oklch(0.45_0.18_27)]" : "text-foreground")}>
        {value}
      </div>
    </div>
  );
}

function dotColor(ev: { message?: string; source?: string }): string {
  const msg = (ev.message ?? "").toLowerCase();
  if (msg.includes("submit")) return "bg-primary";
  if (msg.includes("eval") || msg.includes("screen")) return "bg-[oklch(0.55_0.15_240)]";
  if (msg.includes("fail") || msg.includes("error")) return "bg-[oklch(0.45_0.18_27)]";
  return "bg-muted-foreground";
}
