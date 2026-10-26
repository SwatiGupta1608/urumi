import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface AsyncState<T> {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => Promise<void>;
}

function toApiError(e: unknown): ApiError {
  return e instanceof ApiError ? e : new ApiError(0, "unknown", String(e));
}

/** Load data on mount (and every `intervalMs` if given); keeps the last data while refreshing. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[] = [], intervalMs?: number): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError>();
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  const alive = useRef(true);

  useEffect(() => {
    loadRef.current = load;
  });

  // State is only updated after the await, never synchronously in an effect.
  const run = useCallback(async () => {
    try {
      const result = await loadRef.current();
      if (alive.current) {
        setData(result);
        setError(undefined);
      }
    } catch (e) {
      if (alive.current) setError(toApiError(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  /** Manual refresh (from event handlers): show the loading state, then load. */
  const reload = useCallback(async () => {
    setLoading(true);
    await run();
  }, [run]);

  useEffect(() => {
    alive.current = true;
    void run();
    const timer = intervalMs ? setInterval(() => void run(), intervalMs) : undefined;
    return () => {
      alive.current = false;
      if (timer) clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, ...deps]);

  return { data, error, loading, reload };
}
