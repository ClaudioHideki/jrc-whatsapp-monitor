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
  const stateTones = {
    available: "green", busy: "orange", ringing: "amber", in_call: "cyan",
    registered: "blue", unregistered: "red", unknown: "neutral",
  };
  const messages = {
    connected: "API conectada.",
    starting: "Aguardando a primeira coleta.",
    disabled: "Monitoramento desativado. Edite a configuração para ativar.",
    scope_pending: "Aguardando a confirmação do escopo operacional.",
    http_origin: "Revise o perfil e a origem HTTP autorizada no servidor do monitor.",
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
    recording_missing: "Consulte o histórico novamente para atualizar as referências das gravações.",
    recording_selection: "A seleção de gravações para o ZIP é inválida.",
    recording_busy: "Há downloads ou reprodução em andamento. Aguarde para tentar novamente.",
    csrf: "A sessão do formulário expirou. Recarregue a página.",
  };
  const search = byId("pabx-search");
  const stateFilters = [...dashboard.querySelectorAll("[data-extension-filter]")];
  const queueFilter = byId("pabx-queue");
  const extensionPrevious = byId("pabx-extension-prev");
  const extensionNext = byId("pabx-extension-next");
  const extensionsPerPage = 10;
  let extensionState = "";
  let extensionPage = 1;
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
    badge.dataset.tone = stateTones[state] || stateTones.unknown;
    return badge;
  }

  function pauseLabel(paused) {
    if (paused === true) return "Pausado";
    if (paused === false) return "Não pausado";
    return "Pausa não informada";
  }

  function renderDistribution(id, categories, total) {
    const container = byId(id);
    if (!total) {
      const empty = element("p", total === 0 ? "Nenhum ramal encontrado." : "Aguardando dados de ramais.");
      empty.className = "muted";
      container.replaceChildren(empty);
      return;
    }

    const bar = element("div");
    bar.className = "pabx-distribution";
    bar.setAttribute("aria-hidden", "true");
    const legend = element("dl");
    legend.className = "pabx-chart-legend";
    for (const [label, tone, count] of categories) {
      if (count > 0) {
        const segment = element("span");
        segment.dataset.tone = tone;
        segment.style.flexGrow = count;
        bar.append(segment);
      }
      const row = element("div");
      row.dataset.tone = tone;
      row.append(element("dt", label), element("dd", formatCount.format(count)));
      legend.append(row);
    }
    container.replaceChildren(bar, legend);
  }

  function renderOperationalCharts(extensions) {
    const states = new Map();
    let registered = 0;
    let unregistered = 0;
    for (const row of extensions || []) {
      states.set(row.status, (states.get(row.status) || 0) + 1);
      if (row.registered === true) registered += 1;
      if (row.registered === false) unregistered += 1;
    }
    const total = extensions?.length;
    const categories = [
      ["available", "Livres", "green"], ["busy", "Ocupados", "orange"],
      ["ringing", "Tocando", "amber"], ["registered", "Registrados sem estado de chamada", "cyan"],
      ["unregistered", "Não registrados", "red"], ["unknown", "Estado indefinido", "neutral"],
    ];
    renderDistribution("pabx-extension-chart", categories.map(([key, label, tone]) => [label, tone, states.get(key) || 0]), total);
    renderDistribution("pabx-registration-chart", [
      ["Registrados", "green", registered], ["Não registrados", "red", unregistered],
      ["Registro não informado", "neutral", (total || 0) - registered - unregistered],
    ], total);
  }

  function extensionQueues(queues, opened) {
    if (!queues.length) return cell("");
    const td = element("td");
    const details = element("details");
    details.open = opened;
    const summary = element("summary");
    const preview = element("span");
    preview.className = "pabx-extension-queue-preview";
    const firstName = element("span", queues[0].name);
    firstName.title = queues[0].name;
    preview.append(firstName);
    if (queues.length > 1) {
      const extra = element("small", `+${formatCount.format(queues.length - 1)}`);
      preview.append(extra);
    }
    summary.append(preview);
    const list = element("ul");
    for (const queue of queues) list.append(element("li", `${queue.name} (${pauseLabel(queue.paused)})`));
    details.append(summary, list);
    td.append(details);
    return td;
  }

  function matchesExtensionState(row, state) {
    if (state === "available") return row.status === state && row.in_call !== true;
    if (state === "in_call") return row.in_call === true;
    if (state === "registered") return row.registered === true;
    return !state || row.status === state;
  }

  function renderExtensions() {
    const term = search.value.trim().toLocaleLowerCase("pt-BR");
    const extensions = snapshot?.data?.extensions;
    const matching = (extensions || []).filter((row) =>
      (!queueFilter.value || row.queues.some((queue) => queue.code === queueFilter.value)) &&
      (!term || `${row.extension} ${row.name}`.toLocaleLowerCase("pt-BR").includes(term)));
    for (const button of stateFilters) {
      const state = button.dataset.extensionFilter;
      const known = extensions && (state !== "in_call" || snapshot.data.indicators.extensions_in_call != null);
      button.setAttribute("aria-pressed", String(state === extensionState));
      button.querySelector("span").textContent = known
        ? formatCount.format(matching.filter(row => matchesExtensionState(row, state)).length) : "—";
    }
    const rows = matching.filter(row => matchesExtensionState(row, extensionState));
    rows.sort((a, b) => Number(a.status === "unregistered") - Number(b.status === "unregistered"));
    const pageCount = Math.ceil(rows.length / extensionsPerPage);
    extensionPage = Math.min(extensionPage, pageCount || 1);
    const start = (extensionPage - 1) * extensionsPerPage;
    const visibleRows = rows.slice(start, start + extensionsPerPage);
    const tbody = byId("pabx-extensions");
    const opened = new Set([...tbody.querySelectorAll("details[open]")].map((details) => details.closest("tr").dataset.extension));
    const fragment = document.createDocumentFragment();
    for (const row of visibleRows) {
      const tr = element("tr");
      tr.dataset.extension = JSON.stringify([row.extension, row.name]);
      const stateCell = element("td");
      stateCell.append(statusBadge(row.in_call ? "in_call" : row.status));
      if (row.in_call && row.status === "ringing") stateCell.append(statusBadge("ringing"));
      const registration = row.registered == null ? "" : row.registered ? "Registrado" : "Não registrado";
      tr.append(cell(row.extension), cell(row.name || "Nome não informado"), stateCell, cell(registration),
        extensionQueues(row.queues, opened.has(tr.dataset.extension)));
      fragment.append(tr);
    }
    tbody.replaceChildren(fragment);
    let countMessage = "Aguardando dados de ramais.";
    if (extensions) {
      if (rows.length) {
        const count = formatCount.format(rows.length);
        countMessage = pageCount > 1
          ? `${formatCount.format(start + 1)}–${formatCount.format(start + visibleRows.length)} de ${count} ramais.`
          : `${count} ${rows.length === 1 ? "ramal encontrado" : "ramais encontrados"}.`;
      } else if (extensionState === "in_call" && snapshot.data.indicators.extensions_in_call == null) {
        countMessage = "A API ainda não informou os ramais em ligação.";
      } else if (extensionState === "in_call" && !term && !queueFilter.value) {
        countMessage = "Nenhum ramal em ligação identificado nesta coleta.";
      } else countMessage = "Nenhum ramal encontrado para os filtros selecionados.";
    }
    byId("pabx-extension-count").textContent = countMessage;
    byId("pabx-extension-pagination").hidden = pageCount < 2;
    byId("pabx-extension-page").textContent = pageCount > 1 ? `Página ${extensionPage} de ${pageCount}` : "";
    extensionPrevious.disabled = extensionPage <= 1;
    extensionNext.disabled = extensionPage >= pageCount;
  }

  function queueMembers(members) {
    const list = element("ul");
    list.className = "pabx-queue-members";
    for (const member of members) {
      const row = element("li");
      const name = element("strong", member.name || "Nome não informado");
      const meta = element("div");
      meta.className = "pabx-queue-member-meta";
      meta.append(element("span", member.extension ? `Ramal ${member.extension}` : "Ramal não identificado"));
      meta.append(statusBadge(member.status));
      if (member.in_call) meta.append(statusBadge("in_call"));
      meta.append(element("span", pauseLabel(member.paused)));
      row.append(name, meta);
      list.append(row);
    }
    return list;
  }

  function renderQueues(queues) {
    const container = byId("pabx-queues");
    const opened = new Set([...container.querySelectorAll("details[open]")].map((details) => details.dataset.code));
    if (!queues?.length) {
      container.replaceChildren(element("p", queues ? "Nenhuma fila cadastrada." : "Aguardando dados de filas."));
      return;
    }
    const fragment = document.createDocumentFragment();
    const visibleMembers = 4;
    for (const queue of queues) {
      const card = element("section");
      card.className = "pabx-queue";
      const count = queue.members.length;
      const context = element("p", [
        `${formatCount.format(count)} ${count === 1 ? "membro" : "membros"}`,
        queue.strategy, queue.monitored ? "Presente no painel operacional" : "Sem dados no painel operacional",
      ].filter(Boolean).join(" · "));
      context.className = "muted";
      card.append(element("h3", queue.name), context);
      if (count) card.append(queueMembers(queue.members.slice(0, visibleMembers)));
      if (count > visibleMembers) {
        const details = element("details");
        details.dataset.code = queue.code;
        details.open = opened.has(queue.code);
        const remaining = count - visibleMembers;
        details.append(
          element("summary", `Ver mais ${formatCount.format(remaining)} ${remaining === 1 ? "membro" : "membros"}`),
          queueMembers(queue.members.slice(visibleMembers)),
        );
        card.append(details);
      }
      fragment.append(card);
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
    renderOperationalCharts(data?.extensions);
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

  function filterExtensions() {
    extensionPage = 1;
    renderExtensions();
  }

  search.addEventListener("input", filterExtensions);
  for (const button of stateFilters) {
    button.addEventListener("click", () => {
      extensionState = button.dataset.extensionFilter;
      filterExtensions();
    });
  }
  queueFilter.addEventListener("change", filterExtensions);
  extensionPrevious.addEventListener("click", () => { extensionPage -= 1; renderExtensions(); });
  extensionNext.addEventListener("click", () => { extensionPage += 1; renderExtensions(); });
  renderExtensions();
  renderQueues();
  poll();

  const historyForm = byId("pabx-history-form");
  if (!historyForm) return;
  const historyMessage = byId("pabx-history-message");
  const historyResults = byId("pabx-history-results");
  const exportButton = byId("pabx-export");
  const archiveButton = byId("pabx-recordings-zip");
  const archiveFrame = byId("pabx-recordings-download");
  const previous = byId("pabx-prev");
  const next = byId("pabx-next");
  const { start, end, status, direction, recording_only: recordingOnly } = historyForm.elements;
  const pageSize = 25;
  let historyPage = 1;
  let queriedRecords = [];
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

  function applyRecordingFilter() {
    records = recordingOnly.checked ? queriedRecords.filter((row) => row.recording) : queriedRecords;
    historyPage = 1;
    renderHistory();
    for (const total of historyResults.querySelectorAll("[data-cdr-status]")) {
      const count = records.filter((row) => row.status.toLocaleLowerCase("pt-BR") === total.dataset.cdrStatus).length;
      total.textContent = formatCount.format(count);
    }
    chart("pabx-status-chart", "status");
    chart("pabx-direction-chart", "direction");
    const recording = recordingOnly.checked ? " com gravação" : "";
    const emptyResult = recordingOnly.checked ? "com gravação" : "retornado";
    historyNotice(records.length ?
      `${formatCount.format(records.length)} registros CDR${recording}. IDs repetidos são mantidos.` :
      `Nenhum registro ${emptyResult} para o período e os filtros selecionados.`);
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
    updateDownloadButtons();
  }

  function updateDownloadButtons() {
    exportButton.disabled = pending || !completedFilters || !records.length;
    archiveButton.disabled = pending || !completedFilters || !records.some((row) => row.recording_id);
  }

  historyForm.addEventListener("change", (event) => {
    if (event.target === recordingOnly && completedFilters) {
      applyRecordingFilter();
      updateDownloadButtons();
      return;
    }
    stopRecording();
    if (event.target === start || event.target === end) {
      options(status, [], "Todas");
      options(direction, [], "Todas");
    }
    completedFilters = null;
    updateDownloadButtons();
    historyResults.hidden = true;
    historyNotice("Consulte para aplicar os filtros selecionados.");
  });

  historyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending) return;
    stopRecording();
    const formData = new FormData(historyForm);
    formData.delete("recording_only");
    const filters = Object.fromEntries(formData);
    completedFilters = null;
    historyResults.hidden = true;
    controls(true);
    historyNotice("Consultando registros CDR…");
    try {
      const { records: result, statuses, directions } = await (
        await historyRequest(dashboard.dataset.cdrUrl, filters)
      ).json();
      queriedRecords = result;
      completedFilters = filters;
      options(status, statuses.map((name) => [name, name]), "Todas");
      options(direction, directions.map((name) => [name, name]), "Todas");
      applyRecordingFilter();
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
      const response = await historyRequest(dashboard.dataset.exportUrl, {
        ...completedFilters, recording_only: recordingOnly.checked,
      });
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

  archiveFrame.addEventListener("load", () => {
    try {
      const error = JSON.parse(archiveFrame.contentDocument?.body?.textContent || "{}").error;
      if (error) historyNotice(messages[error] || "Falha ao baixar o ZIP de gravações.", true);
    } catch {
      // A resposta de download não altera o conteúdo do quadro.
    }
  });

  archiveButton.addEventListener("click", () => {
    if (archiveButton.disabled) return;
    const ids = [...new Set(records.map((row) => row.recording_id).filter(Boolean))];
    const form = element("form");
    form.method = "post";
    form.action = dashboard.dataset.recordingsZipUrl;
    form.target = archiveFrame.name;
    const fields = [["csrf", dashboard.dataset.csrf], ...ids.map((id) => ["recording_id", id])];
    for (const [name, value] of fields) {
      const input = element("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.append(input);
    }
    document.body.append(form);
    form.submit();
    form.remove();
  });
})();
