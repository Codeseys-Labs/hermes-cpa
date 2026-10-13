// One read batch at a time. Unavailable transport requires explicit user retry.
type PollingOptions = {
  read: () => Promise<boolean>;
  hidden: () => boolean;
  subscribe: (changed: () => void) => () => void;
  schedule: (callback: () => void, delay: number) => number;
  cancel: (timer: number) => void;
};

export function createDashboardPolling(options: PollingOptions) {
  let disposed = false;
  let running = false;
  let paused = false;
  let timer: number | undefined;
  const clear = () => {
    if (timer !== undefined) options.cancel(timer);
    timer = undefined;
  };
  const arm = () => {
    clear();
    if (!disposed && !running && !paused) {
      timer = options.schedule(() => { timer = undefined; void poll(); },
        options.hidden() ? 30000 : 3000);
    }
  };
  const poll = async () => {
    if (disposed || running) return;
    clear();
    running = true;
    try { paused = !(await options.read()); }
    catch { paused = true; }
    finally { running = false; arm(); }
  };
  const unsubscribe = options.subscribe(arm);
  void poll();
  return {
    refresh: () => { void poll(); },
    dispose: () => { disposed = true; clear(); unsubscribe(); },
  };
}
