/* ── Constants ────────────────────────────────────────────────────── */

export const NAV_ITEMS = [
  { label: "Dashboard", to: "/" },
  { label: "Setup", to: "/setup" },
  { label: "Autopilot", to: "/autopilot" },
  { label: "Review", to: "/review" },
  { label: "Settings", to: "/settings" },
] as const;

export const NAV_ROUTE_ALIASES: Record<string, string> = { "/daily": "/autopilot" };

export const STAGE_LABELS: Record<string, string> = {
  idle: "Idle",
  queue: "Queue",
  discovery: "Discovery",
  screening: "Screening",
  evaluation: "Evaluation",
  drafting: "Drafting",
  prepare: "Prepare",
  review: "Review",
  preview: "Preview",
  submit: "Submit",
  question_resolution: "Questions",
  complete: "Complete",
};

export const STAGE_ORDER = [
  "queue",
  "discovery",
  "screening",
  "evaluation",
  "drafting",
  "prepare",
  "review",
  "preview",
  "submit",
  "question_resolution",
  "complete",
] as const;

export const TERMINAL_STATUSES = new Set([
  "completed",
  "completed_with_failures",
  "failed",
  "interrupted",
  "cancelled",
]);

export const ROUTING_FAMILIES = [
  {
    id: "screening",
    label: "Screening Model",
    description:
      "One model for discovery routing, classification, and extraction.",
    note: "Keeps separate prompts for each task, but updates all three roles together. LM Studio stays recommended; OpenRouter remote bindings are allowed.",
    remoteAllowed: true,
    roles: [
      { role: "text_router", label: "Text Router", profilePrefix: "lmstudio-screen" },
      { role: "classifier", label: "Classifier", profilePrefix: "lmstudio-screen" },
      { role: "extractor", label: "Extractor", profilePrefix: "lmstudio-screen" },
    ],
  },
  {
    id: "drafting",
    label: "Legacy Drafting Roles",
    description:
      "Optional local writer bindings kept for rollback only. Live document drafting now uses the managed ChatGPT profile.",
    note: "These roles are no longer part of the primary launch gate when ChatGPT drafting is active.",
    remoteAllowed: false,
    roles: [
      { role: "writer", label: "Writer", profilePrefix: "lmstudio-draft" },
      { role: "resume_writer", label: "Resume Writer", profilePrefix: "lmstudio-draft" },
      { role: "cover_letter_writer", label: "Cover Letter Writer", profilePrefix: "lmstudio-draft" },
    ],
  },
  {
    id: "qa",
    label: "Q&A Model",
    description: "A separate model for application-question answering.",
    note: "Kept separate because form Q&A tends to need different prompting and lower-variance answers. OpenRouter remote bindings are allowed here.",
    remoteAllowed: true,
    roles: [
      { role: "question_answerer", label: "Question Answerer", profilePrefix: "lmstudio-draft" },
    ],
  },
] as const;

export const LMSTUDIO_DEFAULT_HOST = "http://127.0.0.1:1234";
export const LMSTUDIO_DEFAULT_PROVIDER = "lmstudio";
export const OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1";

export const PORTAL_SOURCE_OPTIONS = [
  {
    id: "greenhouse",
    label: "Greenhouse",
    note: "Launch default. Uses the curated Greenhouse board universe plus seed crawling when no boards are pinned.",
    launchDefault: true,
    experimental: false,
  },
  {
    id: "lever",
    label: "Lever",
    note: "Experimental. Disabled by default and excluded from autonomous launch runs until you opt in.",
    launchDefault: false,
    experimental: true,
  },
  {
    id: "ashby",
    label: "Ashby",
    note: "Experimental. Disabled by default and excluded from autonomous launch runs until you opt in.",
    launchDefault: false,
    experimental: true,
  },
] as const;

export const MODEL_PROVIDER_OPTIONS = [
  { value: "lmstudio", label: "LM Studio" },
  { value: "openrouter", label: "OpenRouter" },
] as const;
export const MODEL_TRANSPORT_OPTIONS = [
  { value: "local_http", label: "Local HTTP" },
  { value: "remote_http", label: "Remote HTTP" },
] as const;

export const REVIEW_TABS = [
  { key: "summary", label: "Summary" },
  { key: "questions", label: "Questions" },
  { key: "handoff", label: "Manual Handoff" },
  { key: "artifacts", label: "Artifacts" },
  { key: "history", label: "History" },
] as const;

export const REVIEW_SECTION_FROM_TAB: Record<string, string> = {
  summary: "needs_attention",
  questions: "questions",
  handoff: "handoff",
  artifacts: "documents",
  history: "advanced",
};

export const REVIEW_TAB_FROM_SECTION: Record<string, string> = {
  needs_attention: "summary",
  questions: "questions",
  handoff: "handoff",
  documents: "artifacts",
  advanced: "history",
};

export const REVIEW_QUEUE_FILTERS = [
  { key: "all", label: "All" },
  { key: "needs_input", label: "Needs Input" },
  { key: "manual_handoff", label: "Manual Handoff" },
  { key: "ready", label: "Ready" },
] as const;

export const REVIEW_ACTION_LABELS: Record<string, string> = {
  approve: "Approve / Apply",
  request_input: "Open For Manual Input",
  sync_manual_input: "Sync Browser Changes",
  mark_submitted: "Mark As Submitted",
  reject: "Reject",
  save_answers: "Save Answers",
  review_summary: "Review Summary",
};

export const REVIEW_SEVERITY_WEIGHT: Record<string, number> = {
  danger: 3,
  warning: 2,
  success: 1,
  neutral: 0,
};
