import { useEffect, useState } from "react";

interface Result<T> {
  key: string | null;
  data: T | null;
  error: string | null;
}

/**
 * Fetch when `key` changes (null = nothing to fetch). Results of older keys
 * are ignored, and `loading` is derived instead of stored.
 */
export function useFetch<T>(key: string | null, fn: (signal: AbortSignal) => Promise<T>) {
  const [res, setRes] = useState<Result<T>>({ key: null, data: null, error: null });
  useEffect(() => {
    if (key === null) return;
    const ac = new AbortController();
    fn(ac.signal).then(
      (data) => setRes({ key, data, error: null }),
      (e: Error) => e.name !== "AbortError" && setRes({ key, data: null, error: e.message }),
    );
    return () => ac.abort();
    // `fn` is recreated on every render; `key` describes the request completely.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const current = key !== null && res.key === key;
  return {
    data: current ? res.data : null,
    error: current ? res.error : null,
    loading: key !== null && !current,
  };
}
