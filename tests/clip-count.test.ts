import { describe, expect, it } from "vitest";

import { appConfig } from "@/config/app";
import { validateClipCount } from "@/lib/validation/clips";

describe("clip count validation", () => {
  it("accepts every count in the configured range", () => {
    for (let count = appConfig.clips.minCount; count <= appConfig.clips.maxCount; count++) {
      expect(validateClipCount(count).valid).toBe(true);
    }
  });

  it("rejects zero clips", () => {
    expect(validateClipCount(0).valid).toBe(false);
  });

  it("rejects counts above the maximum", () => {
    expect(validateClipCount(appConfig.clips.maxCount + 1).valid).toBe(false);
    expect(validateClipCount(100).valid).toBe(false);
  });

  it("rejects negative counts", () => {
    expect(validateClipCount(-1).valid).toBe(false);
  });

  it("rejects non-integer counts", () => {
    expect(validateClipCount(2.5).valid).toBe(false);
  });

  it("rejects non-numeric counts", () => {
    expect(validateClipCount(Number.NaN).valid).toBe(false);
  });

  it("provides an error message on failure", () => {
    const result = validateClipCount(0);
    expect(result.valid).toBe(false);
    expect(result.error).toBeTruthy();
  });
});
