import { useState, type FormEvent } from "react";
import { placeholderChatApi, type ChatApi, type ChatMessage, type ToolActivity } from "../api/chat";
import { EmptyState, ErrorBanner, Notice } from "../components/ui";

/** Layout and state wiring for the chat agent; the agent itself arrives in the next task. */
export function Chat({ chat = placeholderChatApi }: { chat?: ChatApi }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<ToolActivity[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState<string>();

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    const mine: ChatMessage = { id: crypto.randomUUID(), role: "user", text: text.trim(), createdAt: new Date().toISOString() };
    setMessages((m) => [...m, mine]);
    setText("");
    try {
      const reply = await chat.send([...messages, mine], mine.text);
      setMessages((m) => [...m, reply.message]);
      setActivity((a) => [...reply.activity, ...a]);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="chat">
      <header className="page-head"><h2>Chat</h2></header>
      {!chat.available && <Notice kind="warning">The chat agent is not connected yet. This screen is a placeholder for the next milestone.</Notice>}
      {error && <ErrorBanner error={error} />}
      <div className="chat-grid">
        <div className="card chat-log" aria-label="Conversation">
          {messages.length === 0 ? <EmptyState>No messages yet.</EmptyState> :
            messages.map((m) => <p key={m.id} className={`msg ${m.role}`}><strong>{m.role}:</strong> {m.text}</p>)}
          <form className="row" onSubmit={send}>
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder={chat.available ? "Ask something…" : "Chat unavailable"}
              disabled={!chat.available} aria-label="Message" />
            <button type="submit" disabled={!chat.available}>Send</button>
          </form>
        </div>
        <aside className="card" aria-label="Tool activity">
          <h3>Tool activity</h3>
          {activity.length === 0 ? <EmptyState>Tool calls made by the agent will appear here.</EmptyState> : (
            <ul className="tools">{activity.map((a) => <li key={a.id}><span className="mono">{a.tool}</span> — {a.status}</li>)}</ul>
          )}
        </aside>
      </div>
    </section>
  );
}
