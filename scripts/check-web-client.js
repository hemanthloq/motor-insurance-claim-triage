/* =============================================================================
 *  Static check for web/index.html.
 *
 *      node scripts/check-web-client.js
 *
 *  The web client is the one part of this system no automated test touched,
 *  and it broke twice in a row because of that:
 *
 *    INC-007  a hard-coded policy period expired, so every submission was
 *             rejected at the ingestion boundary
 *    INC-008  a `const` used above its declaration threw on page load, which
 *             aborted the script before the submit handler was attached --
 *             the page rendered perfectly and the button did nothing
 *
 *  Both are invisible to the Python test suite, which never loads this file.
 *  So: stub just enough DOM to execute the page's script, then assert the
 *  three things that actually have to be true.
 *
 *    1. the script runs to completion (catches the dead-button class)
 *    2. a submit handler is attached
 *    3. buildFNOL() produces a body that satisfies the fnol.v1 cross-field
 *       rules -- in particular, a loss date inside the policy period
 *
 *  This is not a browser. It does not render, and it cannot tell you the page
 *  looks right. It tells you the page is not dead.
 * ============================================================================= */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "web", "index.html");

// ---------------------------------------------------------------- DOM stub
function makeElement(id) {
  return {
    id,
    value: "",
    textContent: "",
    innerHTML: "",
    dataset: {},
    style: {},
    disabled: false,
    onsubmit: null,
    onclick: null,
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    appendChild() {},
    setAttribute() {},
  };
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

const fetches = [];
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  Promise,
  Date,
  Math,
  JSON,
  String,
  Number,
  Boolean,
  Array,
  Object,
  isNaN,
  encodeURIComponent,
  location: { port: "8080", href: "http://localhost:8080/" },
  prompt: () => "test",
  document: {
    getElementById: (id) => el(id),
    createElement: (tag) => makeElement("created-" + tag),
  },
  window: {
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
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

// ---------------------------------------------------------------- run it
const html = fs.readFileSync(HTML, "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length === 0) {
  fail("no <script> block found in web/index.html");
}

const problems = [];
function fail(message) {
  problems.push(message);
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

// ---- 2. is the submit handler attached? ----------------------------------
const form = elements.get("form");
if (!form) {
  fail("the script never looked up the form element");
} else if (typeof form.onsubmit !== "function") {
  fail("no submit handler is attached to the form -- the button will do nothing");
}

// ---- 3. does buildFNOL produce a valid body? -----------------------------
let body = null;
if (typeof context.buildFNOL === "function") {
  // Fill the form the way loadScenario + init would have.
  const now = new Date(Date.now() - 2 * 3600 * 1000);
  el("dol").value = now.toISOString().slice(0, 16);
  el("ref").value = "CHECK-000001";
  el("policy").value = "IL/PC/2025/889012";
  el("reg").value = "KA01MJ2023";
  el("idv").value = "620000";
  el("est").value = "48000";
  el("use").value = "food_or_parcel_delivery";
  el("drivable").value = "true";
  el("tp").value = "false";
  el("desc").value =
    "I was making a food delivery run in my own car when the accident happened.";
  try {
    body = context.buildFNOL();
  } catch (error) {
    fail(`buildFNOL() threw: ${error.message}`);
  }
} else {
  fail("buildFNOL is not defined");
}

if (body) {
  const loss = new Date(body.date_of_loss);
  const start = new Date(body.policy_start_date);
  const end = new Date(body.policy_end_date);

  // INC-007: the defect that made every submission a 422.
  if (loss < start || loss > end) {
    fail(
      `date_of_loss ${body.date_of_loss.slice(0, 10)} falls outside the policy period ` +
      `${body.policy_start_date}..${body.policy_end_date} -- the ingestion boundary ` +
      `will reject every submission (INC-007)`
    );
  }
  if (end <= start) {
    fail("policy_end_date is not after policy_start_date");
  }
  if (new Date(body.reported_at) < loss) {
    fail("reported_at precedes date_of_loss");
  }
  for (const field of ["claim_reference", "policy_number", "insurer_code", "product_code",
                       "channel", "reporter_phone", "vehicle", "driver", "incident"]) {
    if (body[field] === undefined) fail(`buildFNOL() omitted a required field: ${field}`);
  }
  if (body.incident && (body.incident.description || "").length < 10) {
    fail("incident.description is shorter than the contract minimum of 10 characters");
  }
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
console.log(`  script executes, submit handler attached, buildFNOL body is contract-valid`);
if (body) {
  console.log(`  loss ${body.date_of_loss.slice(0, 10)} inside cover ` +
              `${body.policy_start_date}..${body.policy_end_date}`);
}
console.log();
