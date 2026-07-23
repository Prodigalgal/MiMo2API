import { describe, expect, it } from "vitest";
import { AccountRequestCoordinator, KeyedSerialQueue } from "../src/accounts/request-coordinator.js";
import type { MimoAccount } from "../src/config/types.js";

const account = (userId: string): MimoAccount => ({
  service_token: `token-${userId}`,
  user_id: userId,
  xiaomichatbot_ph: `ph-${userId}`,
  is_valid: true,
  login_time: "",
  last_test: "",
  email: "",
  password: "",
  pass_token: "",
  c_user_id: "",
  device_id: "",
  auto_renew: true,
  last_renew: "",
  renew_error: "",
  mail_jwt: "",
  region: "",
});

describe("account request coordinator", () => {
  it("uses another account instead of exceeding the per-account limit", async () => {
    const coordinator = new AccountRequestCoordinator({ maxPerAccount: 1 });
    const first = await coordinator.acquireAny([account("a"), account("b")], new AbortController().signal);
    const second = await coordinator.acquireAny([account("a"), account("b")], new AbortController().signal);

    expect(first.account.user_id).not.toBe(second.account.user_id);
    expect(coordinator.status()).toMatchObject({ active: 2, queued: 0, busy_accounts: 2 });
    first.release();
    second.release();
    expect(coordinator.status().active).toBe(0);
  });

  it("queues a request pinned to a busy account and releases it in order", async () => {
    const coordinator = new AccountRequestCoordinator({ maxPerAccount: 1 });
    const first = await coordinator.acquire(account("a"), new AbortController().signal);
    let started = false;
    const waiting = coordinator.acquire(account("a"), new AbortController().signal).then((lease) => {
      started = true;
      return lease;
    });
    await Promise.resolve();

    expect(started).toBe(false);
    expect(coordinator.status().queued).toBe(1);
    first.release();
    const second = await waiting;
    expect(started).toBe(true);
    second.release();
  });

  it("removes cancelled requests from the account queue", async () => {
    const coordinator = new AccountRequestCoordinator({ maxPerAccount: 1 });
    const first = await coordinator.acquire(account("a"), new AbortController().signal);
    const controller = new AbortController();
    const waiting = coordinator.acquire(account("a"), controller.signal);
    controller.abort(new DOMException("cancelled", "AbortError"));

    await expect(waiting).rejects.toMatchObject({ name: "AbortError" });
    expect(coordinator.status().queued).toBe(0);
    first.release();
  });
});

describe("session serial queue", () => {
  it("allows only one active request for the same session", async () => {
    const queue = new KeyedSerialQueue();
    const releaseFirst = await queue.acquire("session-a", new AbortController().signal);
    let started = false;
    const waiting = queue.acquire("session-a", new AbortController().signal).then((release) => {
      started = true;
      return release;
    });
    await Promise.resolve();

    expect(started).toBe(false);
    expect(queue.status()).toEqual({ active_sessions: 1, queued_sessions: 1 });
    releaseFirst();
    const releaseSecond = await waiting;
    expect(started).toBe(true);
    releaseSecond();
    expect(queue.status()).toEqual({ active_sessions: 0, queued_sessions: 0 });
  });
});
