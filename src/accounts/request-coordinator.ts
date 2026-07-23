import type { MimoAccount } from "../config/types.js";
import { ApiError } from "../core/errors.js";

export interface AccountLease {
  account: MimoAccount;
  release(): void;
}

interface AccountWaiter {
  accounts: MimoAccount[];
  resolve: (lease: AccountLease) => void;
  reject: (error: unknown) => void;
  settled: boolean;
  cleanup: () => void;
}

export class AccountRequestCoordinator {
  readonly maxPerAccount: number;
  readonly queueLimit: number;
  readonly queueTimeoutMs: number;
  #active = new Map<string, number>();
  #waiters: AccountWaiter[] = [];
  #cursor = 0;
  #closed = false;

  constructor(options: { maxPerAccount?: number; queueLimit?: number; queueTimeoutMs?: number } = {}) {
    this.maxPerAccount = integer(options.maxPerAccount ?? process.env.MIMO2API_ACCOUNT_MAX_CONCURRENCY, 1, 32, 1);
    this.queueLimit = integer(options.queueLimit ?? process.env.MIMO2API_ACCOUNT_QUEUE_LIMIT, 1, 10_000, 200);
    this.queueTimeoutMs = integer(options.queueTimeoutMs ?? process.env.MIMO2API_ACCOUNT_QUEUE_TIMEOUT_MS, 1_000, 3_600_000, 600_000);
  }

  async acquireAny(accounts: MimoAccount[], signal: AbortSignal): Promise<AccountLease> {
    if (this.#closed) throw new ApiError(503, "account_coordinator_closed", "account coordinator is shutting down");
    if (signal.aborted) throw signal.reason;
    const candidates = uniqueAccounts(accounts);
    if (candidates.length === 0) throw new ApiError(503, "no_mimo_account", "no usable MiMo account is configured");
    const immediate = this.#tryAcquire(candidates);
    if (immediate) return immediate;
    if (this.#waiters.length >= this.queueLimit) {
      throw new ApiError(429, "account_queue_full", "all MiMo accounts are busy and the local request queue is full");
    }

    return new Promise<AccountLease>((resolve, reject) => {
      let timer: NodeJS.Timeout;
      const waiter: AccountWaiter = {
        accounts: candidates,
        resolve,
        reject,
        settled: false,
        cleanup: () => {
          clearTimeout(timer);
          signal.removeEventListener("abort", onAbort);
        },
      };
      const fail = (error: unknown) => {
        if (waiter.settled) return;
        waiter.settled = true;
        waiter.cleanup();
        this.#waiters = this.#waiters.filter((item) => item !== waiter);
        reject(error);
      };
      const onAbort = () => fail(signal.reason);
      timer = setTimeout(() => fail(new ApiError(
        503,
        "account_queue_timeout",
        "timed out waiting for an available MiMo account",
      )), this.queueTimeoutMs);
      timer.unref();
      signal.addEventListener("abort", onAbort, { once: true });
      this.#waiters.push(waiter);
    });
  }

  acquire(account: MimoAccount, signal: AbortSignal): Promise<AccountLease> {
    return this.acquireAny([account], signal);
  }

  status(): { active: number; queued: number; busy_accounts: number; max_per_account: number; queue_limit: number } {
    return {
      active: [...this.#active.values()].reduce((sum, count) => sum + count, 0),
      queued: this.#waiters.length,
      busy_accounts: this.#active.size,
      max_per_account: this.maxPerAccount,
      queue_limit: this.queueLimit,
    };
  }

  close(): void {
    this.#closed = true;
    const error = new ApiError(503, "account_coordinator_closed", "account coordinator is shutting down");
    for (const waiter of this.#waiters.splice(0)) {
      if (waiter.settled) continue;
      waiter.settled = true;
      waiter.cleanup();
      waiter.reject(error);
    }
  }

  #tryAcquire(accounts: MimoAccount[]): AccountLease | undefined {
    const available = accounts.filter((account) => (this.#active.get(account.user_id) ?? 0) < this.maxPerAccount);
    if (available.length === 0) return undefined;
    const lowest = Math.min(...available.map((account) => this.#active.get(account.user_id) ?? 0));
    const leastBusy = available.filter((account) => (this.#active.get(account.user_id) ?? 0) === lowest);
    const account = leastBusy[this.#cursor % leastBusy.length]!;
    this.#cursor += 1;
    this.#active.set(account.user_id, (this.#active.get(account.user_id) ?? 0) + 1);
    let released = false;
    return {
      account: structuredClone(account),
      release: () => {
        if (released) return;
        released = true;
        const next = (this.#active.get(account.user_id) ?? 1) - 1;
        if (next > 0) this.#active.set(account.user_id, next);
        else this.#active.delete(account.user_id);
        this.#dispatch();
      },
    };
  }

  #dispatch(): void {
    for (const waiter of [...this.#waiters]) {
      if (waiter.settled) continue;
      const lease = this.#tryAcquire(waiter.accounts);
      if (!lease) continue;
      waiter.settled = true;
      waiter.cleanup();
      this.#waiters = this.#waiters.filter((item) => item !== waiter);
      waiter.resolve(lease);
    }
  }
}

interface LockWaiter {
  resolve: (release: () => void) => void;
  reject: (error: unknown) => void;
  settled: boolean;
  cleanup: () => void;
}

export class KeyedSerialQueue {
  readonly queueLimit: number;
  readonly queueTimeoutMs: number;
  #active = new Set<string>();
  #waiters = new Map<string, LockWaiter[]>();
  #queued = 0;
  #closed = false;

  constructor(options: { queueLimit?: number; queueTimeoutMs?: number } = {}) {
    this.queueLimit = integer(options.queueLimit ?? process.env.MIMO2API_SESSION_QUEUE_LIMIT, 1, 10_000, 200);
    this.queueTimeoutMs = integer(options.queueTimeoutMs ?? process.env.MIMO2API_ACCOUNT_QUEUE_TIMEOUT_MS, 1_000, 3_600_000, 600_000);
  }

  async acquire(key: string, signal: AbortSignal): Promise<() => void> {
    if (this.#closed) throw new ApiError(503, "session_queue_closed", "session queue is shutting down");
    if (signal.aborted) throw signal.reason;
    if (!this.#active.has(key)) {
      this.#active.add(key);
      return this.#release(key);
    }
    if (this.#queued >= this.queueLimit) {
      throw new ApiError(429, "session_queue_full", "too many requests are waiting for an active MiMo session");
    }
    return new Promise<() => void>((resolve, reject) => {
      let timer: NodeJS.Timeout;
      const waiter: LockWaiter = {
        resolve,
        reject,
        settled: false,
        cleanup: () => {
          clearTimeout(timer);
          signal.removeEventListener("abort", onAbort);
        },
      };
      timer = setTimeout(() => onAbortWith(new ApiError(
        503,
        "session_queue_timeout",
        "timed out waiting for the previous request in this MiMo session",
      )), this.queueTimeoutMs);
      timer.unref();
      const onAbortWith = (error: unknown) => {
        if (waiter.settled) return;
        waiter.settled = true;
        waiter.cleanup();
        this.#queued -= 1;
        const queue = this.#waiters.get(key)?.filter((item) => item !== waiter) ?? [];
        if (queue.length > 0) this.#waiters.set(key, queue);
        else this.#waiters.delete(key);
        reject(error);
      };
      const onAbort = () => onAbortWith(signal.reason);
      signal.addEventListener("abort", onAbort, { once: true });
      const queue = this.#waiters.get(key) ?? [];
      queue.push(waiter);
      this.#waiters.set(key, queue);
      this.#queued += 1;
    });
  }

  status(): { active_sessions: number; queued_sessions: number } {
    return { active_sessions: this.#active.size, queued_sessions: this.#queued };
  }

  close(): void {
    this.#closed = true;
    const error = new ApiError(503, "session_queue_closed", "session queue is shutting down");
    for (const queue of this.#waiters.values()) {
      for (const waiter of queue) {
        if (waiter.settled) continue;
        waiter.settled = true;
        waiter.cleanup();
        waiter.reject(error);
      }
    }
    this.#waiters.clear();
    this.#queued = 0;
  }

  #release(key: string): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const queue = this.#waiters.get(key) ?? [];
      let next: LockWaiter | undefined;
      while (queue.length > 0 && !next) {
        const candidate = queue.shift()!;
        if (!candidate.settled) next = candidate;
      }
      if (queue.length > 0) this.#waiters.set(key, queue);
      else this.#waiters.delete(key);
      if (!next) {
        this.#active.delete(key);
        return;
      }
      next.settled = true;
      next.cleanup();
      this.#queued -= 1;
      next.resolve(this.#release(key));
    };
  }
}

const uniqueAccounts = (accounts: MimoAccount[]): MimoAccount[] => [...new Map(
  accounts.filter((account) => account.user_id).map((account) => [account.user_id, account]),
).values()];

const integer = (value: unknown, minimum: number, maximum: number, fallback: number): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, Math.floor(parsed))) : fallback;
};
