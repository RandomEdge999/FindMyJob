import { afterEach, describe, expect, it, vi } from "vitest";
import { requestJson, appendQuery } from "@/lib/api";

describe("requestJson", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    const result = await requestJson<{ ok: boolean }>("/api/test");
    expect(result).toEqual({ ok: true });
  });

  it("returns null for 204 No Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    const result = await requestJson("/api/test");
    expect(result).toBeNull();
  });

  it("throws with detail message on error responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not found" }), { status: 404, headers: { "Content-Type": "application/json" } }),
    );

    await expect(requestJson("/api/missing")).rejects.toThrow("Not found");
  });

  it("sets Content-Type to application/json", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    await requestJson("/api/test", { method: "POST", body: JSON.stringify({ a: 1 }) });
    const [, init] = spy.mock.calls[0];
    expect(init?.headers).toEqual(expect.objectContaining({ "Content-Type": "application/json" }));
  });
});

describe("appendQuery", () => {
  it("appends non-empty params", () => {
    expect(appendQuery("/api/test", { a: "1", b: "", c: "3" })).toBe("/api/test?a=1&c=3");
  });

  it("returns base URL when all params are empty", () => {
    expect(appendQuery("/api/test", { a: "", b: "" })).toBe("/api/test");
  });
});
