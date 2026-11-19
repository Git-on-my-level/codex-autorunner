import { describe, expect, test } from "bun:test";
import { CarConfig } from "../../src/config/config.ts";

describe("agentctl observer config", () => {
  test("stays off by default for the no-dependency native setup", () => {
    const observer = CarConfig.parse({}).agentctl_observer;
    expect(observer.enabled).toBe(false);
    expect(observer.retention_days).toBe(30);
    expect(observer.retention_max_terminal).toBe(2_000);
  });

  test("requires explicit labels or explicit observe-all scope", () => {
    expect(() => CarConfig.parse({ agentctl_observer: { enabled: true, required_labels: [], observe_all: false } })).toThrow(/required_labels/);
    expect(CarConfig.parse({ agentctl_observer: { enabled: true, required_labels: ["car-observe"] } }).agentctl_observer.enabled).toBe(true);
    expect(CarConfig.parse({ agentctl_observer: { enabled: true, required_labels: [], observe_all: true } }).agentctl_observer.observe_all).toBe(true);
    expect(() => CarConfig.parse({ agentctl_observer: { discovery_limit: 201 } })).toThrow(/200/);
  });
});
