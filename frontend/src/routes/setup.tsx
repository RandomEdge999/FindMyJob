import { createFileRoute, Link } from "@tanstack/react-router";
import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useState } from "react";
import {
  CircleAlert,
  CircleCheck,
  ChevronDown,
  ExternalLink,
  RefreshCw,
  Save,
  Settings,
} from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel, PanelHeader } from "@/components/app/Card";
import { StatusBadge } from "@/components/app/StatusBadge";
import { usePolledJson } from "@/hooks/use-polled-json";
import { requestJson } from "@/lib/api";
import type { BasicProfileData, ReadinessData } from "@/lib/types";

type SetupFormState = {
  name: string;
  email: string;
  phone: string;
  location: string;
  linkedin: string;
  github: string;
  website: string;
  summary: string;
  targetRoles: string;
  titleKeywords: string;
  locations: string;
  countries: string;
  regions: string;
  cities: string;
  remoteOnly: boolean;
  isAuthorized: "" | "true" | "false";
  requiresFutureSponsorship: "" | "true" | "false";
};

type FlashState = {
  tone: "success" | "danger";
  message: string;
};

const EMPTY_FORM: SetupFormState = {
  name: "",
  email: "",
  phone: "",
  location: "",
  linkedin: "",
  github: "",
  website: "",
  summary: "",
  targetRoles: "",
  titleKeywords: "",
  locations: "",
  countries: "US",
  regions: "",
  cities: "",
  remoteOnly: true,
  isAuthorized: "",
  requiresFutureSponsorship: "",
};

export const Route = createFileRoute("/setup")({
  component: SetupPage,
});

function SetupPage() {
  const { data: profileData, refresh: refreshProfile } = usePolledJson<BasicProfileData>("/api/profile/basic", 15000);
  const { data: readiness, refresh: refreshReadiness } = usePolledJson<ReadinessData>("/api/setup/readiness", 8000);
  const [form, setForm] = useState<SetupFormState>(EMPTY_FORM);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<FlashState | null>(null);

  useEffect(() => {
    if (!profileData || dirty) return;
    setForm(formStateFromProfile(profileData));
  }, [profileData, dirty]);

  const findings = useMemo(
    () => (readiness?.findings ?? []).filter((item) => item.status && item.status !== "pass"),
    [readiness],
  );

  const guidance = profileData?.guidance ?? {};
  const profileSurface = profileData?.profile_surface ?? {};
  const canSave = guidance.can_save !== false;
  const hasAdvancedOverrides = (guidance.active_advanced_paths ?? []).length > 0;

  const saveProfile = useCallback(async () => {
    setBusy(true);
    setFlash(null);
    try {
      await requestJson("/api/profile/basic", {
        method: "PUT",
        body: JSON.stringify({
          candidate: {
            name: form.name,
            email: form.email || null,
            phone: form.phone || null,
            location: form.location || null,
            linkedin: form.linkedin || null,
            github: form.github || null,
            website: form.website || null,
            summary: form.summary || null,
            target_roles: parseLineList(form.targetRoles),
          },
          targets: {
            title_keywords: parseLineList(form.titleKeywords),
            locations: parseLineList(form.locations),
            countries: parseLineList(form.countries),
            regions: parseLineList(form.regions),
            cities: parseLineList(form.cities),
            remote_only: form.remoteOnly,
          },
          authorization: {
            is_authorized: parseBooleanChoice(form.isAuthorized),
            requires_future_sponsorship: parseBooleanChoice(form.requiresFutureSponsorship),
          },
        }),
      });
      setDirty(false);
      setFlash({ tone: "success", message: "Basic profile saved to the local workspace profile path." });
      await Promise.all([refreshProfile(), refreshReadiness()]);
    } catch (error) {
      setFlash({ tone: "danger", message: error instanceof Error ? error.message : "Failed to save the basic profile." });
    } finally {
      setBusy(false);
    }
  }, [form, refreshProfile, refreshReadiness]);

  return (
    <AppShell>
      <PageHeader
        title="Setup"
        subtitle="Reusable profile basics and readiness checklist."
        actions={
          <div className="flex gap-2">
            <button
              onClick={() => void Promise.all([refreshProfile(), refreshReadiness()])}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[13px] font-medium hover:bg-muted"
            >
              <RefreshCw className="h-4 w-4" /> Refresh
            </button>
            <Link
              to="/settings"
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[13px] font-medium hover:bg-muted"
            >
              <Settings className="h-4 w-4" /> Settings
            </Link>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-3">
          <Panel>
            <PanelHeader
              title="Basic Profile"
              description="This page only covers reusable setup data. Job-specific answers remain in the manual answer learning flow."
              actions={
                canSave ? (
                  <button
                    disabled={busy}
                    onClick={() => void saveProfile()}
                    className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" /> Save basic profile
                  </button>
                ) : null
              }
            />

            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge tone={profileSurface.mode === "sample_mode" ? "warning" : "success"}>
                  {profileSurface.mode ?? "sample_mode"}
                </StatusBadge>
                {guidance.writes_to ? (
                  <span className="text-[11.5px] text-muted-foreground">
                    Writes to <span className="font-medium text-foreground">{guidance.writes_to}</span>
                  </span>
                ) : null}
              </div>

              {flash ? (
                <div className={"rounded-lg border p-3 text-[12.5px] " + (flash.tone === "success"
                  ? "border-primary/30 bg-primary/10 text-foreground"
                  : "border-[oklch(0.45_0.18_27)]/30 bg-[oklch(0.45_0.18_27)]/10 text-foreground")}>
                  {flash.message}
                </div>
              ) : null}

              {!canSave ? (
                <div className="rounded-lg border border-[oklch(0.45_0.18_27)]/30 bg-[oklch(0.45_0.18_27)]/10 p-3 text-[12.5px] text-foreground">
                  <div className="font-medium">Advanced local overrides are already active.</div>
                  <p className="mt-1 text-muted-foreground">
                    Setup is intentionally read-only in this mode so it does not layer a conflicting `user-profile.yml` on top of the active override files.
                  </p>
                  {(guidance.active_advanced_paths ?? []).length > 0 ? (
                    <ul className="mt-2 space-y-1 text-[11.5px] text-muted-foreground">
                      {(guidance.active_advanced_paths ?? []).map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}

              {hasAdvancedOverrides && canSave ? (
                <div className="rounded-lg border border-border bg-surface p-3 text-[12px] text-muted-foreground">
                  Advanced override files are also present. The setup form will save to `user-profile.yml`, but any deeper local override files still take precedence at runtime.
                </div>
              ) : null}

              <div className={!canSave ? "pointer-events-none opacity-70" : undefined}>
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <Panel className="border-dashed">
                    <PanelHeader title="Contact Basics" description="Used by readiness checks, drafting, and apply flows." />
                    <div className="grid grid-cols-1 gap-3">
                      <TextField label="Full name" value={form.name} onChange={(value) => updateField(setForm, setDirty, "name", value)} />
                      <TextField label="Email" value={form.email} onChange={(value) => updateField(setForm, setDirty, "email", value)} />
                      <TextField label="Phone" value={form.phone} onChange={(value) => updateField(setForm, setDirty, "phone", value)} />
                      <TextField label="Location" value={form.location} onChange={(value) => updateField(setForm, setDirty, "location", value)} />
                      <TextField label="LinkedIn" value={form.linkedin} onChange={(value) => updateField(setForm, setDirty, "linkedin", value)} />
                      <TextField label="GitHub" value={form.github} onChange={(value) => updateField(setForm, setDirty, "github", value)} />
                      <TextField label="Website" value={form.website} onChange={(value) => updateField(setForm, setDirty, "website", value)} />
                      <TextAreaField label="Short summary" value={form.summary} rows={4} onChange={(value) => updateField(setForm, setDirty, "summary", value)} />
                    </div>
                  </Panel>

                  <Panel className="border-dashed">
                    <PanelHeader title="Discovery Defaults" description="Reusable filters for discovery and drafting context." />
                    <div className="grid grid-cols-1 gap-3">
                      <TextAreaField label="Target roles" value={form.targetRoles} rows={4} onChange={(value) => updateField(setForm, setDirty, "targetRoles", value)} hint="One role family per line. These expand into title keywords during discovery." />
                      <TextAreaField label="Title keywords" value={form.titleKeywords} rows={4} onChange={(value) => updateField(setForm, setDirty, "titleKeywords", value)} hint="Used directly by discovery when scanning source boards." />
                      <TextAreaField label="Preferred locations" value={form.locations} rows={3} onChange={(value) => updateField(setForm, setDirty, "locations", value)} />
                      <TextAreaField label="Countries" value={form.countries} rows={2} onChange={(value) => updateField(setForm, setDirty, "countries", value)} />
                      <TextAreaField label="Regions / states" value={form.regions} rows={2} onChange={(value) => updateField(setForm, setDirty, "regions", value)} />
                      <TextAreaField label="Cities" value={form.cities} rows={2} onChange={(value) => updateField(setForm, setDirty, "cities", value)} />
                      <ToggleField label="Remote only" checked={form.remoteOnly} onChange={(value) => updateField(setForm, setDirty, "remoteOnly", value)} />
                    </div>
                  </Panel>
                </div>

                <Panel className="mt-3 border-dashed">
                  <PanelHeader title="Work Authorization" description="Saved as reusable authorization facts for apply flows." />
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <SelectField
                      label="Authorized to work in target countries"
                      value={form.isAuthorized}
                      options={BOOLEAN_SELECT_OPTIONS}
                      onChange={(value) => updateField(setForm, setDirty, "isAuthorized", value as SetupFormState["isAuthorized"])}
                    />
                    <SelectField
                      label="Requires future sponsorship"
                      value={form.requiresFutureSponsorship}
                      options={BOOLEAN_SELECT_OPTIONS}
                      onChange={(value) => updateField(setForm, setDirty, "requiresFutureSponsorship", value as SetupFormState["requiresFutureSponsorship"])}
                    />
                  </div>
                </Panel>
              </div>

              <div className="rounded-lg border border-border bg-surface p-3 text-[12px] text-muted-foreground">
                Setup intentionally excludes education, languages, work/project facts, skills, and job-specific answers. Those continue through the deeper onboarding pipeline or manual answer learning during applications.
              </div>

              <div className="rounded-lg border border-border bg-card p-3 text-[12px] text-muted-foreground">
                Setup only saves reusable profile defaults. Model-provider bindings, browser automation, submit mode, and ChatGPT drafting stay in{" "}
                <Link to="/settings" className="font-medium text-foreground underline underline-offset-2">
                  Settings
                </Link>
                .
              </div>
            </div>
          </Panel>
        </div>

        <div className="min-w-0 space-y-3">
          <Panel>
            <PanelHeader title="Readiness" description="Use these findings to finish the rest of first-run setup honestly." />
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                {readiness?.overall_status === "pass" ? (
                  <CircleCheck className="h-5 w-5 text-primary" />
                ) : (
                  <CircleAlert className="h-5 w-5 text-[oklch(0.45_0.18_27)]" />
                )}
                <span className="text-[13px] font-semibold text-foreground">
                  {readiness?.overall_status === "pass" ? "Setup is ready" : `Status: ${readiness?.overall_status ?? "loading"}`}
                </span>
              </div>

              {findings.length > 0 ? (
                <ul className="space-y-2">
                  {findings.map((item, index) => (
                    <li key={item.key ?? index} className="rounded-lg border border-border bg-card p-3">
                      <div className="flex items-start gap-2">
                        <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-[oklch(0.45_0.18_27)]" />
                        <div className="min-w-0 flex-1">
                          <div className="text-[12.5px] font-medium text-foreground">{item.summary ?? item.key ?? "Issue"}</div>
                          {item.detail ? <div className="mt-1 text-[11.5px] text-muted-foreground">{item.detail}</div> : null}
                          {item.hint ? <div className="mt-1 text-[11px] text-muted-foreground italic">{item.hint}</div> : null}
                        </div>
                        <StatusBadge tone={item.status === "blocked" ? "danger" : "warning"}>{item.status}</StatusBadge>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-[12px] text-muted-foreground">No current setup blockers were returned.</p>
              )}
            </div>
          </Panel>

          <SetupCollapsible title="Setup scope & next steps">
            <Panel>
            <PanelHeader title="Scope" description="What this page does and does not own." />
            <div className="space-y-3 text-[12px] break-words">
              <div>
                <div className="font-medium text-foreground">Included</div>
                <ul className="mt-1 space-y-1 text-muted-foreground">
                  {(guidance.included_fields ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="font-medium text-foreground">Still handled elsewhere</div>
                <ul className="mt-1 space-y-1 text-muted-foreground">
                  {(guidance.excluded_fields ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              {profileSurface.public_template_path ? (
                <div className="rounded-lg border border-border bg-card p-3 text-muted-foreground break-all">
                  File-first fallback: <span className="font-medium text-foreground">{profileSurface.public_template_path}</span>
                </div>
              ) : null}
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="Next Steps" />
            <ol className="space-y-2 text-[12.5px] text-muted-foreground">
              <li>Save your basic profile here so the workspace stops relying on tracked sample data.</li>
              <li>Open Settings to configure LM Studio, drafting, and source readiness.</li>
              <li>Leave job-specific prompts to Autopilot and Review so learned answers stay separate from the base profile.</li>
            </ol>
            <div className="mt-3 flex gap-2">
              <Link
                to="/settings"
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted"
              >
                <Settings className="h-3.5 w-3.5" /> Open Settings
              </Link>
              {guidance.template_path ? (
                <span className="inline-flex h-8 min-w-0 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] text-muted-foreground">
                  <ExternalLink className="h-3.5 w-3.5" /> Template: {guidance.template_path}
                </span>
              ) : null}
            </div>
          </Panel>
          </SetupCollapsible>
        </div>
      </div>
    </AppShell>
  );
}

function SetupCollapsible({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded-xl border border-border bg-card px-4 py-3 text-left text-[12.5px] font-semibold text-foreground shadow-card hover:bg-muted/40"
      >
        {title}
        <ChevronDown className={"h-4 w-4 text-muted-foreground transition-transform " + (open ? "rotate-180" : "")} />
      </button>
      {open && <div className="mt-3 min-w-0 space-y-4 overflow-hidden">{children}</div>}
    </div>
  );
}

const BOOLEAN_SELECT_OPTIONS = [
  { value: "", label: "Not set" },
  { value: "true", label: "Yes" },
  { value: "false", label: "No" },
];

function formStateFromProfile(payload: BasicProfileData): SetupFormState {
  const candidate = payload.values?.candidate ?? {};
  const targets = payload.values?.targets ?? {};
  const authorization = payload.values?.authorization ?? {};
  return {
    name: String(candidate.name ?? ""),
    email: String(candidate.email ?? ""),
    phone: String(candidate.phone ?? ""),
    location: String(candidate.location ?? ""),
    linkedin: String(candidate.linkedin ?? ""),
    github: String(candidate.github ?? ""),
    website: String(candidate.website ?? ""),
    summary: String(candidate.summary ?? ""),
    targetRoles: toLineList(candidate.target_roles),
    titleKeywords: toLineList(targets.title_keywords),
    locations: toLineList(targets.locations),
    countries: toLineList(targets.countries) || "US",
    regions: toLineList(targets.regions),
    cities: toLineList(targets.cities),
    remoteOnly: Boolean(targets.remote_only ?? true),
    isAuthorized: toBooleanChoice(authorization.is_authorized),
    requiresFutureSponsorship: toBooleanChoice(authorization.requires_future_sponsorship),
  };
}

function toLineList(values: string[] | undefined | null): string {
  return Array.isArray(values) ? values.filter(Boolean).join("\n") : "";
}

function parseLineList(value: string): string[] {
  const seen = new Set<string>();
  return value
    .replace(/,/g, "\n")
    .split(/\n+/)
    .map((item) => item.trim())
    .filter((item) => {
      if (!item) return false;
      const key = item.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function parseBooleanChoice(value: SetupFormState["isAuthorized"]): boolean | null {
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

function toBooleanChoice(value: boolean | null | undefined): SetupFormState["isAuthorized"] {
  if (value === true) return "true";
  if (value === false) return "false";
  return "";
}

function updateField<K extends keyof SetupFormState>(
  setForm: Dispatch<SetStateAction<SetupFormState>>,
  setDirty: Dispatch<SetStateAction<boolean>>,
  key: K,
  value: SetupFormState[K],
) {
  setDirty(true);
  setForm((current) => ({ ...current, [key]: value }));
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground placeholder:text-muted-foreground"
      />
    </label>
  );
}

function TextAreaField({
  label,
  value,
  rows,
  hint,
  onChange,
}: {
  label: string;
  value: string;
  rows: number;
  hint?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <textarea
        value={value}
        rows={rows}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground placeholder:text-muted-foreground"
      />
      {hint ? <span className="mt-1 block text-[10.5px] text-muted-foreground">{hint}</span> : null}
    </label>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground"
      >
        {options.map((option) => (
          <option key={option.value || option.label} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

function ToggleField({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="flex items-center gap-2.5">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={"relative h-5 w-9 shrink-0 rounded-full transition-colors " + (checked ? "bg-primary" : "bg-muted")}
      >
        <span className={"absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform " + (checked ? "translate-x-4" : "translate-x-0")} />
      </button>
      <span className="text-[12.5px] text-foreground">{label}</span>
    </label>
  );
}
