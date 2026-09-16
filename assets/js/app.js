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

    // Every `#main` swap in this app is a navigation to a genuinely different
    // page — it always carries `hx-push-url` (see e.g. `calendar_body.html`,
    // `detail_body.html`), never a same-page filter refresh. The `morph` swap
    // above exists precisely to *preserve* scroll position across an in-place
    // update, which is the wrong thing here: without this, opening a shorter
    // page while scrolled halfway down a long one leaves the reader dropped
    // wherever the old scroll position happened to land, not at its top.
    window.scrollTo(0, 0);
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
//
// Retargeting alone is not enough: an action modal's form submits with
// hx-swap="none" (the real update travels separately, as an out-of-band
// fragment), and HTMX reads the swap *style* from that same form regardless
// of where beforeSwap points the target. Left alone, the retarget succeeds
// but nothing is ever drawn into it — the modal just sits there with no sign
// anything happened. swapOverride is what HTMX actually consults, so the
// style has to be forced here too.
document.body.addEventListener("htmx:beforeSwap", (event) => {
  if (event.detail.xhr?.status !== 422) return;
  const container = document.getElementById("modal-container");
  const existing = container?.querySelector(".modal");
  if (!container || !existing) return;

  // The innerHTML swap below destroys `existing` out from under its still-shown
  // Modal instance. Bootstrap appends that instance's backdrop to <body>, a
  // sibling of #modal-container, so the swap never touches it — and the only
  // thing that ever removes it is that instance's own .hide(), which this path
  // skips. Tear both down synchronously first so no backdrop is orphaned.
  Modal.getInstance(existing)?.dispose();
  document.querySelectorAll(".modal-backdrop").forEach((el) => el.remove());
  document.body.classList.remove("modal-open");
  document.body.style.removeProperty("overflow");
  document.body.style.removeProperty("padding-right");

  event.detail.shouldSwap = true;
  event.detail.target = container;
  event.detail.swapOverride = "innerHTML";
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
// Choosing a password
//
// The server is the only authority on whether a password is acceptable —
// `RegistrationForm.clean` runs Django's configured validators and nothing here
// can overrule it. What these two components buy is the feedback *before* the
// round trip, which is the difference between correcting a password and
// abandoning a sign-up: a rejected submit clears both password boxes, and the
// second time someone retypes a password they reach for one they reuse.
//
// So this deliberately only rates what a browser can honestly know. It does not
// claim anything about the common-password list or similarity to the name and
// email above; those are enforced server-side and are not advertised here.
// ---------------------------------------------------------------------------

// Long is strong: a passphrase of four ordinary words beats a short string with
// a symbol wedged into it, and a meter that says otherwise teaches the wrong
// habit. Length dominates the score and variety only tops it up.
function scorePassword(value, minLength) {
  if (!value) return 0;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length;

  let score = 0;
  if (value.length >= minLength) score += 2;
  if (value.length >= minLength + 4) score += 1;
  if (value.length >= minLength + 10) score += 1;
  if (classes >= 2) score += 1;
  if (classes >= 3) score += 1;

  // Anything that is one repeated character, one run of digits, or a keyboard
  // walk is not saved by being long.
  if (/^(.)\1*$/.test(value) || /^\d+$/.test(value)) score = Math.min(score, 1);

  return Math.min(score, 5);
}

const STRENGTH_TIERS = [
  { tier: "weak", label: "Too weak" },
  { tier: "weak", label: "Weak" },
  { tier: "fair", label: "Fair" },
  { tier: "good", label: "Good" },
  { tier: "strong", label: "Strong" },
  { tier: "strong", label: "Very strong" },
];

Alpine.data("passwordStrength", ({ minLength = 10 } = {}) => ({
  value: "",
  show: false,

  get score() {
    return scorePassword(this.value, minLength);
  },
  get tier() {
    return STRENGTH_TIERS[this.score].tier;
  },
  get label() {
    return STRENGTH_TIERS[this.score].label;
  },
  get percent() {
    // Never zero while there is something typed: a bar with no width reads as
    // "the meter is broken" rather than as "this password is terrible".
    return Math.max(8, (this.score / 5) * 100);
  },
  get rules() {
    const value = this.value;
    return {
      length: value.length >= minLength,
      notAllDigits: value.length > 0 && !/^\d+$/.test(value),
      // Either real variety, or enough length that variety stops mattering.
      variety:
        [/[A-Za-z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length >= 2 ||
        value.length >= minLength + 6,
    };
  },
}));

Alpine.data("passwordConfirm", (targetId) => ({
  value: "",
  other: "",
  show: false,

  init() {
    const source = document.getElementById(targetId);
    if (!source) return;
    // A plain listener rather than a shared Alpine scope: crispy renders each
    // field in its own template, so there is no element both boxes sit inside.
    // `input` covers typing, paste and autofill-on-interaction alike.
    const sync = () => {
      this.other = source.value;
    };
    source.addEventListener("input", sync);
    sync();
  },

  get matches() {
    return this.value.length > 0 && this.value === this.other;
  },
}));

// ---------------------------------------------------------------------------
// Stale validation errors
//
// crispy-forms decides `is-invalid` (and, through it, whether the paired
// `invalid-feedback` text is visible — see `.password-field:has(.is-invalid)`
// in `_forms.scss` for the one field group where that isn't a plain CSS
// sibling) once, at render time. Nothing then updates it: a user correcting a
// rejected field kept the *previous* submit's red border and error text under
// their cursor until the next full round trip re-rendered the form, client
// or server error alike, since both arrive the same way — baked into the HTML.
//
// Clearing the class on the first edit doesn't skip validation, it just stops
// displaying a verdict already known to be stale. The next submit — the
// application's actual validation strategy throughout, HTMX fragment or full
// page alike — re-renders with whatever is true of the corrected value, so a
// field that is still invalid gets a fresh, accurate error rather than none.
// ---------------------------------------------------------------------------
function clearStaleFieldError(event) {
  const field = event.target;
  if (!field.classList || !field.classList.contains("is-invalid")) return;
  field.classList.remove("is-invalid");
}
document.addEventListener("input", clearStaleFieldError);
document.addEventListener("change", clearStaleFieldError);

// ---------------------------------------------------------------------------
// Double-submit guard
//
// The authentication screens post normally — `layouts/auth.html` carries no
// `hx-boost`, because those responses set cookies and change who the session is
// — so `hx-disabled-elt`, which is what the rest of the product relies on, never
// fires there. A second click on "Create account" while the first request is in
// flight is an ordinary thing for a person on a slow connection to do, and it
// produced two sign-up attempts, two users racing on one unique email, and an
// IntegrityError.
//
// Disabling rather than swallowing the event: the button says what is happening,
// which is the other half of the problem — a form that looks inert after a click
// is a form people click again.
// ---------------------------------------------------------------------------
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;

  const button = form.querySelector("[data-submit-guard]");
  if (!button || button.dataset.submitting === "1") return;

  // Let the browser build and send the request first; disabling a submit button
  // synchronously inside its own submit handler drops it from the payload in
  // some browsers, and the view reads `request.POST` expecting it.
  window.setTimeout(() => {
    button.dataset.submitting = "1";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (button.tagName === "INPUT") button.value = button.dataset.submitGuard + "\u2026";
    else button.textContent = button.dataset.submitGuard + "\u2026";
  }, 0);
});

// Back/forward into a cached page restores the disabled button with it, leaving
// a form nobody can submit. `pageshow` with `persisted` is the only event that
// fires for a bfcache restore.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  document.querySelectorAll("[data-submit-guard]").forEach((button) => {
    delete button.dataset.submitting;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.tagName === "INPUT") button.value = button.dataset.submitGuard;
    else button.textContent = button.dataset.submitGuard;
  });
});

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
