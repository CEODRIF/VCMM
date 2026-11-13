import { describe, expect, it } from "vitest";

import { validateClipDuration } from "@/lib/validation/clips";

describe("clip duration validation", () => {
  it("accepts the boundaries and intermediate values", () => {
    expect(validateClipDuration(60).valid).toBe(true);
    expect(validateClipDuration(90).valid).toBe(true);
    expect(validateClipDuration(120).valid).toBe(true);
    expect(validateClipDuration(180).valid).toBe(true);
  });

  it("rejects durations below 60 seconds", () => {
    expect(validateClipDuration(59).valid).toBe(false);
    expect(validateClipDuration(0).valid).toBe(false);
    expect(validateClipDuration(-10).valid).toBe(false);
  });

  it("rejects durations above 180 seconds", () => {
    expect(validateClipDuration(181).valid).toBe(false);
    expect(validateClipDuration(3600).valid).toBe(false);
  });

  it("rejects non-numeric values", () => {
    expect(validateClipDuration(Number.NaN).valid).toBe(false);
    expect(validateClipDuration(Number.POSITIVE_INFINITY).valid).toBe(false);
  });

  it("rejects fractional seconds", () => {
    expect(validateClipDuration(60.5).valid).toBe(false);
    expect(validateClipDuration(120.25).valid).toBe(false);
  });

  it("provides an error message on failure", () => {
    const result = validateClipDuration(30);
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/at least 60/);
  });
});
