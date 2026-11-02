import {
  applyGenerationBump,
  buildBrowserStateMessage,
  createPendingForceGenerationTracker,
  createPortConnectionTracker,
  createRefreshCoordinator,
  probeNativeIntegrationStatus,
  queryFocusedNormalWindow,
  resolveRefreshObservation,
  shouldSkipStateSend,
} from "./hostname.mjs";

const NATIVE_HOST = "com.spotco.activity_tracker";
const HEARTBEAT_MS = 5000;
const HEARTBEAT_ALARM = "activity-tracker-heartbeat";
const BROWSER_ID = "brave";

const portTracker = createPortConnectionTracker();
const refreshCoordinator = createRefreshCoordinator();
const pendingForceGeneration = createPendingForceGenerationTracker();
let nativePort = null;
let activeConnectionId = 0;
let heartbeatTimer = null;
let initializationStarted = false;
let sessionEpoch = Date.now();
let sequence = 0;
let generation = 0;
let lastState = null;

const debugSnapshot = {
  extensionId: chrome.runtime.id,
  nativeConnected: false,
  messagesSent: 0,
  messagesAcknowledged: 0,
  lastNativeError: null,
  lastRefreshError: null,
  lastTrackerError: null,
  lastHostResponse: null,
  lastSentAt: null,
  lastSentPreview: null,
  sessionEpoch: null,
  sequence: null,
  generation: null,
  updatedAt: Date.now(),
};

function touchDebug(partial) {
  Object.assign(debugSnapshot, partial, { updatedAt: Date.now() });
}

function formatError(error) {
  if (!error) {
    return null;
  }
  if (typeof error === "string") {
    return error;
  }
  return error.message || String(error);
}

function beginSession() {
  sessionEpoch = Date.now();
  sequence = 0;
  generation = 0;
  lastState = null;
  touchDebug({ sessionEpoch, sequence, generation });
}

function attachPortListeners(port, connectionId) {
  port.onMessage.addListener(handleNativeResponse);
  port.onDisconnect.addListener(() => {
    const disconnectError = chrome.runtime.lastError?.message || "native_disconnected";
    if (!portTracker.isActive(connectionId)) {
      return;
    }
    if (nativePort === port) {
      nativePort = null;
    }
    portTracker.endConnection(connectionId);
    touchDebug({
      nativeConnected: false,
      lastNativeError: disconnectError,
    });
    scheduleReconnect();
  });
}

function handleNativeResponse(response) {
  touchDebug({ lastHostResponse: response });
  const trackerPayload = response?.tracker;
  if (trackerPayload?.command === "browser_state" || response?.type === "browser_state") {
    if (response?.ok && trackerPayload?.ok) {
      debugSnapshot.messagesAcknowledged += 1;
      touchDebug({ lastTrackerError: null });
    } else {
      touchDebug({
        lastTrackerError: trackerPayload?.error || response?.error || "tracker_rejected",
      });
    }
  }
}

function sendState(state, { forceGeneration = false, heartbeat = false } = {}) {
  generation = applyGenerationBump({
    forceGeneration,
    previous: lastState,
    next: state,
    generation,
  });
  if (shouldSkipStateSend({ heartbeat, previous: lastState, next: state, generation })) {
    return;
  }
  sequence += 1;
  const message = buildBrowserStateMessage({
    browserId: BROWSER_ID,
    sessionEpoch,
    sequence,
    generation,
    focused: state.focused,
    hostname: state.hostname,
  });
  lastState = { focused: state.focused, hostname: state.hostname, generation };
  touchDebug({
    sessionEpoch,
    sequence,
    generation,
    lastSentPreview: {
      focused: state.focused,
      hostname: state.hostname,
    },
  });
  const port = nativePort;
  if (!port) {
    scheduleReconnect();
    return;
  }
  try {
    port.postMessage(message);
    debugSnapshot.messagesSent += 1;
    touchDebug({ lastSentAt: Date.now(), lastNativeError: null });
  } catch (error) {
    touchDebug({ lastNativeError: formatError(error) });
    scheduleReconnect();
  }
}

async function waitForNativePort(maxAttempts = 20, delayMs = 50) {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (nativePort) {
      return true;
    }
    if (attempt === 0 && !portTracker.hasScheduledReconnect()) {
      connectNative();
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return Boolean(nativePort);
}

async function refreshState(options = {}) {
  if (!nativePort) {
    const connected = await waitForNativePort();
    if (!connected) {
      return;
    }
  }
  pendingForceGeneration.noteRefreshOptions(options);
  const requestId = refreshCoordinator.nextRequest();
  const result = await resolveRefreshObservation({
    coordinator: refreshCoordinator,
    requestId,
    queryFocusedNormalWindow,
    getAllWindows: chrome.windows.getAll.bind(chrome.windows),
  });
  if (!result.applied) {
    return;
  }
  sendState(result.observed, pendingForceGeneration.buildSendOptions(options));
}

function safeRefreshState(options = {}) {
  void refreshState(options).catch((error) => {
    touchDebug({ lastRefreshError: formatError(error) });
  });
}

function ensureHeartbeatAlarm() {
  chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 1 });
}

function connectNative() {
  portTracker.cancelReconnect();
  const connectionId = portTracker.beginConnection();
  const previousPort = nativePort;
  let nextPort;
  try {
    nextPort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (error) {
    portTracker.endConnection(connectionId);
    touchDebug({
      nativeConnected: false,
      lastNativeError: formatError(error) || "native_connect_failed",
    });
    scheduleReconnect();
    return;
  }
  beginSession();
  nativePort = nextPort;
  activeConnectionId = connectionId;
  touchDebug({ nativeConnected: true, lastNativeError: null });
  attachPortListeners(nextPort, connectionId);
  if (previousPort && previousPort !== nextPort) {
    try {
      previousPort.disconnect();
    } catch (_error) {
      // ignore
    }
  }
  safeRefreshState({ forceGeneration: true });
  startHeartbeat();
  ensureHeartbeatAlarm();
}

function scheduleReconnect() {
  stopHeartbeat();
  if (portTracker.hasScheduledReconnect()) {
    return;
  }
  portTracker.scheduleReconnect(connectNative, 2000);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (!nativePort) {
      connectNative();
      return;
    }
    safeRefreshState({ heartbeat: true });
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function probeIntegrationStatus() {
  return probeNativeIntegrationStatus(
    chrome.runtime.connectNative.bind(chrome.runtime),
    NATIVE_HOST,
  );
}

function buildDebugStatus() {
  return {
    ...debugSnapshot,
    nativePortOpen: Boolean(nativePort),
    heartbeatIntervalMs: HEARTBEAT_MS,
    nativeHostName: NATIVE_HOST,
  };
}

function initialize() {
  if (initializationStarted) {
    return;
  }
  initializationStarted = true;
  ensureHeartbeatAlarm();
  connectNative();
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== HEARTBEAT_ALARM) {
    return;
  }
  if (!nativePort) {
    connectNative();
    return;
  }
  safeRefreshState({ heartbeat: true });
});

chrome.runtime.onStartup.addListener(initialize);
chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "probe_status") {
    probeIntegrationStatus().then(sendResponse);
    return true;
  }
  if (message?.type === "debug_status") {
    sendResponse(buildDebugStatus());
    return false;
  }
  if (message?.type === "force_refresh") {
    refreshState({ forceGeneration: true })
      .then(() => sendResponse(buildDebugStatus()))
      .catch((error) => {
        touchDebug({ lastRefreshError: formatError(error) });
        sendResponse(buildDebugStatus());
      });
    return true;
  }
  return undefined;
});
chrome.tabs.onActivated.addListener(() => {
  safeRefreshState({ forceGeneration: true });
});
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === "complete") {
    if (tab.active) {
      safeRefreshState({ forceGeneration: Boolean(changeInfo.url) });
    }
  }
});
chrome.windows.onFocusChanged.addListener(() => {
  safeRefreshState({ forceGeneration: true });
});

initialize();
