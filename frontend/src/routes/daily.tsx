import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/daily")({
  beforeLoad: () => { throw redirect({ to: "/autopilot" }); },
});
