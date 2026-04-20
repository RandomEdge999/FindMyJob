import { createFileRoute, useNavigate, useSearch } from "@tanstack/react-router";
import { useState, useCallback, useDeferredValue } from "react";
import {
  Activity,
  ArrowLeftRight,
  Bot,
  CircleAlert,
  CircleCheck,
  Globe,
  Heart,
  Layers,
  MessageSquare,
  RefreshCw,
  Save,
  Trash2,
  Zap,
} from "lucide-react";
import { AppShell } from "@/components/app/AppShell";
import { PageHeader } from "@/components/app/PageHeader";
import { Panel, PanelHeader } from "@/components/app/Card";
import { ReleasePosturePanel } from "@/components/app/ReleasePosturePanel";
import { StatusBadge, type Tone } from "@/components/app/StatusBadge";
import { usePolledJson } from "@/hooks/use-polled-json";
import { requestJson } from "@/lib/api";
import { formatDate, modelCatalogUrl, providerLabel, transportLabel } from "@/lib/helpers";
import { LMSTUDIO_DEFAULT_HOST, MODEL_PROVIDER_OPTIONS, OPENROUTER_DEFAULT_BASE_URL, ROUTING_FAMILIES } from "@/lib/constants";
import type {
  LaunchProfile,
  LaunchRoleStatus,
  ModelCatalogData,
  ModelCatalogEntry,
  ModelProfile,
  ReadinessData,
  ReleasePosture,
  SettingsData,
} from "@/lib/types";

type Section = "readiness" | "automation" | "chatgpt" | "sources" | "models" | "portability";

type RoutingFamily = (typeof ROUTING_FAMILIES)[number];

export const Route = createFileRoute("/settings")({
  component: SettingsPage,
  validateSearch: (search: Record<string, unknown>) => ({
    section: (search.section as Section) ?? "readiness",
  }),
});

const SECTIONS: { key: Section; label: string; icon: typeof Activity }[] = [
  { key: "readiness", label: "Readiness & Health", icon: Heart },
  { key: "automation", label: "Automation & Runtime", icon: Zap },
  { key: "chatgpt", label: "ChatGPT Drafting", icon: MessageSquare },
  { key: "sources", label: "Sources & Discovery", icon: Globe },
  { key: "models", label: "Models & Profiles", icon: Layers },
  { key: "portability", label: "Backup & Transfer", icon: ArrowLeftRight },
];

function SettingsPage() {
  const { section } = useSearch({ from: "/settings" });
  const navigate = useNavigate();
  const setSection = useCallback(
    (s: Section) => navigate({ to: "/settings", search: { section: s } }),
    [navigate],
  );

  const { data: settings, refresh: refreshSettings } = usePolledJson<SettingsData>("/api/settings", 8000);
  const { data: readiness, refresh: refreshReadiness } = usePolledJson<ReadinessData>("/api/setup/readiness", 8000);
  const { data: releasePosture } = usePolledJson<ReleasePosture>("/api/release/posture", 15000);

  const runtimeCatalogBaseUrl = useDeferredValue(
    String(settings?.runtime_model?.base_url ?? LMSTUDIO_DEFAULT_HOST).trim() || LMSTUDIO_DEFAULT_HOST,
  );
  const { data: runtimeCatalog, error: runtimeCatalogError, refresh: refreshRuntimeCatalog } = usePolledJson<ModelCatalogData>(
    modelCatalogUrl({
      name: "runtime-model",
      provider: "lmstudio",
      transport: "local_http",
      base_url: runtimeCatalogBaseUrl,
    }),
    15_000,
  );

  return (
    <AppShell>
      <PageHeader title="Settings" subtitle="Sources, model bindings, ChatGPT drafting, and runtime configuration." />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-5">
        <nav className="flex gap-1 overflow-x-auto pb-1 lg:col-span-1 lg:flex-col lg:overflow-visible lg:pb-0">
          {SECTIONS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setSection(key)}
              className={
                "flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-[12.5px] font-medium transition-colors " +
                (section === key
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground")
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
            </button>
          ))}
        </nav>
        <div className="min-w-0 lg:col-span-4">
          {section === "readiness" && <ReadinessSection readiness={readiness} refresh={refreshReadiness} releasePosture={releasePosture} />}
          {section === "automation" && (
            <AutomationSection
              settings={settings}
              refresh={refreshSettings}
              modelCatalog={runtimeCatalog}
              modelCatalogError={runtimeCatalogError}
              refreshCatalog={refreshRuntimeCatalog}
            />
          )}
          {section === "chatgpt" && <ChatGPTSection settings={settings} refresh={refreshSettings} />}
          {section === "sources" && <SourcesSection settings={settings} refresh={refreshSettings} />}
          {section === "models" && (
            <ModelsSection
              settings={settings}
              refresh={refreshSettings}
              modelCatalog={runtimeCatalog}
              modelCatalogError={runtimeCatalogError}
            />
          )}
          {section === "portability" && <PortabilitySection refresh={refreshSettings} />}
        </div>
      </div>
    </AppShell>
  );
}

/* ------------------------------------------------------------------- */
/*  Readiness Section                                                 */
/* ------------------------------------------------------------------- */

function ReadinessSection({ readiness, refresh, releasePosture }: { readiness?: ReadinessData | null; refresh: () => Promise<unknown>; releasePosture?: ReleasePosture | null }) {
  const [busy, setBusy] = useState(false);
  const runTest = useCallback(async () => {
    setBusy(true);
    try {
      await requestJson("/api/settings/test", { method: "POST" });
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const isReady = readiness?.overall_status === "pass";
  const findings = readiness?.findings ?? [];
  const blockedFindings = findings.filter((finding) => finding.status !== "pass");

  return (
    <>
    <Panel>
      <PanelHeader
        title="Readiness & Workspace Health"
        description="Check whether the workspace is configured and healthy."
        actions={
          <button
            disabled={busy}
            onClick={runTest}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className={"h-3.5 w-3.5" + (busy ? " animate-spin" : "")} /> Run check
          </button>
        }
      />
      {readiness ? (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            {isReady ? (
              <CircleCheck className="h-5 w-5 text-primary" />
            ) : (
              <CircleAlert className="h-5 w-5 text-[oklch(0.45_0.18_27)]" />
            )}
            <span className="text-[14px] font-semibold">
              {isReady ? "All systems go" : `Status: ${readiness.overall_status ?? "unknown"}`}
            </span>
          </div>
          {blockedFindings.length > 0 && (
            <ul className="space-y-2">
              {blockedFindings.map((finding, index) => (
                <li key={finding.key ?? index} className="flex items-start gap-2 rounded-lg border border-border bg-surface p-3">
                  <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-[oklch(0.45_0.18_27)]" />
                  <div>
                    <div className="text-[12.5px] font-medium text-foreground">{finding.summary ?? finding.key ?? "Issue"}</div>
                    {finding.detail ? <div className="mt-0.5 text-[11px] text-muted-foreground">{finding.detail}</div> : null}
                    {finding.hint ? <div className="mt-0.5 text-[11px] italic text-muted-foreground">{finding.hint}</div> : null}
                  </div>
                  <StatusBadge tone={finding.status === "blocked" ? "danger" : "warning"}>{finding.status}</StatusBadge>
                </li>
              ))}
            </ul>
          )}
          {readiness.config_validation ? <DetailGrid title="Config Validation" data={readiness.config_validation} /> : null}
          {readiness.doctor ? <DetailGrid title="Doctor" data={readiness.doctor} /> : null}
          {readiness.launch_check ? <DetailGrid title="Launch Check" data={readiness.launch_check} /> : null}
        </div>
      ) : (
        <p className="text-[12px] text-muted-foreground">Loading readiness dataï¿½</p>
      )}
    </Panel>
    {releasePosture && <ReleasePosturePanel posture={releasePosture} className="mt-4" />}
    </>
  );
}
/* ------------------------------------------------------------------- */

function AutomationSection({
  settings,
  refresh,
  modelCatalog,
  modelCatalogError,
  refreshCatalog,
}: {
  settings?: SettingsData | null;
  refresh: () => Promise<unknown>;
  modelCatalog?: ModelCatalogData | null;
  modelCatalogError: string;
  refreshCatalog: () => Promise<ModelCatalogData>;
}) {
  const auto = settings?.autonomous ?? {};
  const runtime = settings?.runtime_model ?? {};
  const [autoForm, setAutoForm] = useState<Record<string, any>>({});
  const [runtimeForm, setRuntimeForm] = useState<Record<string, any>>({});
  const [busy, setBusy] = useState("");
  const merged = { ...auto, ...autoForm };
  const mergedRuntime = { ...runtime, ...runtimeForm };
  const catalogModels = modelCatalog?.models ?? [];
  const catalogIssue = modelCatalogError || String(modelCatalog?.error ?? "").trim();

  const saveAuto = useCallback(async () => {
    setBusy("auto");
    try {
      await requestJson("/api/settings/autonomous", {
        method: "POST",
        body: JSON.stringify({ ...auto, ...autoForm }),
      });
      setAutoForm({});
      await refresh();
    } finally {
      setBusy("");
    }
  }, [auto, autoForm, refresh]);

  const saveRuntime = useCallback(async () => {
    setBusy("runtime");
    try {
      await requestJson("/api/settings/runtime-model", {
        method: "PUT",
        body: JSON.stringify({ ...runtime, ...runtimeForm }),
      });
      setRuntimeForm({});
      await refresh();
    } finally {
      setBusy("");
    }
  }, [runtime, runtimeForm, refresh]);

  const refreshLmStudio = useCallback(async () => {
    setBusy("catalog");
    try {
      await refreshCatalog();
    } finally {
      setBusy("");
    }
  }, [refreshCatalog]);

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Automation Defaults"
          description="Pipeline behaviour, caps, and browser settings."
          actions={
            <button
              disabled={!!busy}
              onClick={saveAuto}
              className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              <Save className="h-3.5 w-3.5" /> Save
            </button>
          }
        />
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <ToggleField label="Pipeline enabled" checked={merged.enabled ?? true} onChange={(value) => setAutoForm((prev) => ({ ...prev, enabled: value }))} />
          <ToggleField label="Submit enabled" checked={merged.submit_enabled ?? false} onChange={(value) => setAutoForm((prev) => ({ ...prev, submit_enabled: value }))} />
          <NumberField label="Daily submit cap" value={merged.daily_submit_cap ?? 100} onChange={(value) => setAutoForm((prev) => ({ ...prev, daily_submit_cap: value }))} />
          <NumberField label="Per-company daily cap" value={merged.per_company_daily_cap ?? 2} onChange={(value) => setAutoForm((prev) => ({ ...prev, per_company_daily_cap: value }))} />
          <NumberField label="Ready-to-apply threshold" value={merged.ready_to_apply_threshold ?? 10} onChange={(value) => setAutoForm((prev) => ({ ...prev, ready_to_apply_threshold: value }))} />
          <NumberField label="Max open tabs" value={merged.max_open_tabs ?? 6} onChange={(value) => setAutoForm((prev) => ({ ...prev, max_open_tabs: value }))} />
          <SelectField
            label="Browser mode"
            value={merged.browser_mode ?? "headed"}
            options={[{ value: "headed", label: "Headed" }, { value: "headless", label: "Headless" }, { value: "attached", label: "Attached" }]}
            onChange={(value) => setAutoForm((prev) => ({ ...prev, browser_mode: value }))}
          />
          <SelectField
            label="Submit mode"
            value={merged.default_submit_mode ?? "preview_first"}
            options={[{ value: "auto_submit", label: "Auto-submit" }, { value: "preview_first", label: "Preview first" }]}
            onChange={(value) => setAutoForm((prev) => ({ ...prev, default_submit_mode: value }))}
          />
          <SelectField
            label="Captcha strategy"
            value={merged.captcha_strategy ?? "skip"}
            options={[{ value: "skip", label: "Skip" }, { value: "manual", label: "Manual" }, { value: "solve", label: "Solve" }]}
            onChange={(value) => setAutoForm((prev) => ({ ...prev, captcha_strategy: value }))}
          />
        </div>
      </Panel>
      <Panel>
        <PanelHeader
          title="LM Studio Runtime Fallback"
          description="This is the only supported launch provider path. These values backstop the launch router when no explicit role override is saved."
          actions={
            <div className="flex gap-2">
              <button
                disabled={!!busy}
                onClick={refreshLmStudio}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50"
              >
                <RefreshCw className={"h-3.5 w-3.5" + (busy === "catalog" ? " animate-spin" : "")} /> Refresh LM Studio
              </button>
              <button
                disabled={!!busy}
                onClick={saveRuntime}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" /> Save
              </button>
            </div>
          }
        />
        <div className="mb-3 flex flex-wrap gap-2">
          <StatusBadge tone="info">Provider: LM Studio</StatusBadge>
          <StatusBadge tone="info">Transport: local_http</StatusBadge>
          <StatusBadge tone={catalogIssue ? "warning" : "success"}>{catalogIssue ? "Catalog unavailable" : `${catalogModels.length} live model${catalogModels.length === 1 ? "" : "s"}`}</StatusBadge>
        </div>
        {catalogIssue ? (
          <div className="mb-3 rounded-lg border border-[oklch(0.9_0.07_75)] bg-[oklch(0.98_0.02_75)] px-3 py-2 text-[11.5px] text-[oklch(0.4_0.1_75)]">
            LM Studio catalog check failed. You can still save a model ID manually, but launch readiness stays blocked until the local endpoint responds.
          </div>
        ) : null}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <TextField label="Base URL" value={String(mergedRuntime.base_url ?? LMSTUDIO_DEFAULT_HOST)} onChange={(value) => setRuntimeForm((prev) => ({ ...prev, base_url: value }))} />
          <ModelInputField
            label="Default model"
            listId="runtime-model-catalog"
            value={String(mergedRuntime.model ?? "")}
            models={catalogModels}
            onChange={(value) => setRuntimeForm((prev) => ({ ...prev, model: value }))}
            hint={modelCatalog?.base_url ? `Live catalog from ${modelCatalog.base_url}` : "Use a live LM Studio model ID or enter one manually."}
          />
          <NumberField label="Temperature" value={Number(mergedRuntime.temperature ?? 0.2)} step={0.05} onChange={(value) => setRuntimeForm((prev) => ({ ...prev, temperature: value }))} />
          <NumberField label="Max tokens" value={Number(mergedRuntime.max_tokens ?? 8192)} onChange={(value) => setRuntimeForm((prev) => ({ ...prev, max_tokens: value }))} />
          <NumberField
            label="Preferred context window"
            value={Number(mergedRuntime.preferred_context_window ?? 131072)}
            onChange={(value) => setRuntimeForm((prev) => ({ ...prev, preferred_context_window: value }))}
          />
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------- */
/*  ChatGPT Drafting Section                                          */
/* ------------------------------------------------------------------- */

function ChatGPTSection({ settings, refresh }: { settings?: SettingsData | null; refresh: () => Promise<unknown> }) {
  const cfg = settings?.chatgpt_drafting ?? {};
  const [form, setForm] = useState<Record<string, any>>({});
  const [busy, setBusy] = useState("");
  const merged = { ...cfg, ...form };

  const saveCfg = useCallback(async () => {
    setBusy("save");
    try {
      await requestJson("/api/settings/chatgpt-drafting", { method: "POST", body: JSON.stringify({ ...cfg, ...form }) });
      setForm({});
      await refresh();
    } finally {
      setBusy("");
    }
  }, [cfg, form, refresh]);

  const testDraft = useCallback(async () => {
    setBusy("test");
    try {
      await requestJson("/api/chatgpt-drafting/test", { method: "POST", body: JSON.stringify({}) });
      await refresh();
    } finally {
      setBusy("");
    }
  }, [refresh]);

  const launchBrowser = useCallback(async () => {
    setBusy("launch");
    try {
      await requestJson("/api/chatgpt-drafting/browser/launch", { method: "POST", body: JSON.stringify({}) });
    } finally {
      setBusy("");
    }
  }, []);

  return (
    <Panel>
      <PanelHeader
        title="ChatGPT Drafting"
        description="Use ChatGPT in a managed browser for resume and cover-letter drafting. The LM Studio model settings above do not replace this path."
        actions={
          <div className="flex gap-2">
            <button disabled={!!busy} onClick={launchBrowser} className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50">Launch browser</button>
            <button disabled={!!busy} onClick={testDraft} className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50">Test</button>
            <button disabled={!!busy} onClick={saveCfg} className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"><Save className="h-3.5 w-3.5" /> Save</button>
          </div>
        }
      />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <ToggleField label="Enabled" checked={merged.enabled ?? true} onChange={(value) => setForm((prev) => ({ ...prev, enabled: value }))} />
        <ToggleField label="Use temporary chat" checked={merged.use_temporary_chat ?? false} onChange={(value) => setForm((prev) => ({ ...prev, use_temporary_chat: value }))} />
        <ToggleField label="Launch if missing" checked={merged.launch_if_missing ?? true} onChange={(value) => setForm((prev) => ({ ...prev, launch_if_missing: value }))} />
        <TextField label="Resume + Cover Letter GPT URL" value={merged.gpt_url ?? ""} onChange={(value) => setForm((prev) => ({ ...prev, gpt_url: value }))} />
        <TextField label="Job Screening GPT URL" value={merged.screening_url ?? ""} onChange={(value) => setForm((prev) => ({ ...prev, screening_url: value }))} />
        <TextField label="Job Application Answering GPT URL" value={merged.qa_url ?? ""} onChange={(value) => setForm((prev) => ({ ...prev, qa_url: value }))} />
        <SelectField label="Browser mode" value={merged.browser_mode ?? "attached"} options={[{ value: "headed", label: "Headed" }, { value: "headless", label: "Headless" }, { value: "attached", label: "Attached" }]} onChange={(value) => setForm((prev) => ({ ...prev, browser_mode: value }))} />
        <TextField label="Browser CDP URL" value={merged.browser_cdp_url ?? ""} onChange={(value) => setForm((prev) => ({ ...prev, browser_cdp_url: value }))} />
        <NumberField label="Max parallel jobs" value={merged.max_parallel_jobs ?? 10} onChange={(value) => setForm((prev) => ({ ...prev, max_parallel_jobs: value }))} />
        <NumberField label="Timeout (seconds)" value={merged.timeout_seconds ?? 900} onChange={(value) => setForm((prev) => ({ ...prev, timeout_seconds: value }))} />
        <NumberField label="Prompt submit delay (ms)" value={merged.prompt_submit_delay_ms ?? 300} onChange={(value) => setForm((prev) => ({ ...prev, prompt_submit_delay_ms: value }))} />
        <NumberField label="Download timeout (s)" value={merged.download_timeout_seconds ?? 300} onChange={(value) => setForm((prev) => ({ ...prev, download_timeout_seconds: value }))} />
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------- */
/*  Sources Section                                                   */
/* ------------------------------------------------------------------- */

function SourcesSection({ settings, refresh }: { settings?: SettingsData | null; refresh: () => Promise<unknown> }) {
  const portals = settings?.portals ?? {};
  const sources: Record<string, any> = portals.sources ?? {};
  const trackedCompanies: any[] = settings?.tracked_companies ?? [];
  const [form, setForm] = useState<Record<string, Record<string, any>>>({});
  const [busy, setBusy] = useState(false);

  const savePortals = useCallback(async () => {
    setBusy(true);
    try {
      const mergedSources: Record<string, any> = {};
      for (const key of new Set([...Object.keys(sources), ...Object.keys(form)])) {
        mergedSources[key] = { ...sources[key], ...form[key] };
      }
      await requestJson("/api/settings/portals", {
        method: "PUT",
        body: JSON.stringify({ sources: mergedSources, tracked_companies: trackedCompanies }),
      });
      setForm({});
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [sources, form, trackedCompanies, refresh]);

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Sources & Discovery"
          description="Configure portal boards and tracked companies."
          actions={<button disabled={busy} onClick={savePortals} className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"><Save className="h-3.5 w-3.5" /> Save</button>}
        />
        {Object.entries(sources).length > 0 ? (
          <div className="space-y-3">
            {Object.entries(sources).map(([key, src]: [string, any]) => {
              const merged = { ...src, ...form[key] };
              return (
                <div key={key} className="rounded-lg border border-border bg-surface p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-[13px] font-semibold capitalize text-foreground">{key}</span>
                    <ToggleField label="" checked={merged.enabled ?? false} onChange={(value) => setForm((prev) => ({ ...prev, [key]: { ...prev[key], enabled: value } }))} />
                  </div>
                  <div className="mt-2 text-[11.5px] text-muted-foreground">
                    {(merged.boards ?? []).length} board{(merged.boards ?? []).length !== 1 ? "s" : ""}, {(merged.seed_urls ?? []).length} seed URL{(merged.seed_urls ?? []).length !== 1 ? "s" : ""}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-[12px] text-muted-foreground">No sources configured.</p>
        )}
      </Panel>
      <Panel>
        <PanelHeader title="Tracked Companies" description={`${trackedCompanies.length} compan${trackedCompanies.length !== 1 ? "ies" : "y"} being tracked.`} />
        {trackedCompanies.length > 0 ? (
          <ul className="-my-1 divide-y divide-border">
            {trackedCompanies.map((company, index) => (
              <li key={company.name ?? index} className="flex items-center gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="text-[12.5px] font-medium text-foreground">{company.name ?? "-"}</div>
                  <div className="text-[11px] text-muted-foreground">{company.source ?? "-"} ï¿½ {company.careers_url ?? "no URL"}</div>
                </div>
                <StatusBadge tone={company.enabled ? "success" : "neutral"}>{company.enabled ? "on" : "off"}</StatusBadge>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-muted-foreground">No tracked companies.</p>
        )}
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------- */
/*  Models Section                                                    */
/* ------------------------------------------------------------------- */

function ModelsSection({
  settings,
  refresh,
  modelCatalog,
  modelCatalogError,
}: {
  settings?: SettingsData | null;
  refresh: () => Promise<unknown>;
  modelCatalog?: ModelCatalogData | null;
  modelCatalogError: string;
}) {
  const advancedModels = settings?.advanced_models ?? {};
  const profiles = advancedModels.profiles ?? [];
  const launchProfile = advancedModels.launch_profile ?? null;
  const modelStrategy = settings?.model_strategy?.mode ?? "lm_studio_local";
  const runtimeBaseUrl = String(settings?.runtime_model?.base_url ?? LMSTUDIO_DEFAULT_HOST).trim() || LMSTUDIO_DEFAULT_HOST;
  const draftingRenderer = String(settings?.drafting_strategy?.renderer ?? "").trim();
  const lastChecks = settings?.last_model_checks ?? {};
  const catalogModels = modelCatalog?.models ?? [];
  const catalogIssue = modelCatalogError || String(modelCatalog?.error ?? "").trim();
  const duplicateRoles = Object.entries(advancedModels.duplicate_roles ?? {});
  const missingRequiredRoles = advancedModels.missing_required_roles ?? launchProfile?.missing_required_roles ?? [];
  const [busy, setBusy] = useState("");
  const [familyForm, setFamilyForm] = useState<Record<string, { model?: string; provider?: string; base_url?: string; api_key_env?: string }>>({});

  const savedProfileForFamily = useCallback((family: RoutingFamily): ModelProfile | undefined => {
    return family.roles
      .map((role) => profileForRole(profiles, role.role))
      .find((profile): profile is ModelProfile => Boolean(profile));
  }, [profiles]);

  const saveFamily = useCallback(async (family: RoutingFamily) => {
    const savedProfile = savedProfileForFamily(family);
    const currentModel = family.roles
      .map((role) => profileForRole(profiles, role.role)?.model ?? launchRoleFor(launchProfile, role.role)?.model)
      .find(Boolean) ?? "";
    const selectedModel = String(familyForm[family.id]?.model ?? currentModel).trim();
    const selectedProvider = family.remoteAllowed
      ? String(familyForm[family.id]?.provider ?? savedProfile?.provider ?? "lmstudio").trim() || "lmstudio"
      : "lmstudio";
    const selectedTransport = selectedProvider === "lmstudio" ? "local_http" : "remote_http";
    const selectedBaseUrl = String(
      familyForm[family.id]?.base_url
        ?? savedProfile?.base_url
        ?? (selectedTransport === "remote_http" ? OPENROUTER_DEFAULT_BASE_URL : runtimeBaseUrl),
    ).trim() || (selectedTransport === "remote_http" ? OPENROUTER_DEFAULT_BASE_URL : runtimeBaseUrl);
    const selectedApiKeyEnv = selectedTransport === "remote_http"
      ? String(familyForm[family.id]?.api_key_env ?? savedProfile?.api_key_env ?? "OPENROUTER_API_KEY").trim() || "OPENROUTER_API_KEY"
      : "";
    if (!selectedModel) {
      return;
    }
    setBusy(`family-${family.id}`);
    try {
      await requestJson("/api/settings/models/family", {
        method: "POST",
        body: JSON.stringify({
          family: family.id,
          model: selectedModel,
          provider: selectedProvider,
          transport: selectedTransport,
          base_url: selectedBaseUrl,
          api_key_env: selectedTransport === "remote_http" ? selectedApiKeyEnv : undefined,
        }),
      });
      setFamilyForm((prev) => {
        const next = { ...prev };
        delete next[family.id];
        return next;
      });
      await refresh();
    } finally {
      setBusy("");
    }
  }, [familyForm, launchProfile, profiles, refresh, runtimeBaseUrl, savedProfileForFamily]);

  const deleteProfile = useCallback(async (name: string) => {
    setBusy(name);
    try {
      await requestJson("/api/settings/models", { method: "DELETE", body: JSON.stringify({ name }) });
      await refresh();
    } finally {
      setBusy("");
    }
  }, [refresh]);

  const pingProfile = useCallback(async (profile: ModelProfile) => {
    setBusy(`ping-${profile.name}`);
    try {
      await requestJson("/api/settings/models/ping", { method: "POST", body: JSON.stringify({ profile_name: profile.name }) });
      await refresh();
    } finally {
      setBusy("");
    }
  }, [refresh]);

  const installRecommended = useCallback(async () => {
    setBusy("recommended");
    try {
      await requestJson("/api/settings/models/recommended", { method: "POST" });
      await refresh();
    } finally {
      setBusy("");
    }
  }, [refresh]);

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="LM Studio Launch Roles"
          description={launchProfile?.summary ?? `Strategy: ${modelStrategy}`}
          actions={
            <button disabled={!!busy} onClick={installRecommended} className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50"><Zap className="h-3.5 w-3.5" /> Install recommended</button>
          }
        />
        <div className="mb-3 flex flex-wrap gap-2">
          <StatusBadge tone={launchTone(launchProfile?.overall_status)}>{`Launch profile: ${launchProfile?.overall_status ?? "unknown"}`}</StatusBadge>
          <StatusBadge tone="info">Local fallback: LM Studio</StatusBadge>
          <StatusBadge tone="warning">Remote screening/Q&amp;A allowed</StatusBadge>
          <StatusBadge tone={draftingRenderer === "chatgpt_download" ? "accent" : "neutral"}>
            {draftingRenderer === "chatgpt_download" ? "ChatGPT drafting primary" : "LM Studio drafting active"}
          </StatusBadge>
        </div>
        <div className="mb-4 rounded-lg border border-border bg-surface p-3 text-[12px] text-muted-foreground">
          Launch roles still default to LM Studio local HTTP. Screening and question-answering families may also be bound to OpenRouter remote HTTP from this page, while writer roles remain pinned to LM Studio. ChatGPT drafting stays configured separately in the ChatGPT section.
        </div>
        {catalogIssue ? (
          <div className="mb-3 rounded-lg border border-[oklch(0.9_0.07_75)] bg-[oklch(0.98_0.02_75)] px-3 py-2 text-[11.5px] text-[oklch(0.4_0.1_75)]">
            The live LM Studio catalog is not available right now. You can still save a known local model ID, but readiness will remain blocked until the endpoint responds.
          </div>
        ) : null}
        {String(advancedModels.error ?? "").trim() ? (
          <div className="mb-3 rounded-lg border border-[oklch(0.92_0.06_27)] bg-[oklch(0.98_0.02_27)] px-3 py-2 text-[11.5px] text-[oklch(0.45_0.18_27)]">
            Advanced model inspection is degraded: {String(advancedModels.error)}
          </div>
        ) : null}
        {missingRequiredRoles.length > 0 ? (
          <div className="mb-3 rounded-lg border border-[oklch(0.92_0.06_27)] bg-[oklch(0.98_0.02_27)] px-3 py-2 text-[11.5px] text-[oklch(0.45_0.18_27)]">
            Missing required launch roles: {missingRequiredRoles.map(roleLabel).join(", ")}.
          </div>
        ) : null}
        {duplicateRoles.length > 0 ? (
          <div className="mb-3 rounded-lg border border-[oklch(0.9_0.07_75)] bg-[oklch(0.98_0.02_75)] px-3 py-2 text-[11.5px] text-[oklch(0.4_0.1_75)]">
            Duplicate role bindings detected: {duplicateRoles.map(([role, names]) => `${roleLabel(role)} (${names.join(", ")})`).join("; ")}.
          </div>
        ) : null}
        <div className="space-y-3">
          {ROUTING_FAMILIES.map((family) => {
            const savedProfile = savedProfileForFamily(family);
            const currentModel = family.roles
              .map((role) => profileForRole(profiles, role.role)?.model ?? launchRoleFor(launchProfile, role.role)?.model)
              .find(Boolean) ?? String(settings?.runtime_model?.model ?? "");
            const selectedModel = String(familyForm[family.id]?.model ?? currentModel ?? "");
            const selectedProvider = family.remoteAllowed
              ? String(familyForm[family.id]?.provider ?? savedProfile?.provider ?? "lmstudio").trim() || "lmstudio"
              : "lmstudio";
            const selectedTransport = selectedProvider === "lmstudio" ? "local_http" : "remote_http";
            const selectedBaseUrl = String(
              familyForm[family.id]?.base_url
                ?? savedProfile?.base_url
                ?? (selectedTransport === "remote_http" ? OPENROUTER_DEFAULT_BASE_URL : runtimeBaseUrl),
            ).trim() || (selectedTransport === "remote_http" ? OPENROUTER_DEFAULT_BASE_URL : runtimeBaseUrl);
            const selectedApiKeyEnv = selectedTransport === "remote_http"
              ? String(familyForm[family.id]?.api_key_env ?? savedProfile?.api_key_env ?? "OPENROUTER_API_KEY").trim() || "OPENROUTER_API_KEY"
              : "";
            const familyStatuses = family.roles
              .map((role) => launchRoleFor(launchProfile, role.role))
              .filter((item): item is LaunchRoleStatus => Boolean(item));
            const familyTone = familyStatuses.some((item) => item.status === "fail")
              ? "danger"
              : familyStatuses.some((item) => item.status === "warning")
                ? "warning"
                : familyStatuses.length > 0
                  ? "success"
                  : "neutral";

            return (
              <div key={family.id} className="rounded-xl border border-border bg-surface p-4">
                <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h4 className="text-[13px] font-semibold text-foreground">{family.label}</h4>
                      <StatusBadge tone={familyTone}>{familyStatuses.length ? (familyStatuses.every((item) => item.status === "pass") ? "ready" : "needs attention") : "unbound"}</StatusBadge>
                      <StatusBadge tone="info">{providerLabel(selectedProvider)}</StatusBadge>
                      <StatusBadge tone="info">{transportLabel(selectedTransport)}</StatusBadge>
                      {family.id === "drafting" && draftingRenderer === "chatgpt_download" ? <StatusBadge tone="accent">fallback only</StatusBadge> : null}
                    </div>
                    <p className="text-[12px] text-muted-foreground">{family.description}</p>
                    <p className="text-[11px] text-muted-foreground">{family.note}</p>
                    <p className="text-[11px] text-muted-foreground">Base URL: {selectedBaseUrl}</p>
                  </div>
                  <button
                    disabled={busy === `family-${family.id}` || !selectedModel.trim()}
                    onClick={() => void saveFamily(family)}
                    className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" /> Save binding
                  </button>
                </div>
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                  {family.remoteAllowed ? (
                    <SelectField
                      label="Provider"
                      value={selectedProvider}
                      options={[...MODEL_PROVIDER_OPTIONS]}
                      onChange={(value) => setFamilyForm((prev) => ({
                        ...prev,
                        [family.id]: {
                          ...prev[family.id],
                          provider: value,
                          base_url: value === "openrouter"
                            ? (prev[family.id]?.base_url || savedProfile?.base_url || OPENROUTER_DEFAULT_BASE_URL)
                            : runtimeBaseUrl,
                          api_key_env: value === "openrouter"
                            ? (prev[family.id]?.api_key_env || savedProfile?.api_key_env || "OPENROUTER_API_KEY")
                            : "",
                        },
                      }))}
                    />
                  ) : null}
                  <ModelInputField
                    label={family.id === "drafting" ? "Fallback LM Studio model" : selectedTransport === "remote_http" ? "Remote model ID" : "LM Studio model"}
                    listId={`family-${family.id}-catalog`}
                    value={selectedModel}
                    models={selectedTransport === "local_http" ? catalogModels : []}
                    onChange={(value) => setFamilyForm((prev) => ({ ...prev, [family.id]: { ...prev[family.id], model: value } }))}
                    hint={selectedTransport === "local_http"
                      ? (modelCatalog?.base_url ? `Catalog from ${modelCatalog.base_url}` : "Choose a live LM Studio model or enter an ID manually.")
                      : "Enter the remote provider model ID exactly as the provider exposes it."}
                  />
                </div>
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                  <TextField
                    label={selectedTransport === "remote_http" ? "Remote base URL" : "LM Studio base URL"}
                    value={selectedBaseUrl}
                    onChange={(value) => setFamilyForm((prev) => ({ ...prev, [family.id]: { ...prev[family.id], base_url: value } }))}
                  />
                  {selectedTransport === "remote_http" ? (
                    <TextField
                      label="API key env name"
                      value={selectedApiKeyEnv}
                      onChange={(value) => setFamilyForm((prev) => ({ ...prev, [family.id]: { ...prev[family.id], api_key_env: value } }))}
                    />
                  ) : (
                    <div className="rounded-lg border border-border bg-card px-3 py-2 text-[11px] text-muted-foreground">
                      LM Studio stays on local HTTP and does not use an API key env reference here.
                    </div>
                  )}
                </div>
                <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-3">
                  {family.roles.map((role) => {
                    const profile = profileForRole(profiles, role.role);
                    const status = launchRoleFor(launchProfile, role.role);
                    return (
                      <div key={role.role} className="rounded-lg border border-border bg-card p-3">
                        <div className="flex items-center justify-between gap-2">
                          <div className="text-[12px] font-medium text-foreground">{role.label}</div>
                          <StatusBadge tone={roleTone(status?.status)}>{status?.status ?? profile?.status ?? "unknown"}</StatusBadge>
                        </div>
                        <div className="mt-1 text-[11px] text-muted-foreground">{profile?.name ?? status?.profile_name ?? "No saved binding"}</div>
                        <div className="mt-1 text-[11px] text-muted-foreground">{profile?.model ?? status?.model ?? "No model selected"}</div>
                        {status?.issues && status.issues.length > 0 ? (
                          <div className="mt-2 text-[10.5px] text-[oklch(0.45_0.18_27)]">{status.issues.join(" ")}</div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </Panel>
      <Panel>
        <PanelHeader title="Resolved Profile Inventory" description={`${profiles.length} model profile${profiles.length === 1 ? "" : "s"} currently active or inferred for the launch router.`} />
        {profiles.length > 0 ? (
          <ul className="space-y-2">
            {profiles.map((profile) => {
              const lastCheck = lastChecks[profile.name];
              return (
                <li key={profile.name} className="flex items-start gap-3 rounded-lg border border-border bg-surface p-3">
                  <Bot className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="text-[13px] font-medium text-foreground">{profile.name}</div>
                      <StatusBadge tone={roleTone(profile.status === "ok" ? "pass" : profile.status)}>{profile.status ?? "unknown"}</StatusBadge>
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      {roleLabel(profile.role)} ï¿½ {providerLabel(profile.provider)} ï¿½ {transportLabel(profile.transport)} ï¿½ {profile.model ?? profile.model_id ?? "-"}
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">{profile.base_url ?? "No base URL configured"}</div>
                    {lastCheck ? (
                      <div className="mt-1 text-[10.5px] text-muted-foreground">
                        Last check {formatDate(lastCheck.checked_at)} ï¿½ {String(lastCheck.classification ?? (lastCheck.ok ? "ok" : "unknown"))}
                      </div>
                    ) : null}
                    {profile.issues && profile.issues.length > 0 ? (
                      <div className="mt-2 text-[10.5px] text-[oklch(0.45_0.18_27)]">{profile.issues.join(" ")}</div>
                    ) : null}
                  </div>
                  <button disabled={busy === `ping-${profile.name}`} onClick={() => void pingProfile(profile)} className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-border bg-card px-2 text-[11px] font-medium hover:bg-muted disabled:opacity-50"><Activity className="h-3 w-3" /> Ping</button>
                  <button disabled={busy === profile.name} onClick={() => void deleteProfile(profile.name)} className="inline-flex h-7 items-center justify-center gap-1 rounded-lg border border-[oklch(0.45_0.18_27)] px-2 text-[11px] font-medium text-[oklch(0.45_0.18_27)] hover:bg-[oklch(0.45_0.18_27)]/10 disabled:opacity-50"><Trash2 className="h-3 w-3" /> Delete</button>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-[12px] text-muted-foreground">No launch-role bindings are available yet.</p>
        )}
      </Panel>
    </div>
  );
}

function PortabilitySection({ refresh }: { refresh: () => Promise<unknown> }) {
  const [busy, setBusy] = useState<"" | "export" | "import">("");
  const [exportText, setExportText] = useState("");
  const [importText, setImportText] = useState("");
  const [message, setMessage] = useState("");

  const loadExport = useCallback(async () => {
    setBusy("export");
    setMessage("");
    try {
      const payload = await requestJson("/api/settings/export");
      setExportText(JSON.stringify(payload.bundle ?? {}, null, 2));
      setMessage("Loaded the current non-personal settings bundle.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Failed to export settings.");
    } finally {
      setBusy("");
    }
  }, []);

  const applyImport = useCallback(async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(importText);
    } catch {
      setMessage("Import bundle is not valid JSON.");
      return;
    }
    setBusy("import");
    setMessage("");
    try {
      const payload = await requestJson("/api/settings/import", {
        method: "POST",
        body: JSON.stringify({ bundle: parsed }),
      });
      setExportText(JSON.stringify(payload.bundle ?? {}, null, 2));
      setMessage(`Imported ${Array.isArray(payload.sections) && payload.sections.length ? payload.sections.join(", ") : "settings"}.`);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Failed to import settings.");
    } finally {
      setBusy("");
    }
  }, [importText, refresh]);

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Non-Personal Settings Bundle"
          description="Export or import sources, automation, runtime-model, ChatGPT drafting, and model-profile settings without moving candidate data or secrets."
          actions={
            <button
              disabled={busy !== ""}
              onClick={() => void loadExport()}
              className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[12px] font-medium hover:bg-muted disabled:opacity-50"
            >
              <ArrowLeftRight className="h-3.5 w-3.5" /> Load export
            </button>
          }
        />
        <div className="mb-3 rounded-lg border border-border bg-surface p-3 text-[12px] text-muted-foreground">
          This bundle excludes profile facts, answer memory, dossiers, runtime history, exports, and secret values. API key env names are preserved so another local workspace can point at the same secret references.
        </div>
        {message ? <div className="mb-3 rounded-lg border border-border bg-card px-3 py-2 text-[11.5px] text-muted-foreground">{message}</div> : null}
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          <TextAreaField
            label="Current export bundle"
            value={exportText}
            onChange={setExportText}
            readOnly
            rows={18}
          />
          <TextAreaField
            label="Import bundle"
            value={importText}
            onChange={setImportText}
            rows={18}
          />
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            disabled={busy !== "" || !importText.trim()}
            onClick={() => void applyImport()}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" /> Apply import
          </button>
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------- */
/*  Shared helpers and form primitives                                */
/* ------------------------------------------------------------------- */

function profileForRole(profiles: ModelProfile[], role: string) {
  return profiles.find((profile) => profile.role === role);
}

function launchRoleFor(launchProfile: LaunchProfile | null | undefined, role: string) {
  return (launchProfile?.roles ?? []).find((item) => item.role === role);
}

function roleLabel(role?: string) {
  if (!role) {
    return "Unbound role";
  }
  for (const family of ROUTING_FAMILIES) {
    const match = family.roles.find((item) => item.role === role);
    if (match) {
      return match.label;
    }
  }
  return role.replace(/_/g, " ");
}

function launchTone(status?: string): Tone {
  if (status === "pass") {
    return "success";
  }
  if (status === "warning") {
    return "warning";
  }
  if (status === "fail" || status === "blocked") {
    return "danger";
  }
  return "neutral";
}

function roleTone(status?: string): Tone {
  if (status === "pass" || status === "ok") {
    return "success";
  }
  if (status === "warning") {
    return "warning";
  }
  if (status === "fail" || status === "blocked") {
    return "danger";
  }
  return "neutral";
}

function modelLabel(model: ModelCatalogEntry) {
  return model.label ?? model.name ?? model.id;
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block">
      {label ? <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span> : null}
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground placeholder:text-muted-foreground" />
    </label>
  );
}

function TextAreaField({
  label,
  value,
  onChange,
  readOnly = false,
  rows = 10,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  readOnly?: boolean;
  rows?: number;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        readOnly={readOnly}
        rows={rows}
        className="w-full rounded-lg border border-border bg-card px-2.5 py-2 text-[12px] text-foreground placeholder:text-muted-foreground"
      />
    </label>
  );
}

function ModelInputField({
  label,
  value,
  onChange,
  listId,
  models,
  hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  listId: string;
  models: ModelCatalogEntry[];
  hint?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <input type="text" list={listId} value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground placeholder:text-muted-foreground" />
      <datalist id={listId}>
        {models.map((model) => (<option key={model.id} value={model.id}>{modelLabel(model)}</option>))}
      </datalist>
      {hint ? <span className="mt-1 block text-[10.5px] text-muted-foreground">{hint}</span> : null}
    </label>
  );
}

function NumberField({ label, value, step, onChange }: { label: string; value: number; step?: number; onChange: (v: number) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <input type="number" value={value} step={step} onChange={(e) => onChange(Number(e.target.value))} className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] tabular-nums text-foreground" />
    </label>
  );
}

function SelectField({ label, value, options, onChange }: { label: string; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px] text-foreground">
        {options.map((option) => (<option key={option.value} value={option.value}>{option.label}</option>))}
      </select>
    </label>
  );
}

function ToggleField({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2.5">
      <button type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)} className={"relative h-5 w-9 shrink-0 rounded-full transition-colors " + (checked ? "bg-primary" : "bg-muted")}>
        <span className={"absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform " + (checked ? "translate-x-4" : "translate-x-0")} />
      </button>
      {label ? <span className="text-[12.5px] text-foreground">{label}</span> : null}
    </label>
  );
}

function DetailGrid({ title, data }: { title: string; data: Record<string, any> }) {
  return (
    <div className="mt-3">
      <div className="mb-2 text-[11.5px] font-medium text-muted-foreground">{title}</div>
      <div className="rounded-lg border border-border bg-surface p-3">
        {Object.entries(data).map(([key, value]) => (
          <div key={key} className="flex items-baseline justify-between py-1 text-[12px]">
            <span className="text-muted-foreground">{key}</span>
            <span className="font-medium text-foreground">{typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

