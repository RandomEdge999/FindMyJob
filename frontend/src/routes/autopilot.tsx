import { createFileRoute, Link } from "@tanstack/react-router";
import { useState, useCallback } from "react";
import {
  Play,
  Search,
  Trash2,
  RotateCcw,
  CheckCircle2,
  Clock,
  MessageSquare,
  Download,
  ChevronDown,
} from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel, PanelHeader } from "@/components/app/Card";
import { ReleasePosturePanel } from "@/components/app/ReleasePosturePanel";
import { StatusBadge } from "@/components/app/StatusBadge";
import { usePolledJson } from "@/hooks/use-polled-json";
import { useLiveConsole } from "@/hooks/use-live-console";
import { requestJson } from "@/lib/api";
import {
  autopilotPrimaryAction,
  buildQuestionAnswerRequest,
  deriveOperatorState,
  formatDate,
  formatNumber,
  questionDraftKey,
  toneFor,
  toneForStream,
} from "@/lib/helpers";
import { STAGE_LABELS } from "@/lib/constants";
import type {
  AutonomousStatus,
  LedgerExportResponse,
  LedgerExportStatus,
  QuestionQueueItem,
  JobTableRow,
  QuestionQueueResponse,
  JobTableResponse,
  ResetOperationalContract,
  ResetOperationalResponse,
  ReleasePosture,
} from "@/lib/types";

export const Route = createFileRoute("/autopilot")({
  component: AutopilotPage,
});

function AutopilotPage() {
  const { data: status, refresh: refreshStatus } = usePolledJson<AutonomousStatus>("/api/autonomous/status", 6000);
  const { data: questionsData, refresh: refreshQ } = usePolledJson<QuestionQueueResponse>("/api/questions/queue", 5000);
  const { data: jobsData, refresh: refreshJobs } = usePolledJson<JobTableResponse>("/api/jobs/table?limit=100", 7000);
  const { data: ledgerExport, refresh: refreshLedgerExport } = usePolledJson<LedgerExportStatus>(
    "/api/workspace/ledger-export",
    15000,
  );
  const { data: resetContract, refresh: refreshResetContract } = usePolledJson<ResetOperationalContract>(
    "/api/workspace/reset-operational/contract",
    15000,
  );
  const { data: releasePosture } = usePolledJson<ReleasePosture>("/api/release/posture", 15000);
  const questions = questionsData?.items ?? [];
  const jobs = jobsData?.items ?? [];
  const { snapshot, connection, lastSnapshotAt } = useLiveConsole();
  const op = deriveOperatorState(snapshot, connection, lastSnapshotAt);

  const [busy, setBusy] = useState("");
  const [lastReset, setLastReset] = useState<ResetOperationalResponse | null>(null);
  const [lastLedgerExport, setLastLedgerExport] = useState<LedgerExportResponse | null>(null);
  const [ledgerExportError, setLedgerExportError] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const doAction = useCallback(
    async (label: string, url: string, body?: Record<string, unknown>) => {
      setBusy(label);
      try {
        await requestJson(url, {
          method: "POST",
          body: body ? JSON.stringify(body) : undefined,
        });
        await Promise.all([refreshStatus(), refreshJobs(), refreshQ(), refreshResetContract()]);
      } finally {
        setBusy("");
      }
    },
    [refreshStatus, refreshJobs, refreshQ, refreshResetContract],
  );

  const resetOperational = useCallback(async () => {
    setBusy("reset");
    try {
      const payload = await requestJson<ResetOperationalResponse>("/api/workspace/reset-operational", {
        method: "POST",
      });
      setLastReset(payload);
      await Promise.all([refreshStatus(), refreshJobs(), refreshQ(), refreshLedgerExport(), refreshResetContract()]);
    } finally {
      setBusy("");
    }
  }, [refreshJobs, refreshLedgerExport, refreshQ, refreshResetContract, refreshStatus]);

  const exportLedger = useCallback(async () => {
    setBusy("ledger-export");
    setLedgerExportError("");
    try {
      const payload = await requestJson<LedgerExportResponse>("/api/workspace/ledger-export", {
        method: "POST",
      });
      setLastLedgerExport(payload);
      await Promise.all([refreshLedgerExport(), refreshResetContract()]);
    } catch (error) {
      setLedgerExportError(error instanceof Error ? error.message : "Failed to export the ledger snapshot.");
    } finally {
      setBusy("");
    }
  }, [refreshLedgerExport, refreshResetContract]);

  const answerQuestion = useCallback(
    async (question: QuestionQueueItem) => {
      const draftKey = questionDraftKey(question);
      const val = answers[draftKey];
      if (!val) return;
      setBusy(`answer-${draftKey}`);
      try {
        await requestJson("/api/questions/answer", {
          method: "POST",
          body: JSON.stringify(buildQuestionAnswerRequest(question, val)),
        });
        setAnswers((p) => {
          const copy = { ...p };
          delete copy[draftKey];
          return copy;
        });
        await Promise.all([refreshQ(), refreshJobs(), refreshStatus()]);
      } finally {
        setBusy("");
      }
    },
    [answers, refreshJobs, refreshQ, refreshStatus],
  );

  return (
    <AppShell>
      <PageHeader
        title="Autopilot"
        subtitle="Manage discovery, autonomous runs, and the live job queue."
        actions={
          <div className="flex items-center gap-2">
            <button
              disabled={!!busy}
              onClick={() => doAction("discover", "/api/discover")}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[13px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <Search className="h-4 w-4" />
              Discover
            </button>
            <button
              disabled={!!busy}
              onClick={() => doAction("run", "/api/autonomous/run")}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[13px] font-medium text-primary-foreground shadow-sm hover:bg-primary/90 disabled:opacity-50"
            >
              <Play className="h-4 w-4" />
              Run pipeline
            </button>
          </div>
        }
      />

      {/* Core: Status + timeline in 2 columns */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Panel>
          <PanelHeader
            title="Run Status"
            actions={
              <StatusBadge tone={toneForStream(op.streamHealth)} dot>
                {op.streamHealth}
              </StatusBadge>
            }
          />
          <dl className="space-y-2 text-[12.5px]">
            <div className="flex justify-between"><dt className="text-muted-foreground">Stage</dt><dd className="font-medium text-foreground">{STAGE_LABELS[op.stage] ?? op.stage}</dd></div>
            <div className="flex justify-between"><dt className="text-muted-foreground">Target</dt><dd className="max-w-[180px] truncate font-medium text-foreground">{op.currentTitle}</dd></div>
            <div className="flex justify-between"><dt className="text-muted-foreground">Elapsed</dt><dd className="font-medium tabular-nums text-foreground">{op.elapsed}</dd></div>
            <div className="flex justify-between"><dt className="text-muted-foreground">Last seen</dt><dd className="font-medium text-foreground">{op.lastSeen}</dd></div>
          </dl>
          {status && (
            <div className="mt-3 rounded-lg border border-border bg-surface p-3 text-[12px]">
              <div className="flex justify-between"><span className="text-muted-foreground">Today submitted</span><span className="font-semibold tabular-nums text-foreground">{status.daily_submitted_today ?? 0}</span></div>
              <div className="mt-1 flex justify-between"><span className="text-muted-foreground">Daily cap</span><span className="font-semibold tabular-nums text-foreground">{status.daily_submit_cap ?? "-"}</span></div>
            </div>
          )}
        </Panel>

        <Panel className="lg:col-span-2">
          <PanelHeader title="Stage trail" description="Pipeline progress." />
          <div className="flex flex-wrap gap-2 mb-3">
            {op.stageTrail.map((s) => (
              <div
                key={s.key}
                className={
                  "inline-flex items-center justify-center rounded-lg border px-3 py-1.5 text-[12px] font-medium " +
                  (s.active
                    ? "border-primary bg-primary/10 text-primary"
                    : s.done
                      ? "border-border bg-muted text-foreground"
                      : "border-border bg-surface text-muted-foreground")
                }
              >
                {s.label}
              </div>
            ))}
          </div>
          {op.eventsDescending.length > 0 && (
            <>
              <div className="mb-2 text-[11.5px] font-medium text-muted-foreground">Timeline</div>
              <ul className="max-h-48 -my-1 divide-y divide-border overflow-y-auto scrollbar-thin">
                {op.eventsDescending.slice(0, 8).map((ev, i) => (
                  <li key={ev.id ?? i} className="flex items-start gap-2 py-2">
                    <Clock className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1 text-[12px]">
                      <span className="font-medium text-foreground">{ev.message ?? "Event"}</span>{" "}
                      <span className="text-muted-foreground">
                        {[ev.company, ev.role].filter(Boolean).join(" \u00b7 ")}
                      </span>
                    </div>
                    <span className="shrink-0 text-[10.5px] text-muted-foreground">
                      {ev.created_at ? formatDate(ev.created_at) : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>
      </div>

      {/* Questions - collapsible */}
      {questions.length > 0 && (
        <div className="mt-3">
          <CollapsibleSection title="Questions" badge={String(questions.length)} defaultOpen>
            <ul className="max-h-80 space-y-3 overflow-y-auto scrollbar-thin">
              {questions.map((q) => {
                const qKey = questionDraftKey(q);
                return (
                  <li key={qKey} className="rounded-lg border border-border bg-surface p-3">
                    <div className="flex items-start gap-2">
                      <MessageSquare className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="text-[12.5px] font-medium text-foreground">{q.prompt_text}</div>
                        {q.company && (
                          <div className="mt-0.5 text-[11px] text-muted-foreground">
                            {q.company} {q.title ? `\u00b7 ${q.title}` : ""}
                          </div>
                        )}
                        {q.option_details && q.option_details.length > 0 ? (
                          <select
                            value={answers[qKey] ?? ""}
                            onChange={(e) => setAnswers((p) => ({ ...p, [qKey]: e.target.value }))}
                            className="mt-2 w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12px]"
                          >
                            <option value="">Select&#8230;</option>
                            {q.option_details.map((opt) => (
                              <option key={opt.value ?? opt.label} value={opt.value ?? opt.label ?? ""}>{opt.label ?? opt.value ?? ""}</option>
                            ))}
                          </select>
                        ) : (
                          <textarea
                            value={answers[qKey] ?? ""}
                            onChange={(e) => setAnswers((p) => ({ ...p, [qKey]: e.target.value }))}
                            rows={2}
                            className="mt-2 w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12px]"
                            placeholder="Your answer&#8230;"
                          />
                        )}
                        <button
                          disabled={!answers[qKey] || busy === `answer-${qKey}`}
                          onClick={() => answerQuestion(q)}
                          className="mt-2 inline-flex h-7 items-center justify-center gap-1 rounded-lg bg-primary px-2.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                        >
                          <CheckCircle2 className="h-3 w-3" /> Submit
                        </button>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </CollapsibleSection>
        </div>
      )}

      {/* Jobs table - bounded height */}
      <Panel className="mt-3" padded={false}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h3 className="text-[13.5px] font-semibold text-foreground">Job queue</h3>
            <p className="text-[11.5px] text-muted-foreground">{jobs.length} job{jobs.length !== 1 ? "s" : ""}</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={!!busy}
              onClick={() => doAction("purge", "/api/jobs/purge-rejected")}
              className="inline-flex h-8 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2.5 text-[12px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" /> Purge
            </button>
            <button
              disabled={!!busy}
              onClick={resetOperational}
              className="inline-flex h-8 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2.5 text-[12px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Reset
            </button>
          </div>
        </div>
        {jobs.length > 0 ? (
          <ul className="max-h-[50vh] divide-y divide-border overflow-y-auto scrollbar-thin">
            {jobs.map((job) => {
              const primaryAction = autopilotPrimaryAction(job);
              return (
                <li key={job.job_id} className="flex flex-col gap-2 px-4 py-3 transition-colors hover:bg-muted/50 sm:flex-row sm:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[13px] font-medium text-foreground">{job.role ?? "Untitled"}</div>
                    <div className="truncate text-[11px] text-muted-foreground">
                      {job.company ?? "-"} {job.source ? `\u00b7 ${job.source}` : ""}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge tone={toneFor(job.application_status)}>{job.application_status ?? "unknown"}</StatusBadge>
                    {job.manual_handoff?.active ? <StatusBadge tone="warning">handoff</StatusBadge> : null}
                    {primaryAction.kind === "approve" && job.application_id ? (
                      <button
                        disabled={!!busy}
                        onClick={() =>
                          doAction(`approve-${job.application_id}`, "/api/review/action", {
                            application_id: job.application_id,
                            action: "approve",
                          })
                        }
                        className="inline-flex h-7 items-center justify-center gap-1 rounded-lg bg-primary px-2.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      >
                        <CheckCircle2 className="h-3 w-3" /> {primaryAction.label}
                      </button>
                    ) : null}
                    {primaryAction.kind === "review" && job.application_id ? (
                      <Link
                        to="/review"
                        search={{ id: job.application_id, section: primaryAction.section ?? "summary" }}
                        className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2.5 text-[11px] font-medium hover:bg-muted"
                      >
                        {primaryAction.label}
                      </Link>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <Search className="mb-3 h-6 w-6 text-muted-foreground" />
            <p className="text-[12.5px] text-muted-foreground">No jobs in queue.</p>
          </div>
        )}
      </Panel>

      {/* Advanced section - collapsed by default */}
      <div className="mt-3">
        <CollapsibleSection title="Advanced">
          <div className="space-y-4">
            <ReleasePosturePanel posture={releasePosture} />

            {(ledgerExport || resetContract || lastReset) && (
              <Panel>
                <PanelHeader title="Workspace management" />
                <div className="space-y-3 text-[11.5px]">
                  {ledgerExport && (
                    <div>
                      <div className="font-medium text-foreground">Ledger export</div>
                      <p className="mt-1 text-muted-foreground">
                        {ledgerExport.exists
                          ? `Latest snapshot: ${formatDate(ledgerExport.last_generated_at)} with ${ledgerExport.files?.length ?? ledgerExport.existing_files?.length ?? 0} file(s).`
                          : "No ledger snapshot generated yet."}
                        {" "}State: {ledgerExport.current_state?.applications ?? 0} apps, {ledgerExport.current_state?.submissions ?? 0} submissions, {ledgerExport.current_state?.pending_questions ?? 0} pending questions.
                      </p>
                      <div className="mt-2 flex items-center gap-2">
                        <button
                          disabled={!!busy}
                          onClick={exportLedger}
                          className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2.5 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
                        >
                          <Download className="h-3.5 w-3.5" /> Export snapshot
                        </button>
                        {lastLedgerExport && (
                          <span className="text-muted-foreground">Generated {lastLedgerExport.generated_files?.length ?? 0} file(s).</span>
                        )}
                      </div>
                      {ledgerExportError && <p className="mt-1 text-red-600">{ledgerExportError}</p>}
                    </div>
                  )}
                  {resetContract && (
                    <div>
                      <div className="font-medium text-foreground">Reset contract</div>
                      {(resetContract.summary ?? []).map((line) => (
                        <p key={line} className="mt-0.5 text-muted-foreground">{line}</p>
                      ))}
                    </div>
                  )}
                  {lastReset && (
                    <p className="text-muted-foreground">
                      Last reset: {lastReset.deleted.applications ?? 0} apps, {lastReset.deleted.submissions ?? 0} submissions, {lastReset.deleted.runs ?? 0} runs removed.
                    </p>
                  )}
                </div>
              </Panel>
            )}
          </div>
        </CollapsibleSection>
      </div>
    </AppShell>
  );
}

function CollapsibleSection({
  title,
  badge,
  defaultOpen = false,
  children,
}: {
  title: string;
  badge?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded-xl border border-border bg-card px-4 py-3 text-left shadow-card transition-colors hover:bg-muted/40"
      >
        <span className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
          {title}
          {badge && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10.5px] font-medium tabular-nums text-muted-foreground">
              {badge}
            </span>
          )}
        </span>
        <ChevronDown className={"h-4 w-4 text-muted-foreground transition-transform " + (open ? "rotate-180" : "")} />
      </button>
      {open && <div className="mt-3 space-y-4">{children}</div>}
    </div>
  );
}
