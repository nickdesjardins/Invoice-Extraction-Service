// Loaded synchronously from <head> so a saved theme applies before first paint.
// Without a saved choice the page follows the operating system (see app.css).
(function () {
  try {
    var saved = localStorage.getItem("playground-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (error) {
    // Storage can be blocked; following the operating system is a fine default.
  }
})();
