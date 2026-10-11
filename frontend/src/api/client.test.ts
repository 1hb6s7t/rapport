import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, connectLocalSession, getCsrf, setCsrf } from "./client";

const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => { setCsrf(null); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("automatic local connection", () => {
  it("shares simultaneous connection attempts", async () => {
    const fetcher = vi.fn().mockResolvedValue(reply(200, { csrf: "session-token" }));
    vi.stubGlobal("fetch", fetcher);
    await Promise.all([connectLocalSession(), connectLocalSession()]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(getCsrf()).toBe("session-token");
  });

  it("reconnects and retries a request rejected before execution", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(reply(401, { error: { code: "unpaired", msg: "expired" } }))
      .mockResolvedValueOnce(reply(200, { csrf: "renewed" }))
      .mockResolvedValueOnce(reply(200, { ok: true }));
    vi.stubGlobal("fetch", fetcher);
    expect(await api.post("/wechat/config", { enabled: true })).toEqual({ ok: true });
    expect(fetcher.mock.calls[1][0]).toBe("/api/session");
    expect(fetcher.mock.calls[2][1].headers["x-rapport-csrf"]).toBe("renewed");
  });

  it("does not replay uncertain sends or network failures", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(reply(409, { error: { code: "dispatch_send_uncertain", msg: "uncertain" } }))
      .mockRejectedValueOnce(new TypeError("Network failed"));
    vi.stubGlobal("fetch", fetcher);
    await expect(api.post("/dispatch/confirm", {})).rejects.toThrow("uncertain");
    expect(fetcher).toHaveBeenCalledTimes(1);
    await expect(api.post("/dispatch/confirm", {})).rejects.toThrow("Network failed");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
