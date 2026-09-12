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
  // Only an outerHTML morph is an "inline" swap. `morph:innerHTML` is not, and
  // saying otherwise breaks out-of-band swaps: HTMX hands an inline swap the OOB
  // *element* rather than its content, so the element would be morphed inside
  // itself. Matches the official idiomorph extension.
  isInlineSwap: (swapStyle) => {
    const style = String(swapStyle);
    return style === "morph" || style === "morph:outerHTML";
  },

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

    // The return value is load-bearing, and returning `true` here was the single
    // most damaging bug in this file. HTMX only schedules `htmx.process()` for
    // swapped-in nodes when an extension returns an *array* of them; anything
    // else truthy makes it skip settling entirely. The symptom is that every
    // `hx-*` attribute inside anything swapped into the page is inert until a
    // full browser reload — "Load more" does nothing, modal buttons do nothing,
    // panel actions do nothing, but all of them work after F5. Idiomorph already
    // returns exactly the node array HTMX wants, so pass it straight through.
    return Idiomorph.morph(target, holder.innerHTML, {
      morphStyle,
      callbacks: {
        // Alpine components own their subtree; morphing into them fights
        // Alpine's reactivity and produces flicker.
        beforeNodeMorphed: (oldNode) =>
          !(oldNode instanceof Element && oldNode.hasAttribute("data-no-morph")),
      },
    });
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

// The status vocabulary is universal in this product — the same colour and word
// for the same meaning everywhere — so a toast level has to be translated into
// it rather than used as a class name directly. `components/toast.html` does the
// same mapping for toasts rendered on a full page load; the two must agree or a
// message looks different depending on how it arrived.
const TOAST_STATUS = {
  error: "overdue",
  danger: "overdue",
  warning: "due-soon",
  success: "on-track",
};

function showToast({ message, level, title }) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;

  const status = TOAST_STATUS[level] ?? "in-progress";
  const el = document.createElement("div");
  el.className = `toast align-items-center border-0 status-chip--${status}`;
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
// Failed requests
//
// `responseHandling` above deliberately discards the body of every 4xx and 5xx:
// an error page must never be injected into #main. But discarding it silently is
// how the app came to look frozen — the click registered, the server refused,
// and nothing on screen moved, so people clicked again. HTMX raises an event for
// each of these; something has to listen.
//
// A session that has expired is not handled here. The server answers those with
// HX-Redirect so the browser navigates to the sign-in screen properly, which is
// the only correct outcome — see AuthorizationExceptionMiddleware.
// ---------------------------------------------------------------------------
function errorToast(status) {
  if (status === 401 || status === 403) {
    return {
      level: "danger",
      title: "Not allowed",
      message: "You do not have permission to do that.",
    };
  }
  if (status === 404) {
    return {
      level: "warning",
      title: "Not found",
      message: "That is no longer there. Refresh the page to see the current state.",
    };
  }
  if (status === 409 || status === 429) {
    return {
      level: "warning",
      title: "Try again",
      message: "That could not be completed just now. Please try again in a moment.",
    };
  }
  return {
    level: "danger",
    title: "Something went wrong",
    message: "That did not save. Please try again.",
  };
}

document.body.addEventListener("htmx:responseError", (event) => {
  showToast(errorToast(event.detail.xhr?.status));
});

// The request never reached the server, or never came back: offline, DNS,
// a dropped connection, a proxy timeout. Worth wording differently, because
// "try again" is genuinely the right advice here and usually is not for a 500.
function connectionToast() {
  showToast({
    level: "danger",
    title: "No connection",
    message: "We could not reach STACOS. Check your connection and try again.",
  });
}

document.body.addEventListener("htmx:sendError", connectionToast);
document.body.addEventListener("htmx:timeout", connectionToast);

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
// `g` then a letter. Keep this in step with PALETTE_DESTINATIONS in
// stacos/tenancy/views.py — that tuple carries the key hints the palette
// actually shows the user, and a hint that names a chord which goes somewhere
// else is worse than no hint at all.
const SHORTCUTS = {
  o: "/app/",
  e: "/app/entities/",
  c: "/app/compliance/",
};

//: Rendered by the shortcuts help dialog. Kept beside the map it documents so
//: the two cannot drift.
const SHORTCUT_HELP = [
  {
    group: "Go to",
    items: [
      { keys: ["g", "o"], label: "Dashboard" },
      { keys: ["g", "e"], label: "Entities" },
      { keys: ["g", "c"], label: "Compliance calendar" },
    ],
  },
  {
    group: "Anywhere",
    items: [
      { keys: ["Ctrl", "K"], label: "Search and commands" },
      { keys: ["/"], label: "Search and commands" },
      { keys: ["?"], label: "This list" },
      { keys: ["Esc"], label: "Close" },
    ],
  },
];

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
      navigate(destination);
    }
  }
});

// ---------------------------------------------------------------------------
// Sidebar highlighting
//
// The shell renders once and #main is all that navigation replaces, so nothing
// in the sidebar re-renders after a click. The active item used to be decided
// server-side from `request.resolver_match`, which meant it was correct for
// whatever page the browser last *loaded* and stale for every boosted navigation
// after it — the highlight only caught up on a manual refresh.
//
// So the decision moved here, and it moved here rather than being duplicated
// here: the shell no longer emits `aria-current` at all. Two copies of this rule
// is how it broke in the first place, with some links compared by url_name, some
// by namespace, and two not compared at all.
//
// Each link declares the URL prefix it owns as `data-nav-match`. Longest match
// wins, so /app/practice/profitability/ highlights Profitability rather than the
// Work board it also sits under. `data-nav-exact` is for Dashboard, whose /app/
// prefix would otherwise match every page in the product.
// ---------------------------------------------------------------------------
function syncNav() {
  const path = window.location.pathname;
  const links = document.querySelectorAll(".app-nav__link[data-nav-match]");

  let best = null;
  let bestLength = 0;

  links.forEach((link) => {
    const prefix = link.dataset.navMatch;
    const hit = link.hasAttribute("data-nav-exact") ? path === prefix : path.startsWith(prefix);
    if (hit && prefix.length >= bestLength) {
      best = link;
      bestLength = prefix.length;
    }
  });

  links.forEach((link) => {
    // `aria-current` is the styling hook as well as the announcement, so setting
    // it is the whole of the change. Removed rather than set to "false": a
    // screen reader treats any non-empty value as current.
    if (link === best) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

// Every way the address bar can change. `htmx:afterSwap` is deliberately not one
// of them — it can fire before the URL has been pushed, which would highlight
// the page being left rather than the one being entered.
document.body.addEventListener("htmx:pushedIntoHistory", syncNav);
document.body.addEventListener("htmx:replacedInHistory", syncNav);
document.body.addEventListener("htmx:historyRestore", syncNav);
window.addEventListener("popstate", syncNav);

// First paint. This script is deferred, so it runs after the document is parsed
// and before the user sees anything settle.
syncNav();

// On a phone the sidebar is a drawer over the page, and `navOpen` was never
// reset — so tapping a destination left the drawer sitting on top of it. Closing
// it belongs with navigation rather than with the link, since a server-directed
// navigation has no link to hang it on.
document.body.addEventListener("htmx:pushedIntoHistory", () => {
  const shell = document.querySelector(".app-shell");
  // `Alpine.$data`, not the v2 `__x` property, which does not exist in v3.
  if (shell) {
    const data = Alpine.$data(shell);
    if (data && "navOpen" in data) data.navOpen = false;
  }
});

/**
 * An in-app navigation, as if the user had clicked a boosted link.
 *
 * `push` rather than `pushUrl` — that is the option name `htmx.ajax` actually
 * reads, and the misspelling meant the go-to chords moved the page without ever
 * updating the address bar, so Back went somewhere else entirely. It takes the
 * path, not `true`: HTMX only expands the literal *string* "true" into the
 * response path, so a boolean falls through and is pushed as-is.
 */
function navigate(url) {
  htmx.ajax("GET", url, { target: "#main", swap: "morph:innerHTML", push: url });
}

// ---------------------------------------------------------------------------
// Server-directed navigation
//
// Creating a notice, a request, a meeting or a work item, and opening working
// papers, all end with the server saying "now go and look at it" through an
// HX-Trigger. Five views sent this event and nothing listened for it, so the
// toast appeared, the record really was created, and the screen stayed on the
// list — the exact "it worked but nothing happened" complaint.
//
// A non-object HX-Trigger value arrives wrapped by HTMX as `{value: …}`.
// ---------------------------------------------------------------------------
document.body.addEventListener("stacos:navigate", (event) => {
  const url = event.detail?.value;
  if (typeof url !== "string" || !url.startsWith("/")) return;
  navigate(url);
});

function openPalette() {
  document.dispatchEvent(new CustomEvent("stacos:palette-open"));
}

/**
 * The keyboard shortcuts dialog, opened with `?`.
 *
 * The chords are otherwise undiscoverable — there is no menu that lists them —
 * so the key that advertises them has to actually do something. It dispatched a
 * `stacos:shortcuts` event that nothing listened for, which is the same failure
 * as a dead link: it looks implemented and is not.
 */
Alpine.data("shortcutsHelp", () => ({
  open: false,
  groups: SHORTCUT_HELP,

  init() {
    document.addEventListener("stacos:shortcuts", () => this.toggle());
  },

  toggle() {
    this.open = !this.open;
  },

  hide() {
    this.open = false;
  },
}));

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
