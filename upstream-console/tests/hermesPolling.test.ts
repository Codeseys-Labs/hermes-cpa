import { expect, test } from 'bun:test';
import { createDashboardPolling } from '../src/features/dashboard/hermesPolling';

function fixture() {
  let hidden = false;
  let listener = () => {};
  let removed = false;
  let next = 0;
  const timers = new Map<number, { fn: () => void; delay: number }>();
  const pending: Array<(available: boolean) => void> = [];
  let calls = 0;
  const polling = createDashboardPolling({
    read: () => { calls++; return new Promise<boolean>(resolve => pending.push(resolve)); },
    hidden: () => hidden,
    subscribe: callback => { listener = callback; return () => { removed = true; }; },
    schedule: (fn, delay) => { timers.set(++next, { fn, delay }); return next; },
    cancel: id => { timers.delete(id); },
  });
  return { polling, timers, pending, calls: () => calls, removed: () => removed,
    visibility: (value: boolean) => { hidden = value; listener(); },
    fire: () => { const [id, timer] = [...timers][0]; timers.delete(id); timer.fn(); },
  };
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };

test('active 3s, hidden 30s, no overlapping manual or visibility reads', async () => {
  const f = fixture();
  expect(f.calls()).toBe(1);
  f.polling.refresh(); f.visibility(true);
  expect(f.calls()).toBe(1);
  f.pending.shift()!(true); await settle();
  expect([...f.timers.values()][0].delay).toBe(30000);
  f.visibility(false);
  expect(f.timers.size).toBe(1);
  expect([...f.timers.values()][0].delay).toBe(3000);
  f.fire(); expect(f.calls()).toBe(2);
  expect(f.timers.size).toBe(0);
  f.polling.refresh(); expect(f.calls()).toBe(2);
  f.pending.shift()!(true); await settle();
  expect(f.timers.size).toBe(1);
  f.polling.dispose(); expect(f.timers.size).toBe(0); expect(f.removed()).toBe(true);
});

test('unavailable pauses until explicit refresh; visibility cannot restart it', async () => {
  const f = fixture();
  f.pending.shift()!(false); await settle();
  expect(f.timers.size).toBe(0);
  f.visibility(true); f.visibility(false);
  expect(f.calls()).toBe(1); expect(f.timers.size).toBe(0);
  f.polling.refresh(); expect(f.calls()).toBe(2);
  f.pending.shift()!(true); await settle();
  expect([...f.timers.values()][0].delay).toBe(3000);
  f.polling.dispose();
});

test('late completion after disposal cannot rearm a timer', async () => {
  const f = fixture(); f.polling.dispose();
  f.pending.shift()!(true); await settle();
  f.polling.refresh(); f.visibility(false);
  expect(f.calls()).toBe(1); expect(f.timers.size).toBe(0);
});

test('read rejection pauses rather than causing unhandled rejection or retry loop', async () => {
  const timers: number[] = [];
  const p = createDashboardPolling({ read: async () => { throw new Error('offline'); },
    hidden: () => false, subscribe: () => () => {},
    schedule: (_, ms) => { timers.push(ms); return 1; }, cancel: () => {},
  });
  await settle(); expect(timers).toEqual([]); p.dispose();
});
