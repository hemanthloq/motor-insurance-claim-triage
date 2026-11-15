/* =============================================================================
 *  Static check for web/index.html.
 *
 *      node scripts/check-web-client.js
 *
 *  The web client is the one part of this system no Python test touches, and
 *  it broke twice in a row because of that:
 *
 *    INC-007  a hard-coded policy period expired, so every submission was
 *             rejected at the ingestion boundary
 *    INC-008  a `const` used above its declaration threw on page load, which
 *             aborted the script before the submit handler was attached --
 *             the page rendered perfectly and the button did nothing
 *
 *  Both are invisible to the Python suite, which never loads this file. So:
 *  stub just enough DOM to execute the page's script, then assert what has to
 *  be true.
 *
 *    1. the script runs to completion, and so does its async start-up
 *       (init() awaits the health probe; a throw after that await is an
 *       unhandled rejection no synchronous check would see)
 *    2. the submit, claims-drawer and delegated click handlers are attached
 *    3. every sample claim builds a body that satisfies the fnol.v1
 *       cross-field rules -- in particular, a loss date inside the policy
 *       period -- and so does whatever the form currently holds
 *    4. the claims list and the attention section render
 *
 *  This is not a browser. It does not render, and it cannot tell you the page
 *  looks right. scripts/ui_e2e.py is the one that clicks things.
 * ============================================================================= */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "web", "index.html");

// ---------------------------------------------------------------- DOM stub
//
// Permissive on purpose. The page is allowed to use whatever ordinary DOM API
// it likes, and this stub has to answer all of them, or the checker fails on
// its own limitations instead of the page's. What it must NOT do is hide a
// genuine throw in the page's own code, so every method is a no-op returning
// something of the right shape rather than a catch-all that swallows errors.
function makeElement(id) {
  const node = {
    id,
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    className: "",
    title: "",
    hidden: false,
    open: false,
    disabled: false,
    scrollTop: 0,
    scrollHeight: 0,
    dataset: {},
    style: {},
    onsubmit: null,
    onclick: null,
    _listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    appendChild() {},
    insertAdjacentHTML() {},
    setAttribute() {},
    getAttribute: () => null,
    removeAttribute() {},
    focus() {},
    blur() {},
    scrollIntoView() {},
    querySelector: () => makeElement("child"),
    querySelectorAll: () => [],
    closest: () => null,
    children: [],
  };
  return node;
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

const fetches = [];
const documentListeners = {};
const storage = {};
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  Promise,
  Date,
  Math,
  JSON,
  String,
  Number,
  Boolean,
  Array,
  Object,
  RegExp,
  Error,
  Set,
  isNaN,
  encodeURIComponent,
  performance: { now: () => Date.now() },
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
  },
  location: { port: "8080", href: "http://localhost:8080/", hash: "" },
  prompt: () => "test",
  document: {
    body: makeElement("body"),
    getElementById: (id) => el(id),
    createElement: (tag) => makeElement("created-" + tag),
    querySelector: () => makeElement("query"),
    querySelectorAll: () => [],
    documentElement: makeElement("html"),
    addEventListener(type, fn) {
      (documentListeners[type] = documentListeners[type] || []).push(fn);
    },
  },
  window: {
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    matchMedia: () => ({ matches: false }),
  },
  // Never actually calls the API: this check must run with nothing serving.
  fetch: (url) => {
    fetches.push(url);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ready: true, checks: {}, chunks: 0, documents: [] }),
    });
  },
};

const problems = [];
function fail(message) {
  problems.push(message);
}

const unhandled = [];
process.on("unhandledRejection", (reason) => unhandled.push(reason));

async function main() {
  // ---------------------------------------------------------------- run it
  const html = fs.readFileSync(HTML, "utf8");
  const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  if (scripts.length === 0) {
    fail("no <script> block found in web/index.html");
  }

  const context = vm.createContext(sandbox);
  try {
    for (const source of scripts) {
      vm.runInContext(source, context, { filename: "web/index.html", timeout: 5000 });
    }
  } catch (error) {
    fail(`the page script threw on load: ${error.message}\n       ` +
         `(this is the dead-button failure: nothing after the throw runs, ` +
         `so no event handler is attached)`);
  }

  // ---- 1. let the async start-up finish, then see whether it threw --------
  await new Promise((r) => setTimeout(r, 150));
  if (unhandled.length) {
    fail(`the page's async start-up threw after its first await: ${unhandled[0] && unhandled[0].message || unhandled[0]}\n       ` +
         `(init() runs after a health probe; this is INC-008 one await later, and a person sees a half-initialised page)`);
  }

  // ---- 2. are the handlers attached? ---------------------------------------
  const form = elements.get("form");
  if (!form) {
    fail("the script never looked up the form element");
  } else if (typeof form.onsubmit !== "function" && !(form._listeners.submit || []).length) {
    fail("no submit handler is attached to the form -- the button will do nothing");
  }
  if (!(documentListeners.click || []).length) {
    fail("no click handler is delegated on the document -- claim cards, tabs and the decision buttons will do nothing");
  }
  const claimsBtn = elements.get("claimsbtn");
  if (!claimsBtn || !(claimsBtn._listeners.click || []).length) {
    fail("the Claims button has no click handler -- the drawer can never be opened");
  }
  if (!(documentListeners.keydown || []).length) {
    fail("no keydown handler: Escape will not close the claims drawer");
  }

  // ---- 3. do the claims build valid bodies? --------------------------------
  const fnolOf = (expr) => vm.runInContext(expr, context);
  const bodies = [];
  try {
    const all = fnolOf("claims.map((c) => ({ id: c.id, body: fnolFrom(c.snap, c.ref) }))");
    if (all.length < 10) fail(`only ${all.length} sample claims are defined; the brief's scenarios must all remain`);
    bodies.push(...all.map((a) => [a.id, a.body]));
  } catch (error) {
    fail(`could not build the sample claims: ${error.message}`);
  }

  if (typeof context.buildFNOL === "function") {
    // Fill the form the way a person editing a claim would.
    const now = new Date(Date.now() - 2 * 3600 * 1000);
    el("dol").value = now.toISOString().slice(0, 16);
    el("ref").value = "CHECK-000001";
    el("policy").value = "IL/PC/2025/889012";
    el("reg").value = "KA01MJ2023";
    el("idv").value = "620000";
    el("est").value = "48000";
    el("use").value = "food_or_parcel_delivery";
    el("drivable").checked = true;
    el("tp").checked = false;
    el("injuries").checked = false;
    el("stolen").checked = false;
    el("fir").value = "";
    el("desc").value =
      "I was making a food delivery run in my own car when the accident happened.";
    try {
      bodies.push(["form", context.buildFNOL()]);
    } catch (error) {
      fail(`buildFNOL() threw: ${error.message}`);
    }
  } else {
    fail("buildFNOL is not defined");
  }

  let sample = null;
  for (const [name, body] of bodies) {
    sample = sample || body;
    const loss = new Date(body.date_of_loss);
    const start = new Date(body.policy_start_date);
    const end = new Date(body.policy_end_date);
    // INC-007: the defect that made every submission a 422.
    if (loss < start || loss > end) {
      fail(
        `[${name}] date_of_loss ${body.date_of_loss.slice(0, 10)} falls outside the policy period ` +
        `${body.policy_start_date}..${body.policy_end_date} -- the ingestion boundary ` +
        `will reject the submission (INC-007)`
      );
    }
    if (end <= start) fail(`[${name}] policy_end_date is not after policy_start_date`);
    if (new Date(body.reported_at) < loss) fail(`[${name}] reported_at precedes date_of_loss`);
    for (const field of ["claim_reference", "policy_number", "insurer_code", "product_code",
                         "channel", "reporter_phone", "vehicle", "driver", "incident"]) {
      if (body[field] === undefined) fail(`[${name}] buildFNOL() omitted a required field: ${field}`);
    }
    if (body.incident && (body.incident.description || "").length < 10) {
      fail(`[${name}] incident.description is shorter than the contract minimum of 10 characters`);
    }
  }

  // ---- 4. did the lists render? ---------------------------------------------
  if (!/data-claim=/.test(el("dlist").innerHTML)) {
    fail("the claims drawer rendered no claims");
  }
  if (!/Needs your attention/.test(el("attn").innerHTML)) {
    fail("the Needs your attention section did not render");
  }
  if (el("verdict").innerHTML.length === 0) {
    fail("the evaluation panel is empty on load: it should prompt the officer to run the analysis");
  }

  // ---- report ---------------------------------------------------------------
  console.log();
  if (problems.length) {
    console.log(`web client check: ${problems.length} PROBLEM(S)\n`);
    for (const p of problems) console.log(`  FAIL  ${p}`);
    console.log();
    process.exit(1);
  }
  console.log("web client check: passed");
  console.log(`  script executes (and so does its async start-up), handlers attached, ` +
              `${bodies.length} claim bodies are contract-valid`);
  if (sample) {
    console.log(`  loss ${sample.date_of_loss.slice(0, 10)} inside cover ` +
                `${sample.policy_start_date}..${sample.policy_end_date}`);
  }
  console.log();
}

main().catch((error) => {
  console.log(`web client check crashed: ${error.message}`);
  process.exit(1);
});
