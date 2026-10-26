import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { NewKeyPanel } from "./pages/Team";
import { validateUpstream } from "./pages/Upstreams";
import { outcome } from "./pages/Audit";
import { ADMIN, fakeBackend, HEALTH, KEY, MEMBER, TOOLS, UPSTREAMS } from "./test/fakeBackend";

const storageWrites = vi.fn();

beforeEach(() => {
  window.location.hash = "";
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(storageWrites);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  storageWrites.mockReset();
});

const signedIn = (user: typeof ADMIN | typeof MEMBER, extra = {}) => ({
  "GET /bff/session": () => ({ status: 200, body: { user } }),
  "GET /bff/health": () => ({ status: 200, body: HEALTH }),
  "GET /api/v1/tools": () => ({ status: 200, body: user.role === "admin" ? TOOLS : TOOLS.filter((t) => !t.admin_only) }),
  "GET /api/v1/upstreams": () => (user.role === "admin" ? { status: 200, body: UPSTREAMS } : { status: 403, body: { detail: "This operation requires the admin role." } }),
  ...extra,
});

describe("login and session", () => {
  it("shows the login form when there is no session, and validates the key format locally", async () => {
    const { calls } = fakeBackend({ "GET /bff/session": () => ({ status: 401, body: { error: "no_session", message: "x" } }) });
    render(<App />);
    const input = await screen.findByLabelText("API key");
    await userEvent.type(input, "not-a-key");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("alert")).toHaveTextContent("doesn't look like a gateway API key");
    expect(calls.some((c) => c.path === "/bff/login")).toBe(false);
  });

  it("signs in with a key, clears the field, and never writes browser storage", async () => {
    const { calls } = fakeBackend({
      ...signedIn(MEMBER),
      "GET /bff/session": () => ({ status: 401, body: { error: "no_session", message: "x" } }),
      "POST /bff/login": () => ({ status: 200, body: { user: MEMBER } }),
    });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("API key"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    const login = calls.find((c) => c.path === "/bff/login")!;
    expect(login.body).toEqual({ api_key: KEY });
    expect(login.headers["X-Urumi-Dashboard"]).toBe("1");
    expect(storageWrites).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it("shows a clear message for a rejected (revoked) key at login", async () => {
    fakeBackend({
      "GET /bff/session": () => ({ status: 401, body: { error: "no_session", message: "x" } }),
      "POST /bff/login": () => ({ status: 401, body: { error: "key_rejected", message: "This API key is invalid, revoked, or its user is disabled." } }),
    });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("API key"), KEY);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid, revoked");
  });

  it("returns to login with an explanation when the key is revoked mid-session", async () => {
    fakeBackend(signedIn(MEMBER, {
      "GET /api/v1/tools": () => ({ status: 401, body: { error: "key_rejected", message: "revoked" } }),
    }));
    render(<App />);
    expect(await screen.findByText(/API key was rejected/)).toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toBeInTheDocument();
  });
});

describe("role-based rendering", () => {
  it("members see no admin navigation or admin controls", async () => {
    const { calls } = fakeBackend(signedIn(MEMBER, { "GET /api/v1/me/keys": () => ({ status: 200, body: [] }) }));
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).queryByText("Upstreams")).toBeNull();
    expect(within(nav).queryByText("Audit log")).toBeNull();
    expect(await screen.findByTestId("upstream-woocommerce")).toHaveTextContent("1 tools available");
    expect(calls.some((c) => c.path === "/api/v1/upstreams")).toBe(false);

    window.location.hash = "#/team";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    expect(await screen.findByRole("heading", { name: "My account" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Issue key|Create user|Revoke/ })).toBeNull();
    expect(calls.some((c) => c.path === "/api/v1/users")).toBe(false);
  });

  it("a member typing an admin URL is sent to the overview", async () => {
    window.location.hash = "#/audit";
    fakeBackend(signedIn(MEMBER));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Audit log" })).toBeNull();
  });

  it("admins see upstream health with sanitized errors", async () => {
    fakeBackend(signedIn(ADMIN));
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("Upstreams")).toBeInTheDocument();
    const gh = await screen.findByTestId("upstream-github");
    expect(gh).toHaveTextContent("unreachable");
    expect(gh).toHaveTextContent("credential reference 'github-token' is not available");
    expect(await screen.findByTestId("upstream-woocommerce")).toHaveTextContent("reachable");
  });
});

describe("loading and errors", () => {
  it("shows a loading state until data arrives", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    fakeBackend(signedIn(ADMIN, { "GET /api/v1/upstreams": async () => { await gate; return { status: 200, body: UPSTREAMS }; } }));
    render(<App />);
    await screen.findByRole("heading", { name: "Overview" });
    expect(screen.getAllByRole("status").some((el) => el.textContent?.includes("Loading"))).toBe(true);
    release();
    expect(await screen.findByTestId("upstream-github")).toBeInTheDocument();
  });

  it("shows gateway 403 and 500 errors instead of data", async () => {
    window.location.hash = "#/audit";
    fakeBackend(signedIn(ADMIN, {
      "GET /api/v1/users": () => ({ status: 200, body: [ADMIN] }),
      "GET /api/v1/audit": () => ({ status: 403, body: { detail: "This operation requires the admin role." } }),
    }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Not permitted: This operation requires the admin role.");
  });

  it("shows backend validation errors when registering an upstream", async () => {
    window.location.hash = "#/upstreams";
    fakeBackend(signedIn(ADMIN, {
      "POST /api/v1/upstreams": () => ({ status: 422, body: { detail: "upstream URL rejected: address 169.254.169.254 is not an allowed upstream destination" } }),
    }));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Register upstream" }));
    const form = screen.getByRole("form", { name: "Register upstream" });
    await userEvent.type(within(form).getByLabelText("Slug"), "meta");
    await userEvent.type(within(form).getByLabelText("Display name"), "Meta");
    await userEvent.type(within(form).getByLabelText("URL"), "http://169.254.169.254/");
    await userEvent.click(within(form).getByRole("button", { name: "Register" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("not an allowed upstream destination");
  });

  it("shows namespaced tools and admin-only markers", async () => {
    window.location.hash = "#/upstreams";
    fakeBackend(signedIn(ADMIN));
    render(<App />);
    const card = await screen.findByTestId("upstream-card-woocommerce");
    expect(card).toHaveTextContent("woocommerce__update_order_status");
    expect(card).toHaveTextContent("admin only");
    expect(screen.getByTestId("upstream-card-github")).toHaveTextContent("github-token");
  });
});

describe("API key handling", () => {
  it("shows a new key once with a copy action, and forgets it on close", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const onDone = vi.fn();
    const created = { id: "0123456789ab", name: "laptop", prefix: "urumi_0123456789ab_…", created_at: "", revoked_at: null, last_used_at: null, key: KEY };
    const { rerender } = render(<NewKeyPanel userName="alice" apiKey={created} onDone={onDone} />);
    expect(screen.getByText(/only time this key is shown/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy key" }));
    expect(writeText).toHaveBeenCalledWith(KEY);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /stored it/ }));
    expect(onDone).toHaveBeenCalled();
    rerender(<></>);
    expect(document.body.innerHTML).not.toContain(KEY);
    expect(storageWrites).not.toHaveBeenCalled();
  });

  it("admin issues a key: shown once, then gone after closing; list shows only the prefix", async () => {
    window.location.hash = "#/team";
    const keys: unknown[] = [];
    fakeBackend(signedIn(ADMIN, {
      "GET /api/v1/users": () => ({ status: 200, body: [ADMIN, MEMBER] }),
      "GET /api/v1/users/m1/keys": () => ({ status: 200, body: keys }),
      "POST /api/v1/users/m1/keys": () => {
        const k = { id: "0123456789ab", name: "laptop", prefix: "urumi_0123456789ab_…", created_at: "2026-10-26T10:00:00Z", revoked_at: null, last_used_at: null };
        keys.push(k);
        return { status: 201, body: { ...k, key: KEY } };
      },
    }));
    render(<App />);
    const alice = await screen.findByTestId("user-alice");
    await userEvent.click(within(alice).getByRole("button", { name: "Keys" }));
    await userEvent.type(await within(alice).findByLabelText("Key label"), "laptop");
    await userEvent.click(within(alice).getByRole("button", { name: "Issue key" }));
    const panel = await screen.findByRole("dialog", { name: "New API key" });
    expect(within(panel).getByLabelText("New API key value")).toHaveValue(KEY);
    expect(await within(alice).findByText("urumi_0123456789ab_…")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: /stored it/ }));
    await waitFor(() => expect(document.body.innerHTML).not.toContain(KEY));
    expect(storageWrites).not.toHaveBeenCalled();
  });

  it("revocation requires confirmation and calls the gateway", async () => {
    window.location.hash = "#/team";
    const { calls } = fakeBackend(signedIn(ADMIN, {
      "GET /api/v1/users": () => ({ status: 200, body: [ADMIN, MEMBER] }),
      "GET /api/v1/users/m1/keys": () => ({ status: 200, body: [{ id: "0123456789ab", name: "laptop", prefix: "urumi_0123456789ab_…", created_at: "2026-10-26T10:00:00Z", revoked_at: null, last_used_at: null }] }),
      "DELETE /api/v1/keys/0123456789ab": () => ({ status: 204 }),
    }));
    render(<App />);
    const alice = await screen.findByTestId("user-alice");
    await userEvent.click(within(alice).getByRole("button", { name: "Keys" }));
    await userEvent.click(await within(alice).findByRole("button", { name: "Revoke" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(alice).getByRole("button", { name: "Revoke now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/v1/keys/0123456789ab")).toBe(true));
  });
});

describe("helpers", () => {
  it("validates upstream input like the gateway", () => {
    const ok = { slug: "notes", display_name: "Notes", url: "http://notes.ns.svc.cluster.local:8000/mcp", credential_ref: "notes-token", tools: "a, b_c" };
    expect(validateUpstream(ok)).toBeNull();
    expect(validateUpstream({ ...ok, slug: "no_underscore" })).toMatch(/Slug/);
    expect(validateUpstream({ ...ok, url: "file:///etc/passwd" })).toMatch(/http/);
    expect(validateUpstream({ ...ok, credential_ref: "../x" })).toMatch(/Credential/);
    expect(validateUpstream({ ...ok, tools: "bad name" })).toMatch(/Invalid tool/);
  });

  it("classifies audit outcomes", () => {
    const base = { id: "1", created_at: "", event: "tool_call", user_id: "u", upstream_slug: "x", tool_name: "x__y", duration_ms: 1, detail: null };
    expect(outcome({ ...base, success: true, error: null })).toBe("success");
    expect(outcome({ ...base, success: false, error: "forbidden: admin-only tool" })).toBe("denied");
    expect(outcome({ ...base, success: false, error: "timeout: no response" })).toBe("failure");
  });
});
