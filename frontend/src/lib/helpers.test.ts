import { describe, expect, it } from "vitest";
import {
  autopilotPrimaryAction,
  buildQuestionAnswerRequest,
  deriveOperatorState,
  providerLabel,
  questionDraftKey,
  reviewQueueMatchesFilter,
  transportLabel,
} from "@/lib/helpers";

describe("deriveOperatorState", () => {
  it("treats recent heartbeats as fresh activity during long-running work", () => {
    const staleEventAt = new Date(Date.now() - 30_000).toISOString();
    const recentHeartbeatAt = Date.now() - 5_000;

    const operator = deriveOperatorState(
      {
        state: {
          run_type: "autonomous",
          status: "running",
          stage: "drafting",
          last_event_at: staleEventAt,
          stats: {},
        },
        events: [],
      },
      "connected",
      recentHeartbeatAt,
    );

    expect(operator.streamHealth).toBe("connected");
    expect(operator.warningNotice).toBe("");
  });

  it("handles missing payload shape without crashing", () => {
    const operator = deriveOperatorState(null, "connecting", Date.now());

    expect(operator.runType).toBe("idle");
    expect(operator.status).toBe("idle");
    expect(operator.events).toEqual([]);
  });

  it("surfaces queued ChatGPT drafting statuses through counters and labels", () => {
    const operator = deriveOperatorState(
      {
        state: {
          run_type: "autonomous",
          status: "running",
          stage: "drafting",
          latest_operator_message: "awaiting_markers",
          stats: {
            drafted: 2,
            ready_for_submit: 1,
          },
        },
        events: [],
      },
      "connected",
      Date.now(),
    );

    expect(operator.stage).toBe("drafting");
    expect(operator.counters.drafted).toBe(2);
    expect(operator.counters.readyToApply).toBe(1);
    expect(operator.latestMessage).toBe("awaiting_markers");
  });

  it("suppresses recovered worker errors after a completed run", () => {
    const operator = deriveOperatorState(
      {
        state: {
          run_type: "submission",
          status: "completed",
          stage: "submit",
          latest_operator_message: "Submission finished successfully.",
          latest_error: "Stale live state recovered without an active worker.",
          stats: {},
        },
        events: [],
      },
      "connected",
      Date.now(),
    );

    expect(operator.latestError).toBe("");
  });

  it("produces stageTrail with correct active/done flags", () => {
    const operator = deriveOperatorState(
      {
        state: {
          run_type: "autonomous",
          status: "running",
          stage: "screening",
          stats: {},
        },
        events: [],
      },
      "connected",
      Date.now(),
    );

    const discovery = operator.stageTrail.find((s) => s.key === "discovery");
    const screening = operator.stageTrail.find((s) => s.key === "screening");
    const drafting = operator.stageTrail.find((s) => s.key === "drafting");

    expect(discovery?.done).toBe(true);
    expect(screening?.active).toBe(true);
    expect(drafting?.done).toBe(false);
    expect(drafting?.active).toBe(false);
  });

  it("warns about interrupted runs", () => {
    const operator = deriveOperatorState(
      {
        state: { run_type: "autonomous", status: "interrupted", stage: "discover", stats: {} },
        events: [],
      },
      "connected",
      Date.now(),
    );

    expect(operator.warningNotice).toContain("interrupted");
  });

  it("builds the backend question-answer payload with the real application and question ids", () => {
    const question = {
      application_id: "app-7",
      question_id: "q-4",
      widget_type: "select",
      option_details: [{ label: "Yes", value: "yes" }],
    };

    expect(questionDraftKey(question)).toBe("app-7::q-4");
    expect(buildQuestionAnswerRequest(question as any, "yes")).toEqual({
      application_id: "app-7",
      question_id: "q-4",
      answer_text: "yes",
      approve_memory: true,
      auto_retry: true,
    });
  });

  it("filters review queue items by the real summary and manual handoff fields", () => {
    expect(reviewQueueMatchesFilter({
      application_id: "app-needs-input",
      status: "Needs Input",
      review_status: "needs_user_input",
      review_summary: { blocker_count: 1, unresolved_question_count: 0 },
      manual_handoff: { active: false },
      remaining_blockers: [{ label: "Missing answer" }],
    } as any, "needs_input")).toBe(true);

    expect(reviewQueueMatchesFilter({
      application_id: "app-handoff",
      status: "Preview Ready",
      review_status: "preview_ready",
      review_summary: { blocker_count: 0, unresolved_question_count: 0, ready_for_submit: true },
      manual_handoff: { active: true },
      remaining_blockers: [],
    } as any, "manual_handoff")).toBe(true);

    expect(reviewQueueMatchesFilter({
      application_id: "app-ready",
      status: "Ready to Submit",
      review_status: "preview_ready",
      review_summary: { blocker_count: 0, unresolved_question_count: 0, ready_for_submit: true },
      manual_handoff: { active: false },
      remaining_blockers: [],
    } as any, "ready")).toBe(true);
  });

  it("chooses the Autopilot primary action from persisted review state", () => {
    expect(autopilotPrimaryAction({
      application_id: "app-ready",
      submit_ready: true,
      review_summary: {
        next_action: "approve",
        blocker_count: 0,
        unresolved_question_count: 0,
        ready_for_submit: true,
      },
      manual_handoff: { active: false },
    } as any)).toEqual({
      kind: "approve",
      label: "Approve / Apply",
    });

    expect(autopilotPrimaryAction({
      application_id: "app-handoff",
      review_summary: {
        next_action: "sync_manual_input",
      },
      manual_handoff: { active: true },
    } as any)).toEqual({
      kind: "review",
      label: "Sync Browser Changes",
      section: "handoff",
    });

    expect(autopilotPrimaryAction({
      application_id: "app-questions",
      review_summary: {
        next_action: "request_input",
      },
      manual_handoff: { active: false },
    } as any)).toEqual({
      kind: "review",
      label: "Answer In Review",
      section: "questions",
    });

    expect(autopilotPrimaryAction({
      application_id: "app-questions-legacy",
      review_summary: {
        next_action: "open_manual_input",
      },
      manual_handoff: { active: false },
    } as any)).toEqual({
      kind: "review",
      label: "Answer In Review",
      section: "questions",
    });
  });

  it("maps remote provider and transport labels used by settings", () => {
    expect(providerLabel("openrouter")).toBe("OpenRouter");
    expect(transportLabel("remote_http")).toBe("Remote HTTP");
  });
});
