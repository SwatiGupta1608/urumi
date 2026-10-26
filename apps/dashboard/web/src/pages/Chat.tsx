import { Fragment, useEffect, useRef, useState, type FormEvent } from "react";
import type { ChatApi, ChatState, ChatStatus, PendingAction, ToolActivity } from "../api/chat";
import { ApiError, chatApi } from "../api/client";
import { Badge, EmptyState, ErrorBanner, Loading, Notice } from "../components/ui";

const EXAMPLES = [
  "List the 5 newest products in the store.",
  "Show my team's notes.",
  "What are the most recent orders and their status?",
  "List recent commits in SwatiGupta1608/urumi.",
];

const EMPTY: ChatState = { messages: [], activity: [], pending: null, busy: false };

function statusBadge(s: ToolActivity["status"]) {
  const map: Record<ToolActivity["status"], ["ok" | "bad" | "warn" | "neutral", string]> = {
    running: ["neutral", "running"], success: ["ok", "success"], failure: ["bad", "failed"],
    awaiting_confirmation: ["warn", "needs confirmation"], cancelled: ["neutral", "cancelled"], expired: ["neutral", "expired"],
  };
  const [tone, label] = map[s] ?? ["neutral", s];
  return <Badge tone={tone}>{label}</Badge>;
}

/** Compact one-line rendering of sanitised arguments (plain text, never HTML). */
function argText(args: Record<string, unknown>): string {
  const s = JSON.stringify(args);
  return s === "{}" ? "no arguments" : s.length > 160 ? s.slice(0, 160) + "…" : s;
}

export function Chat({ chat = chatApi }: { chat?: ChatApi }) {
  const [status, setStatus] = useState<ChatStatus>();
  const [state, setState] = useState<ChatState>(EMPTY);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"send" | "confirm" | "cancel" | "reset" | null>(null);
  const [error, setError] = useState<ApiError | string>();
  const [loaded, setLoaded] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([chat.status(), chat.state()])
      .then(([st, conv]) => { if (alive) { setStatus(st); setState(conv); } })
      .catch((e) => alive && setError(e instanceof ApiError ? e : String(e)))
      .finally(() => alive && setLoaded(true));
    return () => { alive = false; };
  }, [chat]);

  useEffect(() => { endRef.current?.scrollIntoView?.({ block: "end" }); }, [state.messages.length, busy]);

  async function run(kind: NonNullable<typeof busy>, fn: () => Promise<ChatState>, onFail?: () => void) {
    setBusy(kind);
    setError(undefined);
    try {
      setState(await fn());
    } catch (e) {
      setError(e instanceof ApiError ? e : String(e));
      onFail?.();
      // Re-sync (e.g. an action that expired or was already handled).
      chat.state().then(setState).catch(() => undefined);
    } finally {
      setBusy(null);
    }
  }

  function send(e?: FormEvent, override?: string) {
    e?.preventDefault();
    const msg = (override ?? text).trim();
    if (!msg || busy) return;
    setText("");
    void run("send", () => chat.send(msg), () => setText(msg));
  }

  const available = status?.available ?? false;
  const pending = state.pending;

  return (
    <section className="chat">
      <header className="page-head">
        <h2>Chat</h2>
        <button className="secondary" disabled={!!busy || state.messages.length === 0}
          onClick={() => void run("reset", () => chat.reset())}>New conversation</button>
      </header>
      {!loaded ? <Loading /> : status && !available && (
        <Notice kind="warning">Chat is unavailable: {status.reason ?? "no language model is configured."}</Notice>
      )}
      {available && status?.model && <p className="muted small">Model: <span className="mono">{status.model}</span>. Tools run with your permissions; changes ask for confirmation.</p>}
      {error && <ErrorBanner error={error} />}

      <div className="chat-grid">
        <div className="card chat-log" aria-label="Conversation">
          {state.messages.length === 0 && loaded && (
            <div className="examples">
              <EmptyState>Ask about the store, the team's notes or GitHub. For example:</EmptyState>
              <div className="row wrap">
                {EXAMPLES.map((ex) => (
                  <button key={ex} className="secondary small" disabled={!available || !!busy} onClick={() => send(undefined, ex)}>{ex}</button>
                ))}
              </div>
            </div>
          )}
          {state.messages.map((m) => (
            <div key={m.id} className={`msg ${m.role}`} data-testid={`msg-${m.role}`}>
              <span className="who-label">{m.role === "user" ? "You" : m.role === "assistant" ? "Assistant" : "Note"}</span>
              <p className="msg-text">{m.text}</p>
            </div>
          ))}
          {busy === "send" && <Loading label="Thinking… the assistant may call tools." />}
          {busy === "confirm" && <Loading label="Running the confirmed action…" />}

          {pending && (
            <PendingCard action={pending} disabled={!!busy}
              onConfirm={() => void run("confirm", () => chat.confirm(pending))}
              onCancel={() => void run("cancel", () => chat.cancel(pending))} />
          )}
          <div ref={endRef} />
          <form className="row composer" onSubmit={send}>
            <input value={text} onChange={(e) => setText(e.target.value)} maxLength={4000}
              placeholder={!available ? "Chat unavailable" : pending ? "Confirm or cancel the action first" : "Ask something…"}
              disabled={!available || !!busy || !!pending} aria-label="Message" />
            <button type="submit" disabled={!available || !!busy || !!pending || !text.trim()}>Send</button>
          </form>
        </div>

        <aside className="card activity" aria-label="Tool activity">
          <h3>Tool activity</h3>
          {state.activity.length === 0 ? <EmptyState>Tool calls appear here as the assistant works.</EmptyState> : (
            <ul className="activity-list">
              {state.activity.map((a) => (
                <li key={a.id} data-testid="activity">
                  <div className="row space wrap"><span className="mono">{a.tool}</span>{statusBadge(a.status)}</div>
                  <div className="muted small mono break">{argText(a.args)}</div>
                  {a.summary && <div className="small msg-text">{a.summary}</div>}
                  {a.duration_ms != null && <div className="muted small">{a.duration_ms} ms</div>}
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </section>
  );
}

function PendingCard({ action, disabled, onConfirm, onCancel }:
  { action: PendingAction; disabled: boolean; onConfirm: () => void; onCancel: () => void }) {
  const [clicked, setClicked] = useState(false);
  return (
    <div className="card highlight" role="dialog" aria-label="Confirm action">
      <h3>Confirm this action?</h3>
      <p>
        The assistant wants to run <span className="mono">{action.tool}</span>
        {action.title ? <> ({action.title})</> : null}. {action.destructive && <Badge tone="bad">destructive</Badge>}
      </p>
      {action.description && <p className="muted small msg-text">{action.description}</p>}
      <dl className="kv">
        {Object.entries(action.args).length === 0 ? <><dt>Arguments</dt><dd>none</dd></> :
          Object.entries(action.args).map(([k, v]) => (
            <Fragment key={k}><dt className="mono">{k}</dt><dd className="mono break">{typeof v === "string" ? v : JSON.stringify(v)}</dd></Fragment>
          ))}
      </dl>
      <p className="muted small">This can change real data (and may send emails or adjust stock). Expires in about {Math.ceil(action.expires_in / 60)} min.</p>
      <div className="row">
        <button disabled={disabled || clicked} onClick={() => { setClicked(true); onConfirm(); }}>Confirm</button>
        <button className="secondary" disabled={disabled || clicked} onClick={() => { setClicked(true); onCancel(); }}>Cancel</button>
      </div>
    </div>
  );
}
