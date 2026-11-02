export function extractHostname(urlString) {
  if (!urlString) {
    return null;
  }
  try {
    const url = new URL(urlString);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }
    const host = url.hostname.trim().toLowerCase().replace(/\.$/, "");
    return host || null;
  } catch (_error) {
    return null;
  }
}

export function isIncognitoWindow(window) {
  return Boolean(window && window.incognito);
}

export function isInternalUrl(urlString) {
  if (!urlString) {
    return true;
  }
  const lower = urlString.toLowerCase();
  return (
    lower.startsWith("chrome://") ||
    lower.startsWith("brave://") ||
    lower.startsWith("edge://") ||
    lower.startsWith("devtools://") ||
    lower.startsWith("chrome-extension://") ||
    lower.startsWith("about:") ||
    lower.startsWith("chrome-search://")
  );
}

export async function queryFocusedNormalWindow(getAllWindows) {
  const windows = await getAllWindows({ populate: true, windowTypes: ["normal"] });
  const focused = windows.filter((window) => window.focused && !isIncognitoWindow(window));
  if (focused.length === 0) {
    return { focused: false, hostname: null };
  }
  const window = focused[0];
  const activeTab = (window.tabs || []).find((tab) => tab.active);
  if (!activeTab || activeTab.incognito) {
    return { focused: false, hostname: null };
  }
  if (isInternalUrl(activeTab.url)) {
    return { focused: true, hostname: null };
  }
  return { focused: true, hostname: extractHostname(activeTab.url) };
}

export function nextGeneration(previous, next, generation) {
  if (
    !previous ||
    previous.focused !== next.focused ||
    previous.hostname !== next.hostname
  ) {
    return generation + 1;
  }
  return generation;
}

export function applyGenerationBump({ forceGeneration, previous, next, generation }) {
  if (forceGeneration) {
    return generation + 1;
  }
  if (!previous) {
    return generation + 1;
  }
  if (previous.focused !== next.focused || previous.hostname !== next.hostname) {
    return nextGeneration(previous, next, generation);
  }
  return generation;
}

export function buildBrowserStateMessage({
  browserId,
  sessionEpoch,
  sequence,
  generation,
  focused,
  hostname,
}) {
  return {
    type: "browser_state",
    browser: browserId,
    session_epoch: sessionEpoch,
    sequence,
    generation,
    focused,
    hostname,
  };
}

export function shouldSkipStateSend({ heartbeat, previous, next, generation }) {
  if (heartbeat) {
    return false;
  }
  return (
    previous &&
    previous.focused === next.focused &&
    previous.hostname === next.hostname &&
    previous.generation === generation
  );
}

export function interpretStatusResponse(response) {
  if (!response) {
    return { nativeHost: false, tracker: false, trackerError: "no_response" };
  }
  const trackerOk = Boolean(response.tracker && response.tracker.ok);
  return {
    nativeHost: true,
    tracker: trackerOk,
    trackerError: trackerOk ? null : response.tracker?.error || response.error || "tracker_unavailable",
  };
}

export function probeNativeIntegrationStatus(
  connectNative,
  nativeHostName,
  { timeoutMs = 3000, disconnectGraceMs = 50 } = {},
) {
  return new Promise((resolve) => {
    let port;
    try {
      port = connectNative(nativeHostName);
    } catch (error) {
      resolve({
        nativeHost: false,
        tracker: false,
        trackerError: "native_connect_failed",
        detail: error?.message || null,
      });
      return;
    }
    let settled = false;
    const finish = (value) => {
      if (settled) {
        return;
      }
      settled = true;
      try {
        port.disconnect();
      } catch (_error) {
        // ignore
      }
      resolve(value);
    };
    const timeout = setTimeout(
      () => finish({ nativeHost: true, tracker: false, trackerError: "timeout" }),
      timeoutMs,
    );
    port.onMessage.addListener((response) => {
      clearTimeout(timeout);
      finish(interpretStatusResponse(response));
    });
    port.onDisconnect.addListener(() => {
      const lastError =
        typeof chrome !== "undefined" ? chrome.runtime?.lastError?.message || null : null;
      clearTimeout(timeout);
      if (settled) {
        return;
      }
      setTimeout(() => {
        if (settled) {
          return;
        }
        finish({
          nativeHost: false,
          tracker: false,
          trackerError: lastError || "native_disconnected",
        });
      }, disconnectGraceMs);
    });
    try {
      port.postMessage({ type: "status" });
    } catch (error) {
      clearTimeout(timeout);
      finish({
        nativeHost: false,
        tracker: false,
        trackerError: error?.message || "native_post_failed",
      });
    }
  });
}

export function createPortConnectionTracker() {
  let nextConnectionId = 0;
  let activeConnectionId = 0;
  let reconnectTimer = null;

  return {
    beginConnection() {
      nextConnectionId += 1;
      activeConnectionId = nextConnectionId;
      return activeConnectionId;
    },
    isActive(connectionId) {
      return connectionId === activeConnectionId;
    },
    endConnection(connectionId) {
      if (connectionId === activeConnectionId) {
        activeConnectionId = 0;
      }
    },
    scheduleReconnect(callback, delayMs) {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        callback();
      }, delayMs);
    },
    cancelReconnect() {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    },
    hasScheduledReconnect() {
      return reconnectTimer !== null;
    },
  };
}

export function handlePortDisconnect({
  connectionId,
  activeConnectionId,
  scheduleReconnect,
  isActive,
}) {
  if (!isActive(connectionId)) {
    return { replaced: true, shouldReconnect: false };
  }
  return { replaced: false, shouldReconnect: true, schedule: scheduleReconnect };
}

export function createRefreshCoordinator() {
  let latestRequestId = 0;
  return {
    nextRequest() {
      latestRequestId += 1;
      return latestRequestId;
    },
    isCurrent(requestId) {
      return requestId === latestRequestId;
    },
  };
}

export async function resolveRefreshObservation({
  coordinator,
  requestId,
  queryFocusedNormalWindow,
  getAllWindows,
}) {
  const observed = await queryFocusedNormalWindow(getAllWindows);
  if (!coordinator.isCurrent(requestId)) {
    return { applied: false, observed };
  }
  return { applied: true, observed };
}

export function createPendingForceGenerationTracker() {
  let pending = false;
  return {
    noteRefreshOptions(options) {
      if (options.forceGeneration) {
        pending = true;
      }
    },
    buildSendOptions(options) {
      const forceGeneration = pending || Boolean(options.forceGeneration);
      if (pending) {
        pending = false;
      }
      return {
        heartbeat: Boolean(options.heartbeat),
        forceGeneration,
      };
    },
    isPending() {
      return pending;
    },
  };
}

export async function raceSupersededForceRefresh({
  coordinator,
  pendingForce,
  slowQuery,
  fastQuery,
  getAllWindows,
}) {
  pendingForce.noteRefreshOptions({ forceGeneration: true });
  const slowRequestId = coordinator.nextRequest();
  const fastRequestId = coordinator.nextRequest();
  let releaseSlow;
  const slowGate = new Promise((resolve) => {
    releaseSlow = resolve;
  });
  const slowPromise = (async () => {
    await slowGate;
    const observed = await slowQuery(getAllWindows);
    return {
      applied: coordinator.isCurrent(slowRequestId),
      observed,
    };
  })();
  const fastObserved = await fastQuery(getAllWindows);
  const fastApplied = coordinator.isCurrent(fastRequestId);
  releaseSlow();
  const slowResult = await slowPromise;
  const fastSendOptions = fastApplied ? pendingForce.buildSendOptions({ heartbeat: true }) : null;
  return { slowResult, fastApplied, fastSendOptions };
}
