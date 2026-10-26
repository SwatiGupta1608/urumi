import { useEffect, useState } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Loading } from "./components/ui";
import { Audit } from "./pages/Audit";
import { Chat } from "./pages/Chat";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Team } from "./pages/Team";
import { Upstreams } from "./pages/Upstreams";

type Page = "overview" | "upstreams" | "team" | "audit" | "chat";

// Navigation only; the gateway enforces every permission server-side.
const PAGES: { id: Page; label: string; adminOnly: boolean }[] = [
  { id: "overview", label: "Overview", adminOnly: false },
  { id: "upstreams", label: "Upstreams", adminOnly: true },
  { id: "team", label: "Team & keys", adminOnly: false },
  { id: "audit", label: "Audit log", adminOnly: true },
  { id: "chat", label: "Chat", adminOnly: false },
];

function currentPage(): Page {
  const h = window.location.hash.replace(/^#\/?/, "") as Page;
  return PAGES.some((p) => p.id === h) ? h : "overview";
}

function Shell() {
  const { state, logout } = useAuth();
  const [page, setPage] = useState<Page>(currentPage);
  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (state.status === "loading") return <main className="login"><Loading label="Checking session…" /></main>;
  if (state.status === "anonymous") return <Login notice={state.notice} />;

  const isAdmin = state.user.role === "admin";
  const visible = PAGES.filter((p) => !p.adminOnly || isAdmin);
  const active = visible.some((p) => p.id === page) ? page : "overview";

  return (
    <div className="app">
      <header className="sidebar">
        <strong className="brand"><span className="brand-mark" aria-hidden="true">U</span>Urumi Gateway</strong>
        <nav aria-label="Main">
          {visible.map((p) => (
            <a key={p.id} href={`#/${p.id}`} aria-current={active === p.id ? "page" : undefined}>{p.label}</a>
          ))}
        </nav>
        <div className="who">
          <span className="avatar" aria-hidden="true">{state.user.name.slice(0, 1).toUpperCase()}</span>
          <span className="who-text">
            <strong>{state.user.name}</strong> <span className="muted small">({state.user.role})</span>
          </span>
          <button className="link" onClick={() => void logout()}>Sign out</button>
        </div>
      </header>
      <main className="content">
        {active === "overview" && <Overview />}
        {active === "upstreams" && <Upstreams />}
        {active === "team" && <Team />}
        {active === "audit" && <Audit />}
        {active === "chat" && <Chat />}
      </main>
    </div>
  );
}

export function App() {
  return <AuthProvider><Shell /></AuthProvider>;
}
