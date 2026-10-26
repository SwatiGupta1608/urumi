// Boundary for the upcoming chat agent. The dashboard UI depends only on this
// interface; the next task supplies a real implementation (e.g. a BFF endpoint
// that runs the agent server-side and calls gateway tools with the user's own
// session). Nothing here talks to an LLM.

export interface ToolActivity {
  id: string;
  tool: string; // namespaced gateway tool name
  status: "running" | "ok" | "error";
  startedAt: string;
  durationMs?: number;
  summary?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: string;
}

export interface ChatReply {
  message: ChatMessage;
  activity: ToolActivity[];
}

export interface ChatApi {
  readonly available: boolean;
  send(history: ChatMessage[], text: string): Promise<ChatReply>;
}

export class ChatNotAvailableError extends Error {
  constructor() {
    super("The chat agent is not available yet.");
  }
}

export const placeholderChatApi: ChatApi = {
  available: false,
  async send() {
    throw new ChatNotAvailableError();
  },
};
