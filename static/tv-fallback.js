(function () {
  var script = document.getElementById("tv-fallback");
  var pabx = script.getAttribute("data-monitor") === "pabx";
  var supported = typeof window.fetch === "function" && (pabx
    ? window.AbortSignal && typeof window.AbortSignal.timeout === "function" &&
      typeof document.documentElement.replaceChildren === "function"
    : typeof String.prototype.replaceAll === "function");

  if (!supported) window.location.replace(script.getAttribute("data-tv-url"));
})();
