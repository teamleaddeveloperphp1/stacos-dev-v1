/**
 * STACOS client-side behaviour.
 *
 * Deliberately small. HTMX handles every server interaction; Alpine handles
 * purely local state (dropdowns, tabs, optimistic toggles). There is no router,
 * no store, and no component framework — the server owns the HTML.
 *
 * What lives here is the handful of things HTMX cannot do by itself: the
 * idiomorph swap, the toast bridge, the command palette shortcut, and honest
 * loading feedback.
 */

import "htmx.org";
import "htmx-ext-preload";
import "htmx-ext-sse";
import Alpine from "alpinejs";
import { Idiomorph } from "idiomorph";
import { Dropdown, Modal, Offcanvas, Toast, Tooltip } from "bootstrap";

window.htmx = window.htmx || htmx;
window.Alpine = Alpine;

// ---------------------------------------------------------------------------
// Morph swap
//
// Patching the DOM instead of replacing it is what preserves scroll position,
// focus, text selection and open dropdowns across an update. It accounts for
// most of the difference between "a web page reloaded" and "the app responded".
// ---------------------------------------------------------------------------
htmx.defineExtension("morph", {
  isInlineSwap: (swapStyle) => swapStyle === "morph",
  handleSwap: (swapStyle, target, fragment) => {
    if (swapStyle !== "morph") return false;
    Idiomorph.morph(target, fragment.outerHTML ?? fragment.innerHTML, {
      morphStyle: "outerHTML",
      callbacks: {
        // Alpine components manage their own subtree; morphing into them fights
        // Alpine's reactivity and produces flicker.
        beforeNodeMorphed: (oldNode) =>
          !(oldNode instanceof Element && oldNode.hasAttribute("x-data-ignore-morph")),
      },
    });
    return true;
  },
});

htmx.config.defaultSwapStyle = "morph";
htmx.config.globalViewTransitions = true;
htmx.config.historyCacheSize = 20;
// Errors should render, not vanish: a 422 carrying re-rendered form errors is a
// normal outcome, not an exception.
htmx.config.responseHandling = [
  { code: "204", swap: false },
  { code: "[23]..", swap: true },
  { code: "422", swap: true, error: false },
  { code: "4..", swap: false, error: true },
  { code: "5..", swap: false, error: true },
];

// ---------------------------------------------------------------------------
// Toasts, delivered by the HX-Trigger header
// ---------------------------------------------------------------------------
document.body.addEventListener("stacos:toast", (event) => {
  const { message, level = "success", title = "" } = event.detail ?? {};
  if (!message) return;
  showToast({ message, level, title });
});

function showToast({ message, level, title }) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;

  const el = document.createElement("div");
  el.className = `toast align-items-center border-0 status-chip--${level}`;
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.setAttribute("aria-atomic", "true");
  el.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">
        ${title ? `<strong class="d-block">${escapeHtml(title)}</strong>` : ""}
        ${escapeHtml(message)}
      </div>
      <button type="button" class="btn-close me-2 m-auto"
              data-bs-dismiss="toast" aria-label="Close"></button>
    </div>`;

  stack.appendChild(el);
  const toast = new Toast(el, { delay: 5000 });
  el.addEventListener("hidden.bs.toast", () => el.remove());
  toast.show();
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value);
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Route progress
//
// Only shown once a request has been in flight past ~180ms. Showing it
// immediately makes fast responses feel slower, because the eye registers the
// bar rather than the result.
// ---------------------------------------------------------------------------
let progressTimer = null;
const progress = () => document.getElementById("route-progress");

document.body.addEventListener("htmx:beforeRequest", (event) => {
  if (!event.detail.boosted && !event.target.hasAttribute("hx-push-url")) return;
  progressTimer = window.setTimeout(() => progress()?.classList.add("is-active"), 180);
});

document.body.addEventListener("htmx:afterRequest", () => {
  window.clearTimeout(progressTimer);
  const bar = progress();
  if (!bar || !bar.classList.contains("is-active")) return;
  bar.classList.add("is-done");
  window.setTimeout(() => bar.classList.remove("is-active", "is-done"), 200);
});

// Announce swapped regions to screen readers. HTMX partial updates are silent
// to assistive technology otherwise — the classic HTMX accessibility failure.
document.body.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail.target;
  if (target && target.id === "main") {
    const heading = target.querySelector("h1, [role=heading]");
    const announcer = document.getElementById("route-announcer");
    if (announcer && heading) announcer.textContent = heading.textContent.trim();
  }
  initTooltips(target);
});

// ---------------------------------------------------------------------------
// Command palette and keyboard shortcuts
// ---------------------------------------------------------------------------
const SHORTCUTS = {
  c: "/app/entities/",
  o: "/app/",
  e: "/app/entities/",
};
let chordPending = false;

document.addEventListener("keydown", (event) => {
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) ||
    event.target.isContentEditable;

  // Cmd/Ctrl-K opens the palette from anywhere, including a text field.
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
    return;
  }

  if (inField) return;

  if (event.key === "/") {
    event.preventDefault();
    openPalette();
    return;
  }

  if (event.key === "?") {
    event.preventDefault();
    document.dispatchEvent(new CustomEvent("stacos:shortcuts"));
    return;
  }

  // `g` then a letter — "go to".
  if (event.key === "g" && !chordPending) {
    chordPending = true;
    window.setTimeout(() => (chordPending = false), 1200);
    return;
  }

  if (chordPending) {
    chordPending = false;
    const destination = SHORTCUTS[event.key.toLowerCase()];
    if (destination) {
      event.preventDefault();
      htmx.ajax("GET", destination, { target: "#main", swap: "morph", pushUrl: true });
    }
  }
});

function openPalette() {
  document.dispatchEvent(new CustomEvent("stacos:palette-open"));
}

// ---------------------------------------------------------------------------
// Bootstrap widget initialisation
// ---------------------------------------------------------------------------
function initTooltips(root = document) {
  root.querySelectorAll?.('[data-bs-toggle="tooltip"]').forEach((el) => {
    if (!Tooltip.getInstance(el)) new Tooltip(el);
  });
}

// Theme: respect the stored choice, otherwise follow the operating system.
(function applyTheme() {
  const stored = localStorage.getItem("stacos-theme");
  const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.setAttribute("data-bs-theme", stored || preferred);
})();

window.stacosToggleTheme = function toggleTheme() {
  const next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-bs-theme", next);
  localStorage.setItem("stacos-theme", next);
};

initTooltips();
Alpine.start();

export { Dropdown, Modal, Offcanvas, Toast, showToast };
