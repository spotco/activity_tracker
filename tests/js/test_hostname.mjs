import test from "node:test";
import assert from "node:assert/strict";

import {
  applyGenerationBump,
  buildBrowserStateMessage,
  createPendingForceGenerationTracker,
  createPortConnectionTracker,
  createRefreshCoordinator,
  extractHostname,
  interpretStatusResponse,
  isInternalUrl,
  nextGeneration,
  probeNativeIntegrationStatus,
  queryFocusedNormalWindow,
  raceSupersededForceRefresh,
  resolveRefreshObservation,
  shouldSkipStateSend,
} from "../../browser_extension/hostname.mjs";

test("extractHostname keeps subdomains separate", () => {
  assert.equal(extractHostname("https://docs.google.com/document"), "docs.google.com");
});

test("forceGeneration increments even when hostname is unchanged", () => {
  const previous = { focused: true, hostname: "github.com", generation: 2 };
  const next = { focused: true, hostname: "github.com" };
  assert.equal(
    applyGenerationBump({ forceGeneration: true, previous, next, generation: 2 }),
    3,
  );
});

test("connection ids are never reused across reconnects", () => {
  const tracker = createPortConnectionTracker();
  const first = tracker.beginConnection();
  tracker.endConnection(first);
  const second = tracker.beginConnection();
  assert.notEqual(first, second);
  assert.equal(tracker.isActive(first), false);
  assert.equal(tracker.isActive(second), true);
});

test("delayed old disconnect callback does not affect the active connection", () => {
  const tracker = createPortConnectionTracker();
  const first = tracker.beginConnection();
  tracker.endConnection(first);
  const second = tracker.beginConnection();
  if (tracker.isActive(first)) {
    tracker.endConnection(first);
  }
  assert.equal(tracker.isActive(second), true);
});

test("superseded forceGeneration is preserved for the next applied refresh", async () => {
  const coordinator = createRefreshCoordinator();
  const pendingForce = createPendingForceGenerationTracker();
  const hostname = { focused: true, hostname: "github.com" };
  const { slowResult, fastApplied, fastSendOptions } = await raceSupersededForceRefresh({
    coordinator,
    pendingForce,
    slowQuery: async () => hostname,
    fastQuery: async () => hostname,
    getAllWindows: async () => [],
  });
  assert.equal(slowResult.applied, false);
  assert.equal(fastApplied, true);
  assert.equal(fastSendOptions.forceGeneration, true);
  assert.equal(
    applyGenerationBump({
      forceGeneration: fastSendOptions.forceGeneration,
      previous: { focused: true, hostname: "github.com", generation: 2 },
      next: hostname,
      generation: 2,
    }),
    3,
  );
});

test("stale async refresh results are discarded", async () => {
  const coordinator = createRefreshCoordinator();
  const firstRequest = coordinator.nextRequest();
  const secondRequest = coordinator.nextRequest();
  let releaseSlow;
  const slowGate = new Promise((resolve) => {
    releaseSlow = resolve;
  });
  const slowQuery = async () => {
    await slowGate;
    return { focused: true, hostname: "stale.example" };
  };
  const fastQuery = async () => ({ focused: true, hostname: "fresh.example" });

  const slowPromise = resolveRefreshObservation({
    coordinator,
    requestId: firstRequest,
    queryFocusedNormalWindow: slowQuery,
    getAllWindows: async () => [],
  });
  const fastResult = await resolveRefreshObservation({
    coordinator,
    requestId: secondRequest,
    queryFocusedNormalWindow: fastQuery,
    getAllWindows: async () => [],
  });
  assert.deepEqual(fastResult, {
    applied: true,
    observed: { focused: true, hostname: "fresh.example" },
  });
  releaseSlow();
  const slowResult = await slowPromise;
  assert.equal(slowResult.applied, false);
  assert.equal(slowResult.observed.hostname, "stale.example");
});

test("heartbeat sends unchanged state", () => {
  const previous = { focused: true, hostname: "github.com", generation: 2 };
  const next = { focused: true, hostname: "github.com" };
  assert.equal(shouldSkipStateSend({ heartbeat: true, previous, next, generation: 2 }), false);
});

test("buildBrowserStateMessage includes session fields", () => {
  const message = buildBrowserStateMessage({
    browserId: "brave",
    sessionEpoch: 42,
    sequence: 3,
    generation: 2,
    focused: true,
    hostname: "github.com",
  });
  assert.equal(message.session_epoch, 42);
});

test("interpretStatusResponse requires tracker acknowledgement", () => {
  assert.deepEqual(
    interpretStatusResponse({ ok: true, tracker: { ok: true, command: "ping" } }),
    { nativeHost: true, tracker: true, trackerError: null },
  );
});

test("probeNativeIntegrationStatus tolerates disconnect before response delivery", async () => {
  const listeners = { message: [], disconnect: [] };
  const port = {
    postMessage() {
      queueMicrotask(() => {
        for (const callback of listeners.disconnect) {
          callback();
        }
        queueMicrotask(() => {
          for (const callback of listeners.message) {
            callback({ ok: true, tracker: { ok: true, command: "ping" } });
          }
        });
      });
    },
    onMessage: { addListener(callback) { listeners.message.push(callback); } },
    onDisconnect: { addListener(callback) { listeners.disconnect.push(callback); } },
    disconnect() {},
  };
  const probe = await probeNativeIntegrationStatus(() => port, "com.example", {
    disconnectGraceMs: 10,
  });
  assert.equal(probe.nativeHost, true);
  assert.equal(probe.tracker, true);
});

test("queryFocusedNormalWindow ignores incognito tabs", async () => {
  const incognito = await queryFocusedNormalWindow(async () => [
    { focused: true, incognito: true, tabs: [{ active: true, url: "https://example.com" }] },
  ]);
  assert.deepEqual(incognito, { focused: false, hostname: null });
  assert.equal(isInternalUrl("brave://settings/"), true);
});

test("nextGeneration advances on hostname changes", () => {
  assert.equal(nextGeneration({ focused: true, hostname: "a.com" }, { focused: true, hostname: "b.com" }, 3), 4);
});
