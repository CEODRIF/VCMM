import { describe, expect, it } from "vitest";

import { appConfig } from "@/config/app";

describe("application configuration", () => {
  it("defines the ClipForge AI brand", () => {
    expect(appConfig.name).toBe("ClipForge AI");
  });

  it("enforces the product clip duration bounds (60-180s, default 60s)", () => {
    expect(appConfig.clips.minSeconds).toBe(60);
    expect(appConfig.clips.maxSeconds).toBe(180);
    expect(appConfig.clips.defaultSeconds).toBe(60);
    expect(appConfig.clips.defaultSeconds).toBeGreaterThanOrEqual(appConfig.clips.minSeconds);
    expect(appConfig.clips.defaultSeconds).toBeLessThanOrEqual(appConfig.clips.maxSeconds);
  });

  it("enforces the clip count bounds", () => {
    expect(appConfig.clips.minCount).toBeGreaterThanOrEqual(1);
    expect(appConfig.clips.maxCount).toBeGreaterThanOrEqual(appConfig.clips.minCount);
  });

  it("enforces vertical 1080x1920 output", () => {
    expect(appConfig.output.aspectRatio).toBe("9:16");
    expect(appConfig.output.width).toBe(1080);
    expect(appConfig.output.height).toBe(1920);
    expect(appConfig.output.resolutionLabel).toBe("1080p");
  });

  it("lists all nine processing states in order (Phase 3 ingestion lifecycle)", () => {
    expect(appConfig.processingStates).toEqual([
      "uploading",
      "queued",
      "validating",
      "ready",
      "transcribing",
      "analyzing",
      "rendering",
      "completed",
      "failed",
    ]);
  });

  it("keeps access-code defaults safe", () => {
    expect(appConfig.access.codeMinLength).toBeGreaterThanOrEqual(6);
    expect(appConfig.access.codeMaxLength).toBeGreaterThanOrEqual(appConfig.access.codeMinLength);
  });
});
