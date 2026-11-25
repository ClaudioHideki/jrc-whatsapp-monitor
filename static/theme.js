(() => {
  const root = document.documentElement;
  const button = document.getElementById("theme-btn");
  const preference = "jrc-monitor-theme";
  function applyTheme(theme) {
    root.dataset.theme = theme;
    button.textContent = theme === "dark" ? "☀️ Claro" : "🌙 Escuro";
  }
  applyTheme(localStorage.getItem(preference) === "dark" ? "dark" : "light");
  button.addEventListener("click", () => {
    const theme = root.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(theme);
    localStorage.setItem(preference, theme);
  });
})();
