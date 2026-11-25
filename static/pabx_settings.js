(() => {
  const form = document.getElementById("pabx-settings-form");
  if (!form) return;
  const button = document.getElementById("pabx-test");
  const result = document.getElementById("pabx-test-result");
  button.addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    data.set("action", "test");
    button.disabled = true;
    result.textContent = "Testando conexão…";
    result.dataset.error = "false";
    try {
      const response = await fetch(form.getAttribute("action"), { method: "POST", body: data });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      const { message } = await response.json();
      result.textContent = message;
      result.dataset.error = String(!response.ok);
    } catch {
      result.textContent = "Não foi possível concluir o teste. Confira a conexão com o monitor.";
      result.dataset.error = "true";
    } finally {
      button.disabled = false;
    }
  });
})();
