// =====================================================================
// CQA — сайт проверки качества диалогов. Чистый JavaScript, без библиотек.
// Данные берутся из web/data/dialogs_<набор>.json (их создаёт evaluate.py).
// Если сайт запущен через server.py, работают живые функции: оценка нового диалога,
// загрузка набора из API и редактор чек-листа. Апелляции хранятся в localStorage браузера.
// =====================================================================

const SETS = ["live", "dev", "prod", "swap", "demo", "demo-prod"];   // какие наборы пробуем загрузить
const SET_NAMES = { live: "Живая оценка", dev: "DEV", prod: "PROD", swap: "SWAP", demo: "DEMO", "demo-prod": "PROD (пример)" };
const LANGUAGES = { ru: "Русский", kk: "Қазақша", en: "English" };
const LANG_CODES = { ru: "RU", kk: "KZ", en: "EN" };
const ROLES = { client: "Клиент", operator: "Оператор", bot: "Бот", event: "Событие" };

let data = null;          // загруженный набор: { set, model, checklist, dialogs }
let available = {};       // какие наборы нашлись: { dev: данные, ... }
let CRITERIA = [];        // рабочий чек-лист: по нему сайт считает баллы (можно менять в разделе «Чек-листы»)
let SERVER = null;        // ответ server.py (/api/status) или null, если сайт открыт без сервера
let filters = { onlyCritical: false, onlyErrors: false, language: "all", channel: "all", search: "" };
let appealTab = "all";

// ---------- Хранилище (localStorage может быть недоступен — тогда просто не сохраняем) ----------
function loadStored(key, fallback) {
  try {
    const text = localStorage.getItem(key);
    return text ? JSON.parse(text) : fallback;
  } catch (e) {
    return fallback;
  }
}
function saveStored(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ничего */ }
}
let appeals = loadStored("cqa_appeals", []);       // список апелляций
let confirmed = loadStored("cqa_confirmed", {});   // "набор:id" -> оценку подтвердил человек

// =====================================================================
// Формула балла — ТА ЖЕ, что в scoring.py
// =====================================================================
// Доля выполнения: Да = 1, Нет = 0, число делим на максимум шкалы (консультация /20, ведение /10)
function criterionShare(c, value) {
  const numbers = c.values.filter((v) => typeof v === "number");
  if (numbers.length) return value / Math.max(...numbers);
  return value === true ? 1 : 0;
}

function calcScore(values, criteria = CRITERIA) {
  let total = 0;
  let totalWeight = 0;
  for (const c of criteria) {
    const value = values[c.id];
    if (c.weight === 0) continue;           // грубые ошибки (вес 0) в сумму не входят
    if (value === null || value === undefined) continue;   // N/A — вес "растворяется"
    total += c.weight * criterionShare(c, value);
    totalWeight += c.weight;
  }
  let finalScore = 0;
  if (totalWeight > 0) {
    finalScore = Math.floor(total / totalWeight * 100 * 100 + 0.5) / 100;  // как в Python
  }
  return { final_score: finalScore, critical_flag: values.critical === true };
}

// =====================================================================
// Небольшие помощники
// =====================================================================
function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
// Иконка из набора в index.html: icon("chat") -> <svg><use href="#i-chat"></svg>
function icon(name) {
  return `<svg class="i"><use href="#i-${name}"></use></svg>`;
}
function channelHtml(d) {
  return d.channel === "call"
    ? `<span class="channel call">${icon("call")}Звонок</span>`
    : `<span class="channel chat">${icon("chat")}Чат</span>`;
}
function langHtml(code) {
  return `<span class="lang lang-${code}">${LANG_CODES[code] || esc(code)}</span>${LANGUAGES[code] || ""}`;
}
function fmtNum(x) {
  return Number(x).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}
function fmtTime(seconds) {
  seconds = Math.floor(seconds);
  return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
}
function turnTime(turn) {
  if (turn.start !== null && turn.start !== undefined) return fmtTime(turn.start);
  if (turn.time_label && turn.time_label.includes("T")) return turn.time_label.split("T")[1].slice(0, 8);
  return turn.time_label || "";
}
function valueLabel(value) {
  if (value === true) return "Да";
  if (value === false) return "Нет";
  if (value === null || value === undefined) return "N/A";
  return String(value);
}
function scoreClass(score) {
  if (score >= 80) return "good";
  if (score >= 60) return "mid";
  return "bad";
}
function shareClass(share) {
  if (share >= 1) return "good";
  if (share >= 0.5) return "mid";
  return "bad";
}
function shareColor(share) {
  return { good: "var(--green)", mid: "var(--yellow)", bad: "var(--red)" }[shareClass(share)];
}
function criterionById(id) {
  return CRITERIA.find((c) => c.id === id);
}
function findDialog(id) {
  return data.dialogs.find((d) => String(d.conversation_id) === String(id));
}
function isOk(dialog) {
  return dialog.result.status === "ok";
}
function showToast(text) {
  const toast = document.getElementById("toast");
  toast.textContent = text;
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 2600);
}

// Значения критериев, которые поставил ИИ
function aiValues(dialog) {
  const values = {};
  for (const c of CRITERIA) values[c.id] = null;     // критерий, который ИИ не оценивал, = N/A
  for (const c of dialog.result.criteria) values[c.criterion_id] = c.value;
  return values;
}
// Финальные значения: оценка ИИ + решения человека по апелляциям
function finalValues(dialog) {
  const values = aiValues(dialog);
  for (const a of appeals) {
    if (a.set === data.set && String(a.conversation_id) === String(dialog.conversation_id) && a.status === "done") {
      values[a.criterion_id] = a.final_value;
    }
  }
  return values;
}
function dialogAppeals(dialog) {
  return appeals.filter((a) => a.set === data.set && String(a.conversation_id) === String(dialog.conversation_id));
}
function reviewStatus(dialog) {
  const list = dialogAppeals(dialog);
  if (list.some((a) => a.status === "new")) return { text: "Апелляция", cls: "mid" };
  if (list.length > 0 || confirmed[data.set + ":" + dialog.conversation_id]) return { text: "Проверен человеком", cls: "good" };
  return { text: "Не проверен", cls: "na" };
}

// =====================================================================
// Загрузка данных и переключение разделов
// =====================================================================
async function start() {
  // запущен ли сайт через server.py (тогда работают живые функции)
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (response.ok) SERVER = await response.json();
  } catch (e) { SERVER = null; }
  await loadChecklist();
  await loadSets();
  setupModal();
  setupNewDialogModal();
  document.getElementById("set-select").addEventListener("change", (event) => {
    saveStored("cqa_set", event.target.value);
    useSet(event.target.value);
    location.hash = "#dialogs";
    render();
  });
  window.addEventListener("hashchange", render);
  render();
}

// Рабочий чек-лист: с сервера или из файла checklist.json (если его ещё нет - из набора данных)
let ACTIVE_CHECKLIST = null;
let OFFICIAL_CHECKLIST = null;
async function loadChecklist() {
  try {
    const response = await fetch(SERVER ? "/api/checklist" : "../checklist.json", { cache: "no-store" });
    if (response.ok) {
      const json = await response.json();
      ACTIVE_CHECKLIST = json.criteria;
      OFFICIAL_CHECKLIST = json.official || null;
    }
  } catch (e) { ACTIVE_CHECKLIST = null; }
}

// Загрузить все наборы, которые есть в web/data/
async function loadSets(preferred) {
  available = {};
  for (const set of SETS) {
    try {
      const response = await fetch("data/dialogs_" + set + ".json", { cache: "no-store" });
      if (response.ok) available[set] = await response.json();
    } catch (e) { /* такого набора нет */ }
  }
  if (SERVER && !available.live) {
    // пустой набор для живой оценки, чтобы было куда добавлять новые диалоги
    available.live = { set: "live", model: { name: SERVER.model }, checklist: ACTIVE_CHECKLIST || [], dialogs: [] };
  }
  const select = document.getElementById("set-select");
  const names = SETS.filter((n) => available[n]);
  if (names.length === 0) {
    document.getElementById("app").innerHTML =
      '<div class="card error-box"><h2>Нет данных</h2>Запустите сайт двойным кликом по <b>«Запустить сайт.command»</b> ' +
      "или командой <b>python3 server.py</b>.</div>";
    return;
  }
  select.innerHTML = names.map((n) =>
    `<option value="${n}">${SET_NAMES[n] || n} · ${available[n].dialogs.length} диалогов</option>`).join("");
  const saved = preferred || loadStored("cqa_set", null);
  // по умолчанию открываем набор, где есть диалоги
  const firstFull = names.find((n) => available[n].dialogs.length) || names[0];
  select.value = names.includes(saved) && available[saved].dialogs.length ? saved : firstFull;
  if (preferred && names.includes(preferred)) select.value = preferred;
  saveStored("cqa_set", select.value);
  useSet(select.value);
}

function useSet(name) {
  data = available[name];
  CRITERIA = ACTIVE_CHECKLIST || data.checklist;
  if (!OFFICIAL_CHECKLIST) OFFICIAL_CHECKLIST = data.checklist;
  document.getElementById("model-name").textContent = "Модель: " + data.model.name;
  checkFormula();
}

// Проверяем, что формула сайта даёт тот же балл, что Python
let formulaCheck = { same: 0, total: 0 };
function checkFormula() {
  formulaCheck = { same: 0, total: 0 };
  if (data.set === "live") return;   // живые оценки могли считаться по старому чек-листу
  for (const d of data.dialogs.filter(isOk)) {
    formulaCheck.total++;
    // сравниваем с Python по тому чек-листу, по которому Python считал (из гайда)
    if (calcScore(aiValues(d), data.checklist).final_score === d.result.score.final_score) formulaCheck.same++;
    else console.warn("Балл не совпал с Python:", d.conversation_id);
  }
}

function render() {
  // останавливаем звук, если уходим со страницы звонка
  document.querySelectorAll("audio").forEach((a) => a.pause());
  stopPlayhead();

  const hash = decodeURIComponent(location.hash.slice(1)) || "dialogs";
  const slash = hash.indexOf("/");
  const view = slash === -1 ? hash : hash.slice(0, slash);
  const param = slash === -1 ? null : hash.slice(slash + 1);

  const menuView = { dialog: "dialogs", appeal: "appeals" }[view] || view;
  document.querySelectorAll(".menu a").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === menuView));
  updateAppealBadge();

  if (view === "dialog") renderDialog(param);
  else if (view === "checklist") renderChecklist();
  else if (view === "appeals" || view === "appeal") renderAppeals(param);
  else if (view === "analytics") renderAnalytics();
  else renderDialogs();
  window.scrollTo(0, 0);
}

function updateAppealBadge() {
  const open = appeals.filter((a) => a.status === "new").length;
  document.getElementById("appeals-count").textContent = open || "";
}

// =====================================================================
// 1. Список диалогов
// =====================================================================
function renderDialogs() {
  const app = document.getElementById("app");
  const ok = data.dialogs.filter(isOk);
  const scores = ok.map((d) => calcScore(finalValues(d)).final_score);
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;
  const critical = ok.filter((d) => finalValues(d).critical).length;
  const calls = data.dialogs.filter((d) => d.channel === "call").length;
  app.innerHTML = `
    <div class="hero">
      <div>
        <div class="hero-kicker">${icon("spark")} Контроль качества · ${SET_NAMES[data.set] || data.set}</div>
        <h1>Диалоги <span class="gradient-text">операторов</span></h1>
        <div class="hero-sub">ИИ оценивает каждый разговор по чек-листу, а человек принимает финальное решение</div>
      </div>
      <div class="hero-stats">
        <div class="hero-stat"><b>${data.dialogs.length}</b><span>${data.dialogs.length - calls} чатов · ${calls} звонков</span></div>
        <div class="hero-stat"><b>${fmtNum(avg.toFixed(1))}</b><span>средний балл</span></div>
        <div class="hero-stat danger"><b>${critical}</b><span>с нарушением</span></div>
        <div class="hero-stat"><b>${data.dialogs.length - ok.length}</b><span>ошибок оценки</span></div>
      </div>
    </div>
    ${liveToolbarHtml()}
    <div class="card">
      <div class="filters">
        <label><input type="checkbox" id="f-critical" ${filters.onlyCritical ? "checked" : ""}> Только нарушения</label>
        <label><input type="checkbox" id="f-errors" ${filters.onlyErrors ? "checked" : ""}> Только ошибки</label>
        <select id="f-language">
          <option value="all">Все языки</option>
          ${Object.entries(LANGUAGES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
        </select>
        <select id="f-channel">
          <option value="all">Все каналы</option>
          <option value="chat">Чаты</option>
          <option value="call">Звонки</option>
        </select>
        <input id="f-search" placeholder="Поиск по ID" value="${esc(filters.search)}">
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>ID</th><th>Канал</th><th>Язык</th><th>Итоговый балл</th>
            <th>Нарушение</th><th>Статус</th><th>Проверка</th>
          </tr></thead>
          <tbody id="dialog-rows"></tbody>
        </table>
      </div>
    </div>`;

  document.getElementById("f-language").value = filters.language;
  document.getElementById("f-channel").value = filters.channel;
  const update = () => {
    filters.onlyCritical = document.getElementById("f-critical").checked;
    filters.onlyErrors = document.getElementById("f-errors").checked;
    filters.language = document.getElementById("f-language").value;
    filters.channel = document.getElementById("f-channel").value;
    filters.search = document.getElementById("f-search").value.trim().toLowerCase();
    fillDialogRows();
  };
  app.querySelectorAll(".filters input, .filters select").forEach((el) => el.addEventListener("input", update));
  setupLiveToolbar();
  fillDialogRows();
}

// Панель живой оценки: работает, только если сайт запущен через server.py
function liveToolbarHtml() {
  if (!SERVER) {
    return `<div class="card live-bar muted">${icon("spark")} Живая оценка (новый диалог, загрузка DEV/PROD из API)
      работает, когда сайт запущен через <b>«Запустить сайт.command»</b> или <b>python3 server.py</b>.</div>`;
  }
  const keyNote = SERVER.has_key ? "" : `<div class="live-warning">${icon("alert")} Нет ключа модели в .env — ИИ не сможет оценивать.</div>`;
  return `<div class="card live-bar">
    <div class="live-title">${icon("spark")} <b>Живая оценка ИИ</b><span class="muted">${esc(SERVER.model)}</span></div>
    <div class="live-actions">
      <button class="btn btn-primary" id="btn-new-dialog">${icon("chat")} Оценить новый диалог</button>
      <button class="btn" data-run="dev">${icon("download")} Загрузить и оценить DEV</button>
      <button class="btn" data-run="prod">${icon("download")} Загрузить и оценить PROD</button>
      <button class="btn" data-run="swap">${icon("download")} Загрузить и оценить SWAP</button>
    </div>
    <div id="job-box" class="hidden"><div class="job-text" id="job-text"></div>
      <div class="progress"><div id="job-bar"></div></div></div>
    ${keyNote}
  </div>`;
}

function setupLiveToolbar() {
  const newButton = document.getElementById("btn-new-dialog");
  if (!newButton) return;
  newButton.addEventListener("click", openNewDialogModal);
  document.querySelectorAll("[data-run]").forEach((button) => button.addEventListener("click", async () => {
    const set = button.dataset.run;
    // для контрольного набора организаторы сообщат своё значение group_id
    const defaults = { dev: "DEV", prod: "FBroker", swap: "Swap" };
    const groupId = prompt(`Значение group_id для набора ${SET_NAMES[set]} (от организаторов):`, defaults[set]);
    if (groupId === null) return;
    if (!groupId.trim()) return showToast("Нужно ввести group_id, который дали организаторы");
    const answer = await postJson("/api/run", { set, group_id: groupId });
    if (answer.error) return showToast(answer.error);
    watchJob();
  }));
  if (SERVER.job && SERVER.job.running) watchJob();
}

// Следим за оценкой набора: раз в секунду спрашиваем сервер о прогрессе
function watchJob() {
  clearInterval(watchJob.timer);
  watchJob.timer = setInterval(async () => {
    let job;
    try {
      job = await (await fetch("/api/status", { cache: "no-store" })).json().then((s) => s.job);
    } catch (e) { return; }
    const box = document.getElementById("job-box");
    if (!box) return;
    box.classList.remove("hidden");
    const percent = job.total ? Math.round(job.done / job.total * 100) : 3;
    document.getElementById("job-bar").style.width = percent + "%";
    document.getElementById("job-text").textContent = `${(job.set || "").toUpperCase()}: ${job.message} `
      + (job.total ? `${job.done} из ${job.total}, ошибок: ${job.errors}` : "");
    if (!job.running) {
      clearInterval(watchJob.timer);
      if (job.summary) {
        showToast(`Готово: оценено ${job.summary.ok} из ${job.summary.records}, средний балл ${fmtNum(job.summary.average)}`);
        await loadSets(job.set);
        render();
      }
    }
  }, 1000);
}

// ---------- Окно «Оценить новый диалог» ----------
function setupNewDialogModal() {
  document.getElementById("new-cancel").addEventListener("click", () =>
    document.getElementById("new-modal").classList.add("hidden"));
  document.getElementById("new-example").addEventListener("click", () => {
    document.getElementById("new-text").value = [
      "Клиент: Здравствуйте, как пополнить брокерский счёт с карты?",
      "Оператор: Здравствуйте! Меня зовут Алина, Freedom Broker. В приложении откройте «Пополнить» и выберите карту.",
      "Клиент: А комиссия есть?",
      "Оператор: Комиссии нет, деньги придут за несколько минут. Остались ли вопросы?",
      "Клиент: Нет, спасибо!",
      "Оператор: Хорошего дня!",
    ].join("\n");
  });
  document.getElementById("new-save").addEventListener("click", async () => {
    const button = document.getElementById("new-save");
    button.disabled = true;
    button.textContent = "ИИ оценивает диалог…";
    const answer = await postJson("/api/evaluate", {
      text: document.getElementById("new-text").value,
      channel: document.getElementById("new-channel").value,
      language: document.getElementById("new-language").value,
    });
    button.disabled = false;
    button.textContent = "Оценить";
    if (answer.error) return showToast(answer.error);
    document.getElementById("new-modal").classList.add("hidden");
    await loadSets("live");
    location.hash = "#dialog/" + encodeURIComponent(answer.conversation_id);
    render();
  });
}

function openNewDialogModal() {
  document.getElementById("new-modal").classList.remove("hidden");
  document.getElementById("new-text").focus();
}

function fillDialogRows() {
  let list = data.dialogs.filter((d) => {
    const critical = isOk(d) && finalValues(d).critical === true;
    if (filters.onlyCritical && !critical) return false;
    if (filters.onlyErrors && isOk(d)) return false;
    if (filters.language !== "all" && d.language !== filters.language) return false;
    if (filters.channel !== "all" && d.channel !== filters.channel) return false;
    if (filters.search && !String(d.conversation_id).toLowerCase().includes(filters.search)) return false;
    return true;
  });

  // сортировка: сначала нарушения, потом ошибки, потом по возрастанию балла
  const rank = (d) => {
    if (!isOk(d)) return [1, 0];
    const values = finalValues(d);
    return [values.critical ? 0 : 2, calcScore(values).final_score];
  };
  list.sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    return ra[0] - rb[0] || ra[1] - rb[1];
  });

  const rows = list.map((d) => {
    let scoreCell = '<span class="badge na score-badge">—</span>';
    let flagCell = "—";
    let statusCell = '<span class="badge bad">Ошибка</span>';
    if (isOk(d)) {
      const values = finalValues(d);
      const score = calcScore(values).final_score;
      const changed = score !== d.result.score.final_score ? ' <span class="changed">изм.</span>' : "";
      scoreCell = `<span class="badge score-badge ${scoreClass(score)}">${fmtNum(score)}</span>${changed}`;
      flagCell = values.critical ? `<span class="flag">${icon("alert")}Да</span>` : '<span class="muted">Нет</span>';
      statusCell = '<span class="badge good">Оценён</span>';
    }
    const review = reviewStatus(d);
    return `<tr class="clickable" data-id="${esc(d.conversation_id)}">
      <td class="id-cell">${esc(d.conversation_id)}</td>
      <td>${channelHtml(d)}</td>
      <td>${langHtml(d.language)}</td>
      <td>${scoreCell}</td>
      <td>${flagCell}</td>
      <td>${statusCell}</td>
      <td><span class="badge ${review.cls}">${review.text}</span></td>
    </tr>`;
  });
  const body = document.getElementById("dialog-rows");
  body.innerHTML = rows.join("") || '<tr><td colspan="7" class="empty">Ничего не найдено</td></tr>';
  body.querySelectorAll("tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = "#dialog/" + encodeURIComponent(tr.dataset.id); }));
}

// =====================================================================
// 2. Анализ разговора
// =====================================================================
function renderDialog(id) {
  const app = document.getElementById("app");
  const d = findDialog(id);
  if (!d) {
    app.innerHTML = '<div class="card empty">Диалог не найден. <a href="#dialogs">К списку</a></div>';
    return;
  }

  const isCall = d.channel === "call";
  app.innerHTML = `
    <button class="btn back" onclick="location.hash='#dialogs'">← К списку диалогов</button>
    <div class="page-head">
      <div class="dialog-title">
        <h1>${esc(d.conversation_id)}</h1>
        <span class="chip">${channelHtml(d)}</span>
        <span class="chip">${langHtml(d.language)}</span>
        <span class="chip">${d.turns.length} реплик</span>
      </div>
    </div>
    <div class="grid-2">
      <div class="stack">
        ${isCall ? playerHtml(d) : ""}
        <div class="card">
          <h2>Расшифровка</h2>
          <div class="transcript ${isCall ? "" : "chat"}" id="transcript">
            ${d.turns.map(turnHtml).join("")}
          </div>
        </div>
      </div>
      <div class="stack">${isOk(d) ? scoreCardsHtml(d) : errorCardHtml(d)}</div>
    </div>`;

  if (isCall) setupPlayer(d);
  if (isOk(d)) {
    animateRing();
    app.querySelectorAll("[data-turn]").forEach((btn) =>
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        goToTurn(d, Number(btn.dataset.turn));
      }));
    app.querySelectorAll(".crit-row[data-crit]").forEach((row) =>
      row.addEventListener("click", () => {
        document.getElementById("detail-" + row.dataset.crit).classList.toggle("hidden");
      }));
    document.getElementById("btn-appeal").addEventListener("click", () => openAppealModal(d));
    document.getElementById("btn-confirm").addEventListener("click", () => {
      confirmed[data.set + ":" + d.conversation_id] = true;
      saveStored("cqa_confirmed", confirmed);
      showToast("Оценка подтверждена проверяющим");
    });
    const reeval = document.getElementById("btn-reeval");
    if (reeval) reeval.addEventListener("click", () => reevaluate(d, reeval));
  }
}

// Отправить диалог ИИ ещё раз по рабочему чек-листу (живая оценка через server.py)
async function reevaluate(d, button) {
  button.disabled = true;
  button.textContent = "ИИ оценивает диалог…";
  const answer = await postJson("/api/evaluate", { set: data.set, conversation_id: d.conversation_id });
  if (answer.error) {
    showToast(answer.error);
    button.disabled = false;
    button.textContent = "Переоценить по текущему чек-листу";
    return;
  }
  await loadSets("live");
  showToast("Готово: новая оценка в наборе «Живая оценка»");
  location.hash = "#dialog/" + encodeURIComponent(answer.conversation_id);
  render();
}

// Отправить JSON на сервер и получить ответ (или {error: "..."})
async function postJson(url, body) {
  try {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify(body) });
    return await response.json();
  } catch (e) {
    return { error: "Сервер не отвечает. Запустите сайт через server.py" };
  }
}

function turnHtml(turn) {
  return `<div class="turn ${turn.role}" id="turn-${turn.n}">
    <div class="turn-n">${turn.n}</div>
    <div>
      <div class="turn-meta"><span class="turn-role">${ROLES[turn.role]}</span><span>${esc(turnTime(turn))}</span></div>
      <div class="turn-text">${esc(turn.text)}</div>
    </div>
  </div>`;
}

function errorCardHtml(d) {
  return `<div class="card">
    <h2>Оценка качества</h2>
    <div class="error-box">
      <b>Этот диалог не удалось оценить автоматически.</b>
      <p>Причина: ${esc(d.result.error)}</p>
      <p>Балл не выставлен (это не ноль). Диалог нужно проверить вручную или запустить оценку ещё раз.</p>
    </div>
  </div>`;
}

function scoreCardsHtml(d) {
  const ai = aiValues(d);
  const values = finalValues(d);
  const score = calcScore(values).final_score;
  const aiScore = d.result.score.final_score;
  const rationale = {};
  for (const c of CRITERIA) {
    rationale[c.id] = { rationale: "ИИ ещё не оценивал этот критерий: он добавлен в чек-лист позже. "
      + "Нажмите «Переоценить по текущему чек-листу».", evidence_turns: [] };
  }
  for (const c of d.result.criteria) rationale[c.criterion_id] = c;

  // строки критериев: "набрано / вес"
  const rows = CRITERIA.map((c) => {
    const value = values[c.id];
    let points, bar = "";
    if (c.id === "critical") {
      points = value ? '<span class="badge bad">Нарушение</span>' : '<span class="badge good">Нет</span>';
    } else if (value === null) {
      points = '<span class="badge na">N/A</span>';
    } else {
      const share = criterionShare(c, value);
      points = `<span style="color:${shareColor(share)}">${fmtNum(c.weight * share)}</span><span class="muted"> / ${c.weight}</span>`;
      bar = `<div class="crit-bar"><div style="width:0;background:${shareColor(share)}" data-width="${share * 100}"></div></div>`;
    }
    const changed = value !== ai[c.id]
      ? `<div class="changed">ИИ: ${valueLabel(ai[c.id])} → человек: ${valueLabel(value)}</div>` : "";
    const r = rationale[c.id];
    return `<div class="crit-row" data-crit="${c.id}" style="cursor:pointer">
        <div><div class="crit-name">${esc(c.name)}</div>${changed}${bar}</div>
        <div class="crit-points">${points}</div>
      </div>
      <div class="hidden reason" id="detail-${c.id}" style="border-color:var(--gray)">
        <p>${esc(r.rationale)}</p>${evidenceButtons(r.evidence_turns)}
      </div>`;
  }).join("");

  // "почему снижена оценка": критерии, где потеряны баллы
  const reasons = CRITERIA.filter((c) => {
    const v = values[c.id];
    if (c.id === "critical") return v === true;
    return v !== null && criterionShare(c, v) < 1;
  }).map((c) => {
    const v = values[c.id];
    const lost = c.id === "critical" ? "грубое нарушение" : "−" + fmtNum(c.weight * (1 - criterionShare(c, v))) + " из " + c.weight;
    const bad = c.id === "critical" || criterionShare(c, v) < 0.5;
    return `<div class="reason ${bad ? "bad" : ""}">
      <div class="reason-title"><span>${esc(c.name)}</span><span>${lost}</span></div>
      <p>${esc(rationale[c.id].rationale)}</p>
      ${evidenceButtons(rationale[c.id].evidence_turns)}
    </div>`;
  }).join("");

  const color = shareColor(score / 100 >= 0.8 ? 1 : score / 100 >= 0.6 ? 0.6 : 0);
  const circle = 2 * Math.PI * 56;
  return `
    <div class="card">
      <h2>Оценка качества</h2>
      ${values.critical ? `<div class="critical-alert">${icon("alert")} Выявлено грубое нарушение. Балл не обнулён, но диалог требует внимания руководителя.</div>` : ""}
      <div class="score-head">
        <div class="ring">
          <svg width="136" height="136">
            <circle class="ring-bg" cx="68" cy="68" r="56"></circle>
            <circle class="ring-fg" id="ring" cx="68" cy="68" r="56" stroke="${color}"
              stroke-dasharray="${circle}" stroke-dashoffset="${circle}" data-target="${circle * (1 - score / 100)}"></circle>
          </svg>
          <div class="ring-value"><div><b>${fmtNum(score)}</b><span>из 100</span></div></div>
        </div>
        <div>
          <div class="muted">Итоговый балл</div>
          ${score !== aiScore
            ? `<div>Оценка ИИ: <b>${fmtNum(aiScore)}</b> → финальная оценка человека: <b>${fmtNum(score)}</b></div>`
            : '<div class="muted">Нажмите на критерий, чтобы увидеть объяснение</div>'}
        </div>
      </div>
      <div class="criteria-list">${rows}</div>
      <div class="actions">
        <button class="btn btn-primary" id="btn-appeal">Оспорить оценку</button>
        <button class="btn" id="btn-confirm">Подтвердить оценку</button>
        ${SERVER ? `<button class="btn" id="btn-reeval">${icon("spark")} Переоценить по текущему чек-листу</button>` : ""}
      </div>
      <div class="ai-note">${icon("spark")} Оценку предложил ИИ. Решение принимает человек.</div>
    </div>
    <div class="card">
      <h2>Почему снижена оценка</h2>
      ${reasons || '<div class="muted">Баллы не снижены: все применимые критерии выполнены.</div>'}
    </div>`;
}

function evidenceButtons(turns) {
  return turns.map((n) =>
    `<button class="btn btn-small btn-link" data-turn="${n}">→ Перейти к фрагменту №${n}</button>`).join(" ");
}

function animateRing() {
  // небольшая задержка, чтобы браузер успел нарисовать круг и запустил анимацию
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const ring = document.getElementById("ring");
    if (ring) ring.style.strokeDashoffset = ring.dataset.target;
    document.querySelectorAll("[data-width]").forEach((el) => { el.style.width = el.dataset.width + "%"; });
  }));
}

// Прокрутить расшифровку к реплике, подсветить её и (для звонка) перемотать аудио
function goToTurn(d, n) {
  const el = document.getElementById("turn-" + n);
  if (!el) return;
  const box = document.getElementById("transcript");
  box.scrollTo({ top: el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2, behavior: "smooth" });
  box.closest(".card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  document.querySelectorAll(".turn.highlight").forEach((t) => t.classList.remove("highlight"));
  el.classList.add("highlight");
  setTimeout(() => el.classList.remove("highlight"), 3500);

  const turn = d.turns.find((t) => t.n === n);
  if (d.channel === "call" && turn && turn.start !== null) {
    player.selection = [turn.start, turn.end ?? turn.start + 2];
    const audio = document.getElementById("audio");
    if (audio && audio.src) {
      audio.currentTime = turn.start;
      audio.play().catch(() => {});
    }
    drawTracks();
  }
}

// =====================================================================
// Аудиоплеер с дорожками "Клиент" / "Оператор" на canvas
// =====================================================================
let player = { dialog: null, duration: 0, selection: null, frame: null };

function playerHtml(d) {
  return `<div class="card player">
    <h2>Запись звонка</h2>
    ${d.audio ? `<audio id="audio" controls></audio>`
              : '<div class="no-audio">Аудиофайл недоступен — дорожки построены по тайм-кодам расшифровки.</div>'}
    <div class="tracks">
      <div style="display:flex;gap:18px;margin-bottom:6px">
        <span class="track-label"><span class="dot" style="background:var(--client)"></span>Клиент</span>
        <span class="track-label"><span class="dot" style="background:var(--operator)"></span>Оператор</span>
        <span class="muted" style="margin-left:auto;font-size:13px" id="time-now">00:00</span>
      </div>
      <canvas id="tracks"></canvas>
    </div>
  </div>`;
}

function setupPlayer(d) {
  const lastEnd = Math.max(1, ...d.turns.map((t) => t.end ?? t.start ?? 0));
  player = { dialog: d, duration: lastEnd, selection: null, frame: null };
  const canvas = document.getElementById("tracks");
  const audio = document.getElementById("audio");

  if (audio) {
    // Скачиваем файл целиком и даём плееру ссылку на него в памяти.
    // Так перемотка работает даже на простом python3 -m http.server.
    fetch(d.audio)
      .then((response) => response.blob())
      .then((blob) => { audio.src = URL.createObjectURL(blob); })
      .catch(() => showToast("Не удалось загрузить аудио"));
    audio.addEventListener("loadedmetadata", () => {
      if (isFinite(audio.duration)) player.duration = Math.max(audio.duration, lastEnd);
      drawTracks();
    });
    audio.addEventListener("play", startPlayhead);
    audio.addEventListener("pause", () => { stopPlayhead(); drawTracks(); });
    audio.addEventListener("seeked", drawTracks);
  }

  // клик по дорожке — перемотка
  canvas.addEventListener("click", (event) => {
    const rect = canvas.getBoundingClientRect();
    const time = (event.clientX - rect.left) / rect.width * player.duration;
    const turn = d.turns.find((t) => t.start !== null && t.start <= time && time <= (t.end ?? t.start));
    player.selection = turn ? [turn.start, turn.end] : null;
    if (audio) audio.currentTime = time;
    if (turn) goToTurn(d, turn.n);
    drawTracks();
  });
  window.onresize = drawTracks;
  drawTracks();
}

function startPlayhead() {
  stopPlayhead();
  const loop = () => { drawTracks(); player.frame = requestAnimationFrame(loop); };
  loop();
}
function stopPlayhead() {
  if (player.frame) cancelAnimationFrame(player.frame);
  player.frame = null;
}

function drawTracks() {
  const canvas = document.getElementById("tracks");
  if (!canvas) return;
  const audio = document.getElementById("audio");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 150;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const duration = player.duration;
  const x = (time) => time / duration * width;
  const lanes = { client: { y: 40, color: "#3D7DD8" }, operator: { y: 110, color: "#5FA02B" } };

  // выбранный фрагмент
  if (player.selection) {
    ctx.fillStyle = "rgba(232, 163, 23, 0.18)";
    ctx.fillRect(x(player.selection[0]), 0, Math.max(3, x(player.selection[1]) - x(player.selection[0])), height);
  }

  // линии дорожек
  ctx.strokeStyle = "#E4E8EC";
  for (const lane of Object.values(lanes)) {
    ctx.beginPath(); ctx.moveTo(0, lane.y); ctx.lineTo(width, lane.y); ctx.stroke();
  }

  // столбики: там, где человек говорит (по тайм-кодам реплик)
  const step = 0.2;                                     // один столбик = 0.2 секунды
  const barWidth = Math.max(1.5, x(step) - 1);
  const now = audio ? audio.currentTime : 0;
  for (const turn of player.dialog.turns) {
    const lane = lanes[turn.role];
    if (!lane || turn.start === null) continue;
    const end = turn.end ?? turn.start + 1;
    for (let t = turn.start, i = 0; t < end; t += step, i++) {
      // высота "волны" — просто красивая псевдослучайная, звук не разбираем
      const wave = 0.3 + 0.7 * Math.abs(Math.sin(i * 1.7 + turn.n * 3.1)) * (0.55 + 0.45 * Math.abs(Math.sin(i * 0.37)));
      const h = wave * 26;
      ctx.fillStyle = lane.color;
      ctx.globalAlpha = t <= now ? 1 : 0.45;           // проигранная часть ярче
      ctx.fillRect(x(t), lane.y - h, barWidth, h * 2);
    }
  }
  ctx.globalAlpha = 1;

  // бегущая линия текущей позиции
  ctx.fillStyle = "#1E2833";
  ctx.fillRect(x(now) - 1, 0, 2, height);
  document.getElementById("time-now").textContent = fmtTime(now) + " / " + fmtTime(duration);

  // подсветка текущей реплики в расшифровке
  if (audio && !audio.paused) {
    const current = player.dialog.turns.find((t) => t.start !== null && t.start <= now && now <= (t.end ?? t.start));
    document.querySelectorAll(".turn.playing").forEach((el) => {
      if (!current || el.id !== "turn-" + current.n) el.classList.remove("playing");
    });
    if (current) {
      const el = document.getElementById("turn-" + current.n);
      if (!el.classList.contains("playing")) {
        el.classList.add("playing");
        const box = document.getElementById("transcript");
        box.scrollTo({ top: el.offsetTop - box.clientHeight / 2, behavior: "smooth" });
      }
    }
  }
}

// =====================================================================
// Окно "Оспорить оценку"
// =====================================================================
let modalDialog = null;

function setupModal() {
  const criterionSelect = document.getElementById("appeal-criterion");
  criterionSelect.addEventListener("change", fillAppealValues);
  document.getElementById("appeal-cancel").addEventListener("click", closeModal);
  document.getElementById("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  document.getElementById("appeal-save").addEventListener("click", () => {
    const criterionId = criterionSelect.value;
    const appeal = {
      id: Date.now(),
      set: data.set,
      conversation_id: modalDialog.conversation_id,
      criterion_id: criterionId,
      ai_value: aiValues(modalDialog)[criterionId],
      proposed_value: JSON.parse(document.getElementById("appeal-value").value),
      comment: document.getElementById("appeal-comment").value.trim(),
      created_at: new Date().toISOString(),
      status: "new",
    };
    appeals.push(appeal);
    saveStored("cqa_appeals", appeals);
    closeModal();
    showToast("Апелляция отправлена");
    location.hash = "#appeal/" + appeal.id;
  });
}

function openAppealModal(d) {
  modalDialog = d;
  document.getElementById("modal-dialog-id").textContent = "Диалог " + d.conversation_id;
  document.getElementById("appeal-criterion").innerHTML =
    CRITERIA.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  document.getElementById("appeal-comment").value = "";
  fillAppealValues();
  document.getElementById("modal").classList.remove("hidden");
}

function fillAppealValues() {
  const criterionId = document.getElementById("appeal-criterion").value;
  const aiValue = aiValues(modalDialog)[criterionId];
  document.getElementById("appeal-ai-value").textContent = valueLabel(aiValue);
  const select = document.getElementById("appeal-value");
  select.innerHTML = criterionById(criterionId).values.map((v) =>
    `<option value='${JSON.stringify(v)}'>${valueLabel(v)}</option>`).join("");
  // по умолчанию предлагаем значение, отличное от оценки ИИ
  const other = criterionById(criterionId).values.find((v) => v !== aiValue);
  select.value = JSON.stringify(other === undefined ? aiValue : other);
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

// =====================================================================
// 3. Апелляции
// =====================================================================
function renderAppeals(selectedId) {
  const app = document.getElementById("app");
  const list = appeals.filter((a) => a.set === data.set)
    .filter((a) => appealTab === "all" || a.status === appealTab)
    .sort((a, b) => (a.status === "new" ? 0 : 1) - (b.status === "new" ? 0 : 1) || b.id - a.id);
  const selected = appeals.find((a) => String(a.id) === String(selectedId)) || list[0];

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Апелляции</h1><div class="muted">Человек проверяет спорные оценки ИИ и принимает финальное решение</div></div>
      <button class="btn" id="download">${icon("download")} Скачать appeals.json</button>
    </div>
    <div class="appeal-layout">
      <div class="card">
        <div class="filters">
          <select id="appeal-tab">
            <option value="all">Все</option><option value="new">Новые</option><option value="done">Решённые</option>
          </select>
        </div>
        ${list.length ? list.map((a) => appealItemHtml(a, selected)).join("")
          : '<div class="empty">Апелляций пока нет.<br>Откройте диалог и нажмите «Оспорить оценку».</div>'}
      </div>
      <div class="card">${selected && selected.set === data.set ? appealDetailHtml(selected) : '<div class="empty">Выберите апелляцию слева</div>'}</div>
    </div>`;

  document.getElementById("appeal-tab").value = appealTab;
  document.getElementById("appeal-tab").addEventListener("change", (e) => { appealTab = e.target.value; renderAppeals(); });
  document.getElementById("download").addEventListener("click", downloadAppeals);
  app.querySelectorAll(".appeal-item").forEach((el) =>
    el.addEventListener("click", () => { location.hash = "#appeal/" + el.dataset.id; }));
  if (selected && selected.set === data.set) setupAppealDetail(selected);
}

function appealItemHtml(a, selected) {
  const c = criterionById(a.criterion_id);
  const isSelected = selected && selected.id === a.id;
  return `<div class="appeal-item ${isSelected ? "selected" : ""}" data-id="${a.id}">
    <div class="appeal-top">
      <span>${esc(a.conversation_id)}</span>
      <span class="badge ${a.status === "new" ? "status-new" : "status-done"}">${a.status === "new" ? "Новая" : "Решена"}</span>
    </div>
    <div class="muted">${esc(c.name)}: ${valueLabel(a.ai_value)} → ${valueLabel(a.proposed_value)}</div>
  </div>`;
}

function appealDetailHtml(a) {
  const d = findDialog(a.conversation_id);
  if (!d || !isOk(d)) return '<div class="empty">Диалог этой апелляции не найден в текущем наборе.</div>';
  const c = criterionById(a.criterion_id);
  const aiItem = d.result.criteria.find((x) => x.criterion_id === a.criterion_id);
  const aiScore = d.result.score.final_score;
  const finalScore = calcScore(finalValues(d)).final_score;
  const quotes = aiItem.evidence_turns.map((n) => {
    const t = d.turns.find((x) => x.n === n);
    return t ? `<div class="quote"><b>№${n} ${ROLES[t.role]}:</b> ${esc(t.text)}</div>` : "";
  }).join("");

  const decision = a.status === "done"
    ? `<div class="reason" style="border-color:var(--green);background:var(--green-light)">
         <b>Решение зафиксировано:</b> ${esc(c.name)} = ${valueLabel(a.final_value)}
         ${a.reviewer_comment ? `<p>${esc(a.reviewer_comment)}</p>` : ""}
         <div class="muted">${new Date(a.resolved_at).toLocaleString("ru-RU")}</div>
       </div>
       <button class="btn" id="reopen">Пересмотреть решение</button>`
    : `<div class="form-row"><label><b>Финальное значение критерия</b></label>
         <select id="final-value">${c.values.map((v) =>
           `<option value='${JSON.stringify(v)}'>${valueLabel(v)}</option>`).join("")}</select></div>
       <div class="form-row"><label><b>Комментарий проверяющего</b></label>
         <textarea id="reviewer-comment" rows="3" placeholder="Почему принято такое решение"></textarea></div>
       <div class="muted" id="preview"></div>
       <div class="actions">
         <button class="btn" id="keep-ai">Подтвердить оценку ИИ</button>
         <button class="btn btn-primary" id="fix-final">Зафиксировать финальную оценку</button>
       </div>`;

  return `
    <div class="dialog-title"><h2 style="margin:0">${esc(c.name)}</h2>
      <a class="btn btn-small" href="#dialog/${encodeURIComponent(d.conversation_id)}">Открыть диалог ${esc(d.conversation_id)}</a></div>
    <div class="compare">
      <span>Оценка ИИ: ${fmtNum(aiScore)}</span><span class="arrow">→</span>
      <span style="color:var(--green-dark)">Финальная: <span id="final-score">${fmtNum(finalScore)}</span></span>
    </div>
    <h3>Что решил ИИ: ${valueLabel(a.ai_value)}</h3>
    <p>${esc(aiItem.rationale)}</p>
    ${quotes}
    <h3 style="margin-top:18px">Апелляция: предлагают «${valueLabel(a.proposed_value)}»</h3>
    <p>${a.comment ? esc(a.comment) : '<span class="muted">Без комментария</span>'}</p>
    <hr style="border:none;border-top:1px solid var(--border);margin:18px 0">
    ${decision}`;
}

function setupAppealDetail(a) {
  const d = findDialog(a.conversation_id);
  if (!d || !isOk(d)) return;
  const reopen = document.getElementById("reopen");
  if (reopen) {
    reopen.addEventListener("click", () => {
      a.status = "new";
      saveStored("cqa_appeals", appeals);
      renderAppeals(a.id);
      updateAppealBadge();
    });
    return;
  }
  const select = document.getElementById("final-value");
  select.value = JSON.stringify(a.proposed_value);

  // показываем, каким станет балл, ещё до сохранения
  const preview = () => {
    const values = finalValues(d);
    values[a.criterion_id] = JSON.parse(select.value);
    document.getElementById("preview").textContent =
      "Если зафиксировать это значение, итоговый балл будет " + fmtNum(calcScore(values).final_score);
  };
  select.addEventListener("change", preview);
  preview();

  const resolve = (value) => {
    a.status = "done";
    a.final_value = value;
    a.reviewer_comment = document.getElementById("reviewer-comment").value.trim();
    a.resolved_at = new Date().toISOString();
    saveStored("cqa_appeals", appeals);
    showToast("Финальная оценка зафиксирована");
    renderAppeals(a.id);
    updateAppealBadge();
  };
  document.getElementById("keep-ai").addEventListener("click", () => resolve(a.ai_value));
  document.getElementById("fix-final").addEventListener("click", () => resolve(JSON.parse(select.value)));
}

function downloadAppeals() {
  const list = appeals.filter((a) => a.set === data.set).map((a) => {
    const d = findDialog(a.conversation_id);
    return Object.assign({}, a, {
      ai_final_score: d && isOk(d) ? d.result.score.final_score : null,
      human_final_score: d && isOk(d) ? calcScore(finalValues(d)).final_score : null,
    });
  });
  const blob = new Blob([JSON.stringify(list, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "appeals.json";
  link.click();
  URL.revokeObjectURL(link.href);
}

// =====================================================================
// 4. Аналитика
// =====================================================================
const ADVICE = {
  contact: ["people", "Сотрудники", "Напомнить операторам приветствие и представление; добавить в скрипт начала диалога."],
  discovery: ["people", "Сотрудники", "Тренинг по уточняющим вопросам: 2–3 обязательных вопроса перед ответом."],
  identification: ["instr", "Инструкция", "Обновить регламент: когда и как проверять личность клиента перед раскрытием данных."],
  consultation: ["instr", "Инструкция", "Дополнить базу знаний ответами на частые вопросы (комиссии, вывод денег, маржин-колл)."],
  empathy: ["people", "Сотрудники", "Тренинг по клиентоориентированности: признавать эмоции клиента."],
  dialogue: ["service", "Сервис", "Проверить нагрузку и время ответа; дать операторам шаблоны быстрых ответов."],
  closure: ["people", "Сотрудники", "Скрипт завершения: «Остались ли вопросы?» и прощание."],
  critical: ["service", "Сервис", "Каждый случай грубого нарушения разбирать с руководителем; добавить автопроверку рискованных фраз."],
};

// Круговая диаграмма (бублик) на SVG: parts = [{ label, value, color }]
function donutHtml(parts, centerValue, centerLabel) {
  const total = parts.reduce((sum, p) => sum + p.value, 0) || 1;
  const r = 62;
  const circle = 2 * Math.PI * r;
  let offset = 0;
  const arcs = parts.map((p) => {
    const length = p.value / total * circle;
    const arc = `<circle cx="80" cy="80" r="${r}" stroke="${p.color}" stroke-dasharray="${length} ${circle - length}"
                   stroke-dashoffset="${-offset}"></circle>`;
    offset += length;
    return arc;
  }).join("");
  const legend = parts.map((p) => `
    <div class="legend-row"><span class="dot" style="background:${p.color}"></span>${p.label}
      <b>${p.value}</b><span class="muted">${Math.round(p.value / total * 100)}%</span></div>`).join("");
  return `<div class="donut-wrap">
    <div class="donut">
      <svg width="160" height="160"><circle cx="80" cy="80" r="${r}" stroke="var(--gray-light)"></circle>${arcs}</svg>
      <div class="donut-center"><b>${centerValue}</b><span>${centerLabel}</span></div>
    </div>
    <div class="legend">${legend}</div>
  </div>`;
}

// Вертикальные столбики: items = [{ label, value (0..100), sub }]
function columnsHtml(items) {
  return `<div class="columns">${items.map((it) => `
    <div class="col">
      <div class="bar" style="height:0;background:${scoreGradient(it.value)}" data-height="${it.value}">
        <span>${fmtNum(it.value.toFixed(1))}</span></div>
      <div class="col-label"><b>${it.label}</b><br>${it.sub}</div>
    </div>`).join("")}</div>`;
}

function scoreGradient(score) {
  if (score >= 80) return "linear-gradient(180deg, #1AE276, #02B140)";
  if (score >= 60) return "linear-gradient(180deg, #FFC94D, #E8A317)";
  return "linear-gradient(180deg, #FF8A7A, #D9473B)";
}

function average(list) {
  return list.length ? list.reduce((a, b) => a + b, 0) / list.length : 0;
}

const SHORT_NAMES = { contact: "Контакт", discovery: "Потреб.", identification: "Идент.", consultation: "Консульт.",
                      empathy: "Эмпатия", dialogue: "Ведение", closure: "Заверш.", critical: "Грубые" };

function renderAnalytics() {
  const app = document.getElementById("app");
  const ok = data.dialogs.filter(isOk);
  const errors = data.dialogs.filter((d) => !isOk(d));
  const scored = ok.map((d) => ({ d, values: finalValues(d), score: calcScore(finalValues(d)).final_score }));
  const avg = average(scored.map((x) => x.score));
  const withCritical = scored.filter((x) => x.values.critical);
  const openAppeals = appeals.filter((a) => a.set === data.set && a.status === "new").length;
  const allAppeals = appeals.filter((a) => a.set === data.set).length;

  // 1) бублик: сколько диалогов хороших, средних, плохих
  const donut = donutHtml([
    { label: "Хорошо (80–100)", value: scored.filter((x) => x.score >= 80).length, color: "#02B140" },
    { label: "Средне (60–79)", value: scored.filter((x) => x.score >= 60 && x.score < 80).length, color: "#E8A317" },
    { label: "Плохо (0–59)", value: scored.filter((x) => x.score < 60).length, color: "#D9473B" },
    { label: "Ошибка оценки", value: errors.length, color: "#A9B2BC" },
  ], fmtNum(avg.toFixed(1)), "средний балл");

  // 2) гистограмма: 10 корзин по 10 баллов
  const bins = new Array(10).fill(0);
  for (const x of scored) bins[Math.min(9, Math.floor(x.score / 10))]++;
  const maxBin = Math.max(1, ...bins);
  const histogram = bins.map((count, i) => `
    <div class="col">
      <div class="bar" style="height:0;background:${scoreGradient(i * 10 + 5)}" data-height="${count / maxBin * 100}">
        <span>${count || ""}</span></div>
      <div class="col-label">${i * 10}–${i === 9 ? 100 : i * 10 + 9}</div>
    </div>`).join("");

  // 3) средний балл по языкам и по каналам
  const byLanguage = Object.keys(LANGUAGES).map((code) => {
    const list = scored.filter((x) => x.d.language === code);
    return { label: LANG_CODES[code], value: average(list.map((x) => x.score)), sub: list.length + " диал." };
  }).filter((it) => it.sub !== "0 диал.");
  const byChannel = [["chat", "Чаты"], ["call", "Звонки"]].map(([code, name]) => {
    const list = scored.filter((x) => x.d.channel === code);
    return { label: name, value: average(list.map((x) => x.score)), sub: list.length + " диал." };
  }).filter((it) => it.sub !== "0 диал.");

  // 4) где теряются баллы: доля диалогов, где критерий выполнен не полностью
  const losses = CRITERIA.map((c) => {
    let applicable = 0, lostCount = 0, lostPoints = 0;
    const examples = [];
    for (const x of scored) {
      const v = x.values[c.id];
      if (c.id === "critical") {
        applicable++;
        if (v) { lostCount++; examples.push(x); }
        continue;
      }
      if (v === null) continue;
      applicable++;
      const share = criterionShare(c, v);
      if (share < 1) { lostCount++; lostPoints += c.weight * (1 - share); examples.push(x); }
    }
    return { c, applicable, lostCount, rate: applicable ? lostCount / applicable : 0,
             avgLost: applicable ? lostPoints / applicable : 0, examples };
  }).sort((a, b) => b.rate - a.rate);

  const lossBars = losses.map((l) => `
    <div class="hbar-row">
      <div>${esc(l.c.name)}<div class="muted" style="font-size:12px">${l.c.id === "critical" ? "диалогов с нарушением" : "−" + fmtNum(l.avgLost) + " в среднем из " + l.c.weight}</div></div>
      <div class="hbar"><div style="width:0" data-width="${l.rate * 100}"></div></div>
      <div class="hbar-value">${Math.round(l.rate * 100)}%</div>
    </div>`).join("");

  // 5) матрица качества: строки — диалоги, столбцы — критерии
  const heatHead = CRITERIA.map((c) => `<div class="heat-head">${esc(SHORT_NAMES[c.id] || c.name.slice(0, 9))}</div>`).join("");
  const heatRows = scored.slice().sort((a, b) => a.score - b.score).map((x) => {
    const cells = CRITERIA.map((c) => {
      const v = x.values[c.id];
      let cls = "na", text = "N/A";
      if (c.id === "critical") { cls = v ? "bad" : "good"; text = v ? "!" : "–"; }
      else if (v !== null) { cls = shareClass(criterionShare(c, v)); text = valueLabel(v); }
      return `<div class="heat-cell ${cls}" title="${esc(c.name)}: ${valueLabel(v)}">${text}</div>`;
    }).join("");
    return `<div class="heat-row" data-id="${esc(x.d.conversation_id)}">
      <div class="heat-id">${esc(x.d.conversation_id)}</div>${cells}
      <div class="heat-score"><span class="badge score-badge ${scoreClass(x.score)}">${fmtNum(x.score)}</span></div></div>`;
  }).join("");

  // 6) кого проверить в первую очередь: нарушения и самые низкие баллы
  const priority = scored.slice().sort((a, b) =>
    (b.values.critical ? 1 : 0) - (a.values.critical ? 1 : 0) || a.score - b.score).slice(0, 6);
  const priorityRows = priority.map((x) => `
    <tr class="clickable" data-id="${esc(x.d.conversation_id)}">
      <td class="id-cell">${esc(x.d.conversation_id)}</td>
      <td><span class="badge score-badge ${scoreClass(x.score)}">${fmtNum(x.score)}</span></td>
      <td>${x.values.critical ? `<span class="flag">${icon("alert")}Нарушение</span>` : "Низкий балл"}</td>
    </tr>`).join("") + errors.map((d) => `
    <tr class="clickable" data-id="${esc(d.conversation_id)}">
      <td class="id-cell">${esc(d.conversation_id)}</td><td><span class="badge bad">Ошибка</span></td><td>Проверить вручную</td>
    </tr>`).join("");

  // 7) самые частые причины снижения: по топ-3 критериям показываем примеры объяснений ИИ
  const topLosses = losses.filter((l) => l.lostCount > 0).slice(0, 3);
  const reasons = topLosses.map((l) => {
    const samples = l.examples.slice(0, 2).map((x) => {
      const item = x.d.result.criteria.find((c) => c.criterion_id === l.c.id);
      return `<div class="quote">«${esc(item.rationale)}» <span class="muted">— ${esc(x.d.conversation_id)}</span></div>`;
    }).join("");
    return `<div style="margin-bottom:14px"><b>${esc(l.c.name)}</b> <span class="muted">(${l.lostCount} диалогов)</span>${samples}</div>`;
  }).join("") || '<div class="muted">Потерь баллов нет.</div>';

  const advice = topLosses.map((l) => {
    const [tag, tagName, text] = ADVICE[l.c.id] || ["people", "Сотрудники", "Разобрать этот критерий с операторами на обучении."];
    return `<div class="reco"><span class="tag tag-${tag}">${tagName}</span><div><b>${esc(l.c.name)}</b> — ${Math.round(l.rate * 100)}% диалогов. ${text}</div></div>`;
  }).join("") + `<div class="reco"><span class="tag tag-product">Продукт</span><div>Если клиенты часто спрашивают одно и то же (комиссии, пополнение, вывод), стоит добавить подсказки прямо в приложение — меньше обращений.</div></div>`;

  app.innerHTML = `
    <div class="hero">
      <div>
        <div class="hero-kicker">${icon("analytics")} ${SET_NAMES[data.set] || data.set} · с учётом решений по апелляциям</div>
        <h1>Аналитика <span class="gradient-text">качества</span></h1>
        <div class="hero-sub">Где операторы теряют баллы и что стоит улучшить</div>
      </div>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="kpi-icon green">${icon("star")}</div><div><div class="kpi-value">${fmtNum(avg.toFixed(1))}</div><div class="kpi-label">Средний балл</div></div></div>
      <div class="card kpi"><div class="kpi-icon red">${icon("alert")}</div><div><div class="kpi-value">${scored.length ? Math.round(withCritical.length / scored.length * 100) : 0}%</div><div class="kpi-label">С грубым нарушением (${withCritical.length})</div></div></div>
      <div class="card kpi"><div class="kpi-icon blue">${icon("check")}</div><div><div class="kpi-value">${ok.length}<span class="muted" style="font-size:18px"> / ${data.dialogs.length}</span></div><div class="kpi-label">Оценено (ошибок: ${errors.length})</div></div></div>
      <div class="card kpi"><div class="kpi-icon violet">${icon("appeals")}</div><div><div class="kpi-value">${allAppeals}</div><div class="kpi-label">Апелляций (открыто: ${openAppeals})</div></div></div>
    </div>
    <div class="grid-3">
      <div class="card"><h2>Качество диалогов</h2>${donut}</div>
      <div class="card"><h2>Распределение баллов</h2><div class="histogram">${histogram}</div></div>
      <div class="card"><h2>Средний балл</h2>
        <div class="split-charts">
          <div><div class="muted small-title">по языкам</div>${columnsHtml(byLanguage)}</div>
          <div><div class="muted small-title">по каналам</div>${columnsHtml(byChannel)}</div>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:22px">
      <h2>Матрица качества</h2>
      <div class="muted" style="margin:-8px 0 14px">Каждая строка — диалог, каждый столбец — критерий. Нажмите на строку, чтобы открыть диалог.</div>
      <div class="heatmap" style="--cols:${CRITERIA.length}"><div class="heat-row heat-header"><div></div>${heatHead}<div></div></div>${heatRows}</div>
    </div>
    <div class="grid-2" style="margin-top:22px">
      <div class="stack">
        <div class="card"><h2>Где чаще всего теряются баллы</h2>${lossBars}</div>
        <div class="card"><h2>Самые частые причины снижения</h2>${reasons}</div>
      </div>
      <div class="stack">
        <div class="card"><h2>Проверить в первую очередь</h2>
          <table><thead><tr><th>Диалог</th><th>Балл</th><th>Причина</th></tr></thead><tbody>${priorityRows}</tbody></table></div>
        <div class="card"><h2>Что стоит улучшить</h2>${advice}</div>
      </div>
    </div>
    <div class="card business" style="margin-top:22px"><h2>Что это даёт бизнесу</h2>
      <div class="benefits">
        <div class="benefit"><div class="kpi-icon green">${icon("star")}</div><b>Качество сервиса</b>Проверяются 100% диалогов, а не 2–3% выборочно.</div>
        <div class="benefit"><div class="kpi-icon red">${icon("shield")}</div><b>Контроль рисков</b>Грубые нарушения (данные без идентификации, просьба кода из SMS) видны сразу.</div>
        <div class="benefit"><div class="kpi-icon blue">${icon("people")}</div><b>Развитие сотрудников</b>Каждая оценка с объяснением и цитатой — готовый материал для обратной связи.</div>
        <div class="benefit"><div class="kpi-icon violet">${icon("gear")}</div><b>Улучшение процессов</b>Видно, что чинить: инструкцию, сервис или продукт.</div>
      </div>
    </div>`;

  app.querySelectorAll("tr[data-id], .heat-row[data-id]").forEach((row) =>
    row.addEventListener("click", () => { location.hash = "#dialog/" + encodeURIComponent(row.dataset.id); }));
  requestAnimationFrame(() => requestAnimationFrame(() => {
    app.querySelectorAll("[data-height]").forEach((el) => { el.style.height = el.dataset.height + "%"; });
    app.querySelectorAll("[data-width]").forEach((el) => { el.style.width = el.dataset.width + "%"; });
  }));
}

// =====================================================================
// 5. Чек-лист
// =====================================================================
// Чек-лист можно менять (через server.py): добавить критерий, изменить описание или вес.
// Изменения сохраняются в checklist.json. Файлы results.json для жюри всегда считаются по чек-листу гайда.
function renderChecklist() {
  const canEdit = Boolean(SERVER);
  const totalWeight = CRITERIA.reduce((sum, c) => sum + c.weight, 0);
  const rows = CRITERIA.map((c, index) => `
    <tr>
      <td class="wrap"><b>${esc(c.name)}</b>${c.official ? '<span class="official-tag">из гайда</span>' : '<span class="custom-tag">свой</span>'}
        <div class="code">${esc(c.id)}</div></td>
      <td class="wrap">${c.values.map((v) => `<span class="chip" style="margin:2px">${valueLabel(v)}</span>`).join("")}</td>
      <td>${canEdit && c.weight > 0
        ? `<input class="weight-input" type="number" min="1" max="100" value="${c.weight}" data-weight="${index}">`
        : `<span style="font-size:20px;font-weight:800;color:var(--green-dark)">${c.weight || "—"}</span>`}</td>
      <td class="muted wrap">${esc(c.description || "")}</td>
      <td>${canEdit ? `<button class="btn btn-small" data-edit="${index}">Изменить</button>
        ${c.official ? "" : `<button class="btn btn-small" data-delete="${index}">Удалить</button>`}` : ""}</td>
    </tr>`).join("");

  document.getElementById("app").innerHTML = `
    <div class="hero">
      <div>
        <div class="hero-kicker">${icon("checklist")} ${CRITERIA.length} критериев · сумма весов ${totalWeight}</div>
        <h1>Чек-лист <span class="gradient-text">оценки</span></h1>
        <div class="hero-sub">Критерии, по которым ИИ оценивает работу оператора</div>
      </div>
      ${canEdit ? `<div class="editor-actions">
        <button class="btn btn-primary" id="add-criterion">+ Добавить критерий</button>
        <button class="btn" id="reset-checklist">Вернуть чек-лист из гайда</button></div>` : ""}
    </div>
    <div class="note-box">${canEdit
      ? "Изменения сразу пересчитывают баллы на сайте по новым весам. Новый критерий ИИ оценит при живой оценке (кнопка «Переоценить по текущему чек-листу» или «Оценить новый диалог»). Официальные results.json для жюри всегда считаются по чек-листу из гайда."
      : "Чтобы менять чек-лист, запустите сайт через «Запустить сайт.command» (server.py)."}</div>
    <div class="card">
      <div class="table-wrap"><table>
        <thead><tr><th style="width:230px">Критерий</th><th style="width:200px">Значения</th><th>Вес</th><th>Что проверяем</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      ${canEdit ? '<div class="actions"><button class="btn btn-primary" id="save-weights">Сохранить веса</button></div>' : ""}
    </div>
    <div class="card hidden" id="edit-card" style="margin-top:22px"></div>
    <div class="card" style="margin-top:22px">
      <h2>Как считается итоговый балл</h2>
      <p>Для каждого критерия берём долю выполнения: <b>Да = 1</b>, <b>Нет = 0</b>, число по шкале делим на максимум шкалы (консультация — на 20, ведение диалога — на 10).</p>
      <div class="formula"><b>Итог = Σ (вес × доля) / Σ весов применимых критериев × 100</b>, округляем до 2 знаков.</div>
      <p><b>N/A</b> — критерий не подходит к диалогу. Это не ноль: критерий просто не участвует, его вес распределяется между остальными.</p>
      <p><b>Грубые ошибки</b> (вес 0) не входят в сумму и не обнуляют балл — это отдельный флаг для руководителя.</p>
      <p><b>Пример из гайда:</b> контакт = Да, потребность = N/A, идентификация = N/A, консультация = 15, эмпатия = Да, ведение = 7, завершение = Да →
         (10 + 15 + 15 + 10,5 + 5) / 65 × 100 = <b>${fmtNum(calcScore({ contact: true, discovery: null, identification: null, consultation: 15, empathy: true, dialogue: 7, closure: true, critical: false }, OFFICIAL_CHECKLIST).final_score)}</b></p>
      <p class="muted">Балл считает код, а не ИИ. Проверка: формула сайта совпала с Python в ${formulaCheck.same} из ${formulaCheck.total} диалогов ${formulaCheck.same === formulaCheck.total ? "✓" : "✗"}</p>
    </div>`;

  if (!canEdit) return;
  const app = document.getElementById("app");
  app.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => openCriterionForm(Number(b.dataset.edit))));
  app.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => {
    const c = CRITERIA[Number(b.dataset.delete)];
    if (!confirm("Удалить критерий «" + c.name + "»?")) return;
    saveChecklist(CRITERIA.filter((x) => x !== c));
  }));
  document.getElementById("add-criterion").addEventListener("click", () => openCriterionForm(null));
  document.getElementById("reset-checklist").addEventListener("click", () => {
    if (!confirm("Вернуть чек-лист из гайда? Свои критерии будут удалены.")) return;
    saveChecklist(OFFICIAL_CHECKLIST.map((c) => Object.assign({}, c, { official: true })));
  });
  document.getElementById("save-weights").addEventListener("click", () => {
    const list = CRITERIA.map((c) => Object.assign({}, c));
    app.querySelectorAll("[data-weight]").forEach((input) => { list[Number(input.dataset.weight)].weight = Number(input.value); });
    saveChecklist(list);
  });
}

// Форма «Добавить / изменить критерий». index = null — новый критерий
function openCriterionForm(index) {
  const c = index === null ? { id: "", name: "", values: [true, false], weight: 10, description: "" } : CRITERIA[index];
  const isNew = index === null;
  const card = document.getElementById("edit-card");
  const hasNa = c.values.includes(null);
  const numbers = c.values.filter((v) => typeof v === "number");
  card.classList.remove("hidden");
  card.innerHTML = `
    <h2>${isNew ? "Новый критерий" : "Изменить критерий «" + esc(c.name) + "»"}</h2>
    <div class="edit-form">
      <label>Название<input id="f-name" value="${esc(c.name)}" placeholder="Например: Вежливость"></label>
      <label>Код (латиницей)<input id="f-id" value="${esc(c.id)}" placeholder="politeness" ${isNew ? "" : "disabled"}></label>
      ${isNew ? `<label>Тип значений
        <select id="f-type">
          <option value="yesno">Да / Нет</option>
          <option value="yesno-na">Да / Нет / N/A</option>
          <option value="scale">Шкала баллов</option>
        </select></label>
      <label>Баллы шкалы (через запятую)<input id="f-scale" value="${numbers.length ? numbers.join(", ") : "0, 5, 10"}"></label>
      <label class="full" style="flex-direction:row;align-items:center"><input type="checkbox" id="f-na" ${hasNa ? "checked" : ""}> Для шкалы разрешить N/A</label>` : ""}
      <label>Вес${c.weight === 0 ? " (у грубых ошибок всегда 0)" : ""}<input id="f-weight" type="number" min="1" max="100" value="${c.weight}" ${c.weight === 0 ? "disabled" : ""}></label>
      <label class="full">Что проверяем (это описание читает ИИ)<textarea id="f-desc" rows="4">${esc(c.description || "")}</textarea></label>
    </div>
    <div class="actions">
      <button class="btn btn-primary" id="f-save">Сохранить критерий</button>
      <button class="btn" id="f-cancel">Отмена</button>
    </div>`;
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  document.getElementById("f-cancel").addEventListener("click", () => card.classList.add("hidden"));
  document.getElementById("f-save").addEventListener("click", () => {
    const edited = Object.assign({}, c, {
      name: document.getElementById("f-name").value.trim(),
      description: document.getElementById("f-desc").value.trim(),
      weight: c.weight === 0 ? 0 : Number(document.getElementById("f-weight").value),
    });
    if (isNew) {
      edited.id = document.getElementById("f-id").value.trim();
      edited.official = false;
      const type = document.getElementById("f-type").value;
      if (type === "yesno") edited.values = [true, false];
      if (type === "yesno-na") edited.values = [true, false, null];
      if (type === "scale") {
        edited.values = document.getElementById("f-scale").value.split(",").map((x) => Number(x.trim()))
          .filter((x) => !Number.isNaN(x));
        if (document.getElementById("f-na").checked) edited.values.push(null);
      }
    }
    const list = CRITERIA.map((x) => Object.assign({}, x));
    if (isNew) list.push(edited); else list[index] = edited;
    saveChecklist(list);
  });
}

// Сохранить чек-лист на сервере (server.py проверит его) и пересчитать баллы на сайте
async function saveChecklist(list) {
  const answer = await postJson("/api/checklist", { criteria: list });
  if (!answer.ok) return showToast((answer.errors || [answer.error]).join("; "));
  ACTIVE_CHECKLIST = list;
  CRITERIA = list;
  showToast("Чек-лист сохранён, баллы пересчитаны");
  renderChecklist();
}

start();
