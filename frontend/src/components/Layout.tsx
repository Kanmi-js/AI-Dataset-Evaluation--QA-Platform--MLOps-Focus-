import { Link, Outlet } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export function Layout() {
  const { user, logout } = useAuth();

  return (
    <div className="flex min-h-screen flex-col bg-pitch-dark text-zinc-100 selection:bg-volt selection:text-pitch-dark">
      {/* Top Navigation */}
      <header className="sticky top-0 z-50 border-b border-pitch-border bg-pitch-card/80 backdrop-blur-md px-6 py-3">
        <div className="mx-auto flex max-w-7xl items-center justify-between">
          {/* Brand */}
          <Link to="/tasks" className="flex items-center gap-3 group">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-volt text-pitch-dark font-mono font-black text-sm shadow-volt-glow">
              ⚡
            </div>
            <div className="flex items-center gap-2">
              <span className="text-base font-extrabold tracking-tight text-white font-mono">
                Terrain<span className="text-volt">AI</span>
              </span>
              <span className="rounded-full border border-volt/30 bg-volt/10 px-2 py-0.5 text-[10px] font-mono text-volt tracking-wider">
                TACTICAL VISION HUD
              </span>
            </div>
          </Link>

          {/* User & Actions */}
          <div className="flex items-center gap-4 text-xs font-mono">
            <div className="flex items-center gap-2 text-zinc-400 bg-pitch-dark border border-pitch-border px-2.5 py-1 rounded-md">
              <span className="w-1.5 h-1.5 rounded-full bg-volt animate-pulse" />
              <span>{user?.email || "analyst@terrain.ai"}</span>
            </div>
            <button
              onClick={logout}
              className="text-zinc-500 hover:text-vector-crimson transition-colors cursor-pointer"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      {/* Page Content */}
      <main className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}