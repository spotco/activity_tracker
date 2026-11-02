function formatProbeStatus(probe) {
  if (!probe?.nativeHost) {
    const detail = probe?.trackerError || probe?.detail || "unavailable";
    return `Native host: unavailable (${detail})`;
  }
  if (!probe.tracker) {
    const detail = probe.trackerError || "unavailable";
    return `Native host: ok; tracker: unavailable (${detail})`;
  }
  return "Native host: ok; tracker: acknowledged";
}

async function refreshPopup() {
  const status = document.getElementById("status");
  const hostname = document.getElementById("hostname");
  const extensionId = document.getElementById("extension-id");
  const debugEl = document.getElementById("debug");
  if (extensionId) {
    extensionId.textContent = `Extension ID: ${chrome.runtime.id}`;
  }

  let probe = null;
  let debug = null;
  try {
    probe = await chrome.runtime.sendMessage({ type: "probe_status" });
    debug = await chrome.runtime.sendMessage({ type: "debug_status" });
  } catch (error) {
    status.textContent = `Native host: service worker unavailable (${error?.message || error})`;
    return;
  }

  status.textContent = formatProbeStatus(probe);
  if (debugEl) {
    if (debug?.nativePortOpen) {
      debugEl.textContent =
        `Extension channel: connected; sent ${debug.messagesSent}; ack ${debug.messagesAcknowledged}` +
        (debug.lastTrackerError ? `; tracker error: ${debug.lastTrackerError}` : "");
    } else {
      const detail = debug?.lastNativeError || debug?.lastRefreshError || "connecting";
      debugEl.textContent = `Extension channel: disconnected (${detail})`;
    }
  }

  // The popup itself is focused while it is open, so getAll()+focused misses the page.
  const lastFocused = await chrome.windows.getLastFocused({
    populate: true,
    windowTypes: ["normal"],
  });
  if (!lastFocused || lastFocused.incognito) {
    hostname.textContent = "hostname: unknown (no focused window)";
    return;
  }
  const tab = (lastFocused.tabs || []).find((entry) => entry.active);
  if (!tab || !tab.url) {
    hostname.textContent = "hostname: unknown";
    return;
  }
  try {
    const parsed = new URL(tab.url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      hostname.textContent = "hostname: unknown (non-http page)";
      return;
    }
    hostname.textContent = `hostname: ${parsed.hostname.toLowerCase()}`;
  } catch (_error) {
    hostname.textContent = "hostname: unknown";
  }
}

refreshPopup();
