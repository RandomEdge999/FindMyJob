import { TopNav } from "./TopNav";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <TopNav />
      <main className="mx-auto w-full min-h-[calc(100vh-3.5rem)] max-w-[1280px] px-4 pb-6 pt-4 md:px-6 lg:px-8">
        {children}
      </main>
    </div>
  );
}
