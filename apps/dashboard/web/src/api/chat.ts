// Chat agent boundary. The agent runs in the dashboard BFF (server side): the
// browser only sends text and confirm/cancel decisions; it never sees the
// gateway key or the LLM provider key. Shapes mirror dashboard_bff/chat.py.

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "notice";
  text: string;
  at: number; // unix seconds
}

export type ActivityStatus = "running" | "success" | "failure" | "awaiting_confirmation" | "cancelled" | "expired";

export interface ToolActivity {
  id: string;
  tool: string; // namespaced gateway tool name
  args: Record<string, unknown>; // sanitised by the server
  status: ActivityStatus;
  summary: string; // concise, untrusted text; render as text only
  at: number;
  duration_ms: number | null;
}

export interface PendingAction {
  id: string;
  digest: string; // binds the confirmation to exactly this action
  tool: string;
  title: string | null;
  description: string;
  args: Record<string, unknown>;
  expires_in: number;
  state: "pending";
  destructive: boolean;
}

export interface ChatState {
  messages: ChatMessage[];
  activity: ToolActivity[];
  pending: PendingAction | null;
  busy: boolean;
}

export interface ChatStatus {
  available: boolean;
  model?: string;
  reason?: string;
}

export interface ChatApi {
  status(): Promise<ChatStatus>;
  state(): Promise<ChatState>;
  send(text: string): Promise<ChatState>;
  confirm(action: PendingAction): Promise<ChatState>;
  cancel(action: PendingAction): Promise<ChatState>;
  reset(): Promise<ChatState>;
}
