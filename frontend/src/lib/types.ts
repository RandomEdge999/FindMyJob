/* ── Types ─────────────────────────────────────────────────────────── */

export interface OperatorSnapshot {
  state: Record<string, any> | null;
  events: LiveEvent[];
  drafting?: {
    batch?: DraftBatch;
    temporary_chat_enabled?: boolean;
    temporary_chat_last_result?: string;
    temporary_chat_checked_at?: string;
  };
  ready_to_apply_threshold?: number;
  data?: { ready_to_apply_threshold?: number };
}

export interface LiveEvent {
  id?: string;
  created_at?: string;
  message?: string;
  company?: string;
  role?: string;
  source?: string;
  payload?: Record<string, any>;
}

export interface DraftBatch {
  member_count?: number;
  target_size?: number;
  handoff_status?: string;
  status?: string;
}

export interface DashboardData {
  stats?: Record<string, any>;
  readiness?: Record<string, any>;
  [key: string]: any;
}

export interface RunHistoryResponse {
  count: number;
  items: RunHistoryRow[];
  activity: RunHistoryRow[];
}

export interface RunHistoryRow {
  run_id: string;
  run_type?: string;
  status?: string;
  event_status?: string;
  started_at?: string;
  completed_at?: string;
  processed_count?: number;
  evaluated_count?: number;
  submitted_count?: number;
  failed_count?: number;
  submitted_application_ids?: string[];
  failed_application_ids?: string[];
  notes?: string[];
  note_count?: number;
  metrics?: Record<string, any>;
}

export interface SettingsData {
  profile?: Record<string, any>;
  portals?: Record<string, any>;
  tracked_companies?: any[];
  autonomous?: Record<string, any>;
  captcha?: Record<string, any>;
  runtime_model?: Record<string, any>;
  local_model?: Record<string, any>;
  chatgpt_drafting?: Record<string, any>;
  model_strategy?: {
    mode?: string;
    provider?: string;
    transport?: string;
    base_url?: string;
    model?: string;
    api_key_env?: string;
    launch_transport_mix?: any;
    role_bindings?: Record<string, any>;
  };
  drafting_strategy?: Record<string, any>;
  submit_mode?: string;
  dossier?: Record<string, any>;
  advanced_models?: AdvancedModelsData;
  last_model_checks?: Record<string, any>;
  live_feed?: { enabled?: boolean; status_path?: string; events_path?: string };
  readiness?: { config_validation?: any; doctor?: any; launch_check?: any; findings?: any[] };
  [key: string]: any;
}

export interface ReleasePostureEntry {
  id: string;
  label: string;
  status?: string;
  detail?: string;
}

export interface ReleaseSensitivePathEntry extends ReleasePostureEntry {
  warnings?: string[];
  credential_source?: string;
}

export interface ReleasePosture {
  phase?: string;
  summary?: string;
  disclaimer?: string;
  operator_responsibilities?: string[];
  platform_matrix?: ReleasePostureEntry[];
  feature_matrix?: ReleasePostureEntry[];
  sensitive_paths?: ReleaseSensitivePathEntry[];
  gates?: ReleasePostureEntry[];
}

export interface ModelCatalogEntry {
  id: string;
  name?: string;
  label?: string;
  context_length?: number;
}

export interface ModelCatalogData {
  models?: ModelCatalogEntry[];
  count?: number;
  live?: boolean;
  source?: string;
  transport?: string;
  base_url?: string;
  error?: string;
  key_scoped?: boolean;
  api_key_configured?: boolean;
}

export interface LaunchRoleStatus {
  role: string;
  profile_name?: string;
  transport?: string;
  provider?: string;
  model?: string;
  fallback_chain?: string[];
  fallback_ready?: string[];
  status?: string;
  issues?: string[];
}

export interface LaunchProfile {
  required_roles?: string[];
  optional_roles?: string[];
  roles?: LaunchRoleStatus[];
  missing_required_roles?: string[];
  transport_mix?: string;
  risks?: string[];
  summary?: string;
  overall_status?: string;
}

export interface AdvancedModelsData {
  config_path?: string;
  exists?: boolean;
  profiles?: ModelProfile[];
  launch_profile?: LaunchProfile | null;
  recommended_split_defaults?: Record<string, any>;
  missing_required_roles?: string[];
  duplicate_roles?: Record<string, string[]>;
  role_bindings?: Record<string, any>;
  error?: string;
  [key: string]: any;
}

export interface BasicProfileCandidate {
  name?: string;
  email?: string | null;
  phone?: string | null;
  location?: string | null;
  linkedin?: string | null;
  github?: string | null;
  website?: string | null;
  summary?: string | null;
  target_roles?: string[];
}

export interface BasicProfileTargets {
  title_keywords?: string[];
  locations?: string[];
  countries?: string[];
  regions?: string[];
  cities?: string[];
  remote_only?: boolean;
}

export interface BasicProfileAuthorization {
  is_authorized?: boolean | null;
  requires_future_sponsorship?: boolean | null;
}

export interface BasicProfileData {
  profile_surface?: {
    mode?: string;
    configured?: boolean;
    has_user_profile?: boolean;
    local_path?: string;
    local_template_path?: string;
    public_template_path?: string;
    active_advanced_paths?: string[];
    [key: string]: any;
  };
  values?: {
    candidate?: BasicProfileCandidate;
    targets?: BasicProfileTargets;
    authorization?: BasicProfileAuthorization;
  };
  effective?: {
    candidate?: BasicProfileCandidate;
    targets?: BasicProfileTargets;
  };
  guidance?: {
    writes_to?: string;
    template_path?: string;
    mode?: string;
    can_save?: boolean;
    active_advanced_paths?: string[];
    included_fields?: string[];
    excluded_fields?: string[];
    [key: string]: any;
  };
}

export interface ModelProfile {
  name: string;
  provider: string;
  transport: string;
  base_url?: string;
  api_key_env?: string;
  role?: string;
  model?: string;
  model_id?: string;
  local?: boolean;
  command?: string[];
  working_dir?: string;
  roles?: string[];
  status?: string;
  issues?: string[];
  policy_tags?: string[];
}

export interface ReadinessData {
  workspace?: Record<string, any>;
  profile_surface?: Record<string, any>;
  model?: Record<string, any>;
  automation?: Record<string, any>;
  sources?: Record<string, boolean>;
  config_validation?: Record<string, any>;
  doctor?: Record<string, any>;
  launch_check?: Record<string, any>;
  overall_status?: string;
  findings?: ReadinessFinding[];
  [key: string]: any;
}

export interface ReadinessFinding {
  key?: string;
  status?: string;
  summary?: string;
  detail?: string;
  hint?: string;
}

export interface AutonomousStatus {
  enabled?: boolean;
  submit_enabled?: boolean;
  default_submit_mode?: string;
  drafting_mode?: string;
  queue_depth?: number;
  blocked_applications?: number;
  ready_for_submit?: number;
  ready_to_apply?: number;
  ready_to_apply_threshold?: number;
  discovered?: number;
  screened_out?: number;
  evaluated?: number;
  drafted?: number;
  submitted?: number;
  daily_submit_cap?: number;
  daily_submitted_today?: number;
  daily_remaining_capacity?: number;
  daily_submit_day?: string;
  blocked_by_questions?: number;
  failed?: number;
  unresolved_prompts?: number;
  source_metrics?: Record<string, any>;
  source_warnings?: string[];
  zero_result_sources?: string[];
  drafting_batch?: { member_count?: number; completed_count?: number; target_size?: number; status?: string; [key: string]: any };
  latest_run?: Record<string, any>;
  latest_error?: string;
  live?: Record<string, any>;
  [key: string]: any;
}

export interface LedgerExportFile {
  path: string;
  size_bytes?: number;
  modified_at?: string;
}

export interface LedgerExportStatus {
  configured_output_base?: string;
  directory?: string;
  existing_files?: string[];
  files?: LedgerExportFile[];
  exists?: boolean;
  survives_reset?: boolean;
  last_generated_at?: string;
  current_state?: {
    applications?: number;
    submissions?: number;
    pending_questions?: number;
    [key: string]: number | undefined;
  };
  generation?: {
    supported?: boolean;
    source?: string;
    trigger?: string;
    targets?: string[];
    summary?: string;
  };
}

export interface LedgerExportResponse extends LedgerExportStatus {
  generated: boolean;
  generated_files?: string[];
}

export interface ResetOperationalContract {
  action?: string;
  clears: Record<string, string>;
  preserved: Record<string, string>;
  ledger_exports: {
    configured_output_base?: string;
    directory?: string;
    existing_files?: string[];
    survives_reset?: boolean;
  };
  history_after_reset: {
    applications?: boolean;
    submissions?: boolean;
    review_history?: boolean;
    run_history?: boolean;
    reports?: boolean;
    output_artifacts?: boolean;
    live_traces?: boolean;
    profile_and_answer_memory?: boolean;
    handled_job_memory?: boolean;
    existing_ledger_exports?: boolean;
    [key: string]: boolean | undefined;
  };
  summary?: string[];
}

export interface ResetOperationalResponse extends ResetOperationalContract {
  reset: boolean;
  deleted: Record<string, number>;
  handled_jobs?: {
    job_ids?: number;
    urls?: number;
    pairs?: number;
    duplicate_clusters?: number;
  };
  autonomous?: AutonomousStatus;
  jobs_table?: JobTableResponse;
}

export interface QuestionQueueItem {
  application_id: string;
  question_id: string;
  job_id?: string;
  company?: string;
  title?: string;
  prompt_text?: string;
  normalized_key?: string;
  canonical_question?: string;
  question_type?: string;
  widget_type?: string;
  required?: boolean;
  source_adapter?: string;
  option_signature?: string[];
  option_details?: { label?: string; value?: string; id?: string }[];
  existing_answer?: string;
  has_approved_memory?: boolean;
  [key: string]: any;
}

export interface JobTableRow {
  job_id: string;
  application_id?: string;
  company?: string;
  role?: string;
  source?: string;
  url?: string;
  apply_url?: string;
  location?: string;
  discovered_at?: string;
  evaluated_at?: string;
  submitted_at?: string;
  previewed_at?: string;
  application_status?: string;
  submission_status?: string;
  event_status?: string;
  preview_ready?: boolean;
  submit_ready?: boolean;
  blocked?: boolean;
  blockers?: any[];
  last_error?: string;
  report?: string;
  pdf?: boolean;
  score?: number;
  grade?: string;
  workflow_state?: string;
  review_status?: string;
  review_summary?: {
    severity?: string;
    blocker_count?: number;
    warning_count?: number;
    unresolved_question_count?: number;
    next_action?: string;
    next_action_reason?: string;
    ready_for_submit?: boolean;
    [key: string]: any;
  };
  manual_handoff?: { active?: boolean; status?: string; pending_count?: number; [key: string]: any };
  [key: string]: any;
}

export interface JobTableResponse {
  count: number;
  counts: Record<string, number>;
  items: JobTableRow[];
}

export interface DailyInboxItem {
  job_id: string;
  application_id?: string | null;
  company?: string;
  title?: string;
  source?: string;
  workflow_state?: string;
  status?: string;
  submission_status?: string | null;
  url?: string;
  [key: string]: any;
}

export interface DailyInboxResponse {
  counts: Record<string, number>;
  workflow_counts: Record<string, number>;
  screening_counts: Record<string, number>;
  items: DailyInboxItem[];
}

export interface QuestionQueueResponse {
  count: number;
  items: QuestionQueueItem[];
}

export interface ReviewQueueResponse {
  count: number;
  counts: Record<string, number>;
  items: ReviewQueueItem[];
}

export interface ReviewQueueItem {
  application_id: string;
  job_id?: string;
  company?: string;
  title?: string;
  status?: string;
  review_status?: string;
  classification?: Record<string, any>;
  hard_reject_reason?: string;
  auth_reject_reason?: string;
  login_wall_detected?: boolean;
  screening?: Record<string, any>;
  gate?: Record<string, any>;
  remaining_blockers?: any[];
  report?: string;
  review_summary?: {
    severity?: string;
    blocker_count?: number;
    warning_count?: number;
    unresolved_question_count?: number;
    next_action?: string;
    next_action_reason?: string;
    ready_for_submit?: boolean;
    [key: string]: any;
  };
  manual_handoff?: { active?: boolean; status?: string; pending_count?: number; [key: string]: any };
  source?: string;
  [key: string]: any;
}

export interface ApplicationDetail {
  application?: {
    application_id?: string;
    job_id?: string;
    company?: string;
    role?: string;
    status?: string;
    score?: number;
    grade?: string;
    report?: string;
    url?: string;
    source?: string;
  };
  job?: Record<string, any>;
  evaluation?: { score?: number; summary?: string; [key: string]: any };
  questions?: any[];
  blockers?: any[];
  submission?: Record<string, any>;
  manual_handoff_watch?: { active?: boolean; last_synced_at?: string; recent_answers?: any[]; learned_global_count?: number; job_scoped_count?: number; [key: string]: any };
  summary?: { next_action_reason?: string; severity?: string; classification?: Record<string, any>; [key: string]: any };
  artifacts?: { kind: string; label?: string; group?: string; target?: string; href?: string; relative_path?: string; exists?: boolean; external?: boolean }[];
  history?: { timestamp?: string; type?: string; actor?: string; summary?: string; metadata?: Record<string, any> }[];
  report_markdown?: string;
  [key: string]: any;
}

export type ConnectionState = "connecting" | "connected" | "reconnecting";

export type RunStatus = "running" | "completed" | "completed_with_failures" | "failed" | "interrupted" | "cancelled" | "queued" | "starting" | "idle";

export interface StageTrailItem {
  key: string;
  label: string;
  active: boolean;
  done: boolean;
}

export interface OperatorDerived {
  state: Record<string, any>;
  stats: Record<string, any>;
  runType: string;
  status: string;
  stage: string;
  isRunning: boolean;
  isTerminal: boolean;
  currentTitle: string;
  currentSource: string;
  latestMessage: string;
  latestError: string;
  warningNotice: string;
  elapsed: string;
  lastSeen: string;
  modelBadge: string;
  modelRole: string;
  modelProfile: string;
  streamHealth: string;
  readyThreshold: number;
  temporaryChatStatus: string;
  temporaryChatCheckedAt: string;
  counters: {
    discovered: number;
    screenedOut: number;
    evaluated: number;
    drafted: number;
    readyToApply: number;
    submitted: number;
    blockedByQuestions: number;
    failed: number;
    discoveryBoardsCompleted: number;
    discoveryBoardsTotal: number;
    discoverySeedPages: number;
    deterministicRejects: number;
  };
  queue: {
    depth: number;
    blocked: number;
    pendingQuestions: number;
    submitted: number;
    failed: number;
    rejected: number;
  };
  events: LiveEvent[];
  eventsDescending: LiveEvent[];
  latestEvent: LiveEvent | null;
  latestEventMeta: { label: string; value: any }[];
  stageTrail: StageTrailItem[];
  sourceMix: [string, number][];
  sourceMetrics: [string, any][];
  sourceWarnings: string[];
  zeroResultSources: string[];
  discoveryErrors: [string, number][];
  screenedOutReasons: [string, number][];
  draftBatch: DraftBatch;
}
