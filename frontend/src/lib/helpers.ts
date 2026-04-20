/* ── Formatting & utility helpers ──────────────────────────────────── */

import {
  STAGE_LABELS,
  STAGE_ORDER,
  TERMINAL_STATUSES,
  NAV_ROUTE_ALIASES,
  REVIEW_ACTION_LABELS,
  REVIEW_SECTION_FROM_TAB,
  REVIEW_TAB_FROM_SECTION,
  REVIEW_SEVERITY_WEIGHT,
  MODEL_TRANSPORT_OPTIONS,
  MODEL_PROVIDER_OPTIONS,
} from "./constants";
import type {
  OperatorSnapshot,
  ConnectionState,
  OperatorDerived,
  LiveEvent,
  DraftBatch,
  JobTableRow,
  ReviewQueueItem,
  QuestionQueueItem,
  ModelProfile,
} from "./types";

/* ── Primitives ────────────────────────────────────────────────────── */

export function safeNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function formatNumber(value: unknown): string {
  return new Intl.NumberFormat("en-US").format(safeNumber(value, 0));
}

export function parseTimestamp(value: unknown): number | null {
  if (!value) return null;
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? null : parsed;
}

export function formatDate(value: unknown): string {
  const parsed = parseTimestamp(value);
  if (parsed === null) return "-";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

export function formatRelativeAge(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "-";
  if (ms < 1000) return "just now";
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`;
  return `${Math.round(ms / 86_400_000)}d ago`;
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "-";
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

export function compactList(values: unknown[], limit = 3): string {
  const items = Array.isArray(values) ? values.filter(Boolean).map(String) : [];
  if (!items.length) return "";
  if (items.length <= limit) return items.join(" / ");
  return `${items.slice(0, limit).join(" / ")} +${items.length - limit}`;
}

export function blockerLabel(blocker: unknown): string {
  if (!blocker) return "";
  if (typeof blocker === "string") return blocker;
  if (typeof blocker === "object") {
    const obj = blocker as Record<string, unknown>;
    return String(obj.label ?? obj.category ?? JSON.stringify(blocker));
  }
  return String(blocker);
}

export function toneFor(value: unknown): "danger" | "warning" | "success" | "neutral" {
  const text = String(value ?? "").toLowerCase();
  if (!text) return "neutral";
  if (/fail|rejected|blocked|error|stale/.test(text)) return "danger";
  if (/warning|preview|needs|reconnecting|queued|running|awaiting|downloading|validating/.test(text)) return "warning";
  if (/ready|submitted|completed|applied|connected|success/.test(text)) return "success";
  return "neutral";
}

export function toneForStream(value: unknown): "danger" | "warning" | "success" | "neutral" {
  const text = String(value ?? "").toLowerCase();
  if (text === "connected") return "success";
  if (text === "reconnecting" || text === "connecting") return "warning";
  if (text === "stale") return "danger";
  return "neutral";
}

export function badgeText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "idle";
  return String(value);
}

export function navPathForLocation(pathname: string): string {
  return NAV_ROUTE_ALIASES[pathname] ?? pathname;
}

export function normalizeChoice(value: unknown): string {
  return String(value ?? "").trim().toLowerCase().replace(/\s+/g, " ");
}

export function dedupeStrings(values: unknown[]): string[] {
  const seen: string[] = [];
  values.forEach((value) => {
    const cleaned = String(value ?? "").trim();
    if (cleaned && !seen.includes(cleaned)) seen.push(cleaned);
  });
  return seen;
}

export function toMultiline(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : "";
}

/* ── Mapping / aggregation ─────────────────────────────────────────── */

export function summarizeEventPayload(payload: Record<string, any> | undefined | null): { label: string; value: any }[] {
  if (!payload || typeof payload !== "object") return [];
  const chips: { label: string; value: any }[] = [];
  const push = (label: string, value: unknown) => {
    if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return;
    chips.push({ label, value: Array.isArray(value) ? value.length : value });
  };
  push("New Jobs", payload.new_jobs);
  push("Scanned", payload.discovered);
  push("Approved", payload.classifier_approved ?? payload.approved_count ?? payload.eligible_count ?? payload.evaluated);
  push("Rejected", payload.classifier_rejected ?? payload.rejected_count ?? payload.screened_out);
  push("Drafted", payload.pdfs);
  push("Submitted", payload.submitted_application_ids);
  push("Failed", payload.failed_application_ids);
  push("Artifacts", payload.artifact_paths);
  if (payload.review_result?.scores) {
    const scores = payload.review_result.scores;
    push("Review", `${scores.resume ?? 0}/${scores.cover_letter ?? 0}/${scores.form ?? 0}`);
  }
  return chips.slice(0, 5);
}

export function mappingEntries(value: unknown): [string, number][] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => [key, safeNumber(item)] as [string, number])
    .filter(([, item]) => item > 0)
    .sort((left, right) => right[1] - left[1]);
}

/* ── Domain helpers ────────────────────────────────────────────────── */

export function describeDraftBatch(batch: DraftBatch | undefined | null, configuredTarget?: number): string {
  const memberCount = safeNumber(batch?.member_count);
  if (!memberCount) return "no active draft batch";
  const targetSize = safeNumber(batch?.target_size ?? memberCount);
  const configured = safeNumber(configuredTarget ?? targetSize);
  const baseStatus = batch?.handoff_status ?? batch?.status ?? "waiting_for_batch";
  if (configured > targetSize)
    return `only ${formatNumber(targetSize)} approved job${targetSize === 1 ? "" : "s"} were available; configured target ${formatNumber(configured)}. ${baseStatus}`;
  return `target ${formatNumber(targetSize)} of configured ${formatNumber(configured)}. ${baseStatus}`;
}

function formatTemporaryChatState(enabled: boolean, lastResult: unknown): string {
  if (enabled) return "enabled";
  switch (String(lastResult ?? "").trim()) {
    case "already_enabled": return "enabled";
    case "toggle_unavailable": return "toggle unavailable";
    case "click_failed": return "enable failed";
    case "enabled": return "enabled";
    default: return "-";
  }
}

export function connectionLoading(operator: OperatorDerived): boolean {
  return !operator.events.length && !operator.isTerminal && operator.status !== "idle";
}

/* ── Portal / model helpers ────────────────────────────────────────── */

export function blankPortalSource(sourceId = "greenhouse") {
  return { enabled: sourceId === "greenhouse", boards: [] as string[], seed_urls: [] as string[], seed_domains: [] as string[] };
}

export function blankTrackedCompany(source = "greenhouse") {
  return { name: "", careers_url: "", source, board: "", api: "", enabled: true, notes: "" };
}

export function transportLabel(value: string): string {
  return MODEL_TRANSPORT_OPTIONS.find((o) => o.value === value)?.label ?? (value || "-");
}

export function providerLabel(value: string): string {
  return MODEL_PROVIDER_OPTIONS.find((o) => o.value === value)?.label ?? (value || "-");
}

export function applyProviderDefaults(model: Partial<ModelProfile>): ModelProfile {
  return {
    name: model.name ?? "",
    base_url: model.base_url || "http://127.0.0.1:1234",
    api_key_env: "",
    local: true,
    command: [],
    working_dir: "",
    ...model,
    provider: "lmstudio",
    transport: "local_http",
  } as ModelProfile;
}

/* ── Review helpers ────────────────────────────────────────────────── */

export function reviewActionLabel(action: string): string {
  return REVIEW_ACTION_LABELS[action] ?? String(action || "Review").replace(/_/g, " ");
}

export function reviewActionTone(action: string): "success" | "danger" | "warning" | "neutral" {
  if (action === "approve" || action === "mark_submitted") return "success";
  if (action === "reject") return "danger";
  if (action === "sync_manual_input" || action === "request_input" || action === "save_answers") return "warning";
  return "neutral";
}

export function reviewSectionFromTab(tab: string): string {
  return REVIEW_SECTION_FROM_TAB[String(tab || "").toLowerCase()] ?? "needs_attention";
}

export function reviewTabForSection(section: string): string {
  return REVIEW_TAB_FROM_SECTION[section] ?? "summary";
}

export function reviewSeverityWeight(summary: { severity?: string } | undefined): number {
  return REVIEW_SEVERITY_WEIGHT[String(summary?.severity ?? "neutral")] ?? 0;
}

function reviewBlockerCount(item: ReviewQueueItem): number {
  return safeNumber(item.review_summary?.blocker_count ?? item.remaining_blockers?.length);
}

function reviewUnresolvedQuestionCount(item: ReviewQueueItem): number {
  return safeNumber(item.review_summary?.unresolved_question_count);
}

export function reviewSortComparator(sortKey: string) {
  return (a: ReviewQueueItem, b: ReviewQueueItem): number => {
    switch (sortKey) {
      case "company":
        return (a.company ?? "").localeCompare(b.company ?? "");
      case "status":
        return String(a.review_status ?? a.status ?? "").localeCompare(String(b.review_status ?? b.status ?? ""));
      case "blockers":
        return reviewBlockerCount(b) - reviewBlockerCount(a);
      default: {
        // severity → blockers → company
        const sw = reviewSeverityWeight(b.review_summary) - reviewSeverityWeight(a.review_summary);
        if (sw !== 0) return sw;
        const bw = reviewBlockerCount(b) - reviewBlockerCount(a);
        if (bw !== 0) return bw;
        return (a.company ?? "").localeCompare(b.company ?? "");
      }
    }
  };
}

export function queueMatchesSearch(item: ReviewQueueItem, search: string): boolean {
  if (!search) return true;
  const lower = search.toLowerCase();
  const haystack = [
    item.company, item.title, item.role, item.source, item.status,
    item.ats_family, ...(item.blockers?.map(blockerLabel) ?? []),
    ...(item.warnings?.map(String) ?? []),
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(lower);
}

export function isEditableShortcutTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea" || tag === "button") return true;
  return target.isContentEditable;
}

export function reviewQueueMatchesFilter(item: ReviewQueueItem, filterKey: string): boolean {
  const blockerCount = reviewBlockerCount(item);
  const unresolvedQuestionCount = reviewUnresolvedQuestionCount(item);
  const manualHandoffActive = Boolean(item.manual_handoff?.active);
  const reviewStatus = String(item.review_status ?? "").toLowerCase();
  const applicationStatus = String(item.status ?? "").toLowerCase();
  const readyForSubmit = Boolean(item.review_summary?.ready_for_submit)
    || reviewStatus === "preview_ready"
    || reviewStatus === "ready_to_submit"
    || applicationStatus === "ready to submit";

  switch (filterKey) {
    case "needs_input":
      return reviewStatus === "needs_user_input"
        || applicationStatus === "needs input"
        || blockerCount > 0
        || unresolvedQuestionCount > 0;
    case "manual_handoff":
      return manualHandoffActive;
    case "ready":
      return readyForSubmit && blockerCount === 0 && unresolvedQuestionCount === 0 && !manualHandoffActive;
    default:
      return true;
  }
}

export function reviewQueueAttentionSummary(item: ReviewQueueItem): string {
  const parts: string[] = [];
  const blockerCount = reviewBlockerCount(item);
  const unresolvedQuestionCount = reviewUnresolvedQuestionCount(item);
  const warningCount = safeNumber(item.review_summary?.warning_count);
  if (blockerCount) parts.push(`${blockerCount} blocker${blockerCount === 1 ? "" : "s"}`);
  if (unresolvedQuestionCount) parts.push(`${unresolvedQuestionCount} answer${unresolvedQuestionCount === 1 ? "" : "s"} needed`);
  if (item.manual_handoff?.active) parts.push("Manual handoff is open");
  if (warningCount) parts.push(`${warningCount} warning${warningCount === 1 ? "" : "s"}`);
  return parts.length ? parts.join(", ") : "Ready";
}

export function reviewQueueSupportingMeta(item: ReviewQueueItem): string[] {
  return dedupeStrings([
    item.classification?.ats_family,
    item.classification?.board_family,
    item.classification?.automation_tier,
    item.source,
  ].filter(Boolean));
}

export function reviewSourceMeta(detail: { source?: string; ats_family?: string }): string[] {
  return dedupeStrings([detail.source, detail.ats_family].filter(Boolean));
}

export function autopilotPrimaryAction(job: JobTableRow): {
  kind: "approve" | "review" | "none";
  label: string;
  section?: string;
} {
  if (!job.application_id) {
    return { kind: "none", label: "" };
  }

  const nextAction = String(job.review_summary?.next_action ?? "").trim().toLowerCase();
  const blockerCount = safeNumber(job.review_summary?.blocker_count);
  const unresolvedQuestionCount = safeNumber(job.review_summary?.unresolved_question_count);
  const manualHandoffActive = Boolean(job.manual_handoff?.active);
  const readyForSubmit = Boolean(job.review_summary?.ready_for_submit ?? job.submit_ready);

  if (
    nextAction === "approve"
    || (!nextAction && readyForSubmit && blockerCount === 0 && unresolvedQuestionCount === 0 && !manualHandoffActive)
  ) {
    return { kind: "approve", label: REVIEW_ACTION_LABELS.approve };
  }

  if (nextAction === "sync_manual_input") {
    return { kind: "review", label: REVIEW_ACTION_LABELS.sync_manual_input, section: "handoff" };
  }

  if (nextAction === "open_manual_input" || nextAction === "request_input" || nextAction === "save_answers") {
    return { kind: "review", label: "Answer In Review", section: "questions" };
  }

  if (nextAction === "review_summary") {
    return { kind: "review", label: REVIEW_ACTION_LABELS.review_summary, section: "summary" };
  }

  return {
    kind: "review",
    label: "Open In Review",
    section: manualHandoffActive ? "handoff" : "summary",
  };
}

export function reviewArtifactForKinds(
  artifacts: { kind: string; [key: string]: any }[] | undefined,
  ...kinds: string[]
) {
  return (artifacts ?? []).find((a) => kinds.includes(a.kind)) ?? null;
}

/* ── Question handling ─────────────────────────────────────────────── */

export function questionOptions(question: QuestionQueueItem) {
  const optionDetails =
    Array.isArray(question?.option_details) && question.option_details.length
      ? question.option_details
      : Array.isArray(question?.option_signature)
        ? question.option_signature.map((option) => ({ label: option, value: option }))
        : [];
  return optionDetails
    .map((option) => {
      const o = option as Record<string, any>;
      const label = String(o?.label ?? o?.value ?? o?.id ?? "").trim();
      const value = String(o?.value ?? o?.id ?? o?.label ?? "").trim();
      if (!label && !value) return null;
      return { label: label || value, value: value || label };
    })
    .filter(Boolean) as { label: string; value: string }[];
}

export function matchQuestionOption(options: { label: string; value: string }[], rawValue: string) {
  const normalized = normalizeChoice(rawValue);
  if (!normalized) return null;
  return options.find(
    (option) => normalized === normalizeChoice(option.label) || normalized === normalizeChoice(option.value),
  ) ?? null;
}

export function parseMultiAnswer(rawValue: unknown): string[] {
  if (Array.isArray(rawValue)) return dedupeStrings(rawValue);
  return dedupeStrings(String(rawValue ?? "").split(/[\n,;|]/));
}

export function hydrateAnswerDraft(question: QuestionQueueItem, rawAnswer: unknown): string | string[] {
  const options = questionOptions(question);
  if (question?.widget_type === "checkbox_group") {
    return parseMultiAnswer(rawAnswer).map(
      (value) => matchQuestionOption(options, value)?.label ?? value,
    );
  }
  if (!options.length) return String(rawAnswer ?? "");
  return matchQuestionOption(options, String(rawAnswer ?? ""))?.label ?? String(rawAnswer ?? "");
}

export function serializeAnswerDraft(question: QuestionQueueItem, draftValue: unknown): string {
  if (question?.widget_type === "checkbox_group") {
    return parseMultiAnswer(draftValue).join(", ");
  }
  return String(draftValue ?? "").trim();
}

export function questionDraftKey(question: Pick<QuestionQueueItem, "application_id" | "question_id">): string {
  return `${question.application_id}::${question.question_id}`;
}

export function buildQuestionAnswerRequest(
  question: QuestionQueueItem,
  draftValue: unknown,
  options?: { approveMemory?: boolean; autoRetry?: boolean },
) {
  return {
    application_id: question.application_id,
    question_id: question.question_id,
    answer_text: serializeAnswerDraft(question, draftValue),
    approve_memory: options?.approveMemory ?? true,
    auto_retry: options?.autoRetry ?? true,
  };
}

/* ── Model catalog URL builder ─────────────────────────────────────── */

export function modelCatalogUrl(
  profile: Pick<ModelProfile, "name" | "provider" | "transport" | "base_url" | "api_key_env"> | undefined,
  forceRefresh = false,
): string {
  return appendQueryHelper("/api/settings/models/available", {
    refresh: forceRefresh ? "true" : undefined,
    profile_name: profile?.name,
    provider: profile?.provider,
    transport: profile?.transport,
    base_url: profile?.base_url,
    api_key_env: profile?.api_key_env,
  });
}

function appendQueryHelper(url: string, params: Record<string, string | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${url}?${suffix}` : url;
}

/* ── deriveOperatorState ───────────────────────────────────────────── */

export function deriveOperatorState(
  snapshot: OperatorSnapshot | null | undefined,
  connection: ConnectionState,
  lastSnapshotAt: number,
): OperatorDerived {
  const state = snapshot?.state ?? {};
  const drafting = snapshot?.drafting && typeof snapshot.drafting === "object" ? snapshot.drafting : {} as NonNullable<OperatorSnapshot["drafting"]>;
  const draftBatch = drafting?.batch && typeof drafting.batch === "object" ? drafting.batch : {};
  const events: LiveEvent[] = Array.isArray(snapshot?.events) ? snapshot!.events : [];
  const latestEvent = events.length ? events[events.length - 1] : null;
  const startedAt = state.run_started_at ?? state.started_at ?? latestEvent?.created_at ?? null;
  const lastEventAt = state.last_event_at ?? latestEvent?.created_at ?? state.updated_at ?? null;
  const startedMs = parseTimestamp(startedAt);
  const lastEventMs = parseTimestamp(lastEventAt);
  const stats: Record<string, any> = state.stats && typeof state.stats === "object" ? state.stats : {};
  const modelActivity: Record<string, any> = state.model_activity && typeof state.model_activity === "object" ? state.model_activity : {};
  const runType = String(state.run_type ?? "idle");
  const status = String(state.status ?? "idle");
  const stage = String(state.stage ?? "idle");
  const isRunning = ["running", "queued", "starting"].includes(status);
  const lastActivityMs = Number.isFinite(lastEventMs) ? Math.max(lastEventMs!, lastSnapshotAt) : lastSnapshotAt;
  const stale = isRunning && Number.isFinite(lastActivityMs) ? Date.now() - lastActivityMs > 15_000 : false;
  const streamHealth =
    connection === "reconnecting" ? "reconnecting"
      : stale ? "stale"
        : String(state.stream_health ?? connection ?? "idle");
  const currentCompany = state.current_company ?? state.company ?? latestEvent?.company ?? "";
  const currentRole = state.current_role ?? state.role ?? latestEvent?.role ?? "";
  const currentTitle =
    state.current_title ?? (compactList([currentCompany, currentRole], 2) || "No active target");
  const modelBadge = compactList(
    [modelActivity.role ?? stats.model_activity?.role, modelActivity.profile ?? stats.model_activity?.profile],
    2,
  ) || "-";
  const readyThreshold = safeNumber(
    snapshot?.ready_to_apply_threshold ?? snapshot?.data?.ready_to_apply_threshold ?? stats.ready_to_apply_threshold,
  );
  const temporaryChatEnabled = Boolean(drafting?.temporary_chat_enabled);
  const temporaryChatStatus = formatTemporaryChatState(temporaryChatEnabled, drafting?.temporary_chat_last_result);

  const counters = {
    discovered: safeNumber(stats.discovered ?? stats.discovery_scanned ?? stats.discover ?? stats.scanned),
    screenedOut: safeNumber(stats.screened_out ?? stats.classifier_rejected ?? stats.screened_rejected),
    evaluated: safeNumber(stats.evaluated ?? stats.applications_created),
    drafted: safeNumber(stats.drafted),
    readyToApply: safeNumber(stats.ready_to_apply ?? stats.ready_for_submit),
    submitted: safeNumber(stats.submitted ?? state.submitted_count),
    blockedByQuestions: safeNumber(stats.blocked_by_questions ?? state.blocked_applications),
    failed: safeNumber(stats.failed ?? state.failed_count),
    discoveryBoardsCompleted: safeNumber(stats.discovery_boards_completed),
    discoveryBoardsTotal: safeNumber(stats.discovery_boards_total),
    discoverySeedPages: safeNumber(stats.discovery_seed_pages),
    deterministicRejects: safeNumber(stats.deterministic_rejects ?? stats.rejected ?? stats.rejected_count),
  };
  const queue = {
    depth: safeNumber(state.queue_depth),
    blocked: safeNumber(state.blocked_applications),
    pendingQuestions: safeNumber(state.pending_questions),
    submitted: safeNumber(state.submitted_count),
    failed: safeNumber(state.failed_count),
    rejected: safeNumber(state.rejected_count),
  };
  const latestErrorRaw = String(state.latest_error ?? "");
  const hideRecoveredWorkerError =
    status === "completed" &&
    latestErrorRaw.toLowerCase() === "stale live state recovered without an active worker.";
  const latestMessage =
    state.latest_operator_message ?? latestEvent?.message ?? state.latest_error ?? "No active run.";

  const stageTrail = (STAGE_ORDER as readonly string[]).map((item) => ({
    key: item,
    label: STAGE_LABELS[item] ?? item,
    active: stage === item,
    done: STAGE_ORDER.indexOf(item as typeof STAGE_ORDER[number]) < STAGE_ORDER.indexOf(stage as typeof STAGE_ORDER[number]),
  }));

  return {
    state,
    stats,
    runType,
    status,
    stage,
    isRunning,
    isTerminal: TERMINAL_STATUSES.has(status),
    currentTitle,
    currentSource: state.source ?? latestEvent?.source ?? "",
    latestMessage,
    latestError: hideRecoveredWorkerError ? "" : latestErrorRaw,
    warningNotice:
      status === "interrupted"
        ? "Previous run was interrupted. Reset operational state before starting a clean end-to-end test."
        : streamHealth === "stale"
          ? "Live discovery or submission events stopped updating. Stop the stale backend and reset if needed."
          : "",
    elapsed: Number.isFinite(startedMs) ? formatDuration(Date.now() - startedMs!) : "0s",
    lastSeen: Number.isFinite(lastActivityMs)
      ? formatRelativeAge(Date.now() - lastActivityMs)
      : formatRelativeAge(Date.now() - lastSnapshotAt),
    modelBadge,
    modelRole: modelActivity.role ?? stats.model_activity?.role ?? "",
    modelProfile: modelActivity.profile ?? stats.model_activity?.profile ?? "",
    streamHealth,
    readyThreshold,
    temporaryChatStatus,
    temporaryChatCheckedAt: drafting?.temporary_chat_checked_at ?? "",
    counters,
    queue,
    events,
    eventsDescending: [...events].reverse(),
    latestEvent,
    latestEventMeta: summarizeEventPayload(latestEvent?.payload),
    stageTrail,
    sourceMix: mappingEntries(stats.source_mix),
    sourceMetrics: Object.entries(stats.source_metrics ?? {}),
    sourceWarnings: Array.isArray(stats.source_warnings) ? stats.source_warnings : [],
    zeroResultSources: Array.isArray(stats.zero_result_sources) ? stats.zero_result_sources : [],
    discoveryErrors: mappingEntries(stats.discovery_error_counts),
    screenedOutReasons: mappingEntries(stats.screened_out_reasons),
    draftBatch,
  };
}
