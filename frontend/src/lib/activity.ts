// One counter of requests in flight (tiles and API calls) for the loading indicator.

let active = 0;
const listeners = new Set<(n: number) => void>();

export function onActivity(fn: (n: number) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function track<T>(p: Promise<T>): Promise<T> {
  active++;
  listeners.forEach((fn) => fn(active));
  return p.finally(() => {
    active = Math.max(0, active - 1);
    listeners.forEach((fn) => fn(active));
  });
}
