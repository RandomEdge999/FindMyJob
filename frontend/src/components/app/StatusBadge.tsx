import { cn } from "@/lib/utils";

export type Tone = "neutral" | "success" | "warning" | "danger" | "info" | "accent";

const toneStyles: Record<Tone, string> = {
  neutral: "bg-muted text-foreground/75 border-border",
  success: "bg-primary-soft text-primary-soft-foreground border-primary-soft",
  warning: "bg-[oklch(0.95_0.06_75)] text-[oklch(0.4_0.1_75)] border-[oklch(0.9_0.07_75)]",
  danger: "bg-[oklch(0.96_0.04_27)] text-[oklch(0.45_0.18_27)] border-[oklch(0.92_0.06_27)]",
  info: "bg-[oklch(0.95_0.04_240)] text-[oklch(0.42_0.12_240)] border-[oklch(0.9_0.05_240)]",
  accent: "bg-accent text-accent-foreground border-accent",
};

export function StatusBadge({
  tone = "neutral",
  children,
  className,
  dot = false,
}: {
  tone?: Tone;
  children: React.ReactNode;
  className?: string;
  dot?: boolean;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-full border px-2.5 py-1 text-[10.5px] font-semibold leading-none whitespace-nowrap",
        toneStyles[tone],
        className,
      )}
    >
      {dot ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-70" /> : null}
      {children}
    </span>
  );
}
