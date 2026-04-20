import { createFileRoute, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  FileText,
  Filter,
  History,
  MessageSquare,
  RefreshCw,
  Search,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel, PanelHeader } from "@/components/app/Card";
import { StatusBadge } from "@/components/app/StatusBadge";
import { SegmentedTabs } from "@/components/app/SegmentedTabs";
import { usePolledJson } from "@/hooks/use-polled-json";
import { requestJson } from "@/lib/api";
import {
  buildQuestionAnswerRequest,
  formatDate,
  hydrateAnswerDraft,
  isEditableShortcutTarget,
  questionDraftKey,
  questionOptions,
  reviewQueueMatchesFilter,
  toneFor,
} from "@/lib/helpers";
import {
  REVIEW_TABS,
  REVIEW_QUEUE_FILTERS,
  REVIEW_SECTION_FROM_TAB,
  REVIEW_TAB_FROM_SECTION,
} from "@/lib/constants";
import type { ApplicationDetail, QuestionQueueItem, ReviewQueueResponse } from "@/lib/types";

type ReviewFilter = "all" | "needs_input" | "manual_handoff" | "ready";

export const Route = createFileRoute("/review")({
  component: ReviewPage,
  validateSearch: (search: Record<string, unknown>) => ({
    id: (search.id as string) ?? undefined,
    section: (search.section as string) ?? "summary",
  }),
});

function ReviewPage() {
  const { id: selectedId, section } = useSearch({ from: "/review" });
  const navigate = useNavigate();

  const { data: queueData, refresh: refreshQueue } = usePolledJson<ReviewQueueResponse>("/api/review/queue", 7000);
  const queue = queueData?.items ?? [];
  const { data: detail, refresh: refreshDetail } = usePolledJson<ApplicationDetail>(
    selectedId ? `/api/applications/${selectedId}` : "",
    7000,
  );

  const [filter, setFilter] = useState<ReviewFilter>("all");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState("");
  const [questionDrafts, setQuestionDrafts] = useState<Record<string, string | string[]>>({});

  const tab = REVIEW_TAB_FROM_SECTION[section] ?? "summary";

  const setTab = useCallback(
    (t: string) => navigate({ to: "/review", search: { id: selectedId, section: REVIEW_SECTION_FROM_TAB[t] ?? t } }),
    [navigate, selectedId],
  );
  const selectItem = useCallback(
    (appId: string) => navigate({ to: "/review", search: { id: appId, section } }),
    [navigate, section],
  );

  const items = useMemo(() => {
    let list = queue;
    if (filter !== "all") list = list.filter((item) => reviewQueueMatchesFilter(item, filter));
    if (search) {
      const q = search.toLowerCase();
      list = list.filter(
        (r) =>
          (r.company ?? "").toLowerCase().includes(q) ||
          (r.title ?? "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [queue, filter, search]);

  const doAction = useCallback(
    async (action: string, appId?: string) => {
      const target = appId ?? selectedId;
      if (!target) return;
      setBusy(action);
      try {
        await requestJson("/api/review/action", {
          method: "POST",
          body: JSON.stringify({ application_id: target, action }),
        });
        await refreshQueue();
        if (target === selectedId) {
          await refreshDetail();
        }
      } finally {
        setBusy("");
      }
    },
    [selectedId, refreshDetail, refreshQueue],
  );

  const answerQuestion = useCallback(
    async (question: QuestionQueueItem) => {
      const draftKey = questionDraftKey(question);
      const draftValue = questionDrafts[draftKey];
      const answerText = Array.isArray(draftValue) ? draftValue.join(", ") : String(draftValue ?? "").trim();
      if (!answerText) return;
      setBusy(`answer-${draftKey}`);
      try {
        await requestJson("/api/questions/answer", {
          method: "POST",
          body: JSON.stringify(buildQuestionAnswerRequest(question, draftValue)),
        });
        setQuestionDrafts((current) => {
          const next = { ...current };
          delete next[draftKey];
          return next;
        });
        await refreshQueue();
        await refreshDetail();
      } finally {
        setBusy("");
      }
    },
    [questionDrafts, refreshDetail, refreshQueue],
  );

  /* Keyboard shortcuts */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (isEditableShortcutTarget(e.target)) return;
      const idx = items.findIndex((r) => r.application_id === selectedId);
      switch (e.key) {
        case "j":
          if (idx < items.length - 1) selectItem(items[idx + 1].application_id);
          break;
        case "k":
          if (idx > 0) selectItem(items[idx - 1].application_id);
          break;
        case "a":
          doAction("approve");
          break;
        case "o":
          doAction("request_input");
          break;
        case "s":
          doAction("sync_manual_input");
          break;
        case "m":
          doAction("mark_submitted");
          break;
        case "r":
          doAction("reject");
          break;
        case "/":
          e.preventDefault();
          document.getElementById("review-search")?.focus();
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [items, selectedId, detail, selectItem, doAction]);

  return (
    <AppShell>
      <PageHeader
        title="Review"
        subtitle="Inspect, approve, or reject applications before submission."
      />

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        {/* Left: queue list */}
        <div className="lg:col-span-1 space-y-3">
          <Panel padded={false}>
            <div className="border-b border-border px-4 py-3">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <input
                  id="review-search"
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search queue�"
                  className="w-full h-8 rounded-lg border border-border bg-card pl-8 pr-3 text-[12px] text-foreground placeholder:text-muted-foreground"
                />
              </div>
              <div className="mt-2">
                <SegmentedTabs
                  options={REVIEW_QUEUE_FILTERS.map((f) => ({ value: f.key, label: f.label }))}
                  value={filter}
                  onChange={(k: string) => setFilter(k as ReviewFilter)}
                />
              </div>
            </div>
            {items.length > 0 ? (
              <ul className="max-h-[60vh] overflow-y-auto divide-y divide-border">
                {items.map((item) => (
                  <li key={item.application_id}>
                    <button
                      onClick={() => selectItem(item.application_id)}
                      className={
                        "w-full text-left px-4 py-3 transition-colors hover:bg-muted/50 " +
                        (item.application_id === selectedId ? "bg-muted" : "")
                      }
                    >
                      <div className="truncate text-[13px] font-medium text-foreground">{item.title ?? "Untitled"}</div>
                      <div className="truncate text-[11px] text-muted-foreground">
                        {item.company ?? "-"} � {item.source ?? "-"}
                      </div>
                      <div className="mt-1 flex items-center gap-1.5">
                        <StatusBadge tone={toneFor(item.review_status ?? item.status)}>{item.review_status ?? item.status}</StatusBadge>
                        {item.manual_handoff?.active ? <StatusBadge tone="warning">manual handoff</StatusBadge> : null}
                      </div>
                      {item.review_summary?.next_action_reason ? (
                        <div className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">
                          {item.review_summary.next_action_reason}
                        </div>
                      ) : null}
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="px-4 py-8 text-center text-[12px] text-muted-foreground">
                {search ? "No matching items." : "Queue is empty."}
              </div>
            )}
          </Panel>
        </div>

        {/* Right: detail */}
        <div className="lg:col-span-2">
          {selectedId && detail ? (
            <Panel>
              <div className="flex items-start justify-between">
                <div>
                  <h2 className="text-[16px] font-semibold text-foreground">{detail.application?.role ?? "Untitled"}</h2>
                  <p className="mt-0.5 text-[12.5px] text-muted-foreground">
                    {detail.application?.company ?? "-"} � {detail.application?.source ?? "-"}
                    {detail.job?.location_raw ? ` � ${detail.job.location_raw}` : ""}
                  </p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {detail.application?.url && (
                    <a
                      href={detail.application.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 text-[11px] font-medium hover:bg-muted"
                    >
                      <ExternalLink className="h-3 w-3" /> Open
                    </a>
                  )}
                  <button
                    disabled={!!busy}
                    onClick={() => doAction("approve")}
                    className="inline-flex h-7 items-center justify-center gap-1 rounded-lg bg-primary px-2.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <ThumbsUp className="h-3 w-3" /> Approve
                  </button>
                  <button
                    disabled={!!busy}
                    onClick={() => doAction("request_input")}
                    className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
                  >
                    <MessageSquare className="h-3 w-3" /> Input
                  </button>
                  <button
                    disabled={!!busy}
                    onClick={() => doAction("sync_manual_input")}
                    className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
                  >
                    <RefreshCw className="h-3 w-3" /> Sync
                  </button>
                  <button
                    disabled={!!busy}
                    onClick={() => doAction("mark_submitted")}
                    className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 text-[11px] font-medium hover:bg-muted disabled:opacity-50"
                  >
                    <CheckCircle2 className="h-3 w-3" /> Submitted
                  </button>
                  <button
                    disabled={!!busy}
                    onClick={() => doAction("reject")}
                    className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-[oklch(0.45_0.18_27)] text-[oklch(0.45_0.18_27)] px-2 text-[11px] font-medium hover:bg-[oklch(0.45_0.18_27)]/10 disabled:opacity-50"
                  >
                    <ThumbsDown className="h-3 w-3" /> Reject
                  </button>
                </div>
              </div>

              <div className="mt-4">
                <SegmentedTabs
                  options={REVIEW_TABS.map((t) => ({ value: t.key, label: t.label }))}
                  value={tab}
                  onChange={setTab}
                />
              </div>

              <div className="mt-4">
                {tab === "summary" && (
                  <div className="space-y-3">
                    {detail.evaluation?.score != null && (
                      <div className="flex items-center gap-2">
                        <span className="text-[11.5px] text-muted-foreground">Score</span>
                        <span className="text-[16px] font-bold tabular-nums text-foreground">{detail.evaluation.score}</span>
                        {detail.application?.grade && <StatusBadge tone={toneFor(detail.application.grade)}>{detail.application.grade}</StatusBadge>}
                      </div>
                    )}
                    {detail.blockers && detail.blockers.length > 0 && (
                      <div className="rounded-lg border border-border bg-surface p-3">
                        <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">Remaining blockers</div>
                        <ul className="space-y-1 text-[12px] text-muted-foreground">
                          {detail.blockers.map((blocker: any, index: number) => (
                            <li key={index}>{String(blocker.label ?? blocker.message ?? blocker.category ?? blocker)}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {detail.evaluation?.summary && (
                      <div className="rounded-lg border border-border bg-surface p-3 text-[12.5px] text-foreground whitespace-pre-wrap">
                        {detail.evaluation.summary}
                      </div>
                    )}
                    {detail.summary?.next_action_reason && (
                      <div className="rounded-lg border border-border bg-surface p-3 text-[12.5px] text-foreground whitespace-pre-wrap">
                        <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">Next action</div>
                        {detail.summary.next_action_reason}
                      </div>
                    )}
                    {detail.job?.description && (
                      <div>
                        <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">Description</div>
                        <div className="rounded-lg border border-border bg-surface p-3 text-[12.5px] text-foreground whitespace-pre-wrap max-h-64 overflow-y-auto">
                          {detail.job.description}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {tab === "questions" && (
                  <div className="space-y-3">
                    {detail.questions && detail.questions.length > 0 ? (
                      detail.questions.map((rawQuestion: any, index: number) => {
                        const question: QuestionQueueItem = {
                          application_id: selectedId ?? "",
                          question_id: String(rawQuestion.question_id ?? index),
                          prompt_text: rawQuestion.prompt_text ?? rawQuestion.question,
                          normalized_key: rawQuestion.normalized_key,
                          question_type: rawQuestion.question_type,
                          widget_type: rawQuestion.widget_type,
                          required: rawQuestion.required,
                          option_signature: Array.isArray(rawQuestion.options) ? rawQuestion.options : [],
                          option_details: Array.isArray(rawQuestion.option_details) ? rawQuestion.option_details : [],
                          existing_answer: rawQuestion.existing_answer,
                        };
                        const draftKey = questionDraftKey(question);
                        const optionList = questionOptions(question);
                        const rawDraft = questionDrafts[draftKey] ?? hydrateAnswerDraft(question, rawQuestion.existing_answer ?? "");
                        const scalarDraft = Array.isArray(rawDraft) ? rawDraft.join(", ") : String(rawDraft ?? "");
                        const useSelect = optionList.length > 0 && question.widget_type !== "checkbox_group";
                        return (
                          <div key={draftKey} className="rounded-lg border border-border bg-surface p-3">
                            <div className="text-[12.5px] font-medium text-foreground">{question.prompt_text}</div>
                            <div className="mt-1 text-[11px] text-muted-foreground">
                              {question.required ? "Required" : "Optional"}
                              {question.question_type ? ` � ${question.question_type}` : ""}
                            </div>
                            {useSelect ? (
                              <select
                                value={scalarDraft}
                                onChange={(event) => setQuestionDrafts((current) => ({ ...current, [draftKey]: event.target.value }))}
                                className="mt-2 w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12px]"
                              >
                                <option value="">Select�</option>
                                {optionList.map((option) => (
                                  <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                              </select>
                            ) : (
                              <textarea
                                value={scalarDraft}
                                onChange={(event) => setQuestionDrafts((current) => ({ ...current, [draftKey]: event.target.value }))}
                                rows={question.widget_type === "checkbox_group" ? 3 : 2}
                                className="mt-2 w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12px]"
                                placeholder={question.widget_type === "checkbox_group" ? "Enter one choice per line or separate answers with commas." : "Your answer�"}
                              />
                            )}
                            {rawQuestion.existing_answer ? (
                              <div className="mt-2 text-[11px] text-muted-foreground">
                                Current answer: {String(rawQuestion.existing_answer)}
                              </div>
                            ) : null}
                            <button
                              disabled={!scalarDraft.trim() || busy === `answer-${draftKey}`}
                              onClick={() => void answerQuestion(question)}
                              className="mt-2 inline-flex h-7 items-center justify-center gap-1 rounded-lg bg-primary px-2.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                            >
                              <CheckCircle2 className="h-3 w-3" /> Save answer
                            </button>
                          </div>
                        );
                      })
                    ) : (
                      <p className="text-[12px] text-muted-foreground">No questions.</p>
                    )}
                  </div>
                )}

                {tab === "handoff" && (
                  <div className="space-y-3">
                    {detail.manual_handoff_watch?.active ? (
                      <div className="rounded-lg border border-border bg-surface p-3 text-[12.5px] text-foreground whitespace-pre-wrap">
                        <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">Manual handoff active</div>
                        {detail.manual_handoff_watch.last_synced_at && (
                          <div className="text-[11px] text-muted-foreground mb-2">Last synced: {formatDate(detail.manual_handoff_watch.last_synced_at)}</div>
                        )}
                        {(Number(detail.manual_handoff_watch.learned_global_count || 0) > 0
                          || Number(detail.manual_handoff_watch.job_scoped_count || 0) > 0) && (
                          <div className="mb-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                            <span>
                              Learned globally: {Number(detail.manual_handoff_watch.learned_global_count || 0)}
                            </span>
                            <span>
                              Kept submission-scoped: {Number(detail.manual_handoff_watch.job_scoped_count || 0)}
                            </span>
                          </div>
                        )}
                        {detail.manual_handoff_watch.recent_answers && detail.manual_handoff_watch.recent_answers.length > 0 && (
                          <ul className="space-y-1">
                            {detail.manual_handoff_watch.recent_answers.map((a: any, i: number) => (
                              <li key={i} className="text-[12px] text-muted-foreground">
                                {typeof a === "string"
                                  ? a
                                  : `${a.prompt_text ?? a.field ?? "answer"}: ${a.answer_text ?? ""}${a.reuse_scope ? ` (${a.reuse_scope})` : ""}`}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    ) : (
                      <p className="text-[12px] text-muted-foreground">No handoff notes.</p>
                    )}
                  </div>
                )}

                {tab === "artifacts" && (
                  <div className="space-y-3">
                    {detail.artifacts && detail.artifacts.length > 0 ? (
                      detail.artifacts.map((art, i) => (
                        <a
                          key={i}
                          href={art.href ?? (art.relative_path ? `/files/${art.relative_path}` : "#")}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="flex items-center gap-2 text-[12.5px] text-primary hover:underline"
                        >
                          <FileText className="h-4 w-4" /> {art.label ?? art.kind}
                        </a>
                      ))
                    ) : (
                      <p className="text-[12px] text-muted-foreground">No artifacts generated yet.</p>
                    )}
                  </div>
                )}

                {tab === "history" && (
                  <div className="space-y-2">
                    {detail.history && detail.history.length > 0 ? (
                      detail.history.map((h: any, i: number) => (
                        <div key={i} className="flex items-start gap-2 text-[12px]">
                          <History className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
                          <div>
                            <span className="font-medium text-foreground">{h.type ?? "event"}</span>
                            {h.actor && <span className="ml-1 text-muted-foreground">({h.actor})</span>}
                            {h.timestamp && <span className="ml-2 text-muted-foreground">{formatDate(h.timestamp)}</span>}
                            {h.summary && <div className="mt-0.5 text-muted-foreground">{h.summary}</div>}
                          </div>
                        </div>
                      ))
                    ) : (
                      <p className="text-[12px] text-muted-foreground">No history events.</p>
                    )}
                  </div>
                )}
              </div>
            </Panel>
          ) : (
            <Panel>
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Filter className="mb-3 h-8 w-8 text-muted-foreground" />
                <div className="text-[14px] font-semibold text-foreground">Select an application</div>
                <p className="mt-1 text-[12.5px] text-muted-foreground">
                  Pick an item from the queue to review its details.
                </p>
                <p className="mt-3 text-[11px] text-muted-foreground">
                  Keyboard: <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">j</kbd>/<kbd className="rounded border border-border px-1 py-0.5 text-[10px]">k</kbd> navigate,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">a</kbd> approve,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">o</kbd> request input,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">s</kbd> sync,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">m</kbd> mark submitted,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">r</kbd> reject,{" "}
                  <kbd className="rounded border border-border px-1 py-0.5 text-[10px]">/</kbd> search
                </p>
              </div>
            </Panel>
          )}
        </div>
      </div>
    </AppShell>
  );
}
