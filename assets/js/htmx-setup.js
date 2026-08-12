/**
 * Publish htmx on `window` before any extension loads.
 *
 * This has to be its own module. ES imports are hoisted and executed before the
 * body of the importing module, so `import htmx from "htmx.org"; window.htmx =
 * htmx; import "htmx-ext-preload";` does *not* work — the extension runs first,
 * finds no global, and throws "htmx is not defined", taking the whole bundle
 * (and therefore Alpine) down with it.
 *
 * Modules execute in import order, so importing this one first is what makes the
 * global exist in time.
 */
import htmx from "htmx.org";

window.htmx = htmx;

export default htmx;
