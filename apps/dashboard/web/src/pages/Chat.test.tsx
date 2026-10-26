import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ChatApi, ChatState, PendingAction } from "../api/chat";
import { ApiError } from "../api/client";
import { Chat } from "./Chat";

const EMPTY: ChatState = { messages: [], activity: [], pending: null, busy: false };
const PENDING: PendingAction = {
  id: "act_0123456789", digest: "d".repeat(64), tool: "notes__create_note", title: "Create note", description: "Create a shared note.",
  args: { title: "Release plan", body: "Ship Friday" }, expires_in: 300, state: "pending", destructive: false,
};

function fakeChat(over: Partial<ChatApi> = {}): ChatApi {
  return {
    status: vi.fn().mockResolvedValue({ available: true, model: "test-model" }),
    state: vi.fn().mockResolvedValue(EMPTY),
    send: vi.fn().mockResolvedValue(EMPTY),
    confirm: vi.fn().mockResolvedValue(EMPTY),
    cancel: vi.fn().mockResolvedValue(EMPTY),
    reset: vi.fn().mockResolvedValue(EMPTY),
    ...over,
  };
}

describe("chat page", () => {
  it("explains when no provider is configured and disables the composer", async () => {
    render(<Chat chat={fakeChat({ status: vi.fn().mockResolvedValue({ available: false, reason: "No language model provider is configured (operator setting)." }) })} />);
    expect(await screen.findByText(/Chat is unavailable: No language model provider/)).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeDisabled();
  });

  it("shows example prompts, a thinking state, then the reply and tool activity", async () => {
    let release!: (s: ChatState) => void;
    const send = vi.fn(() => new Promise<ChatState>((r) => (release = r)));
    render(<Chat chat={fakeChat({ send })} />);
    await userEvent.click(await screen.findByRole("button", { name: "Show my team's notes." }));
    expect(send).toHaveBeenCalledWith("Show my team's notes.");
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
    release({
      ...EMPTY,
      messages: [{ id: "1", role: "user", text: "Show my team's notes.", at: 1 }, { id: "2", role: "assistant", text: "You have 1 note.", at: 2 }],
      activity: [{ id: "a", tool: "notes__list_notes", args: {}, status: "success", summary: "1 note", at: 1, duration_ms: 120 }],
    });
    expect(await screen.findByText("You have 1 note.")).toBeInTheDocument();
    const act = screen.getByTestId("activity");
    expect(act).toHaveTextContent("notes__list_notes") ;
    expect(act).toHaveTextContent("success");
    expect(act).toHaveTextContent("120 ms");
  });

  it("renders untrusted tool output and replies as text, not HTML", async () => {
    const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    render(<Chat chat={fakeChat({ state: vi.fn().mockResolvedValue({
      ...EMPTY, messages: [{ id: "m", role: "assistant", text: evil, at: 1 }],
      activity: [{ id: "a", tool: "notes__get_note", args: {}, status: "success", summary: evil, at: 1, duration_ms: 1 }],
    }) })} />);
    await screen.findAllByText(evil);
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector(".chat script")).toBeNull();
  });

  it("previews a mutation and confirms it exactly once", async () => {
    let release!: (s: ChatState) => void;
    const confirm = vi.fn(() => new Promise<ChatState>((r) => (release = r)));
    render(<Chat chat={fakeChat({ state: vi.fn().mockResolvedValue({ ...EMPTY, pending: PENDING }), confirm })} />);
    const card = await screen.findByRole("dialog", { name: "Confirm action" });
    expect(card).toHaveTextContent("notes__create_note");
    expect(card).toHaveTextContent("Release plan");
    expect(screen.getByLabelText("Message")).toBeDisabled();
    const btn = within(card).getByRole("button", { name: "Confirm" });
    await userEvent.click(btn);
    await userEvent.click(btn); // double click must not send twice
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm).toHaveBeenCalledWith(PENDING);
    release({ ...EMPTY, messages: [{ id: "x", role: "assistant", text: "Note created.", at: 1 }] });
    expect(await screen.findByText("Note created.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Confirm action" })).toBeNull();
  });

  it("cancels a pending action", async () => {
    const cancel = vi.fn().mockResolvedValue({ ...EMPTY, messages: [{ id: "x", role: "assistant", text: "Okay, cancelled.", at: 1 }] });
    render(<Chat chat={fakeChat({ state: vi.fn().mockResolvedValue({ ...EMPTY, pending: PENDING }), cancel })} />);
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(cancel).toHaveBeenCalledWith(PENDING);
    expect(await screen.findByText("Okay, cancelled.")).toBeInTheDocument();
  });

  it("shows provider and gateway errors and keeps the unsent text", async () => {
    const send = vi.fn().mockRejectedValue(new ApiError(504, "provider_timeout", "The language model did not respond within 60s."));
    render(<Chat chat={fakeChat({ send })} />);
    const input = await screen.findByLabelText("Message");
    await userEvent.type(input, "list products");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("did not respond within 60s");
    await waitFor(() => expect(input).toHaveValue("list products"));
  });

  it("an expired confirmation shows the error and re-syncs state", async () => {
    const state = vi.fn().mockResolvedValueOnce({ ...EMPTY, pending: PENDING }).mockResolvedValue(EMPTY);
    const confirm = vi.fn().mockRejectedValue(new ApiError(410, "expired", "This action expired and was not executed."));
    render(<Chat chat={fakeChat({ state, confirm })} />);
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("expired and was not executed");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("starts a new conversation", async () => {
    const reset = vi.fn().mockResolvedValue(EMPTY);
    render(<Chat chat={fakeChat({ reset, state: vi.fn().mockResolvedValue({ ...EMPTY, messages: [{ id: "1", role: "user", text: "hi", at: 1 }] }) })} />);
    await userEvent.click(await screen.findByRole("button", { name: "New conversation" }));
    expect(reset).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("hi")).toBeNull());
  });
});
