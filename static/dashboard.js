let monitorData = null;
let seconds = 60;
let timer = null;

let activeStatus = "";
let specialView = "";

const groupStatusFilter = new Map();

const statusLabels = {
  connected: "🟢 Conectado",
  connecting: "🟡 Conectando",
  disconnected: "🔴 Desconectado",
  unknown: "⚪ Desconhecido"
};


/* =========================================================
   UTILITÁRIOS
========================================================= */

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


function formatDate(value) {
  if (!value) return "-";

  const d = new Date(value);

  return Number.isNaN(d.getTime())
    ? String(value)
    : d.toLocaleString("pt-BR");
}


function summarize(items) {
  return {
    total: items.length,

    connected:
      items.filter(x => x.status === "connected").length,

    connecting:
      items.filter(x => x.status === "connecting").length,

    disconnected:
      items.filter(x => x.status === "disconnected").length,

    unknown:
      items.filter(x => x.status === "unknown").length
  };
}


/* =========================================================
   FILTRO GLOBAL
========================================================= */

function applyGlobalStatus(status) {

  specialView = "";

  activeStatus = status || "";

  document.getElementById("status-filter").value =
    activeStatus;

  renderAll();

  document
    .getElementById("grouped-instances")
    .scrollIntoView({
      behavior: "smooth",
      block: "start"
    });
}


/* =========================================================
   KPIs
========================================================= */

function renderSummary(summary) {

  const cards = [

    [
      "total",
      "Total",
      summary.total,
      ""
    ],

    [
      "connected",
      "Conectadas",
      summary.connected,
      "connected"
    ],

    [
      "connecting",
      "Conectando",
      summary.connecting,
      "connecting"
    ],

    [
      "disconnected",
      "Desconectadas",
      summary.disconnected,
      "disconnected"
    ],

    [
      "unknown",
      "Desconhecidas",
      summary.unknown,
      "unknown"
    ],

    [
      "api",
      "APIs indisponíveis",
      summary.apis_unavailable,
      "api"
    ]

  ];


  document.getElementById("summary").innerHTML =
    cards.map(
      ([kind, label, value, filter]) => `

        <button
          class="summary-card summary-${kind}"
          type="button"
          data-kpi-filter="${filter}"
        >

          <div class="summary-value">
            ${value}
          </div>

          <div class="summary-label">
            ${label}
          </div>

          <div class="summary-action">
            Ver detalhes →
          </div>

        </button>

      `
    ).join("");


  document
    .querySelectorAll("[data-kpi-filter]")
    .forEach(btn => {

      btn.addEventListener("click", () => {

        const filter =
          btn.dataset.kpiFilter;


        /*
         * API INDISPONÍVEL
         */

        if (filter === "api") {

          showUnavailableApis();

          return;
        }


        /*
         * OUTROS KPIs
         */

        applyGlobalStatus(filter);

      });

    });

}


/* =========================================================
   VISUALIZAÇÃO DE APIs INDISPONÍVEIS
========================================================= */

function showUnavailableApis() {

  if (!monitorData)
    return;


  specialView = "api";

  activeStatus = "";

  document.getElementById("status-filter").value = "";

  const unavailable =
    monitorData.sources.filter(
      source => !source.online
    );


  const container =
    document.getElementById(
      "grouped-instances"
    );


  /*
   * NENHUMA API OFFLINE
   */

  if (!unavailable.length) {

    container.innerHTML = `

      <div class="api-status-empty">

        <div class="api-status-icon">
          ✅
        </div>

        <div class="api-status-title">
          Nenhuma API indisponível
        </div>

        <div class="api-status-text">
          Todas as APIs Evolution configuradas
          responderam normalmente na última consulta.
        </div>

      </div>

    `;


    updateTabsForApiView();


    container.scrollIntoView({
      behavior: "smooth",
      block: "start"
    });


    return;
  }


  /*
   * EXISTE API OFFLINE
   */

  container.innerHTML =
    unavailable.map(source => `

      <section class="api-unavailable-section">

        <div class="api-unavailable-header">

          <div>

            <div class="api-unavailable-title">
              ⚫ ${escapeHtml(source.name)}
            </div>

            <div class="api-unavailable-host">
              ${escapeHtml(source.host || "-")}
            </div>

          </div>

          <div class="api-unavailable-badge">
            API indisponível
          </div>

        </div>


        <div class="api-unavailable-details">

          <div class="api-detail">

            <span class="api-detail-label">
              Status HTTP
            </span>

            <strong>
              ${source.http_status ?? "-"}
            </strong>

          </div>


          <div class="api-detail">

            <span class="api-detail-label">
              Erro
            </span>

            <strong>
              ${escapeHtml(
                source.error ||
                "Sem detalhe informado"
              )}
            </strong>

          </div>


          <div class="api-detail">

            <span class="api-detail-label">
              Tempo da tentativa
            </span>

            <strong>
              ${source.duration_ms ?? "-"} ms
            </strong>

          </div>


          <div class="api-detail">

            <span class="api-detail-label">
              Instâncias retornadas
            </span>

            <strong>
              ${source.count ?? 0}
            </strong>

          </div>

        </div>

      </section>

    `).join("");


  updateTabsForApiView();


  container.scrollIntoView({
    behavior: "smooth",
    block: "start"
  });

}


/* =========================================================
   ABAS DE STATUS
========================================================= */

function renderTabs(summary) {

  const tabs = [

    [
      "",
      `Todos (${summary.total})`
    ],

    [
      "connected",
      `🟢 Conectadas (${summary.connected})`
    ],

    [
      "connecting",
      `🟡 Conectando (${summary.connecting})`
    ],

    [
      "disconnected",
      `🔴 Desconectadas (${summary.disconnected})`
    ],

    [
      "unknown",
      `⚪ Desconhecidas (${summary.unknown})`
    ]

  ];


  document.getElementById("status-tabs").innerHTML =

    tabs.map(
      ([value, label]) => `

        <button
          type="button"
          class="
            status-tab
            ${
              specialView !== "api" &&
              activeStatus === value
                ? "active"
                : ""
            }
          "
          data-tab-status="${value}"
        >

          ${label}

        </button>

      `
    ).join("")

    +

    `

      <button
        class="
          status-tab
          ${specialView === "api"
            ? "active"
            : ""}
        "
        type="button"
        id="api-unavailable-tab"
      >

        ⚫ APIs indisponíveis
        (${summary.apis_unavailable})

      </button>

    `;


  document
    .querySelectorAll("[data-tab-status]")
    .forEach(btn => {

      btn.addEventListener(
        "click",
        () => {

          applyGlobalStatus(
            btn.dataset.tabStatus
          );

        }
      );

    });


  const apiTab =
    document.getElementById(
      "api-unavailable-tab"
    );


  if (apiTab) {

    apiTab.addEventListener(
      "click",
      showUnavailableApis
    );

  }

}


/*
 * Apenas atualiza o destaque
 * da aba quando estamos olhando APIs.
 */

function updateTabsForApiView() {

  if (!monitorData)
    return;

  renderTabs(
    monitorData.summary
  );

}


/* =========================================================
   FILTRO DE FONTES
========================================================= */

function populateSources(sources) {

  const select =
    document.getElementById(
      "source-filter"
    );


  const current =
    select.value;


  select.innerHTML =

    '<option value="">Todas as APIs</option>'

    +

    sources.map(
      source => `

        <option value="${escapeHtml(source.name)}">

          ${escapeHtml(source.name)}

        </option>

      `
    ).join("");


  select.value =
    current;

}


/* =========================================================
   FILTROS GERAIS
========================================================= */

function matchesGlobalFilters(item) {

  const q =
    document
      .getElementById("search")
      .value
      .trim()
      .toLowerCase();


  const source =
    document
      .getElementById(
        "source-filter"
      )
      .value;


  const status =
    activeStatus ||
    document
      .getElementById(
        "status-filter"
      )
      .value;


  const haystack = [

    item.source,

    item.client,

    item.inbox,

    item.instance,

    item.number,

    item.profile

  ]
    .join(" ")
    .toLowerCase();


  return (

    (!q ||
      haystack.includes(q))

    &&

    (!source ||
      item.source === source)

    &&

    (!status ||
      item.status === status)

  );

}


/* =========================================================
   IDENTIFICAÇÃO DO GRUPO
========================================================= */

function groupKey(
  source,
  client
) {

  return `${source}|||${client}`;

}


/* =========================================================
   LINHAS DAS INSTÂNCIAS
========================================================= */

function renderRows(
  items,
  localStatus
) {

  const rows =
    localStatus
      ? items.filter(
          x =>
            x.status ===
            localStatus
        )
      : items;


  if (!rows.length) {

    return `

      <div class="no-group-results">

        Nenhuma instância
        neste status.

      </div>

    `;

  }


  return `

    <div class="client-table-wrap">

      <table class="client-table">

        <thead>

          <tr>

            <th>
              Caixa / Instância
            </th>

            <th>
              Número / Perfil
            </th>

            <th>
              Status / Código
            </th>

            <th>
              Última desconexão
            </th>

          </tr>

        </thead>


        <tbody>

          ${rows.map(item => `

            <tr>

              <td>

                <div class="primary-cell">
                  ${escapeHtml(
                    item.inbox || "-"
                  )}
                </div>

                <div class="secondary-cell">
                  ${escapeHtml(
                    item.instance || "-"
                  )}
                </div>

              </td>


              <td>

                <div class="primary-cell">
                  ${escapeHtml(
                    item.number || "-"
                  )}
                </div>

                <div class="secondary-cell">
                  ${escapeHtml(
                    item.profile || "-"
                  )}
                </div>

              </td>


              <td>

                <div class="status ${item.status}">

                  ${
                    statusLabels[
                      item.status
                    ]
                    ||
                    "⚪ Desconhecido"
                  }

                </div>


                <div class="secondary-cell">

                  ${
                    item.disconnect_code !== null &&
                    item.disconnect_code !== undefined

                      ?

                      `Último código:
                      ${escapeHtml(
                        item.disconnect_code
                      )}`

                      :

                      "Sem código de queda"
                  }

                </div>

              </td>


              <td>

                ${escapeHtml(
                  formatDate(
                    item.last_disconnect
                  )
                )}

              </td>

            </tr>

          `).join("")}

        </tbody>

      </table>

    </div>

  `;

}


/* =========================================================
   CLIENTES / INSTÂNCIAS
========================================================= */

function renderGroupedInstances() {

  if (!monitorData)
    return;


  specialView = "";


  const container =
    document.getElementById(
      "grouped-instances"
    );


  const filtered =
    monitorData.instances.filter(
      matchesGlobalFilters
    );


  if (!filtered.length) {

    container.innerHTML = `

      <div class="empty-card">

        Nenhuma instância encontrada
        com os filtros atuais.

      </div>

    `;

    return;

  }


  const bySource =
    new Map();


  for (const item of filtered) {

    if (
      !bySource.has(
        item.source
      )
    ) {

      bySource.set(
        item.source,
        new Map()
      );

    }


    const byClient =
      bySource.get(
        item.source
      );


    const client =
      item.client?.trim()
      ||
      "Cliente não identificado";


    if (
      !byClient.has(
        client
      )
    ) {

      byClient.set(
        client,
        []
      );

    }


    byClient
      .get(client)
      .push(item);

  }


  container.innerHTML =

    [...bySource.entries()]
      .map(
        ([sourceName, clients]) => {


          const sourceItems =
            [...clients.values()]
              .flat();


          const sourceSummary =
            summarize(
              sourceItems
            );


          const clientsHtml =

            [...clients.entries()]

              .sort(
                ([a], [b]) =>
                  a.localeCompare(
                    b,
                    "pt-BR"
                  )
              )

              .map(
                ([clientName, items]) => {


                  const s =
                    summarize(items);


                  const key =
                    groupKey(
                      sourceName,
                      clientName
                    );


                  const localStatus =
                    groupStatusFilter.get(
                      key
                    )
                    ||
                    "";


                  return `

                    <details
                      class="client-card"
                      data-group-key="${escapeHtml(key)}"
                    >

                      <summary>


                        <div class="client-summary-left">

                          <div class="client-title">

                            ${escapeHtml(
                              clientName
                            )}

                          </div>


                          <div class="client-meta">

                            ${s.total}
                            WhatsApp${s.total === 1 ? "" : "s"}

                          </div>

                        </div>


                        <div class="status-dots">


                          <button
                            type="button"
                            class="
                              dot-filter
                              ${localStatus === "connected"
                                ? "selected"
                                : ""}
                            "
                            data-group-status="connected"
                            title="Mostrar somente conectados"
                          >

                            🟢 ${s.connected}

                          </button>


                          <button
                            type="button"
                            class="
                              dot-filter
                              ${localStatus === "connecting"
                                ? "selected"
                                : ""}
                            "
                            data-group-status="connecting"
                            title="Mostrar somente conectando"
                          >

                            🟡 ${s.connecting}

                          </button>


                          <button
                            type="button"
                            class="
                              dot-filter
                              ${localStatus === "disconnected"
                                ? "selected"
                                : ""}
                            "
                            data-group-status="disconnected"
                            title="Mostrar somente desconectados"
                          >

                            🔴 ${s.disconnected}

                          </button>


                          <button
                            type="button"
                            class="
                              dot-filter
                              ${localStatus === "unknown"
                                ? "selected"
                                : ""}
                            "
                            data-group-status="unknown"
                            title="Mostrar somente desconhecidos"
                          >

                            ⚪ ${s.unknown}

                          </button>


                          <span class="chevron">
                            ⌄
                          </span>


                        </div>

                      </summary>


                      <div class="group-details">

                        ${renderRows(
                          items,
                          localStatus
                        )}

                      </div>


                    </details>

                  `;

                }
              )
              .join("");


          return `

            <section class="source-section">


              <div class="source-section-head">

                <div>

                  <h3>

                    ${escapeHtml(
                      sourceName
                    )}

                  </h3>


                  <div class="source-meta">

                    ${sourceSummary.total}
                    WhatsApps

                    · 🟢
                    ${sourceSummary.connected}

                    · 🟡
                    ${sourceSummary.connecting}

                    · 🔴
                    ${sourceSummary.disconnected}

                    · ⚪
                    ${sourceSummary.unknown}

                  </div>

                </div>

              </div>


              <div class="clients-list">

                ${clientsHtml}

              </div>


            </section>

          `;

        }
      )
      .join("");


  /*
   * CLIQUE NAS BOLINHAS
   */

  document
    .querySelectorAll(
      ".dot-filter"
    )
    .forEach(btn => {

      btn.addEventListener(
        "click",
        event => {


          event.preventDefault();

          event.stopPropagation();


          const details =
            btn.closest(
              ".client-card"
            );


          const key =
            details.dataset.groupKey;


          const clickedStatus =
            btn.dataset.groupStatus;


          const currentStatus =
            groupStatusFilter.get(
              key
            )
            ||
            "";


          /*
           * CLICOU DE NOVO
           * NA MESMA BOLINHA:
           * REMOVE FILTRO.
           */

          groupStatusFilter.set(

            key,

            currentStatus ===
              clickedStatus

              ?

              ""

              :

              clickedStatus

          );


          renderGroupedInstances();


          /*
           * REABRE O CLIENTE
           */

          const reopened =

            [...document.querySelectorAll(
              ".client-card"
            )]

              .find(
                el =>
                  el.dataset.groupKey ===
                  key
              );


          if (reopened) {

            reopened.open =
              true;

          }

        }
      );

    });

}


/* =========================================================
   RENDERIZAÇÃO GERAL
========================================================= */

function renderAll() {

  if (!monitorData)
    return;


  renderSummary(
    monitorData.summary
  );


  renderTabs(
    monitorData.summary
  );


  populateSources(
    monitorData.sources
  );


  renderGroupedInstances();

}


/* =========================================================
   CONSULTA DAS APIs
========================================================= */

async function refreshData() {

  const btn =
    document.getElementById(
      "refresh-btn"
    );


  btn.disabled = true;

  btn.textContent =
    "Atualizando...";


  try {

    const response =
      await fetch(
        "/api/status",
        {

          method: "GET",

          credentials:
            "same-origin",

          cache:
            "no-store"

        }
      );


    if (
      response.status === 401
    ) {

      window.location.href =
        "/login";

      return;

    }


    if (!response.ok) {

      throw new Error(
        `HTTP ${response.status}`
      );

    }


    monitorData =
      await response.json();


    renderAll();


    document
      .getElementById(
        "last-update"
      )
      .textContent =

        new Date(
          monitorData.generated_at
          *
          1000
        )
        .toLocaleTimeString(
          "pt-BR"
        );


    resetCountdown();

  }

  catch (error) {

    document
      .getElementById(
        "last-update"
      )
      .textContent =
        "Falha na consulta";


    console.error(
      "Falha ao atualizar dashboard."
    );

  }

  finally {

    btn.disabled =
      false;


    btn.textContent =
      "↻ Atualizar agora";

  }

}


/* =========================================================
   CONTADOR DE 60 SEGUNDOS
========================================================= */

function resetCountdown() {

  seconds = 60;

  document
    .getElementById(
      "countdown"
    )
    .textContent =
      seconds;

}


function startTimer() {

  clearInterval(
    timer
  );


  timer =
    setInterval(
      () => {


        seconds -= 1;


        document
          .getElementById(
            "countdown"
          )
          .textContent =

            Math.max(
              seconds,
              0
            );


        if (
          seconds <= 0
        ) {

          refreshData();

        }

      },

      1000

    );

}


/* =========================================================
   EVENTOS
========================================================= */

document
  .getElementById(
    "refresh-btn"
  )
  .addEventListener(
    "click",
    refreshData
  );


document
  .getElementById(
    "search"
  )
  .addEventListener(
    "input",
    () => {

      specialView = "";

      renderGroupedInstances();

    }
  );


document
  .getElementById(
    "status-filter"
  )
  .addEventListener(
    "change",
    event => {

      specialView = "";

      activeStatus =
        event.target.value;

      renderAll();

    }
  );


document
  .getElementById(
    "source-filter"
  )
  .addEventListener(
    "change",
    () => {

      specialView = "";

      renderGroupedInstances();

    }
  );


document
  .getElementById(
    "clear-filters"
  )
  .addEventListener(
    "click",
    () => {


      document
        .getElementById(
          "search"
        )
        .value =
          "";


      document
        .getElementById(
          "status-filter"
        )
        .value =
          "";


      document
        .getElementById(
          "source-filter"
        )
        .value =
          "";


      activeStatus =
        "";


      specialView =
        "";


      groupStatusFilter.clear();


      renderAll();

    }
  );


/* =========================================================
   INICIALIZAÇÃO
========================================================= */

refreshData();

startTimer();