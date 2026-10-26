import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, onSessionEnded } from "../api/client";
import type { User } from "../api/types";

type AuthState =
  | { status: "loading" }
  | { status: "anonymous"; notice?: string }
  | { status: "authenticated"; user: User };

interface AuthValue {
  state: AuthState;
  login: (apiKey: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

const ENDED_NOTICE: Record<string, string> = {
  key_rejected: "Your API key was rejected (revoked, or your user was disabled). Sign in with a valid key.",
  no_session: "Your session expired. Sign in again.",
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    let alive = true;
    api.session()
      .then(({ user }) => alive && setState({ status: "authenticated", user }))
      .catch(() => alive && setState({ status: "anonymous" }));
    return () => {
      alive = false;
    };
  }, []);

  // Any 401 from any request means the session is over (expired/revoked).
  useEffect(() => onSessionEnded((err: ApiError) => {
    setState((prev) => prev.status === "authenticated"
      ? { status: "anonymous", notice: ENDED_NOTICE[err.code] ?? err.message }
      : prev);
  }), []);

  const login = useCallback(async (apiKey: string) => {
    const { user } = await api.login(apiKey);
    setState({ status: "authenticated", user });
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setState({ status: "anonymous", notice: "You have signed out." });
    }
  }, []);

  const value = useMemo(() => ({ state, login, logout }), [state, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** The signed-in user (only valid under an authenticated layout). */
export function useUser(): User {
  const { state } = useAuth();
  if (state.status !== "authenticated") throw new Error("not authenticated");
  return state.user;
}
