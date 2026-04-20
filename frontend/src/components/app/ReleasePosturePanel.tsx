import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { Panel, PanelHeader } from "@/components/app/Card";
import { StatusBadge, type Tone } from "@/components/app/StatusBadge";
import type { ReleasePosture, ReleasePostureEntry, ReleaseSensitivePathEntry } from "@/lib/types";

const toneByStatus: Record<string, Tone> = {
  supported: "success",
  partially_supported: "warning",
  not_yet_evidenced: "neutral",
  unsupported: "danger",
  enabled: "warning",
  disabled: "neutral",
  ready: "success",
  guarded: "info",
  blocked: "danger",
  misconfigured: "danger",
};

function releaseTone(status?: string): Tone {
  return toneByStatus[status ?? ""] ?? "neutral";
}

function releaseStatusLabel(status?: string): string {
  return (status ?? "unknown").replaceAll("_", " ");
}

function MatrixList({
  title,
  entries,
}: {
  title: string;
  entries: ReleasePostureEntry[];
}) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface p-3">
      <div className="text-[12px] font-medium text-foreground">{title}</div>
      <ul className="space-y-2">
        {entries.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-border bg-card p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-[12.5px] font-medium text-foreground">{entry.label}</div>
              <StatusBadge tone={releaseTone(entry.status)}>{releaseStatusLabel(entry.status)}</StatusBadge>
            </div>
            {entry.detail ? <p className="mt-1 text-[11.5px] text-muted-foreground">{entry.detail}</p> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SensitivePathList({ entries }: { entries: ReleaseSensitivePathEntry[] }) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface p-3">
      <div className="text-[12px] font-medium text-foreground">Sensitive paths</div>
      <ul className="space-y-2">
        {entries.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-border bg-card p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-[12.5px] font-medium text-foreground">{entry.label}</div>
              <StatusBadge tone={releaseTone(entry.status)}>{releaseStatusLabel(entry.status)}</StatusBadge>
            </div>
            {entry.detail ? <p className="mt-1 text-[11.5px] text-muted-foreground">{entry.detail}</p> : null}
            {entry.credential_source ? (
              <p className="mt-1 text-[11px] text-muted-foreground">Credential source: {releaseStatusLabel(entry.credential_source)}</p>
            ) : null}
            {entry.warnings && entry.warnings.length > 0 ? (
              <ul className="mt-2 space-y-1 text-[11px] text-[oklch(0.45_0.18_27)]">
                {entry.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ReleasePosturePanel({
  posture,
  className,
}: {
  posture?: ReleasePosture | null;
  className?: string;
}) {
  const [open, setOpen] = useState(false);

  if (!posture) {
    return null;
  }

  const blockedSensitive = (posture.sensitive_paths ?? []).some((entry) =>
    ["blocked", "misconfigured"].includes(entry.status ?? ""),
  );

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded-xl border border-border bg-card px-4 py-3 text-left shadow-card transition-colors hover:bg-muted/40"
      >
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-foreground">Release posture</span>
          <StatusBadge tone="accent">{releaseStatusLabel(posture.phase)}</StatusBadge>
          {blockedSensitive && <StatusBadge tone="danger">attention required</StatusBadge>}
        </div>
        <ChevronDown className={"h-4 w-4 text-muted-foreground transition-transform " + (open ? "rotate-180" : "")} />
      </button>
      {open && (
        <Panel className="mt-3">
      <div className="space-y-4">
        <div className="rounded-lg border border-[oklch(0.9_0.07_75)] bg-[oklch(0.98_0.02_75)] p-3">
          {posture.summary ? <p className="text-[12.5px] text-foreground">{posture.summary}</p> : null}
          {posture.disclaimer ? <p className="mt-2 text-[11.5px] text-muted-foreground">{posture.disclaimer}</p> : null}
        </div>

        {posture.gates && posture.gates.length > 0 ? (
          <div className="rounded-lg border border-border bg-surface p-3">
            <div className="mb-2 text-[12px] font-medium text-foreground">Current gates</div>
            <ul className="space-y-2">
              {posture.gates.map((entry) => (
                <li key={entry.id} className="flex flex-wrap items-start justify-between gap-2 rounded-lg border border-border bg-card p-3">
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] font-medium text-foreground">{entry.label}</div>
                    {entry.detail ? <p className="mt-1 text-[11.5px] text-muted-foreground">{entry.detail}</p> : null}
                  </div>
                  <StatusBadge tone={releaseTone(entry.status)}>{releaseStatusLabel(entry.status)}</StatusBadge>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          <MatrixList title="Launch surfaces" entries={posture.platform_matrix ?? []} />
          <MatrixList title="Release paths" entries={posture.feature_matrix ?? []} />
        </div>

        {posture.sensitive_paths && posture.sensitive_paths.length > 0 ? (
          <SensitivePathList entries={posture.sensitive_paths} />
        ) : null}

        {posture.operator_responsibilities && posture.operator_responsibilities.length > 0 ? (
          <div className="rounded-lg border border-border bg-surface p-3">
            <div className="mb-2 text-[12px] font-medium text-foreground">Operator responsibilities</div>
            <ul className="space-y-1 text-[11.5px] text-muted-foreground">
              {posture.operator_responsibilities.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Panel>
      )}
    </div>
  );
}