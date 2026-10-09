// Playground front end: one ES module, no framework, no build step.
//
// Everything that comes back from the service (model output, error messages, test
// names) is untrusted, so the DOM is only ever built with createElement and text
// nodes. Nothing in this file assigns innerHTML.

const CUSTOM = "custom";

const state = {
  config: null,
  health: null,
  selected: null, // scenario id, or CUSTOM
  view: null, // key of the run shown in the result panel
  draft: { text: "", simulate: [] },
  runs: new Map(), // key -> the latest run for that scenario (or CUSTOM)
  history: [], // every finished run, for the session summary
  busy: false,
  batch: null, // { done, total, stop, skipWait } while "Run all" is going
  abort: null,
  halt: null, // why the last "Run all" stopped before the end, as a sentence
};

// ------------------------------------------------------------------ DOM ---

const $ = (selector, root = document) => root.querySelector(selector);
const SVG_NS = "http://www.w3.org/2000/svg";

/** Build an element. `on*` props are listeners, `dataset` is merged, null props are skipped. */
function h(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

const TONE_ICON = { good: "check", crit: "x", warn: "bang", neutral: "ban", muted: "dash" };

/** A round status mark. Always sits next to words, so colour never carries meaning alone. */
function badge(tone, extra = "") {
  return h(
    "span",
    { class: `badge badge--${tone} ${extra}`.trim() },
    TONE_ICON[tone] ? icon(TONE_ICON[tone]) : null,
  );
}

function chip(text, { tone = "", iconName = null } = {}) {
  return h(
    "span",
    { class: `chip ${tone ? `chip--${tone}` : ""}`.trim() },
    iconName ? icon(iconName) : null,
    text,
  );
}

/** Say `words` to a screen reader, through the page's live region for announcements (#announcer). */
function announce(words) {
  $("#announcer").textContent = words;
}

// ----------------------------------------------------------- formatting ---

function modelParts(id) {
  const slash = id.indexOf("/");
  const vendor = slash === -1 ? "" : id.slice(0, slash);
  const [name, tag = ""] = (slash === -1 ? id : id.slice(slash + 1)).split(":");
  return { vendor, name, tag };
}

const shortModel = (id) => modelParts(id).name;

function roleLabel(index, chain = state.config.chain) {
  if (index === 0) return "Primary";
  return chain.length > 2 ? `Fallback ${index}` : "Fallback";
}

/** Milliseconds as a [value, unit] pair, so the unit can be set smaller than the number. */
function duration(ms) {
  if (ms == null || Number.isNaN(ms)) return ["–", ""];
  if (ms < 1) return ["<1", "ms"];
  if (ms < 1000) return [String(Math.round(ms)), "ms"];
  if (ms < 10000) return [(ms / 1000).toFixed(2), "s"];
  return [(ms / 1000).toFixed(1), "s"];
}

const durationText = (ms) => duration(ms).join(" ").trim();

/** A wait still to come, in words: "23 s", "12 min", "5 h 12 min". Rounded up, so it never ends early. */
function waitText(ms) {
  const seconds = Math.ceil(ms / 1000);
  if (seconds <= SHORT_WAIT_SECONDS) return `${seconds} s`;
  const minutes = Math.ceil(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** The time left until `until`, a performance.now() value. tickTimers keeps it current. */
const countdown = (until) =>
  h("span", { class: "countdown", dataset: { until } }, waitText(until - performance.now()));

function money(amount, currency) {
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency }).format(amount);
  } catch {
    // The currency field is model output; an unknown code must not break the page.
    return `${amount.toFixed(2)} ${currency}`;
  }
}

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** "2026-10-07" as "7 October 2026". Anything that is not such a date comes back unchanged. */
function longDate(isoDay) {
  const day = new Date(`${isoDay}T00:00:00Z`);
  if (Number.isNaN(day.getTime())) return isoDay;
  return day.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

const number = (value) => value.toLocaleString("en");

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

/** Pretty-printed JSON as text nodes and spans, for a <pre>. */
function jsonNodes(value) {
  const text = JSON.stringify(value, null, 2) ?? "null";
  const token = /("(?:[^"\\]|\\.)*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  const nodes = [];
  let last = 0;
  for (const match of text.matchAll(token)) {
    nodes.push(text.slice(last, match.index));
    if (match[1]) {
      nodes.push(h("span", { class: match[2] ? "k" : "s" }, match[1]), match[2] ?? "");
    } else {
      nodes.push(h("span", { class: "v" }, match[0]));
    }
    last = match.index + match[0].length;
  }
  nodes.push(text.slice(last));
  return nodes;
}

// ---------------------------------------------------------------- boot ---

// The demo runs on free hosting that switches it off when nobody has visited for a
// while. The next request wakes it, which takes about a minute, and until then the host
// answers in its place: with a page of its own, with an error, or with nothing at all.
// The page then waits for the service instead of failing. It cannot know why no answer
// came (on a developer's machine the server may simply be stopped), so its sentences
// say "probably". And every request keeps the host awake, so the page asks only while
// something is happening: see "model upkeep" below for the other half of that rule.
const HEALTH_EVERY_MS = 3000; // while waiting: one GET /health every 3 seconds
const GIVE_UP_AFTER_MS = 180000; // stop waiting after 3 minutes
const SLOW_START_MS = 5000; // a request this long without an answer: say it may be waking up
// A run that hears nothing has to end as well. The service itself is silent for as long
// as one model takes to answer, so the page waits that long and 90 seconds more: a host
// that is waking the service may hold a request for about a minute.
const SILENCE_SPARE_MS = 90000;
const silenceLimit = () => state.config.limits.timeout_seconds * 1000 + SILENCE_SPARE_MS;

// Written for a visitor, who can wait or come back, and cannot fix anything.
const WHY_ASLEEP =
  "Free hosting switches it off when nobody has visited for a while, and waking it takes about a minute.";
/** Why no answer came, as far as a page can tell: the second half of a sentence. */
const probably = () =>
  navigator.onLine === false ? "and this device seems to be offline." : `so it has probably gone to sleep. ${WHY_ASLEEP}`;
const NOT_WAKING =
  "The demo has not answered for 3 minutes. Please try again in a few minutes; if it stays like this, please contact the admin.";
const NOT_LOADING =
  "The demo is answering, but this page could not load. Please try again; if it stays like this, please contact the admin.";
const NO_ANSWER_TWICE =
  "The demo is answering again, but your run got no answer a second time. Please try again; if it keeps happening, please contact the admin.";
const CUT_OFF =
  "The demo had started on your run, but the connection broke off before a result arrived. Please try again; if it keeps happening, please contact the admin.";
const WENT_QUIET = (ms) =>
  `The demo had started on your run, but then nothing more came from it for ${waitText(ms)}, so this page stopped waiting. Please try again; if it keeps happening, please contact the admin.`;
const SENT_AGAIN =
  "The demo did not answer at first, probably because it was asleep. This page waited for it, then sent your run again.";
const SIMULATED_STILL_WORK = "Scenarios that only use simulated failures still work.";

// What the service itself sends, as opposed to whatever a host sends in its place.
const isHealth = (body) => Array.isArray(body?.providers);
const isConfig = (body) => Array.isArray(body?.chain) && Array.isArray(body?.scenarios);
const isPicture = (body) => Array.isArray(body?.chain) && typeof body?.mode === "string";
const isEnvelope = (body) => body?.success === false && typeof body.error === "string";

/**
 * A limit on waiting for one request, so that no request of this page can wait for ever.
 * `signal` ends once `ms` have passed without a word, or when `stop` (the visitor's own
 * signal) does. `renew()` says that something arrived, and the wait starts again;
 * `done()` puts the timer away when the request has ended.
 */
function patience(ms, stop) {
  const limit = new AbortController();
  const end = () => limit.abort();
  let timer = setTimeout(end, ms);
  if (stop?.aborted) end();
  stop?.addEventListener("abort", end, { once: true });
  return {
    signal: limit.signal,
    renew() {
      clearTimeout(timer);
      timer = setTimeout(end, ms);
    },
    done() {
      clearTimeout(timer);
      stop?.removeEventListener("abort", end);
    },
  };
}

/**
 * The JSON behind `url`, whatever its status, or null when none came back: the request
 * failed, a host answered with a page of its own where the service's JSON should be, or
 * nothing came for 3 minutes.
 */
async function readJSON(url, stop) {
  const wait = patience(GIVE_UP_AFTER_MS, stop);
  try {
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: wait.signal });
    return await response.json();
  } catch {
    return null;
  } finally {
    wait.done();
  }
}

async function boot() {
  wireTheme();
  wireTooltip();
  renderResult();
  setInterval(tickTimers, 100);
  await load();
}

/** What the parts that are drawn from the service's answers say until they are. */
function pending(words) {
  for (const part of document.querySelectorAll("[data-pending]")) part.textContent = words;
}

/** Read what the page is drawn from. False unless the answers are the service's own. */
async function readPage() {
  // /health answers 503 with a normal body when no key is configured, so the status is not looked at.
  const [config, health, picture] = await Promise.all([
    readJSON("/playground/config"),
    readJSON("/health"),
    readJSON("/playground/models"),
  ]);
  if (!isConfig(config) || !isHealth(health)) return false;
  state.config = config;
  // The page draws without the picture of the chain's upkeep, and asks for it again later.
  upkeep.picture = isPicture(picture) ? picture : null;
  upkeep.receivedAt = performance.now();
  showHealth(health);
  return true;
}

/** What a first load shows while it waits for the service. */
function sayWaking() {
  showStatus("waiting");
  chainNote("Waiting for the demo to answer", true);
  pending("Waiting for the demo");
  showNotice(
    "warn",
    `The demo has not answered yet, ${probably()} This page keeps asking, and finishes loading by itself when the demo answers.`,
  );
}

/** Draw the page from the service's answers, first waiting for the service if it is asleep. */
async function load() {
  const since = performance.now();
  // A host may also hold these requests while it wakes the service, and then deliver them.
  const slow = setTimeout(sayWaking, SLOW_START_MS);
  let ready = await readPage();
  clearTimeout(slow);
  if (!ready) {
    sayWaking();
    // The 3 minutes count from when the load began: requests that a host held for all
    // of them are not followed by 3 minutes of questions.
    const awake = await waitForService(undefined, since);
    ready = awake && (await readPage());
    if (!ready) {
      chainNote("The model chain could not be loaded.", false);
      pending("Not loaded");
      // "Try again" says at once that it is trying, which also takes the button away.
      showNotice("crit", awake ? NOT_LOADING : NOT_WAKING, () => {
        sayWaking();
        load();
      });
      return;
    }
  }

  renderChrome();
  renderChain();
  renderBenchmark();
  renderSuiteControls();
  wireRequest();
  select(state.config.scenarios[0]?.id ?? CUSTOM);
  $("#run-all").addEventListener("click", runAll);
  wireUpkeep();
}

// True while the page waits for the service. That wait does the asking and has the pill,
// so the looks at the chain's upkeep stand back until it is over.
let waitingForService = false;

/**
 * Ask GET /health every 3 seconds until the service's own JSON answers. Resolves with that
 * answer, or with null 3 minutes after `since`, or when `signal` says the visitor has stopped.
 *
 * It starts from a page load or from a run, never by itself, and it always ends.
 */
function waitForService(signal, since = performance.now()) {
  waitingForService = true;
  showStatus("waiting");
  return new Promise((resolve) => {
    const asking = new AbortController(); // ends the questions still in flight
    const finish = (health) => {
      if (asking.signal.aborted) return; // already over
      asking.abort();
      clearInterval(every);
      clearTimeout(limit);
      waitingForService = false;
      if (health) showHealth(health);
      else showStatus("offline");
      resolve(health);
    };
    // A question may outlast the next one: a waking host can hold it and answer it late.
    const ask = async () => {
      const health = await readJSON("/health", asking.signal);
      if (isHealth(health)) finish(health);
    };
    const left = since + GIVE_UP_AFTER_MS - performance.now();
    const every = setInterval(ask, HEALTH_EVERY_MS);
    const limit = setTimeout(() => finish(null), left);
    signal?.addEventListener("abort", () => finish(null), { once: true });
    if (left > 0) ask();
  });
}

const STATUS_WORDS = {
  ok: "Operational",
  degraded: "Degraded",
  unavailable: "No API key",
  withdrawn: "Models withdrawn",
  waiting: "No answer yet",
  offline: "Not answering",
};

function setStatus(status) {
  $("#status").dataset.state = status;
  $("#status-text").textContent = STATUS_WORDS[status] ?? status;
}

// "waiting" while the page has no answer and is still asking, "offline" once it has given
// up, null after any answer of the service's own.
let away = null;

/**
 * The pill, from all the page knows, in one place. With `notAnswering` it says that the
 * service is not answering; without, that an answer has just come, and then it shows what
 * the service last said of itself. /health only says that a key is set, so when every
 * model of the chain is known to be withdrawn, "Operational" would not be true.
 */
function showStatus(notAnswering = null) {
  away = notAnswering;
  setStatus(away ?? (upkeep.withdrawn ? "withdrawn" : state.health.status));
}

/** Show what the service's own /health said: in the status pill, and as a notice when no key is set. */
function showHealth(health) {
  if (away) announce("The demo is answering.");
  state.health = health;
  showStatus();
  if (health.status === "unavailable") {
    showNotice("warn", `No OpenRouter API key is configured, so live model calls will fail. ${SIMULATED_STILL_WORK}`);
  } else {
    $("#notice").hidden = true;
  }
}

/** Ask /health again after the chain changed. The pill follows; the notice above the playground is left alone. */
async function refreshHealth() {
  const health = await readJSON("/health");
  if (!isHealth(health)) return;
  state.health = health;
  showStatus();
}

/**
 * A notice above the playground, read out as well: the notice is no live region of its
 * own. `retry`, when given, is what its "Try again" button does.
 */
function showNotice(tone, message, retry = null) {
  const notice = $("#notice");
  notice.className = ["notice", tone === "crit" && "notice--crit", retry && "notice--action"].filter(Boolean).join(" ");
  notice.replaceChildren(icon("warning"), h("span", null, message));
  if (retry) notice.append(h("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: retry }, "Try again"));
  notice.hidden = false;
  announce(message);
}

/** One line in the chain card, for as long as the page has no chain to draw there. */
function chainNote(words, waiting) {
  $("#chain").replaceChildren(h("li", { class: "chain__wait" }, waiting ? h("span", { class: "spinner" }) : null, words));
}

function wireTheme() {
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try {
      localStorage.setItem("playground-theme", root.dataset.theme);
    } catch {
      // Storage can be blocked; the choice then lasts for this page view only.
    }
  });
}

function renderChrome() {
  const { service, limits, chain } = state.config;

  const version = $("#version");
  version.textContent = `v${service.version}`;
  version.hidden = false;
  $("#footer-version").textContent = `${service.name} v${service.version} · ${service.environment}`;

  if (service.docs_url) {
    const docs = $("#docs-link");
    docs.href = service.docs_url;
    docs.hidden = false;
  }

  $("#limit-min").textContent = number(limits.min_chars);
  $("#limit-max").textContent = number(limits.max_chars);

  const facts = [
    [String(chain.length), chain.length === 1 ? "model in the chain" : "models in the chain"],
    [`${limits.timeout_seconds} s`, "time budget per model"],
  ];
  if (chain.every((entry) => entry.model.endsWith(":free"))) {
    facts.push(["$0", "per request: every model is on a free tier"]);
  }
  // The service's own ceiling. A run that would cross it is refused before any model is called.
  facts.push([String(limits.calls_per_minute), "live model calls a minute, for all visitors together"]);
  const line = $("#hero-facts");
  line.replaceChildren(...facts.map(([value, label]) => h("span", null, h("strong", null, value), " ", label)));
  line.hidden = false;
}

// Written for whoever is looking at the page, who is usually not the person who can
// fix it. The fix itself is spelled out in the server log, where the admin reads it.
const UNLISTED_LABEL = "Unlisted from OpenRouter, please contact the admin.";

/**
 * What OpenRouter's catalogue said about a model when the service started, as a chip.
 * Null when there is nothing to flag: the model is listed with no end date, or nobody asked.
 */
function listingChip(entry) {
  const listing = entry.listing;
  const mode = upkeep.picture?.mode;
  if (mode === "automatic") {
    // The service replaces a withdrawn model by itself, so nobody has to be contacted.
    if (upkeep.picture.gone.includes(entry.model)) {
      const flag = chip(WITHDRAWN_LABEL, { tone: "warn", iconName: "warning" });
      setTip(flag, "Withdrawn", ["OpenRouter no longer offers this model.", "The service looks for a replacement after the next run that fails on it."]);
      return flag;
    }
  } else if (mode && listing?.status === "not_listed") {
    // Only where the page knows that nobody but the admin replaces a model. Without the
    // picture of the chain's upkeep it does not know that, and sends nobody to the admin.
    const flag = chip(UNLISTED_LABEL, { tone: "warn", iconName: "warning" });
    setTip(flag, "Unlisted", ["Withdrawn, or the id is mistyped.", "It is still tried in its turn, and fails."]);
    return flag;
  }
  if (listing?.status === "listed" && listing.expires) {
    const flag = chip(`Ends ${listing.expires}`, { tone: "warn", iconName: "warning" });
    setTip(flag, longDate(listing.expires), ["The end date OpenRouter has announced for this model."]);
    return flag;
  }
  return null;
}

function renderChain() {
  const chain = state.config.chain;
  const items = chain.map((entry) => {
    const { vendor, name, tag } = modelParts(entry.model);
    const meta = [vendor, tag].filter(Boolean).map((part) => h("span", null, part));
    if (entry.benchmark) meta.push(h("span", null, snapshotLine(entry.benchmark)));
    return h(
      "li",
      { class: "chain__item" },
      h("span", { class: "chain__num" }, entry.index + 1),
      h("span", { class: "chain__name" }, name),
      chip(roleLabel(entry.index)),
      h("div", { class: "chain__meta" }, meta, listingChip(entry)),
    );
  });
  $("#chain").replaceChildren(...items);

  // Where this chain comes from, in one line. The section below says the rest.
  const p = shownPicture();
  const origin = $("#chain-origin");
  origin.hidden = !p;
  if (p) {
    origin.replaceChildren(
      ORIGIN_LINE[p.origin.kind](p),
      " ",
      h("a", { href: "#upkeep" }, p.busy ? "Watch what it is doing" : p.mode === "automatic" ? "How it looks after itself" : "What that means"),
    );
  }
  let foot = "A healthy request calls only the first model. Each later model runs only when the one before it fails.";
  if (chain.length === 1) foot = "A single-model chain: there is no fallback, so a failure is final.";
  // The words for a chain that only the admin can repair, where the page knows it is one.
  const unlisted = p && p.mode !== "automatic" ? chain.filter((entry) => entry.listing?.status === "not_listed") : [];
  if (p?.mode === "automatic" && p.gone.length) {
    foot = goneWords(p);
  } else if (unlisted.length) {
    const one = unlisted.length === 1;
    const names = new Intl.ListFormat("en-GB", { type: "disjunction" }).format(unlisted.map((entry) => shortModel(entry.model)));
    foot =
      `OpenRouter did not list ${names} when this service started. ` +
      `${one ? "It is" : "They are"} still tried in turn and ${one ? "fails" : "fail"} every time. ` +
      "The server log says how to replace " + (one ? "it." : "them.");
  }
  $("#chain-foot").textContent = foot;
}

// ------------------------------------------------------------ scenarios ---

const scenarioById = (id) => state.config.scenarios.find((scenario) => scenario.id === id) ?? null;
const currentScenario = () => scenarioById(state.selected);

function isEdited() {
  const scenario = currentScenario();
  if (!scenario) return false;
  return (
    state.draft.text !== scenario.text ||
    scenario.simulate.some((fault, index) => (fault ?? null) !== (state.draft.simulate[index] ?? null))
  );
}

function select(id) {
  const scenario = scenarioById(id);
  state.selected = scenario ? id : CUSTOM;
  state.view = state.selected;
  state.draft = scenario
    ? { text: scenario.text, simulate: [...scenario.simulate] }
    : { text: "", simulate: state.config.chain.map(() => null) };
  renderScenarios();
  renderRequest();
  renderResult();
}

/**
 * A finished run with something to score: the pipeline answered it. A run that was
 * stopped, left without an answer or refused by the ceiling says nothing about a scenario.
 */
const isScored = (run) => run?.phase === "done" && !run.problem && !run.refusal;

/** How a finished scenario run did against its expectations. */
function verdictOf(run) {
  const total = run.checks.length;
  const passed = run.checks.filter((check) => check.pass).length;
  let tone = "good";
  if (run.problem) tone = "crit";
  else if (run.refusal) tone = "warn";
  else if (passed < total) tone = run.checks[0]?.pass ? "warn" : "crit"; // first check is the status
  return { passed, total, tone };
}

function runSummary(run) {
  if (run.problem) return "No result";
  if (run.refusal) return run.refusal.today ? "Not run: today's share is used up" : "Not run: the demo was busy";
  const who = run.status === 200 && run.body?.model_used ? ` · ${shortModel(run.body.model_used)}` : "";
  return `${run.status}${who} · ${durationText(run.serverMs ?? run.clientMs)}`;
}

function scenarioButton(scenario) {
  const run = state.runs.get(scenario.id);
  let mark = h("span", { class: "badge badge--idle" });
  let left = scenario.live_calls ? plural(scenario.live_calls, "model call") : "No model call";
  let right = plural(scenario.checks.length, "check");

  if (run?.phase === "running") {
    mark = h("span", { class: "spinner" });
    left = run.waking ? "Waiting for the demo" : "Running";
  } else if (run) {
    const verdict = verdictOf(run);
    mark = badge(verdict.tone);
    left = runSummary(run);
    right = isScored(run) ? `${verdict.passed}/${verdict.total}` : "";
  }

  return h(
    "button",
    {
      class: "scenario",
      type: "button",
      "aria-current": state.selected === scenario.id ? "true" : null,
      disabled: locked(),
      dataset: { id: scenario.id },
      onclick: () => select(scenario.id),
    },
    mark,
    h("span", { class: "scenario__title" }, scenario.title),
    h("span", { class: "scenario__meta" }, h("span", null, left), h("span", null, right)),
  );
}

function renderScenarios() {
  const { groups, scenarios } = state.config;
  const nodes = [];
  for (const group of groups) {
    const members = scenarios.filter((scenario) => scenario.group === group.id);
    if (!members.length) continue;
    nodes.push(
      h("p", { class: "kicker rail__group" }, group.title, h("small", null, group.summary)),
      ...members.map(scenarioButton),
    );
  }

  const custom = state.runs.get(CUSTOM);
  nodes.push(
    h(
      "button",
      {
        class: "scenario scenario--custom",
        type: "button",
        "aria-current": state.selected === CUSTOM ? "true" : null,
        disabled: locked(),
        onclick: () => select(CUSTOM),
      },
      h("span", { class: "badge" }, icon("plus")),
      h("span", { class: "scenario__title" }, "Your own document"),
      h(
        "span",
        { class: "scenario__meta" },
        h("span", null, custom && custom.phase === "done" ? runSummary(custom) : "Paste any text"),
      ),
    ),
  );

  const list = $("#scenarios");
  const scroll = list.scrollTop;
  list.replaceChildren(...nodes);
  list.scrollTop = scroll;
  const current = state.batch ? $(".scenario[aria-current]", list) : null;
  if (current) {
    // Scroll the list itself; scrollIntoView would also move the page under the reader.
    const item = current.getBoundingClientRect();
    const frame = list.getBoundingClientRect();
    if (item.top < frame.top) list.scrollTop -= frame.top - item.top + 8;
    else if (item.bottom > frame.bottom) list.scrollTop += item.bottom - frame.bottom + 8;
  }
  renderRailHead();
}

function renderRailHead() {
  const { scenarios } = state.config;
  const button = $("#run-all");
  const meta = $("#run-all-meta");
  const progress = $("#batch-progress");

  button.textContent = state.batch ? "Stop" : "Run all";
  button.disabled = state.busy && !state.batch;
  progress.hidden = !state.batch;

  if (state.batch) {
    const { done, total, waitUntil } = state.batch;
    progress.firstElementChild.style.width = `${(done / total) * 100}%`;
    const place = `${Math.min(done + 1, total)} of ${total}`;
    // waitUntil is set for as long as "Run all" waits out a refusal. The time left is said
    // here as well: on a narrow screen the result panel is far below this list.
    if (waitUntil) meta.replaceChildren("Waiting ", countdown(waitUntil), ` to run ${place} again`);
    else meta.textContent = `Running ${place}`;
    return;
  }

  // The last answer each scenario got. A later run that was refused, or left without an
  // answer, does not take it back: the session card below counts the same way.
  const answered = state.history.filter(isScored);
  const finished = scenarios.map((scenario) => answered.findLast((run) => run.key === scenario.id)).filter(Boolean);
  if (finished.length) {
    const met = finished.filter((run) => verdictOf(run).tone === "good").length;
    const rest = scenarios.length - finished.length;
    meta.textContent = `${met} of ${finished.length} met every expectation${rest ? ` · ${rest} not run` : ""}`;
  } else {
    const calls = scenarios.reduce((sum, scenario) => sum + scenario.live_calls, 0);
    meta.textContent = `${plural(scenarios.length, "scenario")} · about ${plural(calls, "model call")}`;
  }
  // Why the last "Run all" ended early comes first, until the next run starts.
  if (state.halt) meta.prepend(h("strong", null, state.halt), " ");
}

// -------------------------------------------------------------- request ---

function wireRequest() {
  $("#document").addEventListener("input", (event) => {
    state.draft.text = event.target.value;
    renderDraftState();
  });
  $("#document").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runDraft();
  });
  $("#run").addEventListener("click", runDraft);
  $("#reset").addEventListener("click", () => select(state.selected));
}

function renderRequest() {
  const scenario = currentScenario();
  const group = scenario && state.config.groups.find((entry) => entry.id === scenario.group);

  $("#scenario-group").textContent = group ? group.title : "Custom";
  $("#scenario-title").textContent = scenario ? scenario.title : "Your own document";
  $("#scenario-summary").textContent = scenario
    ? scenario.summary
    : "Paste anything that might contain an invoice: an email thread, a receipt, an OCR dump. The chain controls work the same way.";

  const covers = $("#scenario-covers");
  covers.hidden = !scenario;
  if (scenario) {
    // "Test 2 · test_name" or "Benchmark case · OCR receipt".
    const [kind, subject] = scenario.covers.split(" · ");
    covers.replaceChildren(
      ...(subject?.startsWith("test_")
        ? [`Automated counterpart (${kind}): `, h("code", null, subject)]
        : [`${kind}: ${subject ?? ""}`]),
    );
  }

  $("#document").value = state.draft.text;
  renderPlan();
  renderDraftState();
}

function renderPlan() {
  const faults = state.config.faults;
  const rows = state.config.chain.map((entry, index) => {
    const fault = faults.find((candidate) => candidate.id === state.draft.simulate[index]) ?? null;
    const flag = listingChip(entry);
    const control = h(
      "select",
      {
        class: "select",
        "aria-label": `What model ${index + 1} does on this run`,
        disabled: locked(),
        onchange: (event) => {
          state.draft.simulate[index] = event.target.value || null;
          renderPlan();
          renderDraftState();
        },
      },
      h("option", { value: "" }, "Call the model"),
      h(
        "optgroup",
        { label: "Simulate a failure" },
        faults.map((candidate) => h("option", { value: candidate.id }, candidate.label)),
      ),
    );
    control.value = fault?.id ?? "";

    return h(
      "div",
      { class: `plan__row ${fault ? "is-fault" : ""}`.trim() },
      h("span", { class: "num" }, index + 1),
      h(
        "div",
        null,
        h("div", { class: "plan__model" }, shortModel(entry.model)),
        h("div", { class: "plan__role" }, roleLabel(index)),
      ),
      control,
      flag ? h("div", { class: "plan__flag" }, flag) : null,
      fault ? h("p", { class: "plan__note" }, fault.summary) : null,
    );
  });
  $("#plan").replaceChildren(...rows);
}

function renderDraftState() {
  const { min_chars: min, max_chars: max } = state.config.limits;
  const length = state.draft.text.trim().length;
  const invalid = length < min || length > max;

  const count = $("#char-count");
  count.textContent = `${number(length)} / ${number(max)} characters${invalid ? " · the API will reject this" : ""}`;
  count.classList.toggle("is-invalid", invalid);

  const scenario = currentScenario();
  const live = state.draft.simulate.filter((fault) => !fault).length;
  let hint;
  if (invalid) hint = "No model call: validation refuses the request first";
  else if (scenario && !isEdited()) {
    hint = scenario.live_calls ? `About ${plural(scenario.live_calls, "live model call")}` : "No live model call";
  } else hint = live ? `Up to ${plural(live, "live model call")}` : "No live model call";
  $("#run-hint").textContent = `${hint} · Ctrl+Enter runs it`;

  // Not offered during a run: resetting would take the run's result off the page.
  $("#reset").hidden = !isEdited() || locked();
  $("#run").disabled = locked();
  $("#document").readOnly = locked();
}

// ------------------------------------------------------------- running ---

// The service keeps one ceiling on live model calls for all visitors together. A run that
// would cross it is refused before any model is called: 503, the error type below, and a
// Retry-After header with the real wait. A wait of up to a minute means "wait": the
// ceiling has room again by then. Anything longer is the day's share, which starts again
// at midnight UTC. Why exactly the service refused is in its own sentence, which the
// page shows under "What a client is told".
const REFUSAL_TYPE = "CallLimitError";
const SHORT_WAIT_SECONDS = 60; // the longest wait the service asks for when waiting helps
const BATCH_WAITS = 2; // how often "Run all" waits out a refusal, per scenario

/** Null unless the ceiling refused the run; else when to come back, and whether that is another day. */
function refusalOf(run) {
  if (run.status !== 503 || run.body?.detail?.error_type !== REFUSAL_TYPE) return null;
  const stated = Number(run.headers["retry-after"]);
  const seconds = Number.isFinite(stated) ? stated : SHORT_WAIT_SECONDS;
  return { until: performance.now() + seconds * 1000, today: seconds > SHORT_WAIT_SECONDS };
}

/** True for a real run that no model answered: no simulated failure in it, and a 503 with one entry per model. */
const failedForReal = (run) => !run.simulate.some(Boolean) && run.status === 503 && Array.isArray(run.body?.detail);

/** True while a run or "Run all" has the controls, the pauses of "Run all" included. */
const locked = () => state.busy || Boolean(state.batch);

async function runDraft() {
  if (locked()) return;
  const scenario = currentScenario();
  const pristine = scenario && !isEdited();
  await execute({
    key: pristine ? scenario.id : CUSTOM,
    scenario: pristine ? scenario : null,
    text: state.draft.text,
    simulate: state.draft.simulate,
  });
}

async function runAll() {
  if (state.batch) {
    state.batch.stop = true;
    state.abort?.abort();
    state.batch.skipWait?.(); // ends a countdown at once
    return;
  }
  if (state.busy) return;

  const { scenarios } = state.config;
  state.batch = { done: 0, total: scenarios.length, stop: false, skipWait: null, waitUntil: null };
  state.halt = null;
  for (const scenario of scenarios) {
    if (state.batch.stop) break;
    select(scenario.id);
    const start = () => execute({ key: scenario.id, scenario, text: scenario.text, simulate: scenario.simulate });
    let run = await start();
    // Refused, with a wait of a minute at most: wait until the service says there is
    // room, then send the scenario again. Carrying on at once would hand the same
    // refusal to every scenario that is left.
    for (let waits = 0; waits < BATCH_WAITS && run.refusal && !run.refusal.today && !state.batch.stop; waits += 1) {
      await new Promise((resolve) => {
        state.batch.skipWait = resolve;
        state.batch.waitUntil = run.refusal.until;
        setTimeout(resolve, run.refusal.until - performance.now());
        renderRailHead();
      });
      state.batch.skipWait = state.batch.waitUntil = null;
      if (!state.batch.stop) run = await start();
    }
    // Stopped by the visitor, or because the service changed its chain meanwhile (see followChain).
    if (state.batch.stop) break;
    // Still refused, or no answer at all: the scenarios that are left would end the same way.
    if (run.refusal || run.problem) {
      let why = "the demo did not answer";
      if (run.refusal) why = run.refusal.today ? "today's share of live runs is used up" : "the demo is still busy";
      state.halt = `Run all stopped: ${why}.`;
      break;
    }
    state.batch.done += 1;
    renderRailHead();
    // A breath between runs keeps each result readable; a longer one after real model
    // calls is kind to the free tier's per-minute rate limits.
    const live = run.attempts.some((attempt) => !attempt.simulated);
    if (!state.batch.stop) await new Promise((resolve) => setTimeout(resolve, live ? 1200 : 300));
  }
  state.batch = null;
  renderEverything();
  if (state.halt) announce(state.halt);
}

/**
 * Bring the result panel into view when the visitor starts a run and it is not: on a
 * narrow screen it sits below the request, and a press of Run would seem to do nothing.
 */
function showResult() {
  const { top, bottom } = $("#result").getBoundingClientRect();
  if (top > innerHeight - 120 || bottom < 120) $("#result").scrollIntoView({ block: "start" });
}

/** One run. `again` marks a run that the page sends itself, after the service changed its chain. */
async function execute({ key, scenario, text, simulate, again = false }) {
  // A notice about a changed chain goes with the visitor's next run, not with a run of the page's own.
  if (upkeep.cleared && !again) {
    upkeep.cleared = false;
    $("#notice").hidden = true;
  }
  const chainAtStart = chainKey(state.config.chain.map((entry) => entry.model));
  const run = {
    // The service can change its chain under the page. A result keeps the chain it ran on.
    chain: state.config.chain,
    chainKey: chainAtStart,
    key,
    scenario,
    text,
    simulate: [...simulate],
    phase: "running",
    startedAt: performance.now(),
    attempts: [],
    status: null,
    headers: {},
    body: null,
    serverMs: null,
    clientMs: null,
    requestId: null,
    problem: null,
    checks: [],
    slow: false, // sent, and nothing has come back for SLOW_START_MS
    quiet: false, // ended by the page: nothing came from the service for too long
    waking: false, // waiting for a service that did not answer to answer /health
    reached: false, // the service's own stream began: the service had taken the run
    resent: false, // sent a second time, by the page, after the service answered again
    again, // sent by the page itself, after the service changed its chain
    refusal: null, // set when the ceiling on live calls refused the run: see refusalOf
  };
  state.runs.set(key, run);
  state.view = key;
  state.busy = true;
  state.abort = new AbortController();
  if (!state.batch) state.halt = null;
  const { signal } = state.abort;
  const pressedAt = run.startedAt;
  renderEverything();
  if (!again && !state.batch) showResult();

  let answered = await send(run, signal);
  if (!answered && !run.reached && !signal.aborted) {
    // Nothing of the service's own came back, so the run never reached it: the host has
    // probably put the service to sleep. Wait for it, then send the run again: once,
    // never in a loop. A run the service had already taken is not sent a second time
    // by the page: it may have called a model.
    run.waking = true;
    renderEverything();
    announce("The demo is not answering. Waiting for it.");
    // The 3 minutes count from the press, whatever part of them the first send took.
    const awake = await waitForService(signal, pressedAt);
    run.waking = false;
    if (awake) {
      run.resent = true;
      announce("The demo is answering again. Sending your run again.");
      renderEverything();
      answered = await send(run, signal);
    }
    if (!answered) run.problem = awake ? NO_ANSWER_TWICE : NOT_WAKING;
  }
  if (!answered && signal.aborted) run.problem = "Stopped before a result arrived.";
  else if (!answered && !run.problem) run.problem = run.quiet ? WENT_QUIET(silenceLimit()) : CUT_OFF;
  // An answer proves the service is up, so the pill goes back to what it last said of itself.
  if (answered) showStatus();

  run.clientMs = performance.now() - (run.problem ? pressedAt : run.startedAt);
  run.phase = "done";
  run.attempts = run.attempts.filter((attempt) => attempt.state !== "running");
  run.refusal = refusalOf(run);
  // A scenario's expectations are written for the chain the page knew. If the service
  // changed its chain in between, they say nothing about this run. Nor can a run that
  // the pipeline never answered be held to them.
  run.moved = run.chainKey !== chainAtStart;
  run.checks = scenario && isScored(run) && !run.moved ? scenario.checks.map((check) => evaluate(check, run)) : [];
  state.history.push(run);
  state.busy = false;
  state.abort = null;
  // No model answered a run the visitor started. If a model was withdrawn, the service is
  // looking for a replacement now, and the run is sent again once the chain has a model it
  // did not have: see rerunIfRepaired. Once: a run the page sent itself is not remembered
  // again, and nothing of "Run all" is sent a second time.
  if (answered && failedForReal(run) && !again && !state.batch) upkeep.rerun = { run, at: Date.now() };
  renderEverything();
  const outcome = outcomeOf(run).title;
  let heard = `${run.status} ${outcome} in ${durationText(run.serverMs ?? run.clientMs)}`;
  if (run.refusal) heard = `${outcome}. ${run.refusal.today ? "Live runs start again at midnight UTC." : `Try again in ${waitText(run.refusal.until - performance.now())}.`}`;
  announce(run.problem ?? heard);

  if (!answered) return run;
  // A failed attempt of a run without simulated failures is what makes the service look,
  // and a run that met another chain than the page had drawn shows that the page is
  // behind. So is a page whose last look got no answer, now that the service has
  // answered this run. Look at once then, and closely for a moment.
  const failed = !run.simulate.some(Boolean) && run.attempts.some((attempt) => attempt.state === "failed");
  touchUpkeep(failed || run.moved || upkeep.lost ? 3 * LOOK_BUSY_MS : 0);
  return run;
}

/**
 * Send one run and read the answer into `run`. Resolves false when what came back is not
 * the service's own answer: the request failed, a host answered in the service's place,
 * the stream stopped before its result, or nothing came for too long.
 */
async function send(run, signal) {
  Object.assign(run, { startedAt: performance.now(), attempts: [], status: null, headers: {}, body: null, serverMs: null });
  // A host that is waking the service may hold the request for a minute and then deliver
  // it. Nothing needs sending twice then: the running view and the pill say why it is
  // quiet, until the service is heard from.
  const slow = setTimeout(() => {
    run.slow = true;
    showStatus("waiting");
    renderResult();
  }, SLOW_START_MS);
  const heard = () => {
    clearTimeout(slow);
    if (run.slow) showStatus();
    run.slow = false;
  };
  // A request that is held and never delivered, or a stream that stops without closing,
  // must not keep the page on "Running" for ever: after too long a silence the page ends
  // the request itself. Every event of the stream starts that wait again.
  const wait = patience(silenceLimit(), signal);

  try {
    const response = await fetch("/playground/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: run.text, simulate: run.simulate }),
      signal: wait.signal,
    });
    run.requestId = response.headers.get("x-request-id");

    if ((response.headers.get("content-type") ?? "").includes("ndjson")) {
      // The service's own headers: it is up and has the run, whenever its events come.
      // (Not left to the stream's first line: a browser may drop that line when the
      // connection breaks right behind it.)
      run.reached = true;
      heard();
      renderResult();
      await readEvents(response, (event) => {
        wait.renew();
        applyEvent(run, event);
        renderResult();
      });
    } else {
      // Refused before the pipeline started, by validation, the size limit or the guard:
      // the body is the API's own error envelope.
      const body = await response.json();
      if (isEnvelope(body)) {
        run.status = response.status;
        run.body = body;
      }
    }
  } catch {
    // A failed request, a page of HTML where JSON was expected, a stream that broke off
    // or went quiet: none of these is an answer. The caller learns that from the return value.
  } finally {
    heard();
    wait.done();
    run.quiet = wait.signal.aborted && !signal.aborted;
  }
  return run.status != null;
}

/** Read a newline-delimited JSON stream, handing each object to `onEvent` as it arrives. */
async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
    }
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

function applyEvent(run, event) {
  if (event.event === "start") {
    run.requestId = event.request_id ?? run.requestId;
    if (Array.isArray(event.chain)) {
      // What the service is about to call, which is not always what the page last drew.
      run.chain = event.chain;
      run.chainKey = chainKey(event.chain.map((entry) => entry.model));
    }
  } else if (event.event === "attempt_started") {
    run.attempts.push({
      index: event.index,
      model: event.model,
      simulated: event.simulated,
      state: "running",
      startedAt: performance.now(),
    });
  } else if (event.event === "attempt_finished") {
    const attempt = run.attempts.findLast((candidate) => candidate.index === event.index);
    if (!attempt) return;
    Object.assign(attempt, {
      state: event.outcome, // answered | failed | no_invoice
      errorType: event.error_type,
      message: event.message,
      retryable: event.retryable,
      elapsedMs: event.elapsed_ms,
    });
  } else if (event.event === "result") {
    run.status = event.status_code;
    run.headers = event.headers ?? {};
    run.body = event.body;
    run.serverMs = event.elapsed_ms;
  }
}

// ---------------------------------------------------------- expectations ---

const dig = (value, path) => path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), value);

function matches(actual, expected) {
  // Money comes back as floats; compare to the cent, not to the last bit.
  if (typeof expected === "number") return typeof actual === "number" && Math.abs(actual - expected) < 0.005;
  return actual === expected;
}

function evaluate(check, run) {
  let actual;
  let expected = check.expected;
  let pass = false;

  switch (check.kind) {
    case "status":
      actual = run.status;
      pass = actual === expected;
      break;
    case "field":
      actual = dig(run.body, check.path);
      pass = matches(actual, expected);
      break;
    case "contains":
      actual = dig(run.body, check.path);
      pass = typeof actual === "string" && actual.toLowerCase().includes(expected.toLowerCase());
      break;
    case "count": {
      const list = dig(run.body, check.path);
      actual = Array.isArray(list) ? list.length : undefined;
      pass = actual === expected;
      break;
    }
    case "answered_by":
      expected = run.chain[check.expected]?.model;
      actual = run.body?.model_used;
      pass = actual != null && actual === expected;
      break;
    case "calls":
      actual = run.attempts.length;
      pass = actual === expected;
      break;
    case "verdicts":
      actual = run.attempts.filter((attempt) => attempt.state === "no_invoice").length;
      pass = actual === expected;
      break;
    case "attempt":
      actual = run.attempts.find((attempt) => attempt.index === check.index)?.errorType ?? undefined;
      pass = actual === expected;
      break;
    case "header":
      actual = run.headers[check.name] != null;
      pass = actual === expected;
      break;
  }
  return { label: check.label, pass, expected, actual };
}

const shown = (value) => (value === undefined ? "nothing" : JSON.stringify(value));

// --------------------------------------------------------------- result ---

const ERROR_WORDS = {
  ProviderRateLimitError: "Rate limited",
  ProviderTimeoutError: "Timed out",
  ProviderAuthError: "Credentials rejected",
  ProviderError: "Upstream error",
  ProviderNotConfiguredError: "No API key",
  SchemaValidationError: "Unusable output",
  ContentBlockedError: "Truncated or blocked",
  UnparseableTextError: "Says “no invoice”",
};

// Three of the engine's messages are written for the admin: each names the setting to
// change. A visitor cannot change a setting, so the page's own lines say this instead.
// The raw response at the bottom of a run still shows what the API returned, word for word.
const VISITOR_WORDS = [
  // [a part of the engine's message, what a visitor reads in its place]
  ["is not configured", "the service has no API key"],
  ["truncated the response", "the answer was longer than the service allows, so it was cut off"],
  ["does not support the requested structured output", "this model cannot answer in the format the service asks for"],
];

/** An engine message as the page prints it: unchanged, unless it is one of the three above. */
function visitorWords(message) {
  const row = VISITOR_WORDS.find(([part]) => message.includes(part));
  return row ? row[1] : message;
}

/** What a run amounts to. `note` is one paragraph, or a list of them. */
function outcomeOf(run) {
  if (run.waking) {
    return {
      tone: "running",
      code: "",
      title: "Waiting for the demo",
      note: [
        `The demo did not answer, ${probably()}`,
        "This page asks every 3 seconds whether it is back, for 3 minutes at most. As soon as it answers, your run is sent again by itself.",
      ],
    };
  }
  if (run.phase === "running") {
    const current = run.attempts.find((attempt) => attempt.state === "running");
    return {
      tone: "running",
      code: "",
      title: "Running",
      note: [
        current ? `Waiting for ${shortModel(current.model)}` : "Sending the request",
        run.slow ? `No answer yet, so the demo may be waking up. ${WHY_ASLEEP}` : null,
      ],
    };
  }
  if (run.problem) return { tone: "crit", code: "", title: "No result", note: run.problem };
  if (run.refusal) return refusalOutcome(run.refusal);

  const error = run.body?.error;
  if (run.status === 200) {
    return { tone: "good", code: "200", title: "Invoice extracted", note: null };
  }
  if (error === "validation_error") {
    return {
      tone: "neutral",
      code: "422",
      title: "Rejected by request validation",
      note: "The request never reached a model.",
    };
  }
  if (error === "unparseable_text") {
    return { tone: "neutral", code: "422", title: "Not an invoice", note: run.body.message };
  }
  if (error === "providers_unavailable") {
    return {
      tone: "crit",
      code: String(run.status),
      title: "Every model failed",
      // What the header means, and no more: whether the service repairs its chain after
      // such a run is said by the lines that upkeepNotes adds.
      note: run.headers["retry-after"]
        ? `Retry-After: ${run.headers["retry-after"]}. At least one failure was transient, so a retry may succeed.`
        : "No Retry-After header: waiting would not make any of these failures go away.",
    };
  }
  // Any other error of the service's own, in the words of its envelope.
  return {
    tone: "crit",
    code: String(run.status ?? ""),
    title: run.body?.message ?? "The demo reported an error",
    note: null,
  };
}

/**
 * A refusal by the ceiling on live calls, for the visitor: what happened and how long to
 * wait. The page knows the refusal and the wait, not which rule of the ceiling it was, so
 * its own sentences stay general; the service's sentence is under "What a client is told".
 */
function refusalOutcome({ until, today }) {
  const waiting = until > performance.now();
  if (today) {
    return {
      tone: "warn",
      code: "503",
      title: waiting ? "Today's share of live runs is used up" : "The day's share of live runs was used up",
      note: [
        [
          "The demo runs on a free daily allowance of model calls. The part set aside for visitors ",
          waiting ? "is used up for today" : "was used up when this run was sent",
          ", so no model was called for this run. ",
          waiting
            ? ["Live runs start again at midnight UTC, in about ", countdown(until), "."]
            : "Midnight UTC has passed since then, so you can try again now.",
        ],
        SIMULATED_STILL_WORK,
      ],
    };
  }
  return {
    tone: "warn",
    code: "503",
    title: "The demo is busy",
    note: [
      [
        "The demo keeps a ceiling on its live model calls, for all visitors together, and ",
        waiting ? "has reached it for the moment" : "had reached it when this run was sent",
        ". No model was called for this run. ",
        state.batch ? "Run all carries on by itself " : "You can try again ",
        waiting ? ["in ", countdown(until), "."] : "now.",
      ],
      SIMULATED_STILL_WORK,
    ],
  };
}

/**
 * The result panel has three parts: the head of the run on view, the notice about the
 * chain's upkeep (drawn by renderUpkeepNotice, and left alone here), and the rest of the run.
 */
function renderResult() {
  const run = state.runs.get(state.view);
  renderOutcome();
  const blocks = [];
  if (!run) {
    blocks.push($("#tpl-empty").content.cloneNode(true));
  } else {
    blocks.push(kpiBlock(run), traceBlock(run));
    if (run.phase === "done" && !run.problem) blocks.push(bodyBlock(run), checksBlock(run), rawBlock(run));
  }
  $("#result-body").replaceChildren(...blocks.filter(Boolean));
}

/**
 * The head of the result panel alone. A countdown that ends, or news about the chain's
 * upkeep, changes a sentence there; what the visitor opened or focused below it stays.
 */
function renderOutcome() {
  const run = state.runs.get(state.view);
  $("#result-head").replaceChildren(...(run ? [outcomeBlock(run)] : []));
}

function outcomeBlock(run) {
  const outcome = outcomeOf(run);
  const chips = [];
  if (run.status === 200 && run.body?.fallback_triggered) {
    chips.push(chip("Fallback used", { tone: "warn", iconName: "shuffle" }));
  }
  if (run.simulate.some(Boolean)) chips.push(chip("Simulated failure", { iconName: "bolt" }));
  if (run.moved) chips.push(chip("The chain changed during this run: expectations not applied"));
  if (run.phase === "done" && !run.scenario && run.key === CUSTOM && currentScenario()) {
    chips.push(chip("Edited: expectations not applied"));
  }

  const notes = Array.isArray(outcome.note) ? outcome.note : [outcome.note];
  // The page sent this run a second time on its own, so it says so, whatever came back.
  if (run.resent) notes.unshift(SENT_AGAIN);
  if (run.again) notes.unshift(REPAIRED_AND_SENT_AGAIN);
  notes.push(...upkeepNotes(run));
  // Nothing came back, or the wait the service asked for is over (a minute, or the rest
  // of the day): offer the same run again.
  const again = run.problem || (run.refusal && run.refusal.until <= performance.now());

  return h(
    "header",
    { class: "outcome" },
    outcome.tone === "running" ? h("span", { class: "spinner" }) : badge(outcome.tone, "badge--lg"),
    outcome.code ? h("span", { class: "outcome__code" }, outcome.code) : null,
    h("span", { class: "outcome__title" }, outcome.title),
    chips.length ? h("span", { class: "outcome__chips" }, chips) : null,
    notes.filter(Boolean).map((words) => h("p", { class: "outcome__note" }, words)),
    again && !state.batch
      ? h("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => execute({ ...run, again: false }) }, icon("reset"), "Try again")
      : null,
  );
}

function kpi(label, value, sub, className = "kpi") {
  return h(
    "div",
    { class: className },
    h("span", { class: "kpi__label" }, label),
    value,
    sub ? h("span", { class: "kpi__sub" }, sub) : null,
  );
}

function kpiBlock(run) {
  const chain = run.chain;
  const running = run.phase === "running";
  const body = run.body ?? {};
  const earlier = run.attempts.filter((attempt) => attempt.state !== "answered").length;
  const simulated = run.attempts.filter((attempt) => attempt.simulated).length;

  // The headline number: how long the caller waited.
  const [amount, unit] = running ? ["0.0", "s"] : duration(run.serverMs ?? run.clientMs);
  let about = run.serverMs != null ? "server side, all attempts" : "round trip from this page";
  if (running) about = null;
  else if (run.problem) about = "without a result";
  const time = kpi(
    running ? "Elapsed" : run.problem ? "Waited" : "Response time",
    h(
      "span",
      { class: "kpi__value" },
      running ? h("span", { dataset: { since: run.startedAt } }, amount) : amount,
      h("small", null, unit),
    ),
    about,
    "kpi kpi--hero",
  );

  let who = kpi("Answered by", h("span", { class: "kpi__value" }, "None"), null, "kpi kpi--who");
  if (running) {
    who = kpi("Answered by", h("span", { class: "kpi__value" }, "…"), "waiting", "kpi kpi--who");
  } else if (run.status === 200) {
    const index = chain.findIndex((entry) => entry.model === body.model_used);
    const { vendor } = modelParts(body.model_used);
    who = kpi(
      "Answered by",
      h("span", { class: "kpi__value kpi__value--model" }, shortModel(body.model_used)),
      [index >= 0 ? roleLabel(index, chain) : null, vendor].filter(Boolean).join(" · "),
      "kpi kpi--who",
    );
  } else if (body.error === "unparseable_text" && body.detail?.provider) {
    who.append(h("span", { class: "kpi__sub" }, `verdict settled by ${shortModel(body.detail.provider)}`));
  } else if (body.error === "validation_error" || run.refusal) {
    who.append(h("span", { class: "kpi__sub" }, "no model was called"));
  }

  // A run that ended without a result does not say how far the service got with it.
  const unknown = run.problem && !run.attempts.length;
  const called = kpi(
    "Models called",
    h("span", { class: "kpi__value" }, unknown ? "–" : [run.attempts.length, h("small", null, `of ${chain.length}`)]),
    simulated ? `${simulated} simulated` : run.attempts.length ? "all live" : null,
  );

  let fallback = kpi("Fallback", h("span", { class: "kpi__value" }, "–"), null);
  if (run.status === 200) {
    fallback = body.fallback_triggered
      ? kpi("Fallback", h("span", { class: "kpi__value" }, "Used"), `after ${plural(earlier, "earlier attempt")}`)
      : kpi("Fallback", h("span", { class: "kpi__value" }, "Not needed"), "the primary answered");
  }

  return h("div", { class: "kpis" }, time, who, called, fallback);
}

function notCalledReason(run) {
  if (run.phase === "running") return "Waiting its turn";
  if (run.status === 200) return "Not needed";
  if (run.body?.error === "unparseable_text") return "Not needed: the verdict was already settled";
  if (run.body?.error === "validation_error") return "Not reached: the request was rejected first";
  if (run.refusal) return "Not called: the run was refused first";
  if (run.problem) return "Not known: no result arrived";
  return "Not reached";
}

function traceRow(run, entry, longest) {
  const attempt = run.attempts.find((candidate) => candidate.index === entry.index);
  // A mark, then the words (and any chip) as one run of text that wraps beside it.
  const who = (mark, ...words) =>
    h(
      "div",
      { class: "trace__who" },
      h("div", { class: "trace__model" }, shortModel(entry.model)),
      h("div", { class: "trace__state" }, mark, h("span", null, words)),
    );
  const position = h("span", { class: "num" }, entry.index + 1);

  if (!attempt) {
    return h(
      "div",
      { class: "trace__row is-skipped" },
      position,
      who(badge("muted"), notCalledReason(run)),
      h("div", { class: "trace__bar" }),
      h("span", { class: "trace__time" }, "–"),
    );
  }

  const fault = state.config.faults.find((candidate) => candidate.id === run.simulate[entry.index]);
  const simulatedChip = attempt.simulated ? chip("simulated", { iconName: "bolt" }) : null;

  if (attempt.state === "running") {
    return h(
      "div",
      { class: "trace__row" },
      position,
      who(
        h("span", { class: "spinner" }),
        attempt.simulated ? `Simulating: ${fault?.label ?? "a failure"}` : "Calling the model",
        simulatedChip,
      ),
      h("div", { class: "trace__bar" }, h("div", { class: "bar bar--running" })),
      h("span", { class: "trace__time" }, h("span", { dataset: { since: attempt.startedAt } }, "0.0"), " s"),
    );
  }

  const look = {
    answered: { tone: "good", words: "Answered with a valid invoice" },
    failed: { tone: "crit", words: ERROR_WORDS[attempt.errorType] ?? attempt.errorType },
    no_invoice: { tone: "neutral", words: ERROR_WORDS.UnparseableTextError },
  }[attempt.state];

  const bar = h("div", { class: `bar bar--${look.tone}` });
  bar.style.width = `${Math.max(0, (attempt.elapsedMs / longest) * 100)}%`;
  const track = h("div", { class: "trace__bar", tabindex: "0" }, bar);
  setTip(track, durationText(attempt.elapsedMs), [
    `${roleLabel(entry.index, run.chain)} · ${entry.model}`,
    look.words + (attempt.simulated ? " (simulated)" : ""),
  ]);

  return h(
    "div",
    { class: "trace__row" },
    position,
    who(badge(look.tone), look.words, simulatedChip),
    track,
    h("span", { class: "trace__time" }, durationText(attempt.elapsedMs)),
    attempt.message
      ? h(
          "p",
          { class: "trace__msg" },
          attempt.state === "failed"
            ? [h("code", null, attempt.errorType), attempt.retryable ? " · retryable · " : " · not retryable · "]
            : null,
          visitorWords(attempt.message),
        )
      : null,
  );
}

function traceBlock(run) {
  const longest = Math.max(1, ...run.attempts.map((attempt) => attempt.elapsedMs ?? 0));
  return h(
    "section",
    { class: "trace" },
    h(
      "div",
      { class: "block__title" },
      h("h4", null, "Each model in the chain"),
      h("span", null, "Bar length: time spent on the attempt"),
    ),
    run.chain.map((entry) => traceRow(run, entry, longest)),
  );
}

const absent = (words) => h("span", { class: "is-absent" }, words);

function invoiceBlock(data) {
  const format = (amount) => money(amount, data.currency);
  const lines = data.items.reduce((sum, item) => sum + item.total, 0);
  const differs = data.items.length > 0 && Math.abs(lines - data.total_amount) >= 0.005;

  const rows = data.items.length
    ? data.items.map((item) =>
        h(
          "tr",
          null,
          h("td", null, item.description),
          h("td", { class: "n" }, item.quantity),
          h("td", { class: "n" }, format(item.unit_price)),
          h("td", { class: "n" }, format(item.total)),
        ),
      )
    : [h("tr", { class: "items__empty" }, h("td", { colspan: "4" }, "No itemised lines in this document"))];

  const totals = [];
  if (data.items.length) totals.push(h("dt", null, "Line items"), h("dd", null, format(lines)));
  totals.push(
    h("dt", null, "Tax"),
    h("dd", null, data.tax_amount == null ? absent("not stated") : format(data.tax_amount)),
    h("dt", { class: "is-total" }, "Total"),
    h("dd", { class: "is-total" }, format(data.total_amount)),
  );

  return h(
    "section",
    { class: "invoice" },
    h("span", { class: "stamp" }, "Schema valid"),
    h("p", { class: "invoice__vendor" }, data.vendor_name || absent("Vendor not stated")),
    h(
      "dl",
      { class: "invoice__meta" },
      h("div", null, h("dt", null, "Invoice number"), h("dd", null, data.invoice_number || absent("not stated"))),
      h("div", null, h("dt", null, "Date"), h("dd", null, data.invoice_date ?? absent("not stated"))),
      h("div", null, h("dt", null, "Currency"), h("dd", null, data.currency)),
    ),
    h(
      "table",
      { class: "items" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", { scope: "col" }, "Description"),
          h("th", { scope: "col", class: "n" }, "Qty"),
          h("th", { scope: "col", class: "n" }, "Unit price"),
          h("th", { scope: "col", class: "n" }, "Line total"),
        ),
      ),
      h("tbody", null, rows),
    ),
    h("dl", { class: "totals" }, totals),
    h(
      "p",
      { class: "invoice__note" },
      differs
        ? `The lines add up to ${format(lines)}. The total is the amount on the document, which also covers tax, shipping or discounts.`
        : "Each line total is recomputed by the service as quantity × unit price, not copied from the model.",
    ),
  );
}

function bodyBlock(run) {
  const body = run.body ?? {};
  if (run.status === 200 && body.data) return invoiceBlock(body.data);

  if (body.error === "validation_error") {
    const issues = Array.isArray(body.detail) ? body.detail : [];
    return h(
      "section",
      { class: "verdict" },
      h("h4", null, "What the API reported"),
      h("ul", null, issues.map((issue) => h("li", null, `${(issue.loc ?? []).join(".")}: ${issue.msg}`))),
      h("p", null, "The submitted text is not echoed back in the error."),
    );
  }

  if (body.error === "unparseable_text") {
    const verdicts = run.attempts.filter((attempt) => attempt.state === "no_invoice").length;
    return h(
      "section",
      { class: "verdict" },
      h("h4", null, "No invoice in this text"),
      h(
        "p",
        null,
        verdicts >= 2
          ? "Two models reached the same verdict independently, which is what it takes to settle a refusal. One model's miss cannot reject a real invoice."
          : "Only one model could give a verdict. A verdict still outranks the transient failures around it, so the caller gets a refusal instead of a retry loop.",
      ),
    );
  }

  if (run.refusal) {
    return h(
      "section",
      { class: "verdict" },
      h("h4", null, "What a client is told"),
      // The service's own sentence, which says which rule of its ceiling refused the run.
      h("p", null, body.message),
      h(
        "p",
        null,
        "The public API refuses in the same way: status 503, the error code ",
        h("code", null, body.error),
        " and a ",
        h("code", null, "Retry-After"),
        " header with the wait in seconds. A refused request costs no model call.",
      ),
    );
  }

  if (body.error === "providers_unavailable") {
    const entries = Array.isArray(body.detail) ? body.detail : [body.detail].filter(Boolean);
    return h(
      "section",
      { class: "verdict" },
      h("h4", null, "What a client is told"),
      h(
        "ul",
        null,
        entries.map((entry) =>
          h("li", null, `${shortModel(entry.model ?? entry.provider ?? "model")}: ${entry.error_type}, ${entry.retryable ? "retryable" : "not retryable"}`),
        ),
      ),
      h("p", null, "One sanitised entry per model. The raw upstream text stays in the server log, under this request's id."),
    );
  }

  return body.message ? h("section", { class: "verdict" }, h("p", null, body.message)) : null;
}

function checksBlock(run) {
  if (!run.checks.length) return null;
  const { passed, total } = verdictOf(run);
  return h(
    "section",
    { class: "checks" },
    h(
      "div",
      { class: "block__title" },
      h("h4", null, "Expectations"),
      h("span", null, `${passed} of ${total} met`),
    ),
    h(
      "ul",
      { class: "checks__list" },
      run.checks.map((check) =>
        h(
          "li",
          null,
          badge(check.pass ? "good" : "crit"),
          h("span", null, check.label),
          check.pass
            ? null
            : h("span", { class: "checks__diff" }, `expected ${shown(check.expected)}, got ${shown(check.actual)}`),
        ),
      ),
    ),
  );
}

function rawBlock(run) {
  const headerLines = [`HTTP ${run.status}`];
  for (const [name, value] of Object.entries(run.headers)) headerLines.push(`${name}: ${value}`);
  if (run.requestId) headerLines.push(`x-request-id: ${run.requestId}`);

  return h(
    "details",
    { class: "raw" },
    h(
      "summary",
      null,
      icon("chevron"),
      "Raw request and response",
      run.requestId ? h("span", { class: "mono" }, `request ${run.requestId.slice(0, 8)}`) : null,
    ),
    h(
      "div",
      { class: "raw__body" },
      h("p", { class: "raw__label" }, "Sent by this page"),
      h(
        "pre",
        { class: "code" },
        h("span", { class: "h" }, "POST /playground/extract"),
        "\n",
        jsonNodes({ text: run.text, simulate: run.simulate }),
      ),
      h("p", { class: "raw__label" }, "What POST /api/v1/extract returns for this outcome"),
      h("pre", { class: "code" }, h("span", { class: "h" }, headerLines.join("\n")), "\n\n", jsonNodes(run.body)),
    ),
  );
}

function tickTimers() {
  const now = performance.now();
  for (const element of document.querySelectorAll("[data-since]")) {
    element.textContent = ((now - Number(element.dataset.since)) / 1000).toFixed(1);
  }
  for (const element of document.querySelectorAll("[data-until]")) {
    const left = Number(element.dataset.until) - now;
    if (left > 0) {
      if (element.textContent !== waitText(left)) element.textContent = waitText(left);
    } else if (element.closest("#result-head")) {
      // The wait of the run on view is over: its sentence changes, the countdown goes
      // and "Try again" appears. ("Run all" takes its own wait up again by itself.)
      renderOutcome();
      if (!state.batch) announce("The wait is over. You can try again now.");
    }
  }
}

function renderEverything() {
  renderScenarios();
  renderPlan();
  renderDraftState();
  renderResult();
  renderSession();
}

// -------------------------------------------------------------- session ---

function tile(label, value, unit, sub) {
  return h(
    "div",
    { class: "tile" },
    h("div", { class: "tile__label" }, label),
    h("div", { class: "tile__value" }, value, unit ? h("small", null, unit) : null),
    h("div", { class: "tile__sub" }, sub),
  );
}

function renderSession() {
  const root = $("#session");
  // The table below has one row per model of the chain in use, so only runs made on
  // that chain are counted. The others stay on the page; they are just not added up here.
  const current = chainKey(state.config.chain.map((entry) => entry.model));
  const finished = state.history.filter(isScored);
  const runs = finished.filter((run) => run.chainKey === current);
  root.hidden = runs.length === 0;
  if (!runs.length) return;
  const earlier = finished.length - runs.length;

  const extracted = runs.filter((run) => run.status === 200);
  const refused = runs.filter((run) => run.status === 422).length;
  const attempts = runs.flatMap((run) => run.attempts);
  const live = attempts.filter((attempt) => !attempt.simulated);
  const fallbacks = extracted.filter((run) => run.body.fallback_triggered).length;
  const [typical, typicalUnit] = duration(median(extracted.map((run) => run.serverMs)));

  const tiles = h(
    "div",
    { class: "tiles" },
    tile("Runs", runs.length, null, `${extracted.length} extracted · ${refused} refused · ${runs.length - extracted.length - refused} failed`),
    tile("Live model calls", live.length, null, `plus ${plural(attempts.length - live.length, "simulated attempt")}`),
    tile("Median response", typical, typicalUnit, "across successful extractions"),
    tile("Fallbacks used", fallbacks, extracted.length ? `of ${extracted.length}` : null, "extractions served by a later model"),
  );

  // One row per model: how often it answered, and how fast when it did.
  const rows = state.config.chain.map((entry) => {
    const mine = attempts.filter((attempt) => attempt.index === entry.index);
    const answers = mine.filter((attempt) => attempt.state === "answered");
    const failures = mine.filter((attempt) => attempt.state === "failed");
    const average = answers.length ? answers.reduce((sum, attempt) => sum + attempt.elapsedMs, 0) / answers.length : null;
    const verdicts = mine.filter((attempt) => attempt.state === "no_invoice");
    // Has the real model, as opposed to a simulated stand-in, ever come back with anything?
    const responded = answers.length > 0 || verdicts.some((attempt) => !attempt.simulated);
    return { entry, answers, failures, verdicts: verdicts.length, responded, average };
  });
  const slowest = Math.max(1, ...rows.map((row) => row.average ?? 0));

  const table = h(
    "table",
    { class: "table" },
    h("caption", null, "Which model answered, and how fast"),
    h(
      "thead",
      null,
      h(
        "tr",
        null,
        h("th", { scope: "col" }, "Model"),
        h("th", { scope: "col", class: "n" }, "Answered"),
        h("th", { scope: "col", class: "n" }, "Said “no invoice”"),
        h("th", { scope: "col", class: "n" }, "Failed"),
        h("th", { scope: "col", class: "n" }, "Average time to answer"),
      ),
    ),
    h(
      "tbody",
      null,
      rows.map(({ entry, answers, failures, verdicts, average }) => {
        const bar = h("div", { class: "bar bar--accent" });
        bar.style.width = average == null ? "0" : `${(average / slowest) * 100}%`;
        const staged = failures.filter((attempt) => attempt.simulated).length;
        const track = h("div", { class: "cellbar__track" }, average == null ? null : bar);
        if (average != null) {
          setTip(track, durationText(average), [entry.model, `average over ${plural(answers.length, "answer")}`]);
        }
        return h(
          "tr",
          null,
          h("td", null, h("div", { class: "table__model" }, shortModel(entry.model), h("small", null, roleLabel(entry.index)))),
          h("td", { class: "n" }, answers.length),
          h("td", { class: "n" }, verdicts),
          h("td", { class: "n" }, failures.length, staged ? ` (${staged} simulated)` : ""),
          h("td", null, h("div", { class: "cellbar" }, track, h("span", { class: "cellbar__value" }, average == null ? "–" : durationText(average)))),
        );
      }),
    ),
  );

  // The last answer each scenario got. A later run that was refused, or left without an
  // answer, does not take it back.
  const scored = state.config.scenarios.map((scenario) => runs.findLast((run) => run.key === scenario.id)).filter(Boolean);
  const checks = scored.flatMap((run) => run.checks);
  const summary = scored.length
    ? `${scored.filter((run) => verdictOf(run).tone === "good").length} of ${plural(scored.length, "scenario")} met every expectation · ${checks.filter((check) => check.pass).length}/${checks.length} checks`
    : "Custom documents only so far";

  root.replaceChildren(
    h(
      "div",
      { class: "session__head" },
      h("h3", { id: "session-title" }, "This session"),
      h("p", null, summary, earlier ? ` · ${plural(earlier, "earlier run")} on the chain before ${earlier === 1 ? "is" : "are"} not counted here` : null),
    ),
    ...rows.map(silentModelNotice).filter(Boolean),
    h("div", { class: "session__grid" }, tiles, h("div", { class: "table-scroll" }, table)),
  );
}

/** A warning for a model that was really called this session and never produced an answer. */
function silentModelNotice({ entry, answers, failures, responded }) {
  const real = failures.filter((attempt) => !attempt.simulated);
  const permanent = real.some((attempt) => !attempt.retryable);
  if (responded || !real.length || (!permanent && real.length < 3)) return null;
  return h(
    "p",
    { class: "notice" },
    icon("warning"),
    h(
      "span",
      null,
      h("code", null, entry.model),
      ` has not answered in ${plural(real.length, "live call")} this session: ${visitorWords(real.at(-1).message)}. `,
      entry.index === 0
        ? "Every request waits for it to fail before a fallback answers. "
        : "The chain has one model fewer to fall back on. ",
      // Same voice as the chip: the reader is a visitor, and the fix is in the server log.
      upkeep.picture?.mode === "automatic"
        ? "The service looks into a failed call by itself and replaces a model that OpenRouter no longer offers."
        : entry.listing?.status === "not_listed"
          ? UNLISTED_LABEL
          : "Please contact the admin.",
    ),
  );
}

// ----------------------------------------------------------- test suite ---

function renderSuiteControls() {
  const { available, reason } = state.config.suite;
  const button = $("#run-suite");
  button.hidden = !available;
  if (available) {
    button.addEventListener("click", runSuite);
  } else {
    const note = $("#suite-note");
    note.replaceChildren(`${reason} Run it from a checkout with `, h("code", null, "pytest -q"), ".");
    note.hidden = false;
  }
}

async function runSuite() {
  const button = $("#run-suite");
  const label = $("span", button);
  const root = $("#suite");
  button.disabled = true;
  label.textContent = "Running";
  root.replaceChildren(
    h("div", { class: "suite__summary" }, h("span", { class: "spinner" }), h("p", null, "pytest is running in a child process")),
  );

  const wait = patience(GIVE_UP_AFTER_MS);
  try {
    const response = await fetch("/playground/tests", { method: "POST", signal: wait.signal });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message ?? `The service answered ${response.status}`);
    renderSuite(body);
  } catch (error) {
    root.replaceChildren(
      h("div", { class: "suite__summary" }, badge("crit", "badge--lg"), h("span", { class: "suite__headline" }, "The suite could not be run")),
      h("pre", { class: "code" }, wait.signal.aborted ? "No answer came within 3 minutes." : error.message),
    );
  } finally {
    wait.done();
    button.disabled = false;
    label.textContent = "Run it again";
  }
}

function renderSuite({ summary, sections }) {
  const clean = summary.failed === 0;
  const headline = [`${summary.passed} passed`];
  if (summary.failed) headline.push(`${summary.failed} failed`);
  if (summary.skipped) headline.push(`${summary.skipped} skipped`);

  let position = 0;
  const groups = sections.map((section) => {
    const total = section.tests.length;
    const dots = section.tests.map((test) => {
      const dot = h("span", { class: `dot dot--${test.outcome}` });
      dot.style.animationDelay = `${Math.min(position++ * 5, 700)}ms`;
      setTip(dot, durationText(test.duration_ms), [test.name, test.outcome]);
      return dot;
    });
    const list = section.tests.map((test) =>
      h(
        "li",
        null,
        badge(test.outcome === "passed" ? "good" : test.outcome === "failed" ? "crit" : "muted"),
        h("span", null, test.name),
        h("time", null, durationText(test.duration_ms)),
        test.detail ? h("pre", null, test.detail) : null,
      ),
    );
    return h(
      "article",
      { class: "group" },
      h(
        "div",
        { class: "group__head" },
        h("h4", null, section.title),
        h("span", null, `${section.passed}/${total} · ${durationText(section.duration_ms)}`),
      ),
      h("div", { class: "dots" }, dots),
      // Open a section that has failures, so they are never hidden behind a click.
      h("details", { open: section.failed > 0 }, h("summary", null, `List the ${plural(total, "test")}`), h("ul", { class: "testlist" }, list)),
    );
  });

  $("#suite").replaceChildren(
    h(
      "div",
      { class: "suite__summary" },
      badge(clean ? "good" : "crit", "badge--lg"),
      h("span", { class: "suite__headline" }, headline.join(", ")),
      h(
        "p",
        null,
        `${plural(summary.total, "test")} in ${durationText(summary.duration_ms)} by pytest's clock, ${durationText(summary.wall_ms)} including interpreter start-up`,
      ),
    ),
    h("div", { class: "suite__sections" }, groups),
  );
}

// ---------------------------------------------------------- model upkeep ---

// Words for everything GET /playground/models can say. The service sends codes, dates
// and figures; every sentence a visitor reads about the chain's upkeep is written
// here, in one place. tests/test_models_page.py compares the keys of these tables with
// the tuples of codes in app/model_chooser.py and app/model_rules.py, so a code the
// service can send and the page has no words for fails the suite.
//
// Written for visitors: nothing here names a setting, a file or a key. What an admin
// has to do is in the server log. Two names stand for two things: "the demo" is what
// the visitor is using (it sleeps, it has an allowance and an admin), "the service" is
// the software and what it does.
//
// Uses app.js's own helpers: shortModel, longDate, plural, seconds, ERROR_WORDS.

const listWords = (models) => new Intl.ListFormat("en-GB", { type: "conjunction" }).format(models.map(shortModel));
const chainWords = (models) => models.map(shortModel).join(", then ");
const clockWords = (stamp) => `${stamp.slice(11, 16)} UTC`;
const dayWords = (stamp) => longDate(stamp.slice(0, 10));
const momentWords = (stamp) => `${dayWords(stamp)}, ${clockWords(stamp)}`;

/** A moment still to come: the clock time in UTC, and the visitor's own where that is another. */
function soonWords(stamp) {
  const local = new Date(stamp).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return local === stamp.slice(11, 16) ? clockWords(stamp) : `${clockWords(stamp)} (${local} your time)`;
}

/** The stand-ins that answer now: models put to use on one right answer, which no test has judged yet. */
const standIns = (p) => (p.origin.why?.code === "first_answer" ? p.origin.why.models : []);

/** The stand-ins in a sentence, and who has the last word about them. */
function standInWords(p) {
  const models = standIns(p);
  const one = models.length === 1;
  return (
    `${listWords(models)} ${one ? "answers as a stand-in" : "answer as stand-ins"}, ` +
    `after one right answer ${one ? "" : "each "}to a sample document. ` +
    (p.test ? "The test that is running has the last word." : `No test has judged ${one ? "it" : "them"} yet: the next one has the last word.`)
  );
}

/** The one line in the "Model chain" card: where the chain in use comes from. */
const ORIGIN_LINE = {
  pinned: () => "Fixed by the admin of this demo.",
  default: (p) => `The tested default, measured on ${dayWords(p.origin.tested)}.`,
  published: (p) =>
    `From this project's scheduled test of ${dayWords(p.origin.tested)}` +
    (p.origin.age_days >= 14 ? `, ${p.origin.age_days} days ago.` : "."),
  own: (p) => ORIGIN_REASON_LINE[p.origin.why?.code]?.(p) ?? "Chosen by the service itself.",
};

/** The same line when the service changed the chain itself, by the reason it gives. */
const ORIGIN_REASON_LINE = {
  gone: (p) => `Changed by the service itself: OpenRouter no longer offers ${listWords(p.origin.why.models)}.`,
  first_answer: (p) => standInWords(p),
  test: (p) => `Changed by the service itself, by its test that ended on ${momentWords(p.origin.tested)}.`,
};

/** Models of the chain are gone: what the service has done about that so far, as the picture says it. */
function goneWords(p) {
  const gone = `OpenRouter no longer offers ${listWords(p.gone)}. Free models are withdrawn without notice.`;
  if (p.doing === "testing") return `${gone} A test of the free models is running now${p.test.by === "failure" ? ", to find a replacement" : ""}.`;
  if (p.doing) return `${gone} The service is looking into it now.`;
  // Only what the service did about these very models counts. A look or a test that
  // was about another model says nothing about them.
  const about = (models) => p.gone.every((model) => models.includes(model));
  const repair = p.last?.by === "failure" && p.last.outcome === "nothing_passed" && about(p.last.gone) ? p.last : null;
  const look = p.look?.reason?.code === "gone" && p.look.result !== "started" && about(p.look.reason.models) ? p.look : null;
  if (look && (!repair || look.at >= repair.finished)) {
    return `${gone} A run at ${clockWords(look.at)} found that out, and no test of replacements could start then.`;
  }
  if (repair) return `${gone} The service tested the other free models at ${clockWords(repair.started)}, and none passed.`;
  // A chain is never emptied: when every model is gone, none is taken out.
  const out = p.gone.length < p.chain.length ? `takes ${p.gone.length === 1 ? "it" : "them"} out of the chain and ` : "";
  return `${gone} The next run will find that out: the service then ${out}looks for a replacement. It tests nothing by itself before that.`;
}

/** The paragraph at the top of the section. An array: one sentence or more per entry. */
function originText(p) {
  const gone = p.gone.length ? goneWords(p) : null;
  if (p.origin.kind === "pinned") {
    return [
      "In use: a chain fixed by the admin of this demo. The service does not test or replace these models.",
      "The recorded test below shows what it does when they are not fixed.",
    ];
  }
  const text = [];
  if (p.origin.kind === "default") {
    text.push(`In use: the tested default, measured on ${dayWords(p.origin.tested)}.`);
    text.push(gone ?? `Since this service last started (${momentWords(p.awake_since)}) it has replaced nothing.`);
  } else if (p.origin.kind === "published") {
    const old = p.origin.age_days >= 14;
    text.push(
      `In use: the chain from this project's scheduled test of ${dayWords(p.origin.tested)}.` +
        (old ? ` That was ${p.origin.age_days} days ago, and no newer list has been published since, so this one is getting old.` : ""),
    );
    text.push(
      "That test is meant to run about once a week: it sends the seven sample documents to the free models " +
        "OpenRouter lists and publishes the chain that results. " +
        `This service read the published list at ${clockWords(p.origin.changed)}.` +
        (old ? " It still repairs its chain itself when a model stops answering." : ""),
    );
    if (gone) text.push(gone);
  } else {
    text.push(OWN_TEXT[p.origin.why?.code]?.(p) ?? "In use: a chain the service chose itself.");
    if (gone) text.push(gone);
  }
  if (p.mode === "no_key") text.push("This demo has no API key, so it cannot test models. Please contact the admin.");
  if (p.mode === "not_openrouter") text.push("This demo does not use OpenRouter's own address, so it has no list of free models to test.");
  return text;
}

/** Why a stand-in answers: what the repair that put it to use was started for. */
function standInFor(p) {
  const reason = p.origin.why.reason;
  const they = standIns(p).length === 1 ? "It stands" : "They stand";
  if (reason?.code === "gone") return ` ${they} in for ${listWords(reason.models)}, which OpenRouter no longer offers.`;
  if (reason?.code === "closed") return ` ${they} in because no model of the chain could be called.`;
  return "";
}

const OWN_TEXT = {
  gone: (p) =>
    `In use: the chain without ${listWords(p.origin.why.models)}, which OpenRouter no longer offers. ` +
    `The service took ${p.origin.why.models.length === 1 ? "it" : "them"} out at ${clockWords(p.origin.changed)}.`,
  first_answer: (p) => `In use for now: ${standInWords(p)}${standInFor(p)}`,
  test: (p) =>
    `In use: the chain as the service's own test left it when it ended on ${momentWords(p.origin.tested)}` +
    (p.origin.why.models.length
      ? `: ${listWords(p.origin.why.models)} ${p.origin.why.models.length === 1 ? "was" : "were"} added.`
      : "."),
};

/** Who started a test, for its headline. */
const STARTER_WORDS = {
  failure: "started by the service itself",
  page: "started by a visitor",
  schedule: "run on GitHub by this project's weekly job",
  recorded: "recorded when the default chain was measured (pass 1 of 3). Nothing is called now",
};

/** Why, when the service started it. Empty when the starter says it all. */
const TEST_REASON_WORDS = {
  gone: (t) => `because OpenRouter no longer offers ${listWords(t.reason.models)}`,
  closed: () => "because no model of the chain could be called",
  asked: () => "",
  schedule: () => "",
  recorded: () => "",
};

/** A model's verdict as a chip: the word, and the tone that picks its icon. */
const VERDICT_LABEL = {
  passed: { label: "Passed", tone: "good" },
  slow: { label: "Right, but slow", tone: "warn" },
  failed: { label: "Failed", tone: "crit" },
  not_judged: { label: "Not judged", tone: "muted" },
};

const errorWords = (row) =>
  Object.keys(row.errors).map((type) => ERROR_WORDS[type] ?? "No answer").join(", ") || "no answer";

/** Said only where it happened: a model is sent no further document once it can no longer pass. */
const endedEarly = (row, t) => (row.calls < t.cases.length ? " Two misses end a model's test." : "");

/** Said of a newcomer only: a model of the chain keeps its place, whatever a test says of its speed. */
const lastPlaceOnly = (row, t) => (t.chain_before.includes(row.model) ? "" : " Good for the last place only.");

/** The sentence beside the chip, by the reason the service gives. */
const REASON_WORDS = {
  all_right: (row, t) =>
    row.verdict === "slow"
      ? `${row.correct} of ${row.calls} right, but its usual answer took ${seconds(row.median_seconds)}, more than half of the ${t.timeout_seconds} s time limit.${lastPlaceOnly(row, t)}`
      : `${row.correct} of ${row.calls} right. Usual answer ${seconds(row.median_seconds)}.`,
  one_missed: (row, t) =>
    row.verdict === "slow"
      ? `${row.correct} of ${row.calls} right, and its usual answer took ${seconds(row.median_seconds)}, more than half of the ${t.timeout_seconds} s time limit.${lastPlaceOnly(row, t)}`
      : `${row.correct} of ${row.calls} right, one missed. Usual answer ${seconds(row.median_seconds)}.`,
  // All the service checks is whether that one answer was right. It does not know what the model did instead.
  hidden_instructions: () => "Its answer to the document with hidden instructions was wrong.",
  wrong_answers: (row, t) => {
    const wrong = row.answered - row.correct;
    const silent = row.calls - row.answered;
    return (
      `${wrong} of its ${plural(row.answered, "answer")} ${wrong === 1 ? "was" : "were"} wrong` +
      (silent ? `, and ${plural(silent, "call")} got no answer` : "") +
      `.${endedEarly(row, t)}`
    );
  },
  unusable_answers: (row, t) => `Its answers could not be used (${errorWords(row)}).${endedEarly(row, t)}`,
  no_answers: (row) =>
    `No answer to its ${plural(row.calls, "call")} (${errorWords(row)}).` +
    (row.placing === "dropped" ? "" : " That says something about the moment, not about the model."),
  unanswered: (row, t) => `${row.answered} of ${t.cases.length} documents answered; the rest got no answer. It is neither added nor dropped for that.`,
};

/**
 * What became of the model once the chain was decided. Follows the sentence above.
 * `would` is true for the recorded test, which was judged and never changed a chain.
 */
const PLACING_WORDS = {
  kept: (would) => (would ? "Would keep its place." : "Kept its place."),
  added: (would) => (would ? "Would be added to the chain." : "Added to the chain."),
  dropped: () => "Taken out: OpenRouter no longer offers it.",
  no_place: (would) => (would ? "Would find no free place in the chain." : "No free place in the chain."),
};

/** How a test ended: the tone of its mark, and the line under the board. */
const OUTCOME_WORDS = {
  changed: { tone: "good", words: (t) => `The chain is now: ${chainWords(t.chain)}.` },
  unchanged: { tone: "good", words: (t) => `The chain stays as it was: ${chainWords(t.chain)}.` },
  nothing_passed: { tone: "warn", words: (t) => `No model passed, so nothing was added. The chain is: ${chainWords(t.chain)}.` },
  key_refused: { tone: "crit", words: () => "The test was stopped at once: the service's API key was refused. Please contact the admin." },
  interrupted: { tone: "warn", words: () => "The test was cut short. Nothing was changed." },
};

/** What the service is doing in the background, when it is doing anything. */
const DOING_WORDS = {
  looking: "The service is looking into a call that a model of the chain did not answer.",
  starting: "A test is about to start: the service is reading OpenRouter's list of free models and what is left of today's allowance.",
  testing: "A test is running.",
};

/** The sentence beside the button, by what the service says about starting a test. */
const REFUSAL_WORDS = {
  pinned: () => "",
  no_key: () => "",
  not_openrouter: () => "",
  // "running" covers a look into a call that failed, the free questions before a test and
  // the test itself. The picture says which of the three it is.
  running: (b, rules, p) => DOING_WORDS[p.doing] ?? DOING_WORDS.testing,
  pause: (b) => `A test ran a short while ago. The next one can start at ${soonWords(b.again_at)}.`,
  list_unreadable: () => "OpenRouter's list of free models could not be read just now. Try again in a minute.",
  nothing_to_test: () => "OpenRouter lists no other free model to test right now.",
  allowance: (b, rules) => `Less than ${rules.button_share}% of today's free allowance is left, and the service starts no test on less. Possible again after midnight UTC.`,
  allowance_unknown: () => "The service cannot check its free allowance right now, so it starts no test. Try again in a minute.",
};

const READY_WORDS = (rules) =>
  `A test sends each model up to ${rules.documents} requests from this demo's free allowance, ${rules.requests_at_most} at most, ` +
  `and takes about three minutes. Tests are kept ${rules.pause_minutes} minutes apart, and the button works only while ` +
  `at least ${rules.button_share}% of today's allowance is left.`;

/** What a look into a failed run was about, as the start of a sentence. */
const lookSubject = (look) =>
  look.reason?.code === "gone"
    ? `OpenRouter no longer offers ${listWords(look.reason.models)}`
    : "No model of the chain could be called just now";

/** What the service did about a failed run, for the notice in the result panel. Null: say nothing. */
const LOOK_WORDS = {
  passing: () => null,
  started: () => null, // the test's own notice says it
  // Shown for as long as that chain is the one in use.
  published: (look, p) =>
    p.origin.kind !== "published"
      ? null
      : {
          tone: "good",
          title: "A tested chain was already waiting",
          text: ["A model of the chain did not answer a run, and the service found a newer tested chain in this project's published list. It is in use now."],
        },
  no_place: () => ({
    tone: "crit",
    title: "No model is answering",
    text: ["No model of the chain could be called just now, although OpenRouter still lists them, and the chain has no free place for another. Please contact the admin."],
  }),
  pinned: () => null,
  no_key: () => null,
  not_openrouter: () => null,
  running: () => null,
  pause: (look) => ({
    tone: "warn",
    title: "No replacement yet",
    text: [`${lookSubject(look)}. The service tested the free models a short while ago, so it waits before it tests again: possible from ${soonWords(look.again_at)}, after the next run that fails.`],
  }),
  list_unreadable: (look) => ({
    tone: "warn",
    title: "The service cannot look for a replacement right now",
    text: [`${lookSubject(look)}, and OpenRouter's list of free models could not be read to find others. Please try again in a few minutes.`],
  }),
  nothing_to_test: (look) => ({
    tone: "crit",
    title: "No replacement is on offer",
    text: [`${lookSubject(look)}, and OpenRouter lists no other free model that this service could use instead. Please contact the admin.`],
  }),
  allowance: (look) => ({
    tone: "warn",
    title: "No replacement today",
    text: [`${lookSubject(look)}, and too few free requests are left today to test replacements. The allowance starts again at midnight UTC.`],
  }),
  allowance_unknown: () => null,
};

/** Why a repair started, in the notice: what the service found, not a story about it. */
function foundWords(t) {
  if (t.reason.code === "gone") {
    return `OpenRouter no longer offers ${listWords(t.reason.models)}: free models are withdrawn without notice. A run at ${clockWords(t.started)} found out.`;
  }
  return `No model of the chain could be called at ${clockWords(t.started)}, although OpenRouter still lists ${t.reason.models.length === 1 ? "it" : "them"}.`;
}

/** The explanation the owner asked for: this happens because the page is not very active. */
const idleWords = (p) =>
  "By itself the service only tests its models when one of them does not answer a run, to spare its free allowance. This page has few visitors, " +
  "so a model can be withdrawn long before anyone notices: the first visitor after a quiet spell is the one who finds out. " +
  `(This service last started at ${clockWords(p.awake_since)}.)`;

/**
 * One line of the notice per tested model while a repair runs: what its first answer came
 * to, because the first right one puts a model to use. `since` asks for a running clock.
 */
function firstAnswerLine(model, t) {
  const name = shortModel(model);
  const mine = t.calls.filter((call) => call.model === model);
  const answer = mine.find((call) => call.correct != null);
  if (answer) {
    return { words: `${name}: ${answer.correct ? "right" : "answered, but not correctly"}, after ${seconds(answer.seconds)}` };
  }
  const flying = t.in_flight.find((entry) => entry.model === model);
  if (flying) return { words: `${name}: asking for `, since: flying.since };
  if (mine.length) return { words: `${name}: no answer (${ERROR_WORDS[mine.at(-1).error_type] ?? "no answer"})` };
  return { words: `${name}: waiting its turn` };
}

/** The notice in the result panel while a repair runs and for an hour after it. */
function repairNotice(p) {
  const t = p.test?.by === "failure" ? p.test : p.last?.by === "failure" ? p.last : null;
  if (!t) return null;
  const one = t.tested.length === 1;
  const others = plural(t.tested.length, "free model");
  // Why nobody noticed before, where a model was withdrawn. Nothing was withdrawn when
  // the chain could not be called although OpenRouter lists it.
  const start = t.reason.code === "gone" ? [foundWords(t), idleWords(p)] : [foundWords(t)];
  if (t.state === "running") {
    const lines = t.tested.map((model) => firstAnswerLine(model, t));
    if (!t.provisional.length) {
      const whom = one
        ? "the one free model OpenRouter lists that is not in the chain"
        : `the ${t.tested.length} free models OpenRouter lists that are not in the chain`;
      // A model that is still offered keeps answering first. A newcomer only joins behind it.
      const place = t.kept.length ? `joins the chain behind ${listWords(t.kept)} at once` : "answers from then on";
      // A stand-in can still come for as long as a model's first answer is still out.
      const hoping = t.models.some((row) => row.verdict == null && !row.answered);
      const then = hoping
        ? `${one ? "If its first answer is right, it" : "The first one whose first answer is right"} ${place}, until the test has decided. ` +
          `A model has ${t.timeout_seconds} seconds for each document.`
        : "No first answer was right, so no model answers in the meantime. The test goes on, and a model that passes it joins the chain when it ends.";
      return {
        tone: "warn",
        busy: true,
        title: "Finding a replacement",
        text: [...start, `It is now sending the seven sample documents to ${whom}. ${then}`],
        lines,
        link: "Watch the test",
      };
    }
    const single = t.provisional.length === 1;
    const behind = t.kept.length ? `, behind ${listWords(t.kept)}` : "";
    return {
      tone: "good",
      title: "A working model is in place",
      text: [
        ...start,
        `${listWords(t.provisional)} ${single ? "answers as a stand-in" : "answer as stand-ins"}${behind}: ` +
          `${single ? "its first answer to a sample document was right" : "each gave a right first answer to a sample document"}. ` +
          `That is a first choice, made from one document${single ? "" : " each"}. The test of all seven documents is still running and has the last word.`,
      ],
      lines,
      link: "Watch the test",
    };
  }
  if (t.outcome === "changed") {
    return {
      tone: "good",
      title: "The chain was repaired",
      text: [...start, `The service tested ${others} on the seven sample documents. ${outcomeWords(t, p).words}`],
      link: "See the test",
    };
  }
  const again = p.button.again_at ? ` The service can test again at ${soonWords(p.button.again_at)}, after the next run that fails.` : "";
  const standing = standIns(p);
  if (standing.length) {
    // Neither added nor dropped: no test has judged it, so it goes on answering.
    const single = standing.length === 1;
    const front = p.chain.filter((model) => !standing.includes(model));
    const why = ["key_refused", "interrupted"].includes(t.outcome)
      ? "The test did not get to its end."
      : "Calls went unanswered, so the test could not judge every model.";
    return {
      tone: "warn",
      title: "A stand-in is answering",
      text: [
        ...start,
        `${why} ${listWords(standing)} ${single ? "goes" : "go"} on answering as ${single ? "a stand-in" : "stand-ins"}` +
          `${front.length ? `, behind ${listWords(front)}` : ""} until a test decides.${again}`,
      ],
      link: "See the test",
    };
  }
  if (t.outcome !== "nothing_passed" && t.outcome !== "unchanged") {
    // Stopped or cut short: the line under the board says which.
    return { tone: OUTCOME_WORDS[t.outcome].tone, title: "The test did not finish", text: [...start, OUTCOME_WORDS[t.outcome].words(t)], link: "See the test" };
  }
  const none = one ? "The one free model that the service tested did not pass just now." : `None of the ${others} that the service tested passed just now.`;
  // A chain that could not be called at all is no chain to go on with.
  const working = t.reason.code === "closed" ? [] : p.chain.filter((model) => !p.gone.includes(model));
  if (working.length) {
    // The chain is shorter, and it works: nobody has to be contacted for that.
    return {
      tone: "warn",
      title: "No replacement passed the test",
      text: [...start, `${none} The chain goes on with: ${chainWords(working)}.${again}`],
      link: "See the test",
    };
  }
  return {
    tone: "crit",
    title: "No free model is answering",
    text: [...start, `${none} The service cannot extract invoices right now.${again} Please contact the admin.`],
    link: "See the test",
  };
}

/** The line under the board: how the test ended. `p` is the picture the test is shown in. */
function outcomeWords(t, p) {
  if (t.by === "recorded") {
    return { tone: "good", words: `Judged by the rules a test uses, starting from the default chain, this pass gives: ${chainWords(t.chain)}.` };
  }
  const { tone, words } = OUTCOME_WORDS[t.outcome];
  // A finished test stays on the page until a newer one takes its place. The chain it
  // left may have changed since: a model taken out, or a stand-in put to use.
  const since = t.chain.length && chainKey(t.chain) !== chainKey(p.chain) && !["key_refused", "interrupted"].includes(t.outcome);
  if (since) return { tone, words: `This test left the chain as: ${chainWords(t.chain)}. The chain in use now is: ${chainWords(p.chain)}.` };
  return { tone, words: words(t) };
}

/** The notice in the result panel: the newest thing the service did about a failed run, for an hour. */
function upkeepNotice(p) {
  const fresh = (stamp) => stamp && new Date(p.now) - new Date(stamp) < 60 * 60 * 1000;
  const t = p.test?.by === "failure" ? p.test : p.last?.by === "failure" ? p.last : null;
  const look = p.look && fresh(p.look.at) ? LOOK_WORDS[p.look.result](p.look, p) : null;
  if (look && (!t || (t.state === "done" && p.look.at >= t.finished))) return look;
  if (t && (t.state === "running" || fresh(t.finished))) return repairNotice(p);
  return look;
}

/** Said in the outcome of a run that the page sent again after a repair. */
const REPAIRED_AND_SENT_AGAIN =
  "No model answered this run at first. The service has put another model into its chain since, so this page sent the run again.";

/**
 * What the chain's upkeep adds to the outcome of a finished run, as sentences. They are
 * said in the result itself, because that is where a visitor who pressed Run is looking.
 */
function upkeepNotes(run) {
  const p = shownPicture();
  if (!p || p.mode !== "automatic" || run.phase !== "done") return [];
  const notes = [];
  if (upkeep.rerun?.run === run && !upkeep.lost) {
    // What the service does as a rule. Whether it is doing it at this moment the page
    // only learns from its next look, and the notice below then says so.
    notes.push("When no model answers a run, the service checks whether a model was withdrawn, and may repair its chain. If it does, this page sends the run again by itself.");
  } else if (failedForReal(run) && run.chainKey !== chainKey(p.chain)) {
    notes.push("The service has changed its model chain since this run. Run it again to use the new chain.");
  }
  // A stand-in may answer runs before its test is over, and then fail that test.
  const by = run.status === 200 ? run.body.model_used : null;
  const judged = by && !p.chain.includes(by) ? (p.test ?? p.last)?.models.find((row) => row.model === by) : null;
  if (judged?.verdict === "failed") {
    notes.push(`${shortModel(by)}, which answered this run, has failed the service's test since and no longer answers. Its answer here may be wrong.`);
  }
  return notes;
}

/** The headline of the test view. */
function testHeadline(t) {
  const who = STARTER_WORDS[t.by];
  const why = TEST_REASON_WORDS[t.reason.code](t);
  if (t.state === "running") {
    return {
      title: "Testing the free models",
      sub: `${plural(t.requests, "request")} so far, ${t.tested.length * t.cases.length} at most · since ${clockWords(t.started)}, ${who}${why ? ` ${why}` : ""}.`,
    };
  }
  const title = { failure: "The last test", page: "The last test", schedule: "The scheduled test", recorded: "A recorded test" }[t.by];
  return { title, sub: `${momentWords(t.started)}, ${who}${why ? ` ${why}` : ""}. ${plural(t.requests, "request")}.` };
}

/** One line under the headline: what OpenRouter listed, and what was tested. */
function listedWords(t) {
  let words = `OpenRouter listed ${plural(t.found.length, "free model")} that ${t.found.length === 1 ? "says it supports" : "say they support"} structured output. Listed is a claim; the calls below are the test.`;
  if (t.by === "failure") {
    const one = t.tested.length === 1;
    const verb = t.state === "running" ? (one ? "is being" : "are being") : one ? "was" : "were";
    words += ` Only the ${one ? "one" : t.tested.length} outside the chain ${verb} tested: a model in the chain keeps its place whatever a test says.`;
  }
  if (t.left_out.length) words += ` ${plural(t.left_out.length, "more model")} had no room this time and ${t.left_out.length === 1 ? "comes" : "come"} first at the next test.`;
  return words;
}

/** The chip and the sentences for one model's row. `p` is the picture the test is shown in. */
function rowWords(row, t, p) {
  const flying = t.in_flight.find((entry) => entry.model === row.model);
  // A stand-in answers real runs while its test goes on, and after a test that could not judge it.
  let standing = "";
  if (t.state === "running" && t.provisional.includes(row.model)) standing = " Answers as a stand-in meanwhile.";
  if (t === p.last && standIns(p).includes(row.model)) standing = " It goes on answering as a stand-in until a test decides.";
  if (!row.verdict) {
    // A test that broke off is over, and its models were never judged.
    if (t.state === "done") return { label: "Not judged", tone: "muted", words: "The test ended before this model could be judged.", standing };
    const words = row.calls || flying ? `${row.calls} of ${t.cases.length} so far` : "";
    // Before its verdict a row speaks in short notes, and this is one more of them.
    const note = standing && `${words ? " · " : ""}answers as a stand-in meanwhile`;
    return { label: row.calls || flying ? "Being tested" : "Waiting its turn", tone: "muted", words, asking: flying, standing: note };
  }
  const { label, tone } = VERDICT_LABEL[row.verdict];
  const placing = row.placing ? ` ${PLACING_WORDS[row.placing](t.by === "recorded")}` : "";
  if (t.outcome === "key_refused" || t.outcome === "interrupted" || !row.calls) {
    return { label, tone, words: `The test ended before this model could be judged.${placing}`, standing };
  }
  return { label, tone, words: REASON_WORDS[row.why](row, t) + placing, standing };
}

/** The legend under the board, with the service's own numbers. */
const legendWords = (rules, limit = rules.timeout_seconds) => [
  ["Each mark", "is one sample document sent to one model: a tick when every checked field was right, a cross when the answer was wrong, an exclamation mark when no answer came, a dash when it was not sent because the model's test had ended, and an empty circle while it is still to be sent."],
  ["Passed:", `all ${rules.documents} documents sent, at most ${rules.max_misses} missed, the document with hidden instructions answered right, and a usual answer (the median) within half of the ${limit} s time limit.`],
  ["Not judged:", "calls went unanswered, usually a rate limit. That says something about the moment, so a model is neither added nor dropped for it."],
  ["Repaired, never reshuffled:", `a model in the chain keeps its place, and leaves only when OpenRouter no longer offers it and it gives no answer. A free place (there are ${rules.places}) goes to a model that passed; one that is right but slow may take the last place.`],
  ["What this cannot tell:", `${rules.documents} documents can rule a model out. They cannot show how accurate a model is.`],
];

/** On a model of a chain that the service looks after itself, when OpenRouter no longer offers it. */
const WITHDRAWN_LABEL = "No longer offered by OpenRouter";

// What the page knows about the chain's upkeep, and what it is doing about it.
//
// The service is asked for the whole picture every time (GET /playground/models), never
// for a change to the answer before. So a tab opened in the middle of a test, a second
// tab and a reload all draw the same thing, and nothing here depends on a stream that a
// proxy might hold back.

const upkeep = {
  picture: null, // the service's last answer to GET /playground/models
  receivedAt: 0, // when it arrived, on this page's own clock
  lost: false, // the last look got no answer of the service's own: the picture may be out of date
  looking: false, // a look is under way
  again: false, // ... and another one was asked for meanwhile
  timer: null,
  activeUntil: 0, // idle looks stop this long after the visitor's last sign of life
  hotUntil: 0, // look often until then: something was just started from this tab
  starting: false, // the button was pressed and the service has not answered yet
  replay: null, // { record, shown, token } while a finished test is played again
  rerun: null, // { run, at }: a real run of the visitor's that no model answered, sent again if the chain gets a new model
  cleared: false, // the notice above the playground says that the chain changed (hidden again at the next run)
  withdrawn: false, // every model of the chain is known to be withdrawn: the top bar says so
  callsOpen: false, // the list of a test's requests is open: kept here, because the list is drawn again
  drawn: {}, // what each part was last drawn from: nothing is drawn again without a change
  sawRunning: null, // the id of the last test this page saw running
  announced: null, // the id of the last test whose end was read out
};

// The page asks for news only while something is happening: every 2 seconds while the
// service is busy, every 20 seconds for 10 minutes after the visitor last did something,
// never while the tab is hidden, and never otherwise. Every request keeps a sleeping host
// awake, so a tab that was left open must go quiet.
const LOOK_BUSY_MS = 2000; // while the service is looking into a run or testing
const LOOK_IDLE_MS = 20000; // otherwise, and only for LOOK_FOR_MS after the visitor last did something
const LOOK_FOR_MS = 600000; // 10 minutes: a tab left open must not keep a sleeping host awake
const RERUN_FOR_MS = 180000; // 3 minutes: how long a run that failed waits for a repaired chain

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const chainKey = (models) => models.join();

/**
 * The service's last picture, as far as the page can still vouch for it; null when it has
 * none. Once a look has gone unanswered nothing is shown as going on: a test that was
 * running then may be over, or forgotten by a service that has started again.
 */
function shownPicture() {
  const p = upkeep.picture;
  return p && upkeep.lost ? { ...p, busy: false, doing: null, test: null } : p;
}

/** The visitor did something: keep looking for a while, and often for `hotMs`. */
function touchUpkeep(hotMs = 0) {
  upkeep.activeUntil = Date.now() + LOOK_FOR_MS;
  upkeep.hotUntil = Math.max(upkeep.hotUntil, Date.now() + hotMs);
  if (hotMs || upkeep.timer == null) watchModels();
}

/** Ask the service where things stand, draw it, and arrange the next look. */
async function watchModels() {
  clearTimeout(upkeep.timer);
  upkeep.timer = null;
  if (upkeep.looking) {
    upkeep.again = true;
    return;
  }
  // A tab nobody is looking at asks for nothing. Nor does a page that is waiting for the
  // service after a load or a run that got no answer: that wait does the asking.
  if (document.hidden || waitingForService) return;
  upkeep.looking = true;
  const picture = await readJSON("/playground/models");
  if (!isPicture(picture)) {
    // Asleep, restarting or out of reach. Nothing may go on spinning or counting for a
    // test the page can no longer see, so the section is drawn again without it. Looking
    // does not end here: it goes on as it does while nothing is happening, for as long as
    // the visitor is around, and the first look that is answered puts the page right.
    upkeep.looking = false;
    upkeep.lost = true;
    renderUpkeep();
    lookLater();
    // The pill says so where the page had the service before, unless a wait has the pill.
    if (upkeep.picture && !waitingForService) showStatus(upkeep.timer == null ? "offline" : "waiting");
    return;
  }
  const before = upkeep.picture;
  if (before && before.awake_since !== picture.awake_since) {
    // Another start of the service remembers nothing of a run or a test made before it.
    upkeep.rerun = null;
    if (before.test && picture.last?.id !== before.test.id) {
      upkeep.cleared = true;
      showNotice("warn", `The service started again at ${clockWords(picture.awake_since)}. The test of the free models that was running before is lost, and it changed nothing.`);
    }
  }
  upkeep.picture = picture;
  upkeep.receivedAt = performance.now();
  upkeep.lost = false;
  try {
    await followChain();
    renderUpkeep();
    showStatus(); // an answer of the service's own: the pill says again what the service says of itself
    rerunIfRepaired();
  } finally {
    // Whatever went wrong while drawing, the page must not stop looking for good.
    upkeep.looking = false;
    lookLater();
  }
}

/** Arrange the next look: soon while something is going on, later while the visitor is around, else not at all. */
function lookLater() {
  const now = Date.now();
  // A page that has lost touch sees nothing going on, so it keeps to the slow looks.
  const hot = !upkeep.lost && (upkeep.picture.busy || upkeep.again || upkeep.rerun != null || now < upkeep.hotUntil);
  upkeep.again = false;
  if (hot || now < upkeep.activeUntil) upkeep.timer = setTimeout(watchModels, hot ? LOOK_BUSY_MS : LOOK_IDLE_MS);
}

/** The service changed its chain under the page: draw again what depends on it. */
async function followChain() {
  const mine = chainKey(state.config.chain.map((entry) => entry.model));
  // A run in progress finishes on the chain it started with.
  if (chainKey(upkeep.picture.chain) === mine || state.busy) return;
  const config = await readJSON("/playground/config");
  if (!isConfig(config)) return; // it could not be read just now: the next look tries again
  state.config = config;
  renderChrome();
  // Results already on the page stay: each run remembers the chain it ran on. What the
  // visitor typed stays too, as a document of the visitor's own when the new chain has
  // no such scenario. Only the failure controls start again, one per model of the chain.
  const scenario = scenarioById(state.selected);
  if (!scenario) state.selected = CUSTOM;
  state.draft.simulate = scenario && state.draft.text === scenario.text ? [...scenario.simulate] : state.config.chain.map(() => null);
  renderRequest();
  renderEverything();
  refreshHealth();
  if (state.batch) {
    // The scenarios still to come were written for the chain before. Stop here.
    state.batch.stop = true;
    upkeep.cleared = true;
    showNotice(
      "warn",
      "The service changed its model chain while the scenarios were running, so the rest were not run. " +
        (upkeep.picture.busy ? "It is still testing models: run all again when the test has finished." : "Run all again to use the new chain."),
    );
    return;
  }
  // During a repair the notice in the result panel tells the whole story already.
  if (state.history.length && !upkeep.rerun && !upkeepNotice(upkeep.picture)) {
    upkeep.cleared = true;
    showNotice("warn", "The service changed its model chain. Results already on this page keep the chain they ran on; the next run uses the new one.");
  }
}

/**
 * Send the remembered run again once the chain has a model that the run did not meet: a
 * chain that only lost a model has nobody new to ask. Once, only from this tab, and only
 * for a visitor who stayed with that run: one who went on to another document is not
 * handed an answer to the old one, and reads in its result that the chain has changed.
 */
function rerunIfRepaired() {
  const waiting = upkeep.rerun;
  if (!waiting || locked()) return;
  const { run } = waiting;
  const age = Date.now() - waiting.at;
  const met = run.chain.map((entry) => entry.model);
  if (upkeep.picture.chain.every((model) => met.includes(model))) {
    // Nobody new. Wait while the service is looking or testing, and not for ever.
    if (age > RERUN_FOR_MS || (!upkeep.picture.busy && age > 2 * LOOK_BUSY_MS + 1000)) upkeep.rerun = null;
    return;
  }
  upkeep.rerun = null;
  const stayed = state.runs.get(state.view) === run && state.draft.text === run.text && !state.draft.simulate.some(Boolean);
  if (!stayed) return;
  const scenario = run.scenario ? scenarioById(run.scenario.id) : null;
  setTimeout(() => {
    if (locked()) return;
    execute({ key: run.key, scenario, text: run.text, simulate: state.config.chain.map(() => null), again: true });
  }, 0);
}

// Starting a test, and playing a finished one again.

async function startModelsTest() {
  // The button stays where the keyboard can reach it while it cannot be used, so a press may come at any time.
  if (upkeep.starting || upkeep.lost || !upkeep.picture?.button.ready) return;
  stopReplay();
  upkeep.starting = true;
  renderUpkeep();
  const wait = patience(GIVE_UP_AFTER_MS);
  try {
    await fetch("/playground/models/test", { method: "POST", signal: wait.signal });
  } catch {
    // Nothing to add here: the picture says whether a test is running or why none
    // started, and a look that gets no answer says that the service is away.
  } finally {
    wait.done();
  }
  upkeep.starting = false;
  renderUpkeep();
  touchUpkeep(3 * LOOK_BUSY_MS); // started or refused, the service says which, to every tab
}

function stopReplay() {
  if (!upkeep.replay) return;
  upkeep.replay.token = null;
  upkeep.replay = null;
}

/** One frame of a replay: the record as it stood after its first `shown` calls. */
function replayFrame(record, shown) {
  const calls = record.calls.slice(0, shown);
  const next = record.calls[shown];
  return {
    ...record,
    state: "running",
    outcome: null,
    calls,
    requests: calls.length,
    in_flight: next ? [{ model: next.model, case: next.case, since: null }] : [],
    models: record.models.map((row) => {
      const mine = calls.filter((call) => call.model === row.model);
      if (mine.length >= row.calls) return { ...row, placing: null };
      return { ...row, calls: mine.length, correct: mine.filter((call) => call.correct === true).length, verdict: null, why: null, placing: null };
    }),
  };
}

/** Play the finished test again from its record. Nothing is called: it costs the service nothing. */
async function replayModelsTest() {
  if (upkeep.replay) {
    stopReplay();
    renderUpkeep();
    return;
  }
  const record = upkeep.picture?.last;
  if (!record || upkeep.picture.test) return;
  const replay = { record, shown: 0, token: {} };
  const token = replay.token;
  upkeep.replay = replay;
  touchUpkeep();
  for (let shown = 0; shown <= record.calls.length; shown++) {
    replay.shown = shown;
    renderUpkeep();
    const next = record.calls[shown];
    if (!next) break;
    // A call's wait is a fraction of what it took, so three minutes play in about half of one.
    await pause(Math.min(1400, Math.max(160, (next.seconds ?? 1) * 110)));
    if (replay.token !== token) return; // stopped, or a real test started
  }
  if (upkeep.replay === replay) upkeep.replay = null;
  renderUpkeep();
}

// Drawing

/**
 * True when `value` is not what `part` was last drawn from. The service is asked every
 * two seconds while it is busy; drawing the same thing again each time would close an
 * open list, take the focus away and make a screen reader repeat itself.
 */
function changed(part, value) {
  const text = JSON.stringify(value) ?? "";
  if (upkeep.drawn[part] === text) return false;
  upkeep.drawn[part] = text;
  return true;
}

/**
 * Draw `blocks` into `root` and leave the keyboard where it was. During a test the board
 * is drawn again every few seconds, and a visitor who tabbed into it must not be thrown
 * back to the top of the page each time.
 */
function redraw(root, blocks) {
  const stops = "button, summary, a[href], [tabindex]";
  const at = [...root.querySelectorAll(stops)].indexOf(document.activeElement);
  root.replaceChildren(...blocks);
  if (at !== -1) root.querySelectorAll(stops)[at]?.focus();
}

/** A clock that counts on from a moment the service named: its own "now" says how long ago that was. */
function clockSince(stamp) {
  const startedAt = upkeep.receivedAt - (new Date(upkeep.picture.now) - new Date(stamp));
  return h("span", { dataset: { since: startedAt } }, ((performance.now() - startedAt) / 1000).toFixed(1));
}

const CHIP_TONE = { good: "good", warn: "warn", crit: "crit", muted: "", neutral: "" };

/** What one call came to, in the words of the legend. */
function callWords(call) {
  if (call.correct == null) return `no answer (${ERROR_WORDS[call.error_type] ?? "the call failed"})`;
  const did = call.outcome === "refused" ? "said “not an invoice”" : "answered";
  return `${did}, and that was ${call.correct ? "right" : "wrong"}`;
}

/** One sample document in one model's test, in words: what its mark stands for. */
function markWords(item, row, t) {
  const call = t.calls.find((entry) => entry.model === row.model && entry.case === item.id);
  if (call) return callWords(call);
  const flying = t.in_flight.find((entry) => entry.model === row.model && entry.case === item.id);
  // A replay has no moment for the call it is about to show.
  if (flying) return flying.since ? "being asked now" : "being played now";
  return row.verdict != null ? "not sent, this model's test had ended" : "not sent yet";
}

/** One mark per sample document: a tick, a cross, an exclamation mark, or what it is waiting for. */
function documentMarks(row, t) {
  return t.cases.map((item) => {
    const call = t.calls.find((entry) => entry.model === row.model && entry.case === item.id);
    let mark;
    if (call) {
      mark = badge(call.correct === true ? "good" : call.correct === false ? "crit" : "warn");
      setTip(mark, item.title, [`The model ${callWords(call)}`, `after ${seconds(call.seconds)}`]);
    } else if (t.in_flight.some((entry) => entry.model === row.model && entry.case === item.id)) {
      mark = h("span", { class: "mark-asking" }, h("span", { class: "spinner" }));
    } else {
      mark = row.verdict != null ? badge("muted") : h("span", { class: "badge badge--idle" });
    }
    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", `${item.title}: ${markWords(item, row, t)}`);
    return mark;
  });
}

function upkeepRow(row, t, p) {
  const look = rowWords(row, t, p);
  const { vendor, name } = modelParts(row.model);
  // Where the model stood when the test began, and said as that: the chain may have
  // changed since, and changes while a test runs when a stand-in is put to use.
  const place = t.chain_before.indexOf(row.model);
  const chain = t.by === "recorded" ? "the default chain" : "the chain";
  const role =
    (place === -1 ? `Not in ${chain}` : `${roleLabel(place, t.chain_before)} in ${chain}`) +
    (t.by === "recorded" ? "" : " before this test");
  // The service's clock says when the call started; this page's clock counts on from there.
  const asking = look.asking?.since ? h("span", { class: "upkeep__asking" }, " · asking for ", clockSince(look.asking.since), " s") : null;
  // One stop for the keyboard per row, not one per mark: its tip says in words what the marks show.
  const marks = h(
    "div",
    { class: "marks", role: "group", tabindex: "0", "aria-label": `${name}: the ${t.cases.length} sample documents` },
    documentMarks(row, t),
  );
  setTip(marks, name, t.cases.map((item) => `${item.title}: ${markWords(item, row, t)}`));
  return h(
    "li",
    { class: "upkeep__row" },
    h("div", { class: "table__model" }, name, h("small", null, [role, vendor].filter(Boolean).join(" · "))),
    marks,
    h(
      "div",
      { class: "upkeep__verdict" },
      chip(look.label, { tone: CHIP_TONE[look.tone], iconName: TONE_ICON[look.tone] }),
      h("span", null, look.words, asking, look.standing),
    ),
  );
}

/**
 * What each request of a test came to, in the order the answers came: the same facts as
 * the marks, as a list. "Results", because a test that was stopped has sent more
 * requests than it has results, and the headline counts the requests.
 */
function callList(t) {
  const titles = new Map(t.cases.map((item) => [item.id, item.title]));
  return h(
    "details",
    {
      class: "upkeep__calls",
      open: upkeep.callsOpen,
      ontoggle: (event) => {
        upkeep.callsOpen = event.target.open;
      },
    },
    h("summary", null, `List the ${plural(t.calls.length, "result")} one by one`),
    h(
      "ol",
      { class: "testlist" },
      t.calls.map((call) =>
        h(
          "li",
          null,
          badge(call.correct === true ? "good" : call.correct === false ? "crit" : "warn"),
          h("span", null, `${shortModel(call.model)} · ${titles.get(call.case) ?? call.case}: ${callWords(call)}`),
          h("time", null, seconds(call.seconds)),
        ),
      ),
    ),
  );
}

function renderUpkeepTest(p) {
  const root = $("#upkeep-test");
  const real = p.test ?? p.last;
  if (p.test) stopReplay(); // a real test has the board
  const replaying = upkeep.replay != null;
  const t = replaying ? replayFrame(upkeep.replay.record, upkeep.replay.shown) : real;
  // The chain in use and its stand-ins are part of what the rows and the last line say.
  if (!changed("test", [t, replaying, p.test != null, p.chain, standIns(p)])) return;
  if (!t) {
    root.replaceChildren(h("p", { class: "upkeep__line" }, "No test has run here yet, and this demo has no recorded one to show."));
    return;
  }
  const running = t.state === "running";
  const head = testHeadline(t);
  const end = running ? null : outcomeWords(t, p);
  const blocks = [
    h(
      "div",
      { class: "suite__summary" },
      running ? h("span", { class: "spinner" }) : badge(end.tone, "badge--lg"),
      h("h3", { class: "suite__headline" }, replaying ? `Replaying the test of ${dayWords(t.started)}` : head.title),
      h("p", null, replaying ? `Nothing is called now: these are its calls as they were recorded. ${plural(t.requests, "request")} of ${real.requests} so far.` : head.sub),
    ),
    h("p", { class: "upkeep__line" }, listedWords(replaying ? real : t)),
    h(
      "div",
      { class: "upkeep__head", "aria-hidden": "true" },
      h("span", null, "Model"),
      h("span", null, `The ${t.cases.length} sample documents`),
      h("span", null, "What the test showed"),
    ),
    h("ul", { class: "upkeep__rows" }, t.models.map((row) => upkeepRow(row, t, p))),
  ];
  if (end) blocks.push(h("p", { class: `upkeep__result upkeep__result--${end.tone}` }, badge(end.tone), h("span", null, end.words)));
  const actions = [];
  if (!p.test && real.calls.length) {
    actions.push(
      h(
        "button",
        { class: "btn btn--ghost btn--sm", type: "button", id: "upkeep-replay", onclick: replayModelsTest },
        icon(replaying ? "stop" : "play"),
        replaying ? " Stop the replay" : " Replay this test",
      ),
    );
  }
  if (t.calls.length) actions.push(callList(t));
  if (actions.length) blocks.push(h("div", { class: "upkeep__actions" }, actions));
  redraw(root, blocks);

  // Read out the end of a test this page saw running, once.
  if (p.test) upkeep.sawRunning = p.test.id;
  else if (real.id === upkeep.sawRunning && upkeep.announced !== real.id) {
    upkeep.announced = real.id;
    announce(`Model test finished. ${outcomeWords(real, p).words}`);
  }
}

/** What the service is doing about a run that failed, in the result panel, under the run's own outcome. */
function renderUpkeepNotice(p) {
  const root = $("#upkeep-notice");
  const notice = upkeepNotice(p);
  if (!changed("notice", notice)) return;
  if (!notice) {
    root.hidden = true;
    delete upkeep.drawn.said;
    return;
  }
  root.className = `upnote upnote--${notice.tone}`;
  const line = ({ words, since }) => h("li", null, words, since ? [clockSince(since), " s"] : null);
  redraw(
    root,
    [
      h("h4", { class: "upnote__title" }, notice.busy ? h("span", { class: "spinner" }) : badge(notice.tone), notice.title),
      ...notice.text.map((words) => h("p", null, words)),
      notice.lines?.length ? h("ul", { class: "upnote__list" }, notice.lines.map(line)) : null,
      notice.link ? h("p", null, h("a", { href: "#upkeep-test" }, notice.link)) : null,
    ].filter(Boolean),
  );
  root.hidden = false;
  // Read out once per state, by its title and its last paragraph. The notice is no live
  // region of its own: every new line of it would be read again in full.
  if (changed("said", notice.title)) announce(`${notice.title}. ${notice.text.at(-1)}`);
}

function renderUpkeep() {
  const origin = $("#upkeep-origin");
  const p = shownPicture();
  if (!p) {
    // The page was drawn without this part. The next look asks for it again.
    origin.replaceChildren(h("p", null, "This part of the page has not loaded: the demo did not answer for it just now."));
    return;
  }
  // The chain card is drawn from the configuration. While a run in progress holds an
  // older chain there, the card waits: its names and its line must be of one chain.
  const mine = chainKey(state.config.chain.map((entry) => entry.model));
  if (mine === chainKey(p.chain) && changed("chain", [mine, p.origin, p.gone, p.mode, p.doing, p.look, p.test?.id, p.last?.id])) renderChain();
  const text = originText(p);
  if (upkeep.lost) text.unshift(`The demo did not answer when this page last asked. What follows is what the page last saw, at ${clockWords(p.now)}, and may be out of date.`);
  if (changed("origin", text)) origin.replaceChildren(...text.map((line) => h("p", null, line)));

  // The button is never switched off with "disabled": that would throw the keyboard off
  // it at the very moment its sentence, which is read out, says why it does not work.
  const button = $("#upkeep-start");
  $("#upkeep-control").hidden = !p.button.offered;
  button.setAttribute("aria-disabled", String(!p.button.ready || upkeep.starting || upkeep.lost));
  const label = p.test ? "Testing" : "Test the free models now";
  if ($("span", button).textContent !== label) $("span", button).textContent = label;
  let words = "";
  if (upkeep.lost) words = "The demo did not answer when this page last asked, so no test can be started from here just now.";
  else if (upkeep.starting) words = DOING_WORDS.starting;
  else if (p.button.offered) words = p.button.ready ? READY_WORDS(p.rules) : REFUSAL_WORDS[p.button.reason](p.button, p.rules, p);
  const note = $("#upkeep-note");
  if (note.textContent !== words) note.textContent = words;

  // The readiness route only says that a key is set. When every model of the chain is
  // known to be withdrawn, "Operational" in the top bar would not be true: see showStatus.
  upkeep.withdrawn = p.mode === "automatic" && p.chain.length > 0 && p.chain.every((model) => p.gone.includes(model));

  renderUpkeepNotice(p);
  renderUpkeepTest(p);
  // The legend speaks about the test on screen, so it uses that test's own time limit.
  const limit = (p.test ?? p.last)?.timeout_seconds ?? p.rules.timeout_seconds;
  const legend = legendWords(p.rules, limit);
  if (changed("legend", legend)) $("#upkeep-legend").replaceChildren(...legend.map(([term, said]) => h("li", null, h("strong", null, term), " ", said)));
  // What the outcome of the run on view says about the upkeep is drawn again when it
  // changes, and nothing else of the result.
  const run = state.runs.get(state.view);
  if (run && changed("outcome", upkeepNotes(run))) renderOutcome();
}

function wireUpkeep() {
  $("#upkeep-start").addEventListener("click", startModelsTest);
  // Looking stops when nobody has done anything for a while, and starts again with the visitor.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) touchUpkeep();
  });
  for (const sign of ["pointerdown", "keydown"]) {
    document.addEventListener(sign, () => {
      if (upkeep.timer == null && !upkeep.looking) touchUpkeep();
      else upkeep.activeUntil = Date.now() + LOOK_FOR_MS;
    }, { passive: true });
  }
  renderUpkeep();
  showStatus();
  // Opening the page is the visitor's first sign of life.
  upkeep.activeUntil = Date.now() + LOOK_FOR_MS;
  // The picture has just been read with the rest of the page, so the first look is the
  // one after it. A page that was drawn without the picture asks for it again now.
  if (upkeep.picture) lookLater();
  else watchModels();
}

// ------------------------------------------------------------ benchmark ---

// A dated record that ships with the service. Nothing here is measured when the page loads.

/** Seconds to one decimal, so "3.0 s" lines up with "22.1 s". */
const seconds = (value) => `${value.toFixed(1)} s`;

/** One model's row of the snapshot in a few words, for the chain card. */
function snapshotLine(row) {
  if (!row.answered) return `benchmark: no answer in ${plural(row.calls, "call")}`;
  return `benchmark: ${row.correct} of ${row.answered} correct · median ${seconds(row.median_seconds)}`;
}

/**
 * How long a model took to answer: the median as a bar against the time limit the
 * benchmark ran with, and a tick at the slowest answer. Both are also written out.
 */
function answerTime(row, limit, inChain) {
  if (row.median_seconds == null) {
    return h("div", { class: "cellbar cellbar--range" }, h("div", { class: "cellbar__track" }), h("span", { class: "cellbar__value" }, "–"));
  }
  const share = (seconds) => `${Math.min(100, (seconds / limit) * 100)}%`;
  // The chain's models are the point; the rest are context, so they recede.
  const bar = h("div", { class: `bar ${inChain ? "bar--accent" : "bar--muted"}` });
  bar.style.width = share(row.median_seconds);
  const tick = h("div", { class: "cellbar__tick" });
  tick.style.left = share(row.slowest_seconds);
  const track = h("div", { class: "cellbar__track" }, bar, tick);
  setTip(track, `${seconds(row.median_seconds)} median`, [
    row.model,
    `slowest answer ${seconds(row.slowest_seconds)}`,
    `the track ends at the ${limit} s time limit`,
  ]);
  return h(
    "div",
    { class: "cellbar cellbar--range" },
    track,
    h("span", { class: "cellbar__value" }, seconds(row.median_seconds), h("small", null, `slowest ${seconds(row.slowest_seconds)}`)),
  );
}

function renderBenchmark() {
  const { rows, measured, window_utc: hours, passes, cases, timeout_seconds: limit, records } = state.config.benchmark;
  const chain = state.config.chain;

  const body = rows.map((row) => {
    const position = chain.findIndex((entry) => entry.model === row.model);
    const inChain = position !== -1;
    const { vendor, name } = modelParts(row.model);
    const about = [inChain ? `${roleLabel(position)} in this chain` : vendor, row.note].filter(Boolean).join(" · ");
    return h(
      "tr",
      { class: inChain ? null : "is-context" },
      // A row header, so each figure is read out with the model it belongs to.
      h("th", { scope: "row" }, h("div", { class: "table__model" }, name, h("small", null, about))),
      h("td", { class: "n" }, `${row.no_answer} of ${row.calls}`),
      h("td", { class: "n" }, row.answered ? `${row.correct} of ${row.answered}` : "–"),
      h("td", null, answerTime(row, limit, inChain)),
    );
  });

  // Outside the table on purpose: the table scrolls sideways on a phone, and a caption would scroll with it.
  $("#benchmark-title").textContent =
    `Measured ${longDate(measured)}, ${hours} UTC: ${passes} ${passes === 1 ? "pass" : "passes"} of ${plural(cases, "document")}`;

  $("#benchmark").replaceChildren(
    h(
      "thead",
      null,
      h(
        "tr",
        null,
        h("th", { scope: "col" }, "Model"),
        h("th", { scope: "col", class: "n" }, "No answer"),
        h("th", { scope: "col", class: "n" }, "Fully correct"),
        h("th", { scope: "col", class: "n" }, `Time to answer, of the ${limit} s limit`),
      ),
    ),
    h("tbody", null, body),
  );
  const term = (name, ...words) => h("li", null, h("strong", null, name), " ", words);
  $("#benchmark-note").replaceChildren(
    term("No answer:", "the call ended without one. A rate limit, a timeout, a rejected format or cut-off output."),
    term("Fully correct:", "of the calls that answered, those with every checked field right, or the meeting email refused."),
    term("Time to answer:", "the bar is the median, the tick the slowest answer, and the track ends at the time limit."),
    term(
      "Order of the default chain:",
      "a model that rejects the format, or whose usual answer takes more than half of the time limit, is left out. " +
        "Of the rest, fewest wrong answers first, then fewest calls with no answer. " +
        "A fallback replaces a failed call, never a wrong answer.",
    ),
    term("Limits:", `${plural(cases, "document")} show whether a model can do the job. They do not measure accuracy.`),
    term(
      "Records:",
      records.map((file, index) => [index ? ", " : null, h("code", null, file)]),
      ", written by ",
      h("code", null, "python -m app.playground.benchmark"),
      ".",
    ),
  );
}

// -------------------------------------------------------------- tooltip ---

const tips = new WeakMap();

/** Attach hover/focus details to a mark. They repeat what the row already says in text. */
function setTip(element, value, lines) {
  tips.set(element, { value, lines });
  element.dataset.tip = "";
}

function wireTooltip() {
  const tooltip = $("#tooltip");
  const hide = () => {
    tooltip.hidden = true;
  };
  const show = (target) => {
    const tip = tips.get(target);
    if (!tip) return hide();
    // Spread on purpose: replaceChildren turns an array argument into its text.
    tooltip.replaceChildren(h("strong", null, tip.value), ...tip.lines.map((line) => h("span", null, line)));
    tooltip.hidden = false;
    const box = target.getBoundingClientRect();
    const size = tooltip.getBoundingClientRect();
    const left = Math.min(Math.max(8, box.left + box.width / 2 - size.width / 2), innerWidth - size.width - 8);
    const above = box.top - size.height - 8;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${above >= 8 ? above : box.bottom + 8}px`;
  };

  document.addEventListener("pointerover", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (target) show(target);
    else hide();
  });
  document.addEventListener("focusin", (event) => {
    if (event.target instanceof Element && event.target.matches("[data-tip]")) show(event.target);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, { capture: true, passive: true });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
}

boot();
