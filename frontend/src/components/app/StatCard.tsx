import { ArrowUpRight, ArrowDownRight } from "lucide-react";
import { cn } from "@/lib/utils";

export function StatCard({
  label,
  value,
  delta,
  hint,
  featured = false,
}: {
  label: string;
  value: string | number;
  delta?: number;
  hint?: string;
  featured?: boolean;
}) {
  const positive = (delta ?? 0) >= 0;
  return (
    <div
      className={cn(
        "flex flex-col rounded-xl border p-3.5 shadow-card transition-colors",
        featured
          ? "border-transparent bg-primary text-primary-foreground"
          : "border-border bg-card text-foreground",
      )}
    >
      <span
        className={cn(
          "text-[11.5px] font-medium",
          featured ? "text-primary-foreground/80" : "text-muted-foreground",
        )}
      >
        {label}
      </span>
      <div className="mt-1.5 text-[24px] font-bold leading-none tracking-tight tabular-nums">
        {value}
      </div>
      {(delta !== undefined || hint) && (
        <div
          className={cn(
            "mt-1.5 flex items-center gap-1.5 text-[11px]",
            featured ? "text-primary-foreground/75" : "text-muted-foreground",
          )}
        >
          {delta !== undefined ? (
            <>
              {positive ? (
                <ArrowUpRight className="h-3 w-3" />
              ) : (
                <ArrowDownRight className="h-3 w-3" />
              )}
              <span className="font-semibold tabular-nums">{Math.abs(delta)}</span>
              <span>vs last week</span>
            </>
          ) : null}
          {hint ? <span>{hint}</span> : null}
        </div>
      )}
    </div>
  );
}
