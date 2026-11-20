// Registers the service worker (installable app). No inline scripts: the CSP forbids them.
"use strict";
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("service worker", e));
  });
}

// Rule form: choosing a time or a number of hours also picks that option's radio button.
document.addEventListener("change", (event) => {
  const choice = event.target.closest(".target-choice");
  if (choice && event.target.tagName === "SELECT") {
    choice.querySelector('input[type="radio"]').checked = true;
  }
});

// "← Back": where the user came from (settings page or dashboard), like the back gesture.
// Opened directly (no page of ours before): the link's own target.
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-back]");
  if (!link) return;
  if (document.referrer.startsWith(location.origin) && history.length > 1) {
    event.preventDefault();
    history.back();
  }
});

// Back to the app after a while (other tab, phone unlocked, page from the back/forward
// cache): the status was frozen meanwhile, so read it at once instead of at the next tick.
function resume() {
  const status = document.getElementById("status");
  if (status && window.htmx) htmx.trigger(status, "resume");
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") resume();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) resume();
});
