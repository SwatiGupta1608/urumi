import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorBanner, Notice } from "../components/ui";

const KEY_RE = /^urumi_[0-9a-f]{12}_[A-Za-z0-9_-]{43}$/;

export function Login({ notice }: { notice?: string }) {
  const { login } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const value = key.trim();
    if (!KEY_RE.test(value)) {
      setError("That doesn't look like a gateway API key (urumi_<12 hex>_<43 chars>).");
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await login(value);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
    } finally {
      // The key is only needed for this one request; drop it from memory.
      setKey("");
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form onSubmit={submit} className="card" aria-label="Sign in">
        <h1>Urumi Gateway</h1>
        <p className="muted">
          Sign in with <strong>your own</strong> gateway API key. It is sent once to the dashboard server, kept there for
          your session, and never stored in this browser.
        </p>
        {notice && <Notice kind="warning">{notice}</Notice>}
        {error && <ErrorBanner error={error} />}
        <label htmlFor="api-key">API key</label>
        <input
          id="api-key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="urumi_…"
          required
        />
        <button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted small">No key? Ask a gateway admin to issue one for you.</p>
      </form>
    </main>
  );
}
