import { useEffect, useRef } from "react";

// Runs `fn` now and then every `intervalMs` while `enabled`; skips a tick if the previous call is
// still in flight and pauses while the tab is hidden. Near-real-time without websockets.
export function usePolling(fn, intervalMs, enabled = true, deps = []) {
  const busy = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    const tick = async () => {
      if (busy.current || cancelled || document.hidden) return;
      busy.current = true;
      try {
        await fnRef.current();
      } catch {
        /* keep polling; the caller surfaces errors */
      } finally {
        busy.current = false;
      }
    };
    tick();
    const handle = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, ...deps]);
}
