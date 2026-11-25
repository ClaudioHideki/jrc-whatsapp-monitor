(() => {
  const dashboard = document.querySelector("[data-status-url]");
  if (!dashboard) return;

  const byId = (id) => document.getElementById(id);
  const formatCount = new Intl.NumberFormat("pt-BR");
  const formatTime = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "medium" });
  const stateLabels = {
    available: "Livre", busy: "Ocupado", ringing: "Tocando", in_call: "Em ligação",
    registered: "Registrado", unregistered: "Não registrado", unknown: "Estado indefinido",
  };
  const messages = {
    connected: "API conectada.",
    starting: "Aguardando a primeira coleta.",
    disabled: "Monitoramento desativado. Edite a configuração para ativar.",
    scope_pending: "Aguardando a confirmação do escopo operacional.",
    http_origin: "O endereço HTTP do perfil precisa ser autorizado nesta execução local.",
    configuration: "A configuração de acesso do servidor precisa ser concluída.",
    storage_setup: "A chave de criptografia e o armazenamento precisam ser configurados no servidor.",
    storage_failure: "Falha ao acessar a configuração do PABX.",
    collector_busy: "Outra instância do monitor está usando o mesmo banco do PABX.",
    credentials: "Credenciais do PABX recusadas.",
    forbidden: "A conta do PABX não tem permissão para essa consulta.",
    scope_mismatch: "O PABX retornou dados fora do escopo confirmado ou sem identificação.",
    token_expired: "O PABX entregou um token expirado.",
    token_rejected: "O PABX recusou o token após a renovação.",
    tls: "Falha na validação do certificado HTTPS.",
    timeout: "O PABX não respondeu dentro do prazo.",
    unavailable: "Falha de comunicação com a API do PABX.",
    upstream_response: "A API do PABX retornou uma resposta inesperada.",
    cdr_disabled: "O histórico CDR ainda não está habilitado.",
    history_busy: "Já existe uma consulta de histórico em andamento. Tente novamente em instantes.",
    page_limit: "O resultado excedeu o limite de páginas. Reduza o período e consulte novamente.",
    filters: "Informe um período válido de até 31 dias.",
    config_changed: "A configuração mudou durante a consulta. Consulte novamente.",
  };
  const search = byId("pabx-search");
  const stateFilter = byId("pabx-state");
  const queueFilter = byId("pabx-queue");
  let snapshot;
  let lastRendered = null;

  function element(tag, content) {
    const node = document.createElement(tag);
    if (content != null) node.textContent = content;
    return node;
  }

  function options(select, choices, allLabel) {
    const current = select.value;
    select.replaceChildren(new Option(allLabel, ""), ...choices.map(([value, text]) => new Option(text, value)));
    if (choices.some(([value]) => value === current)) select.value = current;
  }

  function cell(content) {
    return element("td", content === "" || content == null ? "—" : content);
  }

  function statusBadge(state) {
    const badge = element("span", stateLabels[state] || stateLabels.unknown);
    badge.className = "pabx-state";
    badge.dataset.state = state;
    return badge;
  }

  function pauseLabel(paused) {
    if (paused === true) return "Pausado";
    if (paused === false) return "Não pausado";
    return "Pausa não informada";
  }

  function renderExtensions() {
    const term = search.value.trim().toLocaleLowerCase("pt-BR");
    const state = stateFilter.value;
    const extensions = snapshot?.data?.extensions;
    const rows = (extensions || []).filter((row) => {
      const matchesState = state === "in_call" ? row.in_call === true :
        state === "registered" ? row.registered === true : !state || row.status === state;
      return matchesState &&
        (!queueFilter.value || row.queues.some((queue) => queue.code === queueFilter.value)) &&
        (!term || `${row.extension} ${row.name}`.toLocaleLowerCase("pt-BR").includes(term));
    });
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const tr = element("tr");
      const stateCell = element("td");
      stateCell.append(statusBadge(row.status));
      if (row.in_call) stateCell.append(" · ", statusBadge("in_call"));
      const registration = row.registered == null ? "" : row.registered ? "Registrado" : "Não registrado";
      const queues = row.queues.map((queue) => `${queue.name} (${pauseLabel(queue.paused)})`).join(", ");
      tr.append(cell(row.extension), cell(row.name), stateCell, cell(registration), cell(queues));
      fragment.append(tr);
    }
    byId("pabx-extensions").replaceChildren(fragment);
    let countMessage = "Aguardando dados de ramais.";
    if (extensions) {
      if (rows.length) countMessage = `${formatCount.format(rows.length)} ramais exibidos.`;
      else if (state === "in_call" && snapshot.data.indicators.extensions_in_call == null) {
        countMessage = "A API ainda não informou os ramais em ligação.";
      } else if (state === "in_call" && !term && !queueFilter.value) {
        countMessage = "Nenhum ramal em ligação identificado nesta coleta.";
      } else countMessage = "Nenhum ramal encontrado para os filtros selecionados.";
    }
    byId("pabx-extension-count").textContent = countMessage;
  }

  function renderQueues(queues) {
    const container = byId("pabx-queues");
    const opened = new Set([...container.querySelectorAll("details[open]")].map((details) => details.dataset.code));
    if (!queues?.length) {
      container.replaceChildren(element("p", queues ? "Nenhuma fila cadastrada." : "Aguardando dados de filas."));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const queue of queues) {
      const details = element("details");
      details.className = "pabx-queue";
      details.dataset.code = queue.code;
      details.open = opened.has(queue.code);
      const summary = element("summary", queue.name);
      const context = element("span", [
        `${formatCount.format(queue.members.length)} membros`,
        queue.strategy, queue.monitored ? "Presente no painel operacional" : "Sem dados no painel operacional",
      ].filter(Boolean).join(" · "));
      context.className = "muted";
      summary.append(" ", context);
      details.append(summary);
      if (queue.members.length) {
        const list = element("ul");
        for (const member of queue.members) {
          const identity = [member.extension, member.name].filter(Boolean).join(" · ") || "Ramal não identificado";
          const row = element("li", `${identity} · `);
          row.append(statusBadge(member.status));
          if (member.in_call) row.append(" · ", statusBadge("in_call"));
          row.append(` · ${pauseLabel(member.paused)}`);
          list.append(row);
        }
        details.append(list);
      }
      fragment.append(details);
    }
    container.replaceChildren(fragment);
  }

  function renderStatus(state, networkError = false) {
    const current = state || {};
    const { last_success: lastSuccess, data, collecting, company } = current;
    const stale = current.stale || (networkError && Boolean(data));
    const code = networkError ? "network" : current.state;
    const message = byId("pabx-message");
    const companyLabel = byId("pabx-company");
    message.closest("section").dataset.state = code;
    message.closest("section").dataset.stale = String(Boolean(stale));
    message.textContent = [
      networkError ? "Falha de comunicação com o servidor do monitor." : messages[code],
      stale ? "Dados desatualizados. Exibindo a última coleta válida." : "",
    ].filter(Boolean).join(" ");
    companyLabel.hidden = !company;
    companyLabel.textContent = company || "";
    byId("pabx-time").textContent = lastSuccess ? formatTime.format(new Date(lastSuccess)) : "—";
    byId("pabx-progress").hidden = !collecting || networkError;
    const renderKey = data ? lastSuccess : "empty";
    if (renderKey === lastRendered) return;
    lastRendered = renderKey;
    for (const indicator of dashboard.querySelectorAll("[data-indicator]")) {
      const value = data?.indicators[indicator.dataset.indicator];
      indicator.textContent = value == null ? "—" : formatCount.format(value);
    }
    options(queueFilter, (data?.queues || []).map((queue) => [queue.code, queue.name]), "Todas as filas");
    renderExtensions();
    renderQueues(data?.queues);
  }

  async function poll() {
    try {
      const response = await fetch(dashboard.dataset.statusUrl, { cache: "no-store", signal: AbortSignal.timeout(10000) });
      if (response.status === 401) {
        window.location.reload();
        return;
      }
      if (!response.ok) throw new Error("unavailable");
      const result = await response.json();
      if (String(result.revision) !== dashboard.dataset.revision) {
        window.location.reload();
        return;
      }
      snapshot = result;
      renderStatus(snapshot);
    } catch {
      renderStatus(snapshot, true);
    }
    window.setTimeout(poll, 1000 * (snapshot?.poll_seconds || 5));
  }

  search.addEventListener("input", renderExtensions);
  stateFilter.addEventListener("change", renderExtensions);
  queueFilter.addEventListener("change", renderExtensions);
  renderExtensions();
  renderQueues();
  poll();

  const historyForm = byId("pabx-history-form");
  if (!historyForm) return;
  const historyMessage = byId("pabx-history-message");
  const historyResults = byId("pabx-history-results");
  const exportButton = byId("pabx-export");
  const previous = byId("pabx-prev");
  const next = byId("pabx-next");
  const { start, end, status, direction } = historyForm.elements;
  const pageSize = 25;
  let historyPage = 1;
  let records = [];
  let completedFilters = null;
  let pending = false;
  let activeRecording = null;

  function dateInput(date) {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
  }
  const today = new Date();
  end.value = dateInput(today);
  today.setDate(today.getDate() - 6);
  start.value = dateInput(today);

  function historyNotice(text, error = false) {
    historyMessage.textContent = text;
    historyMessage.dataset.error = String(error);
  }

  function stopRecording() {
    if (!activeRecording) return;
    const player = activeRecording;
    activeRecording = null;
    player.pause();
    player.removeAttribute("src");
    player.load();
    player.remove();
  }

  function recordingCell(record) {
    if (!record.recording_id) return cell(record.recording ? "Referência disponível" : "");
    const container = element("td");
    const actions = element("div");
    actions.className = "pabx-recording-actions";
    const recordingUrl = dashboard.dataset.recordingUrl.replace("RECORDING_ID", record.recording_id);
    const button = element("button", "Ouvir gravação");
    button.type = "button";
    button.className = "clear-btn";
    button.addEventListener("click", () => {
      if (activeRecording?.parentElement === container) return;
      stopRecording();
      const player = element("audio");
      player.className = "pabx-recording";
      player.controls = true;
      player.preload = "none";
      player.setAttribute("controlslist", "nodownload");
      player.setAttribute("aria-label", "Gravação da chamada");
      player.src = recordingUrl;
      player.addEventListener("error", () => {
        if (activeRecording === player) {
          stopRecording();
          historyNotice("Não foi possível tocar a gravação. Consulte o histórico novamente para atualizar a referência.", true);
        }
      });
      activeRecording = player;
      container.append(player);
      player.play().catch(() => {});
    });

    const download = element("button", "Baixar gravação");
    download.type = "button";
    download.className = "clear-btn";
    download.addEventListener("click", () => {
      const link = element("a");
      link.href = recordingUrl + "?download=1";
      link.download = "";
      link.click();
    });
    actions.append(button, download);
    container.append(actions);
    return container;
  }

  function chart(id, field) {
    const counts = new Map();
    for (const record of records) {
      const name = record[field] || "Não informada";
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    const rows = [...counts].sort((left, right) => right[1] - left[1]).map(([name, count]) => {
      const row = element("div");
      row.className = "pabx-chart-row";
      const bar = element("meter");
      bar.setAttribute("aria-label", name);
      bar.max = records.length;
      bar.value = count;
      row.append(element("span", name), bar, element("strong", formatCount.format(count)));
      return row;
    });
    byId(id).replaceChildren(...rows);
  }

  function renderHistory() {
    stopRecording();
    const pages = Math.ceil(records.length / pageSize);
    historyResults.hidden = !pages;
    const startIndex = (historyPage - 1) * pageSize;
    const tableRows = records.slice(startIndex, startIndex + pageSize).map((row) => {
      const tr = element("tr");
      const id = row.id_occurrences > 1 ? `${row.uniqueid} (${row.id_occurrences} no período)` : row.uniqueid;
      tr.append(...[row.date, id, row.source, row.destination, row.status, row.direction].map(cell), recordingCell(row));
      return tr;
    });
    byId("pabx-records").replaceChildren(...tableRows);
    byId("pabx-page").textContent = pages ? `Página ${historyPage} de ${pages}` : "";
    previous.disabled = historyPage <= 1;
    next.disabled = historyPage >= pages;
  }

  async function historyRequest(url, filters) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(filters),
      cache: "no-store",
      signal: AbortSignal.timeout(65000),
    });
    if (response.status === 401) {
      window.location.reload();
      throw new Error("credentials");
    }
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.error || "unavailable");
    }
    return response;
  }

  function controls(busy) {
    pending = busy;
    for (const input of historyForm.elements) input.disabled = busy;
    exportButton.disabled = busy || !completedFilters || !records.length;
  }

  historyForm.addEventListener("change", (event) => {
    stopRecording();
    if (event.target === start || event.target === end) {
      options(status, [], "Todas");
      options(direction, [], "Todas");
    }
    completedFilters = null;
    exportButton.disabled = true;
    historyResults.hidden = true;
    historyNotice("Consulte para aplicar os filtros selecionados.");
  });

  historyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending) return;
    stopRecording();
    const filters = Object.fromEntries(new FormData(historyForm));
    completedFilters = null;
    historyResults.hidden = true;
    controls(true);
    historyNotice("Consultando registros CDR…");
    try {
      const { records: result, statuses, directions } = await (
        await historyRequest(dashboard.dataset.cdrUrl, filters)
      ).json();
      records = result;
      completedFilters = filters;
      historyPage = 1;
      options(status, statuses.map((name) => [name, name]), "Todas");
      options(direction, directions.map((name) => [name, name]), "Todas");
      renderHistory();
      for (const total of historyResults.querySelectorAll("[data-cdr-status]")) {
        const count = records.filter((row) => row.status.toLocaleLowerCase("pt-BR") === total.dataset.cdrStatus).length;
        total.textContent = formatCount.format(count);
      }
      chart("pabx-status-chart", "status");
      chart("pabx-direction-chart", "direction");
      historyNotice(records.length ?
        `${formatCount.format(records.length)} registros CDR. IDs repetidos são mantidos.` :
        "Nenhum registro retornado para o período e os filtros selecionados.");
    } catch (error) {
      historyNotice(messages[error.message] || "Falha ao consultar o histórico CDR.", true);
    } finally {
      controls(false);
    }
  });

  previous.addEventListener("click", () => { historyPage -= 1; renderHistory(); });
  next.addEventListener("click", () => { historyPage += 1; renderHistory(); });
  exportButton.addEventListener("click", async () => {
    if (pending || !completedFilters) return;
    controls(true);
    try {
      const response = await historyRequest(dashboard.dataset.exportUrl, completedFilters);
      const url = URL.createObjectURL(await response.blob());
      const anchor = element("a");
      anchor.href = url;
      anchor.download = "pabx-cdr.csv";
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      historyNotice(messages[error.message] || "Falha ao exportar os registros CDR.", true);
    } finally {
      controls(false);
    }
  });
})();
