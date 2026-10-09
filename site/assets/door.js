// Front door script: the colour theme switch, and the one line on the page that is live.
//
// Everything else on the page is a record and sits in the HTML. This script builds no
// markup and cannot call a model: the only request it makes is for the live demo's own
// icon, loaded as a picture.
(() => {
  "use strict";

  const root = document.documentElement;

  // ---------------------------------------------------------------- theme ---

  // The same switch as on the demo's own page. It is hidden in the HTML, because
  // without this script it would do nothing.
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      const dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try {
        localStorage.setItem("playground-theme", root.dataset.theme); // the key theme.js reads
      } catch {
        // Storage can be blocked; the choice then lasts for this page view only.
      }
    });
  }

  // ------------------------------------------------------------ live demo ---
  //
  // The demo runs on a free host that puts it to sleep after 15 minutes without a
  // request. The next request wakes it, which takes "about one minute", and every
  // request restarts the 15 minutes (https://render.com/docs/free, read on
  // 8 October 2026). So this page asks as soon as it has loaded, and then stops
  // asking: once the demo has answered, or when it has waited long enough. A tab
  // that is left open sends nothing more, whether it is in front or behind another.
  // Only someone who comes back to it makes it ask afresh, and twice at most.
  //
  // The demo is at another address, so this page may not read its answers. What it
  // can do is load the demo's icon as a picture. "load" fires only when the demo
  // itself sends the icon. The host's own loading page is not a picture and counts
  // as an error, and so does a request that is refused.

  /** How long the host says waking takes. Every sentence below uses these words. */
  const WAKE_TAKES = "about a minute";

  /** A demo that is awake answers within this time, also on a slow connection: say nothing
      about waking before it has passed, unless a request has already failed. */
  const QUIET_START_MS = 4000;
  /** The pause after a request that came back as an error, before the next one. The page
      looks at its clock once a second, so in practice the next one goes out 3 to 4 s later. */
  const RETRY_AFTER_MS = 3000;
  /** A request still unanswered after this long is asked again. */
  const ATTEMPT_TIMEOUT_MS = 20000;
  /** Give up after this much waiting: three times the host's minute. */
  const GIVE_UP_AFTER_MS = 180000;
  /** And never send more requests than this in one round, whatever the clock does. With the
      pause above, the waiting ends first: this is a limit of last resort. */
  const MAX_ATTEMPTS = 80;
  /** The host's 15 minutes, less one: after this the page no longer says "awake". */
  const AWAKE_FOR_MS = 840000;
  /** How often the page looks at its clock while there is something to wait for. */
  const TICK_MS = 1000;
  /** A browser lets a tab that is not being looked at see its clock only once a minute,
      while its requests still go out. A gap up to this long is waiting like any other. A
      longer one is a page that did not run at all (a sleeping laptop, a frozen tab). */
  const SLOWED_TICK_MS = 90000;
  /** A page that has stopped asks afresh when its tab is looked at again: this many times
      at most, so that a tab nobody cares about cannot wake the demo all day. */
  const MAX_RETURNS = 2;

  /** Served by the demo itself, to any address, with no special header. */
  const ICON_PATH = "/playground/assets/favicon.svg";

  const WORDS = {
    waking: `Waking the live demo. It runs on free hosting and sleeps between visits, so this takes ${WAKE_TAKES}.`,
    awake: "The live demo is awake. Try a document there yourself, or make a model fail and watch the next one take over.",
    asleep: `The live demo may be asleep again. Opening it wakes it, which takes ${WAKE_TAKES}.`,
    quiet: "The live demo has not answered for three minutes, please contact the admin. This page shows recorded runs instead.",
  };
  /** In the tab's title, for someone who went to another tab while waiting. */
  const AWAKE_TITLE = "Live demo awake";
  /** After the count of minutes and seconds beside the line: it counts up, not down. */
  const COUNT_ENDS = "so far";
  /** Said once in the browser's console, for a reader who opens it while the demo wakes
      and finds a list of failed requests there. */
  const NOTE =
    "This page loads the live demo's icon to learn when the demo is awake. Requests for it that fail while the demo wakes are expected.";

  const link = document.getElementById("demo-link");
  const words = document.getElementById("demo-words");
  const clock = document.getElementById("demo-clock");
  const chip = document.getElementById("demo-chip");
  if (!link || !words || !clock) return;

  // The address is written once, in the link. While it is still the placeholder there
  // is nothing to wake, and the stylesheet shows "not online yet" instead.
  let address;
  try {
    address = new URL(link.href);
  } catch {
    return;
  }
  if (address.protocol !== "https:" || address.hostname.endsWith(".invalid")) return;

  // The icon is asked for at the demo's own address: the part of the page the link leads to
  // ("#playground") is not sent.
  const icon = new URL(ICON_PATH, address).href;
  const title = document.title;

  // The link in the top bar stays in view while the page is read. It gets the same address.
  if (chip) {
    chip.href = address.href;
    chip.hidden = false;
  }

  let phase = "idle"; // idle -> asking -> awake -> over, or asking -> over; "over" ends a round
  let shown = ""; // which of WORDS the line shows; "" while it still shows the page's own sentence
  let attempts = 0; // requests sent in this round
  let pending = null; // the newest request on its way: { image, startedAt }
  let kept = null; // one request that is left on its way for as long as the page waits
  let askedAt = 0; // when the newest request was sent
  let nextAt = 0; // the next request is not sent before this time
  let waited = 0; // milliseconds this page has spent waiting for an answer in this round
  let lastTick = 0;
  let awakeUntil = 0;
  let ticker = 0;
  let returns = 0; // rounds begun because the tab was looked at again
  let left = false; // true while the page is kept in the browser's memory after a link was followed
  let frozen = false; // true while the browser has frozen this tab
  let explained = false;

  /** How long this page has been waiting, in minutes and seconds. */
  function count() {
    const seconds = Math.floor(waited / 1000);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} ${COUNT_ENDS}`;
  }

  /** Change the line. The words sit in a status region, so each change is read out once. */
  function show(next) {
    if (next === shown) return;
    shown = next;
    root.dataset.demo = next; // the stylesheet draws the mark, the button and the top bar from this
    words.textContent = WORDS[next];
    // The count beside the words is outside the status region: it is seen, not read out.
    clock.textContent = count();
    clock.hidden = next !== "waking";
    retitle();
  }

  /** Someone who went to another tab while waiting sees in its title that the wait is over.
      A page that cannot run (frozen, or left behind a link) could not take the claim back
      when its time is up, so it does not make it. */
  function retitle() {
    const claim = shown === "awake" && document.hidden && !frozen && !left;
    const wanted = claim ? `${AWAKE_TITLE} · ${title}` : title;
    if (document.title !== wanted) document.title = wanted;
  }

  function ask() {
    const attempt = { image: new Image(), startedAt: Date.now() };
    attempts += 1;
    askedAt = attempt.startedAt;
    pending = attempt;
    if (!kept) kept = attempt;
    attempt.image.onload = () => {
      if (attempt === pending || attempt === kept) answered(attempt);
    };
    attempt.image.onerror = () => {
      if (attempt === kept) kept = null;
      if (attempt === pending) failed();
    };
    // A new address every time, so that no cache can answer in the demo's place.
    attempt.image.src = `${icon}?wake=${attempt.startedAt}`;
  }

  /** Cancel one request. */
  function withdraw(attempt) {
    attempt.image.onload = null;
    attempt.image.onerror = null;
    attempt.image.removeAttribute("src");
  }

  /** Cancel every request on its way: there are two at most. */
  function drop() {
    if (pending) withdraw(pending);
    if (kept && kept !== pending) withdraw(kept);
    pending = null;
    kept = null;
  }

  /** The waiting is used up, or the count of requests is. */
  function spent() {
    return waited >= GIVE_UP_AFTER_MS || attempts >= MAX_ATTEMPTS;
  }

  /** Not the icon: the host's loading page, an error, or no connection. Ask again after a pause. */
  function failed() {
    pending = null;
    if (!explained) {
      explained = true;
      console.info(NOTE);
    }
    if (spent()) {
      giveUp();
      return;
    }
    nextAt = Date.now() + RETRY_AFTER_MS;
    show("waking");
  }

  function answered(attempt) {
    // This request is done. The other one, if two were on their way, is withdrawn.
    if (attempt === pending) pending = null;
    if (attempt === kept) kept = null;
    drop();
    phase = "awake";
    // The host counts its 15 minutes from the request, so the page does too.
    awakeUntil = attempt.startedAt + AWAKE_FOR_MS;
    tick();
  }

  function giveUp() {
    drop();
    show("quiet");
    stop();
  }

  /** After this nothing runs and nothing is requested, until the tab is looked at again. */
  function stop() {
    clearInterval(ticker);
    ticker = 0;
    phase = "over";
  }

  function tick() {
    const now = Date.now();
    // A slowed clock still counts in full. A longer gap is time in which nothing could be
    // asked and nothing could answer: it counts as one look.
    const gap = Math.max(0, now - lastTick);
    const step = gap > SLOWED_TICK_MS ? 2 * TICK_MS : gap;
    lastTick = now;

    if (phase === "awake") {
      if (now < awakeUntil) {
        show("awake");
      } else {
        show("asleep");
        stop();
      }
      return;
    }
    if (phase !== "asking") return;

    waited += step;
    if (pending && now - pending.startedAt >= ATTEMPT_TIMEOUT_MS) {
      // No answer yet. One request is kept on its way, in case the host holds it until the
      // demo is up and answers it then; any other is withdrawn. Either way, ask again.
      if (pending !== kept) withdraw(pending);
      pending = null;
      if (spent()) {
        giveUp();
        return;
      }
      nextAt = now;
    }
    // The time is up. If this page asked a moment ago, that was its answer. If its clock was
    // slowed and it has not asked for a while, it asks once more, and that answer decides.
    const justAsked = pending && now - pending.startedAt < QUIET_START_MS;
    if (waited >= GIVE_UP_AFTER_MS && !justAsked && (pending || now - askedAt < ATTEMPT_TIMEOUT_MS)) {
      giveUp();
      return;
    }
    if (waited >= QUIET_START_MS) show("waking");
    if (!pending && now >= nextAt) ask();
    clock.textContent = count();
  }

  /** One round: ask until the demo answers, or until the page has waited long enough. */
  function begin() {
    drop();
    phase = "asking";
    attempts = 0;
    waited = 0;
    nextAt = 0;
    lastTick = Date.now();
    ask(); // the first request is what starts the wake-up
    if (!ticker) ticker = setInterval(tick, TICK_MS);
  }

  /** The tab is in front of someone again. A page that is still waiting looks at its clock
      at once. One that had stopped (it gave up, or "awake" ran out) asks afresh. */
  function lookedAt() {
    if (document.hidden || frozen || left) return;
    if (ticker) tick();
    if (phase === "over" && returns < MAX_RETURNS) {
      returns += 1;
      begin();
    }
  }

  document.addEventListener("visibilitychange", () => {
    lookedAt();
    retitle();
  });
  // A browser may freeze a tab that has not been looked at for a while, and thaw it later.
  document.addEventListener("freeze", () => {
    frozen = true;
    retitle();
  });
  document.addEventListener("resume", () => {
    frozen = false;
    if (ticker) tick();
    lookedAt();
    retitle();
  });
  // Following a link leaves this page, and the Back button can bring it back as it was
  // left. That is a new visit.
  addEventListener("pagehide", () => {
    left = true;
    retitle();
  });
  addEventListener("pageshow", (event) => {
    left = false;
    if (event.persisted) {
      if (phase === "awake") tick(); // "awake" may have run out while the page was away
      returns = 0;
      begin();
    }
    retitle();
  });

  // The asking starts when the page itself has finished loading, a moment from here. A
  // request sent earlier would count as part of the page: for as long as the host kept it
  // waiting, the browser would show the page as still loading.
  function start() {
    if (document.readyState === "complete") begin();
    else addEventListener("load", begin, { once: true });
  }
  // A page the browser loads ahead of time, in case it is opened, wakes nothing.
  if (document.prerendering) document.addEventListener("prerenderingchange", start, { once: true });
  else start();
})();
