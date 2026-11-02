const targets = await fetch("http://127.0.0.1:9222/json").then((response) => response.json());
const worker = targets.find((target) =>
  String(target.url || "").startsWith("chrome-extension://pccnbfglhhflbjeapnjgijgdogfgjpcl/"),
);
if (!worker) {
  console.error("service worker target not found");
  process.exit(1);
}
const ws = new WebSocket(worker.webSocketDebuggerUrl);

const expression = `(() => new Promise((resolve) => {
  let port;
  try {
    port = chrome.runtime.connectNative("com.spotco.activity_tracker");
  } catch (error) {
    resolve({ thrown: String(error && error.message || error) });
    return;
  }
  const immediate = chrome.runtime.lastError && chrome.runtime.lastError.message;
  if (immediate) {
    resolve({ immediate });
    return;
  }
  const timer = setTimeout(() => {
    resolve({ timeout: true });
  }, 2000);
  port.onDisconnect.addListener(() => {
    clearTimeout(timer);
    resolve({
      disconnected: true,
      lastError: (chrome.runtime.lastError && chrome.runtime.lastError.message) || null,
    });
  });
  port.onMessage.addListener((message) => {
    clearTimeout(timer);
    resolve({ message });
  });
  try {
    port.postMessage({ type: "status" });
  } catch (error) {
    clearTimeout(timer);
    resolve({ postFailed: String(error && error.message || error) });
  }
}))()`;

ws.addEventListener("open", () => {
  ws.send(
    JSON.stringify({
      id: 1,
      method: "Runtime.evaluate",
      params: {
        expression,
        awaitPromise: true,
        returnByValue: true,
      },
    }),
  );
});

ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id !== 1) {
    return;
  }
  console.log(JSON.stringify(message.result || message, null, 2));
  ws.close();
  process.exit(0);
});

setTimeout(() => {
  console.error("cdp timeout");
  process.exit(1);
}, 8000);
