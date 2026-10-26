import { useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { ApiKey, NewApiKey, Role, User } from "../api/types";
import { useUser } from "../auth/AuthContext";
import { Badge, ConfirmButton, EmptyState, ErrorBanner, Loading, Notice, Time } from "../components/ui";
import { useAsync } from "../components/useAsync";

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,99}$/;

export function Team() {
  const me = useUser();
  return me.role === "admin" ? <AdminTeam me={me} /> : <MyKeys me={me} />;
}

function MyKeys({ me }: { me: User }) {
  const keys = useAsync(() => api.myKeys());
  return (
    <section>
      <header className="page-head">
        <div>
          <h2>My account</h2>
          <p className="page-sub">Your role and the API keys issued to you.</p>
        </div>
      </header>
      <div className="card">
        <div className="user-id">
          <span className="avatar" aria-hidden="true">{me.name.slice(0, 1).toUpperCase()}</span>
          <p><strong>{me.name}</strong> <Badge tone="neutral">{me.role}</Badge></p>
        </div>
        <Notice>API keys are issued and revoked by gateway admins. Ask an admin if you need a new key or want one revoked.</Notice>
      </div>
      <h3 className="section-title">My API keys</h3>
      {keys.error ? <ErrorBanner error={keys.error} onRetry={keys.reload} /> : !keys.data ? <Loading /> : <KeyTable keys={keys.data} />}
    </section>
  );
}

function AdminTeam({ me }: { me: User }) {
  const users = useAsync(() => api.users());
  const [newKey, setNewKey] = useState<{ userId: string; user: string; key: NewApiKey } | null>(null);
  const [flash, setFlash] = useState<string>();
  const [error, setError] = useState<ApiError>();

  async function act(fn: () => Promise<unknown>, ok: string) {
    setError(undefined);
    setFlash(undefined);
    try {
      await fn();
      setFlash(ok);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      await users.reload();
    }
  }

  return (
    <section>
      <header className="page-head">
        <div>
          <h2>Team &amp; API keys</h2>
          <p className="page-sub">Users, roles and the gateway API keys issued to them.</p>
        </div>
      </header>
      {/* Fallback: never hide a just-issued key if its user's card is not on screen (e.g. the list failed to reload). */}
      {newKey && (users.error || !users.data?.some((u) => u.id === newKey.userId)) &&
        <NewKeyPanel userName={newKey.user} apiKey={newKey.key} onDone={() => setNewKey(null)} />}
      {flash && <Notice kind="success">{flash}</Notice>}
      {error && <ErrorBanner error={error} />}
      <CreateUserForm onCreated={(name) => act(async () => undefined, `Created ${name}.`)} />
      {users.error ? <ErrorBanner error={users.error} onRetry={users.reload} /> : !users.data ? <Loading /> :
        users.data.length === 0 ? <EmptyState>No users.</EmptyState> :
          users.data.map((u) => (
            <UserCard key={u.id} u={u} isSelf={u.id === me.id}
              onRole={(role) => act(() => api.updateUser(u.id, { role }), `${u.name} is now ${role}.`)}
              onDisabled={(disabled) => act(() => api.updateUser(u.id, { disabled }), `${u.name} ${disabled ? "disabled" : "enabled"}.`)}
              onKeyCreated={(key) => setNewKey({ userId: u.id, user: u.name, key })}
              onError={setError}>
              {/* Shown inside the card of the user it was issued for, right after the Issue key form. */}
              {newKey?.userId === u.id && <NewKeyPanel userName={newKey.user} apiKey={newKey.key} onDone={() => setNewKey(null)} />}
            </UserCard>
          ))}
    </section>
  );
}

function UserCard({ u, isSelf, onRole, onDisabled, onKeyCreated, onError, children }: {
  u: User; isSelf: boolean; onRole: (r: Role) => void; onDisabled: (d: boolean) => void;
  onKeyCreated: (k: NewApiKey) => void; onError: (e: ApiError) => void; children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <article className="card user-card" data-testid={`user-${u.name}`}>
      <div className="card-head">
        <div className="user-id">
          <span className="avatar" aria-hidden="true">{u.name.slice(0, 1).toUpperCase()}</span>
          <div>
            <h3>{u.name} {isSelf && <span className="muted small">(you)</span>}</h3>
            <span className="badges">
              <Badge tone={u.role === "admin" ? "warn" : "neutral"}>{u.role}</Badge>{" "}
              {u.disabled ? <Badge tone="bad">disabled</Badge> : <Badge tone="ok">active</Badge>}
            </span>
          </div>
        </div>
        <div className="row wrap">
          <button className="secondary" onClick={() => onRole(u.role === "admin" ? "member" : "admin")}>
            Make {u.role === "admin" ? "member" : "admin"}
          </button>
          {u.disabled
            ? <button className="secondary" onClick={() => onDisabled(false)}>Enable</button>
            : <ConfirmButton label="Disable" confirmLabel={`Disable ${u.name}`} onConfirm={() => onDisabled(true)} />}
          <button className="secondary" onClick={() => setOpen((v) => !v)}>{open ? "Hide keys" : "Keys"}</button>
        </div>
      </div>
      {open && <UserKeys user={u} onKeyCreated={onKeyCreated} onError={onError} />}
      {children}
    </article>
  );
}

function UserKeys({ user, onKeyCreated, onError }: { user: User; onKeyCreated: (k: NewApiKey) => void; onError: (e: ApiError) => void }) {
  const keys = useAsync(() => api.userKeys(user.id), [user.id]);
  const [label, setLabel] = useState("");
  async function create(e: FormEvent) {
    e.preventDefault();
    const name = label.trim() || "default";
    if (!NAME_RE.test(name)) return onError(new ApiError(400, "validation", "Key name: letters, digits, space, . _ @ -"));
    try {
      onKeyCreated(await api.createKey(user.id, name));
      setLabel("");
    } catch (err) {
      onError(err as ApiError);
    } finally {
      await keys.reload();
    }
  }
  async function revoke(k: ApiKey) {
    try {
      await api.revokeKey(k.id);
    } catch (err) {
      onError(err as ApiError);
    } finally {
      await keys.reload();
    }
  }
  return (
    <div className="nested">
      {keys.error ? <ErrorBanner error={keys.error} /> : !keys.data ? <Loading /> : <KeyTable keys={keys.data} onRevoke={revoke} />}
      <form className="row wrap key-form" onSubmit={create} aria-label={`New key for ${user.name}`}>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="key label (e.g. laptop)" aria-label="Key label" />
        <button type="submit">Issue key</button>
      </form>
    </div>
  );
}

export function KeyTable({ keys, onRevoke }: { keys: ApiKey[]; onRevoke?: (k: ApiKey) => void }) {
  if (keys.length === 0) return <EmptyState>No keys.</EmptyState>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Key</th><th>Label</th><th>Created</th><th>Last used</th><th>Status</th>{onRevoke && <th><span className="sr-only">Actions</span></th>}</tr></thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id}>
              <td className="mono nowrap">{k.prefix}</td>
              <td>{k.name}</td>
              <td><Time value={k.created_at} /></td>
              <td><Time value={k.last_used_at} /></td>
              <td>{k.revoked_at ? <Badge tone="bad">revoked</Badge> : <Badge tone="ok">active</Badge>}</td>
              {onRevoke && <td className="actions">{!k.revoked_at && <ConfirmButton label="Revoke" confirmLabel="Revoke now" onConfirm={() => onRevoke(k)} />}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Shows a newly created raw key exactly once. Held in React state only; cleared on dismiss. */
export function NewKeyPanel({ userName, apiKey, onDone }: { userName: string; apiKey: NewApiKey; onDone: () => void }) {
  const [copied, setCopied] = useState<"no" | "yes" | "failed">("no");
  async function copy() {
    try {
      await navigator.clipboard.writeText(apiKey.key);
      setCopied("yes");
    } catch {
      setCopied("failed");
    }
  }
  return (
    <div className="card highlight key-reveal" role="dialog" aria-label="New API key">
      <h3>New API key for {userName}</h3>
      <Notice kind="warning">
        This is the only time this key is shown. Copy it now and give it to {userName} through a secure channel.
        It is not stored anywhere in the dashboard.
      </Notice>
      <input className="key-value" readOnly value={apiKey.key} aria-label="New API key value" onFocus={(e) => e.target.select()} />
      <div className="row wrap">
        <button onClick={copy}>{copied === "yes" ? "Copied" : "Copy key"}</button>
        <button className="secondary" onClick={onDone}>I have stored it — close</button>
      </div>
      {copied === "failed" && <p className="error-text small">Copy failed; select the field and copy manually.</p>}
    </div>
  );
}

function CreateUserForm({ onCreated }: { onCreated: (name: string) => Promise<void> }) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [error, setError] = useState<string>();
  async function submit(e: FormEvent) {
    e.preventDefault();
    const n = name.trim();
    if (!NAME_RE.test(n)) return setError("Name: 1-100 characters; letters, digits, space, . _ @ -");
    try {
      setError(undefined);
      await api.createUser(n, role);
      setName("");
      await onCreated(n);
    } catch (err) {
      setError((err as ApiError).message);
    }
  }
  return (
    <form className="card row wrap create-user" onSubmit={submit} aria-label="Create user">
      <h3>Add a user</h3>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="new user name" aria-label="New user name" />
      <select value={role} onChange={(e) => setRole(e.target.value as Role)} aria-label="Role">
        <option value="member">member</option>
        <option value="admin">admin</option>
      </select>
      <button type="submit">Create user</button>
      {error && <ErrorBanner error={error} />}
    </form>
  );
}
