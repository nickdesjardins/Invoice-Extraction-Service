// Runs the page's script (app/playground/static/assets/app.js) without a browser, to hold
// it to what it DOES: which requests it sends and when, when it stops asking, what it
// decides. tests/test_page_behaviour.py starts it and reads its verdicts:
//
//     node tests/page_harness.mjs <app.js> <fixtures.json>
//
// The script is loaded as it is; one line of exports is added to a temporary copy. It
// gets three stand-ins: a page that swallows what is drawn on it and remembers the words,
// a clock and timers that only move when a case moves them, and a fetch that answers as
// the case says. The answers themselves are the real service's: the test asks the
// service for them and writes them into the fixtures file.
//
// Each case gets a page of its own, freshly loaded. Prints one JSON object:
// { "cases": [{ "name": ..., "ok": true | false, "why": ... }] }.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [scriptPath, fixturesPath] = process.argv.slice(2);
const fx = JSON.parse(readFileSync(fixturesPath, "utf8"));
const EXPORTS =
  "\nexport { state, upkeep, execute, runAll, select, startModelsTest, outcomeOf, visitorWords, shownPicture, upkeepNotes };\n";
const folder = mkdtempSync(join(tmpdir(), "page-harness-"));
const moduleFile = join(folder, "app-under-test.mjs");
writeFileSync(moduleFile, readFileSync(scriptPath, "utf8") + EXPORTS);

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const RUNAWAY = 3000; // no case needs more than about a hundred requests
const realImmediate = setImmediate;
const realTimeout = setTimeout;
const realClearTimeout = clearTimeout;
const turn = () => new Promise((resolve) => realImmediate(resolve));
const settle = async () => {
  for (let i = 0; i < 6; i += 1) await turn();
};

// ------------------------------------------------------- a clock moved by hand ---

function makeClock() {
  let now = 10_000;
  let ids = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms = 0, ...args) {
      ids += 1;
      timers.set(ids, { at: now + Math.max(0, Number(ms) || 0), fn, args, every: null });
      return ids;
    },
    setInterval(fn, ms, ...args) {
      ids += 1;
      timers.set(ids, { at: now + ms, fn, args, every: ms });
      return ids;
    },
    clear: (id) => timers.delete(id),
    /** Let `ms` pass: every timer that falls due runs, in order, and what it started gets time to finish. */
    async advance(ms) {
      const end = now + ms;
      let quiet = false; // the timer that just fired only redrew the page's clocks
      for (;;) {
        if (!quiet) await settle();
        let next = null;
        for (const [id, timer] of timers) {
          if (timer.at > end) continue;
          if (!next || timer.at < next.timer.at || (timer.at === next.timer.at && id < next.id)) next = { id, timer };
        }
        if (!next) break;
        now = Math.max(now, next.timer.at);
        if (next.timer.every == null) timers.delete(next.id);
        else next.timer.at += next.timer.every;
        // The page redraws its clocks ten times a second. That starts nothing that has to
        // be waited for, and waiting after each of them is most of the harness's time.
        quiet = next.timer.every === 100;
        next.timer.fn(...next.timer.args);
      }
      now = end;
      await settle();
    },
  };
}

// ------------------------------------------- a page that swallows what is drawn ---

const IS_NODE = Symbol("an element of the stand-in page");

function makePage() {
  const known = new Map();
  const words = []; // every piece of text the script put on the page, in order
  const element = (name) => {
    const store = { listeners: {}, dataset: {}, style: {}, attributes: {} };
    return new Proxy(function () {}, {
      get(_, key) {
        if (key === IS_NODE) return true;
        if (key === Symbol.toPrimitive) return () => `[${name}]`;
        if (typeof key === "symbol" || key === "then") return undefined;
        if (key === "store") return store;
        if (key === "addEventListener") return (type, fn) => void (store.listeners[type] ??= []).push(fn);
        if (key === "setAttribute") return (attribute, value) => void (store.attributes[attribute] = String(value));
        if (key === "getAttribute") return (attribute) => store.attributes[attribute] ?? null;
        if (key === "querySelector") return (selector) => lookup(`${name} ${selector}`);
        if (key === "querySelectorAll") return () => [];
        if (key === "closest" || key === "matches" || key === "contains") return () => null;
        if (key === "getBoundingClientRect") return () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 });
        if (key in store) return store[key];
        return (store[key] = element(`${name}.${key}`));
      },
      set(_, key, value) {
        if (key === "textContent") words.push(String(value));
        store[key] = value;
        return true;
      },
      apply: () => undefined, // any method: nothing happens, nothing comes back
    });
  };
  const lookup = (selector) => {
    if (!known.has(selector)) known.set(selector, element(selector));
    return known.get(selector);
  };
  const listeners = {};
  const document = {
    hidden: false,
    activeElement: null,
    querySelector: lookup,
    querySelectorAll: () => [],
    createElement: (tag) => element(`<${tag}>`),
    createElementNS: (_, tag) => element(`<${tag}>`),
    createTextNode: (text) => {
      words.push(String(text));
      return element("text");
    },
    addEventListener: (type, fn) => void (listeners[type] ??= []).push(fn),
    documentElement: element("html"),
    body: element("body"),
  };
  return {
    document,
    lookup,
    words,
    /** What the visitor does to the page as a whole: a press, a key, coming back to the tab. */
    fire: (type) => listeners[type]?.forEach((fn) => fn({ target: document.body })),
  };
}

// ---------------------------------------------- the answers a case can give ---

const headersOf = (map) => ({ get: (name) => map[name.toLowerCase()] ?? null });
const asJSON = (status, body) => ({ status, ok: status < 400, headers: headersOf({ "content-type": "application/json" }), json: async () => body });
/** A page of a host's own, where the service's answer should be. */
const asHTML = (status) => ({
  status,
  ok: status < 400,
  headers: headersOf({ "content-type": "text/html; charset=utf-8" }),
  json: async () => {
    throw new SyntaxError("Unexpected token '<'");
  },
});
const aborted = () => Object.assign(new Error("aborted"), { name: "AbortError" });
/**
 * The service's own stream. `every` ms pass before each event; after `stallAfter` events
 * nothing more comes and the stream does not close.
 */
const asStream = (events, { every = 0, stallAfter = Infinity } = {}) => (signal) => ({
  status: 200,
  ok: true,
  headers: headersOf({ "content-type": "application/x-ndjson", "x-request-id": "req-harness" }),
  body: {
    getReader() {
      let given = 0;
      return {
        read: () =>
          new Promise((resolve, reject) => {
            signal?.addEventListener("abort", () => reject(aborted()), { once: true });
            if (given >= events.length) return resolve({ done: true, value: undefined });
            if (given >= stallAfter) return undefined;
            const line = () => resolve({ done: false, value: new TextEncoder().encode(`${JSON.stringify(events[given++])}\n`) });
            return every ? setTimeout(line, every) : line();
          }),
      };
    },
  },
});
/** A run that the ceiling on live calls refuses, as the service's stream carries it. */
const refusal = (seconds) => {
  const result = structuredClone(fx.streams.refused.at(-1));
  result.headers["retry-after"] = String(seconds);
  return asStream([fx.streams.refused[0], result]);
};
/** The service is up: its three answers for the page, from `world`, and `more` for everything else. */
const up = (world, more = () => null) => (url, request, sent) => {
  if (url === "/health") return asJSON(200, world.health);
  if (url === "/playground/config") return asJSON(200, world.config);
  if (url === "/playground/models") return asJSON(200, world.picture);
  return more(url, request, sent);
};
const idleWorld = () => structuredClone({ health: fx.health, config: fx.idle.config, picture: fx.idle.picture });
/** The same world after the service put one more model into its chain. */
function withNewModel(world, model = "vendor/new-model:free") {
  const next = structuredClone(world);
  next.picture.chain.push(model);
  next.config.chain.push({ ...next.config.chain.at(-1), index: next.config.chain.length, model, benchmark: null, listing: null });
  return next;
}

// ---------------------------------------------------- one case = one fresh page ---

let serial = 0;
let thrown = []; // what the script threw during the case that is running

async function page(answer) {
  const clock = makeClock();
  const dom = makePage();
  const sent = [];
  globalThis.document = dom.document;
  globalThis.Node = class Node {
    static [Symbol.hasInstance](value) {
      return typeof value === "function" && value[IS_NODE] === true;
    }
  };
  globalThis.Element = globalThis.Node;
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.innerHeight = 800;
  Object.defineProperty(globalThis, "navigator", { value: { onLine: true }, configurable: true, writable: true });
  Object.defineProperty(globalThis, "performance", { value: { now: clock.now }, configurable: true });
  Date.now = () => 1_800_000_000_000 + clock.now();
  globalThis.setTimeout = clock.setTimeout;
  globalThis.setInterval = clock.setInterval;
  globalThis.clearTimeout = clock.clear;
  globalThis.clearInterval = clock.clear;
  const behaviour = { answer };
  globalThis.fetch = (url, options = {}) => {
    const request = { at: clock.now(), method: options.method ?? "GET", path: url, body: options.body };
    sent.push(request);
    if (sent.length > RUNAWAY) giveUp(`more than ${RUNAWAY} requests in one case: the page asks without end (${request.method} ${url})`);
    return new Promise((resolve, reject) => {
      if (options.signal?.aborted) return reject(aborted());
      options.signal?.addEventListener("abort", () => reject(aborted()), { once: true });
      turn()
        .then(() => behaviour.answer(url, request, sent))
        .then((response) => {
          if (response === "held") return; // kept, and never answered
          if (response === "refused" || response == null) reject(new TypeError("Failed to fetch"));
          else resolve(typeof response === "function" ? response(options.signal) : response);
        }, reject);
    });
  };
  serial += 1;
  const script = await import(`${pathToFileURL(moduleFile).href}?page=${serial}`);
  await clock.advance(0);
  const count = (method, path, from = 0) => sent.filter((request) => request.method === method && request.path === path && request.at >= from).length;
  const pill = () => dom.lookup("#status").store.dataset.state;
  const announced = () => dom.lookup("#announcer").store.textContent;
  return { script, clock, dom, sent, count, behaviour, pill, announced };
}

const cases = [];
let current = "(loading)";
function finish() {
  rmSync(folder, { recursive: true, force: true });
  console.log(JSON.stringify({ cases }, null, 1));
  process.exit(0);
}
// A page that asks without end, or a case that never finishes, cannot be stopped from
// outside: the harness reports the case and ends there.
function giveUp(why) {
  cases.push({ name: current, ok: false, why });
  finish();
}
// In a browser an error nobody catches is a dead part of the page. Here it fails the case.
process.on("unhandledRejection", (reason) => thrown.push(String(reason?.stack ?? reason).slice(0, 400)));
process.on("uncaughtException", (error) => thrown.push(String(error?.stack ?? error).slice(0, 400)));

async function check(name, body) {
  current = name;
  thrown = [];
  const watchdog = realTimeout(() => giveUp("the case did not finish in 30 s of real time"), 30_000);
  try {
    await body();
    await settle();
    if (thrown.length) throw new Error(`the script threw: ${thrown[0]}`);
    cases.push({ name, ok: true, why: null });
  } catch (error) {
    cases.push({ name, ok: false, why: String(error?.message ?? error).slice(0, 600) });
  } finally {
    realClearTimeout(watchdog);
  }
}
function expect(condition, why) {
  if (!condition) throw new Error(typeof why === "function" ? why() : why);
}

const firstScenario = fx.idle.config.scenarios[0];
const EXTRACT = "/playground/extract";
const PICTURE = "/playground/models";
const runOf = (script) => script.state.runs.get(script.state.view);
const titleOf = (script) => script.outcomeOf(runOf(script)).title;
const notesOf = (script) => [script.outcomeOf(runOf(script)).note].flat(Infinity).filter(Boolean).join(" | ");
/** The visitor runs the first scenario, which makes one live model call. */
const start = (script) => script.execute({ key: firstScenario.id, scenario: firstScenario, text: firstScenario.text, simulate: firstScenario.simulate });
const seen = (sent, from = 0) => sent.slice(from).map((request) => `${request.method} ${request.path}`);
/** The longest silence the page accepts from a run: one model's time and 90 seconds more. */
const SILENCE = fx.idle.config.limits.timeout_seconds * SECOND + 90 * SECOND;

// ------------------------------------------- asking only while something happens ---

await check("a loaded page looks every 20 s for 10 minutes, and then asks nothing", async () => {
  const { script, clock, sent, count } = await page(up(idleWorld()));
  expect(script.state.config && script.state.health && script.upkeep.picture, "the page did not load from the service's own answers");
  expect(sent.length === 3, `the load sent ${seen(sent)}: one request for each of its three answers was expected`);
  await clock.advance(10 * MINUTE + 30 * SECOND);
  const looks = sent.slice(3);
  expect(looks.every((request) => request.method === "GET" && request.path === PICTURE), `an idle page also sent: ${seen(sent, 3).filter((line) => line !== `GET ${PICTURE}`)}`);
  expect(looks.length >= 29 && looks.length <= 31, `${looks.length} looks in 10 minutes, not 30`);
  const gaps = looks.slice(1).map((request, index) => request.at - looks[index].at);
  expect(gaps.every((gap) => gap === 20 * SECOND), `looks are not 20 s apart: ${gaps.slice(0, 5)}`);
  const after = sent.length;
  await clock.advance(30 * MINUTE);
  expect(sent.length === after, `${sent.length - after} request(s) after the 10 minutes: ${seen(sent, after).slice(0, 4)}`);
});

await check("a tab nobody is looking at asks nothing, and looks once when the visitor comes back", async () => {
  const { clock, sent, dom } = await page(up(idleWorld()));
  dom.document.hidden = true;
  const before = sent.length;
  await clock.advance(5 * MINUTE);
  expect(sent.length === before, `a hidden tab sent ${seen(sent, before)}`);
  dom.document.hidden = false;
  dom.fire("visibilitychange");
  await clock.advance(SECOND);
  expect(sent.length === before + 1 && sent.at(-1).path === PICTURE, `back in view it sent ${seen(sent, before)}`);
});

await check("a sign of life starts the looks again, for 10 more minutes", async () => {
  const { clock, sent, dom } = await page(up(idleWorld()));
  await clock.advance(20 * MINUTE);
  const quiet = sent.length;
  dom.fire("keydown");
  await clock.advance(SECOND);
  expect(sent.length === quiet + 1, `a key press was followed by ${seen(sent, quiet)}`);
  await clock.advance(30 * MINUTE);
  const more = sent.length - quiet;
  expect(more >= 30 && more <= 32, `${more} looks after the key press, not about 31`);
});

await check("while the service is busy the page looks every 2 s; once it gets no answer it shows no test and keeps to the slow looks", async () => {
  const busy = fx.pictures.find((entry) => entry.picture.test);
  expect(busy, "the fixtures hold no picture of a running test");
  const world = structuredClone({ health: fx.health, config: busy.config, picture: busy.picture });
  let away = false;
  const { script, clock, sent, count, pill } = await page((url, ...rest) => (away ? asHTML(503) : up(world)(url, ...rest)));
  const from = clock.now();
  await clock.advance(20 * SECOND);
  const looks = count("GET", PICTURE, from + 1);
  expect(looks >= 9 && looks <= 11, `${looks} looks in 20 s of a running test, not 10`);
  away = true;
  await clock.advance(10 * SECOND);
  const shown = script.shownPicture();
  expect(script.upkeep.lost === true, "the page does not know that it has lost touch");
  expect(shown.test === null && shown.busy === false && shown.doing === null, "a test is still shown as running although the page cannot see it");
  expect(pill() === "waiting", `pill: ${pill()}`);
  const asking = sent.length;
  await clock.advance(2 * MINUTE);
  const slow = seen(sent, asking);
  expect(slow.every((line) => line === `GET ${PICTURE}`) && slow.length === 6, `in 2 minutes without an answer it sent ${slow.length} requests, not 6 looks: ${slow.slice(0, 8)}`);
  // The looks end with the 10 minutes after the visitor's last sign of life: opening the page.
  await clock.advance(10 * MINUTE);
  expect(pill() === "offline", `pill once the page has stopped asking: ${pill()}`);
  const stopped = sent.length;
  expect(stopped - asking <= 30, `${stopped - asking} looks without an answer: more than 10 minutes of them`);
  await clock.advance(30 * MINUTE);
  expect(sent.length === stopped, `${sent.length - stopped} request(s) after the 10 minutes: ${seen(sent, stopped).slice(0, 4)}`);
});

await check("a look without an answer does not end looking: every 20 s while the visitor is there, and the first answer puts the page right", async () => {
  const world = idleWorld();
  let away = false;
  const awake = up(world, () => asStream(fx.streams.answered));
  const { script, clock, sent, count, dom, pill } = await page((url, ...rest) => (away ? "refused" : awake(url, ...rest)));
  away = true;
  await clock.advance(30 * SECOND); // the look at 20 s finds nobody
  expect(script.upkeep.lost === true && pill() === "waiting", `lost: ${script.upkeep.lost}, pill: ${pill()}`);
  const from = sent.length;
  await clock.advance(MINUTE);
  expect(String(seen(sent, from)) === String([`GET ${PICTURE}`, `GET ${PICTURE}`, `GET ${PICTURE}`]), `in the next minute it sent ${seen(sent, from)}`);
  // A tab nobody is looking at asks nothing, lost or not, and the visitor's return is a look.
  dom.document.hidden = true;
  dom.fire("visibilitychange");
  const hidden = sent.length;
  await clock.advance(5 * MINUTE);
  expect(sent.length === hidden, `a hidden tab sent ${seen(sent, hidden)}`);
  dom.document.hidden = false;
  dom.fire("visibilitychange");
  await clock.advance(SECOND);
  expect(String(seen(sent, hidden)) === `GET ${PICTURE}`, `back in view it sent ${seen(sent, hidden)}`);
  away = false;
  world.picture = { ...world.picture, awake_since: "2030-01-01T00:00:00Z" }; // it has started again meanwhile
  await clock.advance(20 * SECOND);
  expect(script.upkeep.lost === false && pill() === fx.health.status, `back: lost ${script.upkeep.lost}, pill ${pill()}`);
  expect(script.upkeep.picture.awake_since === "2030-01-01T00:00:00Z", "the page did not take the new picture");
  expect(count("GET", "/health") === 1, `${count("GET", "/health")} x /health: a look that got no answer started a wait of its own`);
  // Lost once more. This time a run of the visitor's is answered before the next slow
  // look: that shows the service is back, so the page looks at once.
  away = true;
  await clock.advance(21 * SECOND);
  expect(script.upkeep.lost === true, "the page does not know that it has lost touch again");
  away = false;
  await start(script);
  await clock.advance(SECOND);
  expect(runOf(script).status === 200 && script.upkeep.lost === false, `after an answered run: status ${runOf(script).status}, lost ${script.upkeep.lost}`);
});

await check("a look that comes back unanswered while a run waits for the service leaves that wait alone", async () => {
  const world = idleWorld();
  let asleep = false;
  // The host answers the run at once and the look a moment later, both with a page of its own.
  const late = () => new Promise((resolve) => setTimeout(() => resolve(asHTML(503)), 200));
  const awake = up(world, () => asStream(fx.streams.answered));
  const { script, clock, dom, count, pill } = await page((url, ...rest) => (!asleep ? awake(url, ...rest) : url === PICTURE ? late() : asHTML(503)));
  await clock.advance(11 * MINUTE); // no look is arranged any more: the next sign of life looks at once
  asleep = true;
  const looks = count("GET", PICTURE);
  dom.fire("pointerdown");
  const running = start(script);
  await clock.advance(5 * SECOND);
  expect(count("GET", PICTURE) === looks + 1, "the press did not start a look: this case no longer tests anything");
  const run = runOf(script);
  expect(run.waking === true && !run.problem && pill() === "waiting", `5 s after the press: waking ${run.waking}, pill ${pill()}, problem: ${run.problem}`);
  expect(script.upkeep.lost === true, "the page does not know that its look got no answer");
  await clock.advance(30 * SECOND);
  expect(count("GET", PICTURE) === looks + 1, `${count("GET", PICTURE) - looks - 1} look(s) while the run was waiting: the wait does the asking`);
  asleep = false;
  await clock.advance(4 * SECOND);
  await running;
  expect(run.status === 200 && run.resent === true && count("POST", EXTRACT) === 2, `after the service was back: status ${run.status}, resent ${run.resent}, ${count("POST", EXTRACT)} POSTs`);
  await clock.advance(SECOND);
  expect(script.upkeep.lost === false && pill() === fx.health.status, `a second after the answer: lost ${script.upkeep.lost}, pill ${pill()}`);
});

await check("a page that never gets the picture keeps to the same looks, and its pill to what the service said of itself", async () => {
  const world = idleWorld();
  const { script, clock, sent, count, pill } = await page((url, ...rest) => (url === PICTURE ? asHTML(500) : up(world)(url, ...rest)));
  expect(script.state.config && script.upkeep.picture === null, "the page should load without the picture");
  await clock.advance(30 * MINUTE);
  // One with the page, one at once after it, and then the slow looks of the first 10 minutes.
  const looks = count("GET", PICTURE);
  expect(looks >= 31 && looks <= 33 && count("GET", "/health") === 1, `it sent ${looks} x the picture and ${count("GET", "/health")} x /health`);
  expect(script.upkeep.lost === true && pill() === fx.health.status, `lost ${script.upkeep.lost}, pill ${pill()}`);
  const quiet = sent.length;
  await clock.advance(30 * MINUTE);
  expect(sent.length === quiet, `${sent.length - quiet} request(s) after the 10 minutes: ${seen(sent, quiet).slice(0, 4)}`);
});

// ------------------------------------------------------------------- one run ---

await check("an answered run is sent once, and the page asks nothing more for it", async () => {
  const { script, clock, sent, count } = await page(up(idleWorld(), () => asStream(fx.streams.answered)));
  await start(script);
  expect(runOf(script).status === 200, `the run ended with status ${runOf(script).status}`);
  expect(JSON.parse(sent.find((request) => request.path === EXTRACT).body).text === firstScenario.text, "the run did not carry the scenario's text");
  await clock.advance(30 * MINUTE);
  expect(count("POST", EXTRACT) === 1 && count("GET", "/health") === 1, `POSTs: ${count("POST", EXTRACT)}, /health: ${count("GET", "/health")}`);
});

await check("a host's page in place of the answer: wait for /health, send again once, say so", async () => {
  const { script, clock, count, dom } = await page(up(idleWorld(), () => asHTML(502)));
  const running = start(script);
  await clock.advance(MINUTE);
  await running;
  await clock.advance(30 * MINUTE);
  expect(count("POST", EXTRACT) === 2, `${count("POST", EXTRACT)} POSTs of the run, not 2`);
  const run = runOf(script);
  expect(run.phase === "done" && run.resent === true, "the run is not marked as sent again");
  expect(/no answer a second time/.test(run.problem ?? ""), `problem: ${run.problem}`);
  expect(titleOf(script) === "No result", `title: ${titleOf(script)}`);
  expect(script.state.busy === false && dom.lookup("#run").store.disabled === false, "the Run button stayed locked");
});

await check("a request that fails outright is handled like a sleeping service, without an error", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => "refused"));
  const running = start(script);
  await clock.advance(MINUTE);
  await running;
  expect(count("POST", EXTRACT) === 2, `${count("POST", EXTRACT)} POSTs, not 2`);
  expect(runOf(script).phase === "done" && script.state.busy === false, "the page is still busy");
});

await check("a host's own JSON is not taken for the service's answer", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => asJSON(503, { message: "Service Unavailable", code: 503 })));
  const running = start(script);
  await clock.advance(MINUTE);
  await running;
  expect(runOf(script).status == null, `the run got status ${runOf(script).status} from a host's answer`);
  expect(count("POST", EXTRACT) === 2, "the host's JSON was shown as the result of the run");
});

await check("the service's own refusal before a stream is the result: no wait, no second send", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => asJSON(fx.too_short.status, fx.too_short.body)));
  await start(script);
  await clock.advance(MINUTE);
  expect(runOf(script).status === 422 && count("POST", EXTRACT) === 1, `status ${runOf(script).status}, ${count("POST", EXTRACT)} POSTs`);
  expect(count("GET", "/health") === 1, "the page asked /health after an answer of the service's own");
});

await check("a sleeping service: /health every 3 s, and 3 minutes after the press the page gives up for good", async () => {
  const world = idleWorld();
  let asleep = false;
  const { script, clock, count, pill, sent } = await page((url, ...rest) => (asleep ? asHTML(503) : up(world)(url, ...rest)));
  asleep = true;
  await clock.advance(SECOND);
  const from = clock.now();
  const running = start(script);
  await clock.advance(10 * SECOND);
  expect(pill() === "waiting" && titleOf(script) === "Waiting for the demo", `pill ${pill()}, title ${titleOf(script)}`);
  expect(count("GET", "/health", from) === 4, `${count("GET", "/health", from)} questions in the first 10 s, not 4 (at 0, 3, 6 and 9 s)`);
  await clock.advance(3 * MINUTE);
  await running;
  const asked = count("GET", "/health", from);
  expect(asked >= 60 && asked <= 61, `${asked} questions in 3 minutes, not 60`);
  expect(/has not answered for 3 minutes/.test(runOf(script).problem ?? ""), `problem: ${runOf(script).problem}`);
  expect(pill() === "offline" && count("POST", EXTRACT) === 1, `pill ${pill()}, ${count("POST", EXTRACT)} POSTs`);
  expect(count("GET", PICTURE, from) === 0, "the page also looked for the picture while it waited");
  const after = sent.length;
  await clock.advance(30 * MINUTE);
  expect(sent.length === after, `${sent.length - after} request(s) after giving up: ${seen(sent, after).slice(0, 4)}`);
});

await check("the service is back after 20 s: one resend, answered, and the pill says so", async () => {
  const world = idleWorld();
  let asleep = false;
  const { script, clock, count, pill, dom, announced } = await page((url, ...rest) => (asleep ? asHTML(502) : up(world, () => asStream(fx.streams.answered))(url, ...rest)));
  asleep = true;
  const running = start(script);
  await clock.advance(20 * SECOND);
  expect(announced() === "The demo is not answering. Waiting for it.", `read out while waiting: ${announced()}`);
  asleep = false;
  await clock.advance(5 * SECOND);
  await running;
  const run = runOf(script);
  expect(run.status === 200 && run.resent === true && !run.problem, `status ${run.status}, resent ${run.resent}, problem ${run.problem}`);
  expect(count("POST", EXTRACT) === 2 && pill() === fx.health.status, `${count("POST", EXTRACT)} POSTs, pill ${pill()}`);
  expect(dom.words.some((text) => /sent your run again/.test(text)), "the page does not say that the run was sent again");
});

await check("a request the host holds and then delivers: one line and the pill after 5 s, no second send", async () => {
  let release;
  const held = new Promise((resolve) => (release = resolve));
  const { script, clock, count, pill } = await page(up(idleWorld(), () => held.then(() => asStream(fx.streams.answered))));
  const running = start(script);
  await clock.advance(4 * SECOND);
  expect(!/may be waking up/.test(notesOf(script)) && pill() === fx.health.status, "the page speaks of waking up before 5 s have passed");
  await clock.advance(2 * SECOND);
  expect(/may be waking up/.test(notesOf(script)) && pill() === "waiting", `after 5 s: pill ${pill()}, notes: ${notesOf(script)}`);
  await clock.advance(50 * SECOND);
  release();
  await clock.advance(SECOND);
  await running;
  expect(runOf(script).status === 200 && count("POST", EXTRACT) === 1, `status ${runOf(script).status}, ${count("POST", EXTRACT)} POSTs`);
  expect(count("GET", "/health") === 1 && pill() === fx.health.status, `/health asked ${count("GET", "/health")} times, pill ${pill()}`);
});

await check("a request the host holds for ever ends by itself: silence, then the wait, then a way out", async () => {
  const world = idleWorld();
  let holding = false;
  const { script, clock, count } = await page((url, ...rest) => (holding ? "held" : up(world)(url, ...rest)));
  holding = true;
  const running = start(script);
  await clock.advance(SILENCE - SECOND);
  expect(titleOf(script) === "Running" && count("GET", "/health") === 1, `before the limit: ${titleOf(script)}, /health asked ${count("GET", "/health")} times`);
  await clock.advance(2 * SECOND);
  expect(titleOf(script) === "Waiting for the demo", `after ${SILENCE / SECOND} s of silence the run is still "${titleOf(script)}"`);
  await clock.advance(3 * MINUTE - SILENCE);
  await running;
  const run = runOf(script);
  expect(run.phase === "done" && script.state.busy === false, "the run never ended");
  expect(/has not answered for 3 minutes/.test(run.problem ?? ""), `problem: ${run.problem}`);
  expect(count("POST", EXTRACT) === 1, `${count("POST", EXTRACT)} POSTs`);
});

await check("a stream that stalls is ended after the silence limit, says so, and is not sent again", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => asStream(fx.streams.answered, { stallAfter: 2 })));
  const running = start(script);
  await clock.advance(SILENCE - SECOND);
  expect(titleOf(script) === "Running", `before the limit: ${titleOf(script)}`);
  await clock.advance(2 * SECOND);
  await running;
  const run = runOf(script);
  expect(run.phase === "done" && /nothing more came from it for 2 min/.test(run.problem ?? ""), `problem: ${run.problem}`);
  expect(count("POST", EXTRACT) === 1 && count("GET", "/health") === 1, "a run the service had taken was sent again, or the page asked /health");
});

await check("a run whose events come slowly is not cut off: every event starts the wait again", async () => {
  const { script, clock } = await page(up(idleWorld(), () => asStream(fx.streams.answered, { every: SILENCE - 5 * SECOND })));
  const running = start(script);
  await clock.advance(fx.streams.answered.length * SILENCE);
  await running;
  expect(runOf(script).status === 200 && !runOf(script).problem, `status ${runOf(script).status}, problem ${runOf(script).problem}`);
});

await check("a run that met another chain than the page had drawn makes the page look at once", async () => {
  const other = structuredClone(fx.streams.answered);
  other[0].chain = other[0].chain.slice(0, 1);
  const { script, clock, count } = await page(up(idleWorld(), () => asStream(other)));
  await clock.advance(SECOND);
  const from = clock.now();
  await start(script);
  await clock.advance(SECOND);
  expect(runOf(script).moved === true, "the run is not marked as moved");
  expect(count("GET", PICTURE, from) === 1, `${count("GET", PICTURE, from)} looks in the second after the run`);
});

// ------------------------------------------------------------- the first load ---

await check("a first load that finds the service asleep finishes by itself once it is back", async () => {
  const world = idleWorld();
  let asleep = true;
  const { script, clock, sent, pill, announced } = await page((url, ...rest) => (asleep ? asHTML(200) : up(world)(url, ...rest)));
  await clock.advance(10 * SECOND);
  expect(script.state.config == null && pill() === "waiting", `config ${script.state.config}, pill ${pill()}`);
  expect(/^The demo has not answered yet/.test(announced() ?? ""), `read out: ${announced()}`);
  asleep = false;
  await clock.advance(5 * SECOND);
  expect(script.state.config != null && script.upkeep.picture != null, "the page did not finish loading after the service came back");
  expect(pill() === fx.health.status, `pill ${pill()}`);
  const after = sent.length;
  await clock.advance(MINUTE);
  expect(seen(sent, after).every((line) => line === `GET ${PICTURE}`), `after loading it sent ${seen(sent, after)}`);
});

await check("a first load gives up after 3 minutes and asks nothing more", async () => {
  const { script, clock, sent, pill, announced } = await page(() => asHTML(503));
  await clock.advance(3 * MINUTE + 5 * SECOND);
  const after = sent.length;
  expect(script.state.config == null && pill() === "offline", `config ${script.state.config}, pill ${pill()}`);
  expect(after >= 60 && after <= 66, `${after} requests in 3 minutes of a first load`);
  expect(/has not answered for 3 minutes/.test(announced() ?? ""), `read out: ${announced()}`);
  await clock.advance(30 * MINUTE);
  expect(sent.length === after, `${sent.length - after} request(s) after giving up`);
});

await check("a first load whose requests are held for ever gives up after 3 minutes, with nothing asked twice", async () => {
  const { script, clock, sent, pill } = await page(() => "held");
  await clock.advance(3 * MINUTE - SECOND);
  expect(pill() === "waiting" && sent.length === 3, `before the limit: pill ${pill()}, ${sent.length} requests`);
  await clock.advance(2 * SECOND);
  expect(script.state.config == null && pill() === "offline", `after 3 minutes: pill ${pill()}`);
  await clock.advance(30 * MINUTE);
  expect(sent.length === 3, `requests in all: ${seen(sent)}`);
});

// --------------------------------------------------------------- the ceiling ---

await check("a refusal by the ceiling is read from the last event: a minute, a day, and neither", async () => {
  for (const [seconds, today] of [[37, false], [60, false], [43_200, true]]) {
    const { script, clock } = await page(up(idleWorld(), () => refusal(seconds)));
    const from = clock.now();
    await start(script);
    const run = runOf(script);
    expect(run.refusal && run.refusal.today === today, `Retry-After ${seconds}: refusal ${JSON.stringify(run.refusal)}`);
    expect(Math.abs(run.refusal.until - from - seconds * SECOND) < 50, `Retry-After ${seconds}: the wait ends ${run.refusal.until - from} ms after the run`);
    expect(run.checks.length === 0, "a refused run was scored against its scenario");
    expect(titleOf(script) === (today ? "Today's share of live runs is used up" : "The demo is busy"), `title: ${titleOf(script)}`);
    expect(/simulated failures still work/.test(notesOf(script)), "the outcome does not say what still works");
  }
  const { script } = await page(up(idleWorld(), () => asStream(fx.streams.all_failed)));
  await start(script);
  expect(runOf(script).status === 503 && runOf(script).refusal === null, "every model failed, and the page calls it a refusal");
});

await check("when the day of a refusal is over, the page says so instead of 'today'", async () => {
  const { script, clock } = await page(up(idleWorld(), () => refusal(100)));
  await start(script);
  expect(titleOf(script) === "Today's share of live runs is used up" && /Live runs start again at midnight UTC/.test(notesOf(script)), `before: ${titleOf(script)}`);
  await clock.advance(101 * SECOND);
  expect(titleOf(script) === "The day's share of live runs was used up", `after: ${titleOf(script)}`);
  expect(/Midnight UTC has passed since then, so you can try again now\./.test(notesOf(script)), `notes: ${notesOf(script)}`);
});

await check("Run all waits out a full minute and carries on with the same scenario", async () => {
  let posts = 0;
  const { script, clock, count } = await page(up(idleWorld(), () => ((posts += 1) === 2 ? refusal(40) : asStream(fx.streams.answered))));
  const batch = script.runAll();
  await clock.advance(20 * SECOND);
  expect(count("POST", EXTRACT) === 2 && script.state.batch?.waitUntil, `${count("POST", EXTRACT)} POSTs 20 s into the wait, not 2`);
  await clock.advance(10 * MINUTE);
  await batch;
  const scenarios = fx.idle.config.scenarios.length;
  expect(count("POST", EXTRACT) === scenarios + 1, `${count("POST", EXTRACT)} POSTs for ${scenarios} scenarios and one refusal`);
  expect(script.state.halt === null && script.state.batch === null, `halt: ${script.state.halt}`);
});

await check("Run all waits twice for one scenario at most, then stops with a sentence", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => refusal(30)));
  const batch = script.runAll();
  await clock.advance(10 * MINUTE);
  await batch;
  expect(count("POST", EXTRACT) === 3, `${count("POST", EXTRACT)} POSTs, not 3`);
  expect(script.state.halt === "Run all stopped: the demo is still busy.", `halt: ${script.state.halt}`);
});

await check("Run all does not wait for midnight: it stops at once and says why", async () => {
  const { script, clock, count } = await page(up(idleWorld(), () => refusal(43_200)));
  const batch = script.runAll();
  await clock.advance(MINUTE);
  await batch;
  expect(count("POST", EXTRACT) === 1, `${count("POST", EXTRACT)} POSTs, not 1`);
  expect(script.state.halt === "Run all stopped: today's share of live runs is used up.", `halt: ${script.state.halt}`);
});

// ----------------------------------------------- the run that is sent again ---

/** A page whose first run was answered by no model, on a chain that the service looks after. */
async function afterFailedRun() {
  const world = idleWorld();
  let failing = true;
  const made = await page(up(world, () => asStream(failing ? fx.streams.all_failed : fx.streams.answered)));
  await start(made.script);
  expect(runOf(made.script).status === 503 && made.script.upkeep.rerun, "the failed run was not remembered");
  const repair = (next) => {
    failing = false;
    Object.assign(world, next);
  };
  return { ...made, world, repair };
}

await check("a run that no model answered is sent again once, when the chain has a new model", async () => {
  const { script, clock, count, world, repair } = await afterFailedRun();
  expect(/If it does, this page sends the run again by itself\.$/.test(String(script.upkeepNotes(runOf(script)))), `the result says: ${script.upkeepNotes(runOf(script))}`);
  await clock.advance(3 * SECOND);
  repair(withNewModel(world));
  await clock.advance(5 * SECOND);
  expect(count("POST", EXTRACT) === 2, `${count("POST", EXTRACT)} POSTs after the chain got a new model, not 2`);
  expect(runOf(script).status === 200 && runOf(script).again === true, `the second run: status ${runOf(script).status}, again ${runOf(script).again}`);
  repair(withNewModel(world, "vendor/another:free"));
  await clock.advance(MINUTE);
  expect(count("POST", EXTRACT) === 2, `${count("POST", EXTRACT)} POSTs after the chain changed once more: a third run was sent`);
});

await check("a chain that only lost a model has nobody new to ask: nothing is sent again", async () => {
  const { script, clock, count, world, repair } = await afterFailedRun();
  const shorter = structuredClone(world);
  shorter.picture.chain.pop();
  shorter.config.chain.pop();
  repair(shorter);
  await clock.advance(MINUTE);
  expect(count("POST", EXTRACT) === 1, `${count("POST", EXTRACT)} POSTs: the run was sent again to the models that had failed`);
  expect(script.upkeep.rerun === null, "the page went on waiting for a repair");
  expect(/has changed its model chain since this run/.test(String(script.upkeepNotes(runOf(script)))), `the result says: ${script.upkeepNotes(runOf(script))}`);
});

await check("a visitor who has moved on is not handed an answer to the old run", async () => {
  const { script, clock, count, world, repair } = await afterFailedRun();
  script.select("custom");
  script.state.draft.text = "Invoice 77 from Acme";
  repair(withNewModel(world));
  await clock.advance(MINUTE);
  expect(count("POST", EXTRACT) === 1, `${count("POST", EXTRACT)} POSTs: the page sent a run the visitor had left`);
  expect(script.state.draft.text === "Invoice 77 from Acme" && script.state.view === "custom", "what the visitor was doing was touched");
});

await check("nothing of Run all is sent a second time when the chain is repaired", async () => {
  const world = idleWorld();
  const { script, clock, count } = await page(up(world, () => asStream(fx.streams.all_failed)));
  const batch = script.runAll();
  await clock.advance(MINUTE);
  await batch;
  const sent = count("POST", EXTRACT);
  expect(sent === fx.idle.config.scenarios.length && script.upkeep.rerun === null, `${sent} POSTs, rerun ${JSON.stringify(script.upkeep.rerun)}`);
  Object.assign(world, withNewModel(world));
  await clock.advance(MINUTE);
  expect(count("POST", EXTRACT) === sent, `${count("POST", EXTRACT) - sent} run(s) sent by the page after Run all had ended`);
});

// ---------------------------------------------------------------- the button ---

await check("the button sends one POST to the service's own route, and a second press meanwhile sends nothing", async () => {
  const TEST = "/playground/models/test";
  const { script, clock, count } = await page(up(idleWorld(), (url) => (url === TEST ? "held" : null)));
  script.startModelsTest();
  script.startModelsTest();
  await clock.advance(10 * SECOND);
  expect(count("POST", TEST) === 1, `${count("POST", TEST)} POSTs to ${TEST}`);
  expect(script.upkeep.starting === true, "the page forgot that it is waiting for the service's answer");
  await clock.advance(3 * MINUTE);
  expect(script.upkeep.starting === false, "a press that never got an answer keeps the button for ever");
});

// ----------------------------------------------------------------- the words ---

await check("no engine message reaches the page with the name of a setting", async () => {
  const { script } = await page(up(idleWorld()));
  for (const message of fx.engine_messages) {
    const shown = script.visitorWords(message);
    expect(!/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/.test(shown), `shown to a visitor: ${shown}`);
  }
  expect(script.visitorWords("a/b:free: rate limited (429)") === "a/b:free: rate limited (429)", "an ordinary message was changed");
});

const BAD_WORDS = /undefined|\bnull\b|NaN|\[object|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/;

await check("every picture the service can send is put into words, on its own and after the one before", async () => {
  expect(fx.pictures.length >= 12, `only ${fx.pictures.length} pictures in the fixtures`);
  const problems = [];
  // Each picture on a page of its own, and once more as the page shows it after losing touch.
  for (const { name, config, picture } of fx.pictures) {
    const world = { health: fx.health, config, picture };
    let away = false;
    const made = await page((url, ...rest) => (away ? "refused" : up(world)(url, ...rest)));
    away = true;
    made.dom.fire("pointerdown");
    await made.clock.advance(25 * SECOND);
    if (thrown.length) problems.push(`${name}: ${thrown.splice(0)[0]}`);
    const bad = made.dom.words.find((text) => BAD_WORDS.test(text));
    if (bad) problems.push(`${name}: the page says "${bad.slice(0, 160)}"`);
    if (!made.script.upkeep.picture) problems.push(`${name}: the page did not take the picture`);
  }
  // And all of them one after the other on one page, as looks bring them in.
  const world = structuredClone({ health: fx.health, config: fx.pictures[0].config, picture: fx.pictures[0].picture });
  const made = await page(up(world));
  for (const { name, config, picture } of fx.pictures.slice(1)) {
    Object.assign(world, { config, picture });
    made.dom.fire("pointerdown");
    await made.clock.advance(21 * SECOND);
    if (thrown.length) problems.push(`after the picture before it, ${name}: ${thrown.splice(0)[0]}`);
  }
  expect(problems.length === 0, problems.join("\n"));
});

await check("the words of a picture say no more than it shows: no test that is not running, no run that is still to find out", async () => {
  const RUNNING = /The test that is running has the last word|A test is running|A test of the free models is running now|is still running and has the last word/;
  const TO_FIND_OUT = "The next run will find that out";
  const problems = [];
  let standing = 0;
  let found = 0;
  for (const { name, config, picture } of fx.pictures) {
    const made = await page(up({ health: fx.health, config, picture }));
    const said = made.dom.words.join(" | ");
    if (!picture.test && RUNNING.test(said)) problems.push(`${name}: no test is running, and the page says "${RUNNING.exec(said)[0]}"`);
    if (!picture.test && picture.origin.why?.code === "first_answer") standing += 1;
    // A run has found out when the last look was about these very models and could start no test.
    const look = picture.look;
    const known = look?.reason?.code === "gone" && look.result !== "started" && picture.gone.every((model) => look.reason.models.includes(model));
    if (picture.gone.length && known) {
      found += 1;
      if (said.includes(TO_FIND_OUT)) problems.push(`${name}: a run has found out, and the page says "${TO_FIND_OUT}"`);
    }
  }
  expect(standing >= 1 && found >= 3, `the pictures show ${standing} stand-in(s) that wait and ${found} look(s) that found models gone: too few to hold the words`);
  expect(problems.length === 0, problems.join("\n"));
});

await check("when the chain no longer has the scenario on view, the request panel is the visitor's own document", async () => {
  const long = fx.pictures.find((entry) => entry.picture.mode === "automatic" && entry.picture.chain.length === 3 && !entry.picture.busy);
  expect(long, "no picture with a chain of three in the fixtures");
  const world = structuredClone({ health: fx.health, config: long.config, picture: long.picture });
  const { script, clock, dom } = await page(up(world));
  const only = long.config.scenarios.find((scenario) => !fx.idle.config.scenarios.some((other) => other.id === scenario.id));
  expect(only, "a chain of three has no scenario that a chain of two lacks");
  script.select(only.id);
  expect(script.state.selected === only.id, "the scenario was not selected");
  // The service wakes up on its default chain of two.
  Object.assign(world, idleWorld());
  dom.fire("pointerdown");
  await clock.advance(21 * SECOND);
  expect(script.state.config.chain.length === 2, "the page did not follow the chain");
  expect(script.state.selected === "custom", `the request panel still shows "${script.state.selected}", a scenario this chain does not have`);
  expect(script.state.draft.text === only.text, "the document on view was touched");
});

await check("a result that a stand-in gave says so once the stand-in has failed the test", async () => {
  const story = fx.pictures.filter((entry) => entry.name.startsWith("a stand-in that fails:"));
  const during = story.find((entry) => entry.picture.test?.provisional.length === 1);
  expect(during, `no picture of a test with a stand-in in: ${story.map((entry) => entry.state)}`);
  const standIn = during.picture.test.provisional[0];
  const failed = (entry) => (entry.picture.test ?? entry.picture.last).models.find((row) => row.model === standIn)?.verdict === "failed";
  const after = story.findLast((entry) => failed(entry) && !entry.picture.chain.includes(standIn));
  expect(after, `no picture in which ${standIn} has failed and is out of the chain: ${story.map((entry) => entry.state)}`);
  // The service's own stream of an answered run, with the stand-in as the model that answered.
  const answered = JSON.parse(JSON.stringify(fx.streams.answered).replaceAll(fx.idle.picture.chain[0], standIn));
  const world = structuredClone({ health: fx.health, config: during.config, picture: during.picture });
  const { script, clock } = await page(up(world, () => asStream(answered)));
  await start(script);
  const run = runOf(script);
  expect(run.status === 200 && run.body.model_used === standIn, `the run: ${run.status}, answered by ${run.body?.model_used}`);
  expect(String(script.upkeepNotes(run)) === "", `while the stand-in answers, the result says: ${script.upkeepNotes(run)}`);
  Object.assign(world, structuredClone({ config: after.config, picture: after.picture }));
  await clock.advance(5 * SECOND);
  const notes = String(script.upkeepNotes(run));
  expect(/has failed the service's test since and no longer answers\. Its answer here may be wrong\./.test(notes), `after the stand-in failed, the result says: ${notes || "nothing"}`);
});

await check("a picture the page cannot draw does not end its looking: the next one is drawn", async () => {
  const world = idleWorld();
  const { script, clock, dom, count } = await page(up(world));
  const good = world.picture;
  world.picture = structuredClone(good);
  world.picture.button = { ...good.button, ready: false, reason: "a reason this page has no words for" };
  await clock.advance(21 * SECOND);
  expect(thrown.length >= 1, "the page drew a picture it has no words for without a sound: this case no longer tests anything");
  thrown.splice(0);
  const looks = count("GET", PICTURE);
  world.picture = good;
  dom.fire("pointerdown");
  await clock.advance(21 * SECOND);
  expect(count("GET", PICTURE) > looks, "after one picture it could not draw, the page never looked again");
  expect(script.upkeep.picture.button.reason === good.button.reason, "the next picture was not taken");
});

finish();
