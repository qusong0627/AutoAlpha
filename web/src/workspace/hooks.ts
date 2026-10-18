import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import axios from 'axios';
import { errorText } from './api';

/** Ignore stale responses, abort on unmount, and never overlap background polls. */
export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>, deps: DependencyList = [], interval = 0, enabled = true) {
  const loaderRef = useRef(loader); loaderRef.current = loader;
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: enabled });
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  useEffect(() => {
    if (!enabled) { setState({ loading: false }); return; }
    let disposed = false; let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setState({ loading: true });
    const load = async () => {
      try {
        const data = await loaderRef.current(controller.signal);
        if (!disposed) setState({ data, loading: false });
      } catch (error) {
        if (!disposed && !axios.isCancel(error)) setState(previous => ({ ...previous, error: errorText(error), loading: false }));
      } finally {
        if (!disposed && interval > 0) timer = setTimeout(load, interval);
      }
    };
    void load();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  // The caller controls identity through deps; loaderRef avoids stale polling closures.
  }, [...deps, revision, interval, enabled]);
  return { ...state, refresh };
}

export function useStored<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try { const stored = sessionStorage.getItem(key); return stored ? JSON.parse(stored) : initial; } catch { return initial; }
  });
  const update = useCallback((next: T | ((previous: T) => T)) => {
    setValue(previous => { const resolved = typeof next === 'function' ? (next as (x: T) => T)(previous) : next; sessionStorage.setItem(key, JSON.stringify(resolved)); return resolved; });
  }, [key]);
  return [value, update] as const;
}
