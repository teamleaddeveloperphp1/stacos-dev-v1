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

// Must come first: it puts htmx on `window`, which the extensions below read at
// import time. See htmx-setup.js.
import htmx from "./htmx-setup.js";
import "htmx-ext-preload";
import Alpine from "alpinejs";
import { Idiomorph } from "idiomorph";
import { Dropdown, Modal, Offcanvas, Toast, Tooltip } from "bootstrap";

window.Alpine = Alpine;

// ---------------------------------------------------------------------------
// Morph swap
//
// Patching the DOM instead of replacing it is what preserves scroll position,
// focus, text selection and open dropdowns across an update. It accounts for
// most of the difference between "a web page reloaded" and "the app responded".
// ---------------------------------------------------------------------------
htmx.defineExtension("morph", {
  isInlineSwap: (swapStyle) => String(swapStyle).startsWith("morph"),

  handleSwap: (swapStyle, target, fragment) => {
    const style = String(swapStyle);
    if (!style.startsWith("morph")) return false;

    // `morph` replaces the target element; `morph:innerHTML` replaces its
    // children. The distinction matters: the shell targets #main, and morphing
    // that with outerHTML turns #main *into* the fragment — destroying the very
    // element every later navigation targets.
    const morphStyle = style.split(":")[1] || "outerHTML";

    const holder = document.createElement("div");
    holder.append(
      ...(fragment.nodeType === Node.DOCUMENT_FRAGMENT_NODE
        ? Array.from(fragment.childNodes)
        : [fragment]),
    );

    Idiomorph.morph(target, holder.innerHTML, {
      morphStyle,
      callbacks: {
        // Alpine components own their subtree; morphing into them fights
        // Alpine's reactivity and produces flicker.
        beforeNodeMorphed: (oldNode) =>
          !(oldNode instanceof Element && oldNode.hasAttribute("data-no-morph")),
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
// Modals
//
// Modal bodies are fetched on demand into #modal-container rather than rendered
// hidden into every page — there will eventually be a lot of them, and the shell
// has to stay small.
//
// Two things have to be handled here because HTMX cannot know about them: the
// modal must be shown once its markup lands, and it must be disposed when
// dismissed so a reopened modal does not stack backdrops.
// ---------------------------------------------------------------------------
document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.target?.id !== "modal-container") return;

  const el = event.detail.target.querySelector(".modal");
  if (!el) return;

  const modal = Modal.getOrCreateInstance(el);
  modal.show();

  el.addEventListener(
    "hidden.bs.modal",
    () => {
      modal.dispose();
      event.detail.target.innerHTML = "";
    },
    { once: true },
  );
});

// A successful submit inside a modal asks it to close via HX-Trigger. The
// server decides, because only the server knows whether the save succeeded.
document.body.addEventListener("stacos:modal-close", () => {
  document.querySelectorAll("#modal-container .modal").forEach((el) => {
    Modal.getOrCreateInstance(el).hide();
  });
});

// A 422 re-renders the form inside the still-open modal. Without this the
// response would be swapped into the list target and the modal would sit there
// looking like nothing happened.
document.body.addEventListener("htmx:beforeSwap", (event) => {
  if (event.detail.xhr?.status !== 422) return;
  const container = document.getElementById("modal-container");
  if (!container || !container.querySelector(".modal")) return;
  event.detail.shouldSwap = true;
  event.detail.target = container;
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

/**
 * The command palette.
 *
 * Registered as an Alpine component rather than written inline, because it has
 * real behaviour: the footer promises ↑/↓ to navigate and Enter to open, and a
 * palette that advertises keyboard control without implementing it is worse than
 * one that does not mention it.
 *
 * Items are re-queried on every access rather than cached, because HTMX replaces
 * the results list on each keystroke.
 */
Alpine.data("palette", () => ({
  open: false,
  index: 0,

  init() {
    document.addEventListener("stacos:palette-open", () => this.show());
  },

  show() {
    this.open = true;
    this.index = 0;
    this.$nextTick(() => {
      this.$refs.input?.focus();
      this.$refs.input?.select();
      this.mark();
    });
  },

  hide() {
    this.open = false;
  },

  items() {
    return Array.from(this.$refs.results?.querySelectorAll("[data-palette-item]") ?? []);
  },

  // Called after each HTMX swap: the previously highlighted node no longer exists.
  reset() {
    this.index = 0;
    this.mark();
  },

  mark() {
    const items = this.items();
    items.forEach((el, i) => el.setAttribute("aria-selected", String(i === this.index)));
    items[this.index]?.scrollIntoView({ block: "nearest" });
  },

  move(delta) {
    const count = this.items().length;
    if (!count) return;
    this.index = (this.index + delta + count) % count;
    this.mark();
  },

  choose() {
    this.items()[this.index]?.click();
  },
}));

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
