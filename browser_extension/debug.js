const output = document.getElementById("output");

async function readDebugStatus() {
  try {
    return await chrome.runtime.sendMessage({ type: "debug_status" });
  } catch (error) {
    return { error: String(error?.message || error) };
  }
}

async function runProbe() {
  try {
    return await chrome.runtime.sendMessage({ type: "probe_status" });
  } catch (error) {
    return { error: String(error?.message || error) };
  }
}

async function render(extra = {}) {
  const debug = await readDebugStatus();
  const payload = {
    extensionId: chrome.runtime.id,
    debug,
    ...extra,
  };
  output.textContent = JSON.stringify(payload, null, 2);
}

document.getElementById("refresh").addEventListener("click", () => {
  void render();
});

document.getElementById("force").addEventListener("click", () => {
  void chrome.runtime.sendMessage({ type: "force_refresh" }).then((debug) => {
    void render({ afterForceRefresh: debug });
  });
});

document.getElementById("probe").addEventListener("click", () => {
  void runProbe().then((probe) => {
    void render({ probe });
  });
});

void render();
setInterval(() => {
  void render();
}, 2000);
