const state = {
  activeView: "schedule",
  perspective: "therapist",
  currentDate: new Date(),
  overview: null,
  schedule: null,
  filters: { therapist: "", patient: "", department: "", location: "", status: "", search: "" },
  patients: [],
  patientPage: 1,
  patientQuery: "",
  latestPreview: null,
  showPreview: false,
  assistantSessionId: null,
  assistantMemoryContext: null,
  operationsTab: "assistant",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;

function newRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return Array.from(bytes, (byte, index) => `${[4, 6, 8, 10].includes(index) ? "-" : ""}${byte.toString(16).padStart(2, "0")}`).join("");
}

function assistantMemorySessionId() {
  if (state.assistantSessionId) return state.assistantSessionId;
  try {
    const stored = sessionStorage.getItem("therapySchedulerAssistantSession");
    const identifier = stored || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    sessionStorage.setItem("therapySchedulerAssistantSession", identifier);
    state.assistantSessionId = identifier;
    return identifier;
  } catch (_) {
    state.assistantSessionId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    return state.assistantSessionId;
  }
}

function rememberedAssistantSessionId() {
  if (state.assistantSessionId) return state.assistantSessionId;
  try { return sessionStorage.getItem("therapySchedulerAssistantSession"); } catch (_) { return null; }
}

function assistantMemoryContext() {
  if (state.assistantMemoryContext) return state.assistantMemoryContext;
  try {
    const stored = sessionStorage.getItem("therapySchedulerAssistantMemory");
    state.assistantMemoryContext = stored ? JSON.parse(stored) : null;
  } catch (_) { state.assistantMemoryContext = null; }
  return state.assistantMemoryContext;
}

function saveAssistantMemoryContext(context) {
  state.assistantMemoryContext = context || null;
  try {
    if (context) sessionStorage.setItem("therapySchedulerAssistantMemory", JSON.stringify(context));
    else sessionStorage.removeItem("therapySchedulerAssistantMemory");
  } catch (_) { /* Browser storage is optional. */ }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function parseLocalDate(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day, 12, 0, 0);
}

function isoDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(value, days) {
  const result = new Date(value);
  result.setDate(result.getDate() + days);
  return result;
}

function startOfWeek(value) {
  const result = new Date(value);
  const weekday = result.getDay();
  result.setDate(result.getDate() - (weekday === 0 ? 6 : weekday - 1));
  return result;
}

function sameDate(a, b) {
  return isoDate(a) === isoDate(b);
}

function formatDate(value, options = {}) {
  return new Intl.DateTimeFormat("en-IN", options).format(value);
}

function formatStatus(status) {
  return String(status || "").toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function initials(name) {
  return String(name || "EIC").split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("").toUpperCase();
}

function therapyClass(name) {
  let hash = 0;
  for (const char of String(name || "")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `therapy-${hash % 5}`;
}

const testWorkspace = /^\/test(?:\/|$)/.test(location.pathname);
const mainWorkflowWorkspace = Boolean(window.therapyWorkspace) || /^\/workflow(?:\/|$)/.test(location.pathname);
const workflowWorkspace = testWorkspace || mainWorkflowWorkspace;
const workflowPrefix = window.therapyWorkspace?.apiPrefix ?? (mainWorkflowWorkspace ? '/workflow' : '/test');
const savePreviewLabel = result => result.autoOfferOnSave
  ? (result.messagingPaused ? "Save & queue offers (messaging OFF)" : result.liveMessaging ? "Save & send WhatsApp offers" : "Save & queue preview offers")
  : "Save as proposed";

async function api(path, options = {}) {
  const response = await fetch(workflowWorkspace ? `${workflowPrefix}${path}` : path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
      ...(workflowWorkspace && !window.therapyWorkspace?.localNoAuth && window.therapyWorkspace?.requiresAccessKey !== false ? { Authorization: `Bearer ${sessionStorage.getItem(mainWorkflowWorkspace ? 'mainWorkflowAdminToken' : 'flowAdminToken') || ''}` } : {}),
    },
  });
  if (workflowWorkspace && response.status === 401) {
    document.dispatchEvent(new Event("test-workspace-login"));
  }
  let payload = {};
  try { payload = await response.json(); } catch { /* response guard */ }
  if (!response.ok) throw new Error(payload.error || "Something went wrong. Please try again.");
  return payload;
}

function closeSearchableSelect(picker) {
  if (!picker) return;
  const select = $("select", picker);
  const input = $("input", picker);
  const selected = select.options[select.selectedIndex];
  input.value = select.value && selected ? selected.textContent.trim() : "";
  picker.classList.remove("is-open");
  input.setAttribute("aria-expanded", "false");
}

function closeSearchableSelects(except = null) {
  $$(".searchable-select.is-open").forEach((picker) => {
    if (picker !== except) closeSearchableSelect(picker);
  });
}

function makeSearchableSelect(select, placeholder) {
  if (!select) return;
  let picker = select.closest(".searchable-select");
  if (!picker) {
    picker = document.createElement("div");
    picker.className = "searchable-select";
    select.parentNode.insertBefore(picker, select);
    picker.append(select);
    select.classList.add("native-select-hidden");

    const input = document.createElement("input");
    input.type = "search";
    input.className = "searchable-select-input";
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", `${select.id}-options`);
    input.setAttribute("aria-label", select.getAttribute("aria-label") || placeholder);

    const options = document.createElement("div");
    options.className = "searchable-select-options";
    options.id = `${select.id}-options`;
    options.setAttribute("role", "listbox");
    picker.append(input, options);

    const renderOptions = (query = "") => {
      const normalized = query.trim().toLowerCase();
      const matching = [...select.options]
        .map((option, index) => ({ option, index, label: option.textContent.trim() }))
        .filter((item) => !normalized || item.label.toLowerCase().includes(normalized))
        .slice(0, 60);
      options.innerHTML = matching.length
        ? matching.map((item) => `<button type="button" role="option" aria-selected="${item.option.value === select.value}" data-option-index="${item.index}">${escapeHtml(item.label)}</button>`).join("")
        : `<p class="searchable-select-empty">No matches found</p>`;
      $$('[data-option-index]', options).forEach((button) => button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        select.selectedIndex = Number(button.dataset.optionIndex);
        select.dispatchEvent(new Event("change", { bubbles: true }));
        closeSearchableSelect(picker);
      }));
    };

    picker._renderOptions = renderOptions;
    input.addEventListener("focus", () => {
      closeSearchableSelects(picker);
      picker.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      input.select();
      renderOptions("");
    });
    input.addEventListener("input", () => renderOptions(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        input.blur();
        closeSearchableSelect(picker);
      }
      if (event.key === "Enter") {
        const first = $('[data-option-index]', options);
        if (!first) return;
        event.preventDefault();
        first.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      }
    });
    select.addEventListener("change", () => {
      if (!picker.classList.contains("is-open")) closeSearchableSelect(picker);
    });
  }
  picker.dataset.placeholder = placeholder;
  const input = $("input", picker);
  input.placeholder = placeholder;
  input.setAttribute("aria-label", select.getAttribute("aria-label") || placeholder);
  if (!picker.classList.contains("is-open")) closeSearchableSelect(picker);
  if (picker._renderOptions) picker._renderOptions("");
}

function toast(title, message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.innerHTML = `
    <span>${icon(type === "error" ? "alert" : "check")}</span>
    <div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p></div>
    <button aria-label="Dismiss">${icon("close")}</button>`;
  $("button", item).addEventListener("click", () => item.remove());
  $("#toastRegion").append(item);
  window.setTimeout(() => item.remove(), 5200);
}

function pageHeading(eyebrow, title, action = "") {
  return `<div class="page-heading"><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1></div>${action ? `<div class="heading-actions">${action}</div>` : ""}</div>`;
}

function setView(view, options = {}) {
  state.activeView = view;
  if (view === "operations") state.operationsTab = options.operationsTab || "assistant";
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#mainContent").focus({ preventScroll: true });
  if (view === "overview") loadOverviewView();
  if (view === "patients") loadPatientsView();
  if (view === "team") loadTeamView();
  if (view === "operations") loadOperationsView();
  if (view === "generator") loadGeneratorView();
}

async function initialize() {
  bindShellEvents();
  try {
    state.overview = await api("/api/overview");
    state.currentDate = parseLocalDate(state.overview.today);
    const badge = $("#attentionBadge");
    badge.textContent = state.overview.needsAttention;
    badge.hidden = !state.overview.needsAttention;
    await loadSchedule();
  } catch (error) {
    $("#calendarSurface").innerHTML = errorState(error.message);
  }
}

function bindShellEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$("[data-view-link]").forEach((button) => button.addEventListener("click", () => {
    setView(button.dataset.viewLink);
  }));
  $("#menuButton").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#filterButton").addEventListener("click", () => openDrawer("filterDrawer"));
  $$('[data-close-drawer]').forEach((button) => button.addEventListener("click", closeDrawers));
  $("#drawerBackdrop").addEventListener("click", closeDrawers);
  document.addEventListener("pointerdown", (event) => {
    $$(".searchable-select.is-open").forEach((picker) => {
      if (!picker.contains(event.target)) closeSearchableSelect(picker);
    });
  });
  $("#todayButton").addEventListener("click", async () => {
    state.currentDate = parseLocalDate(state.overview.today);
    await loadSchedule();
  });
  $("#previousDate").addEventListener("click", () => stepDate(-1));
  $("#nextDate").addEventListener("click", () => stepDate(1));
  $("#dateDisplay").addEventListener("click", () => {
    const picker = $("#datePicker");
    picker.value = isoDate(state.currentDate);
    if (picker.showPicker) picker.showPicker(); else picker.click();
  });
  $("#datePicker").addEventListener("change", async (event) => {
    if (!event.target.value) return;
    state.currentDate = parseLocalDate(event.target.value);
    await loadSchedule();
  });
  $$('[data-perspective]').forEach((button) => button.addEventListener("click", async () => {
    state.perspective = button.dataset.perspective;
    $$('[data-perspective]').forEach((item) => item.classList.toggle("active", item === button));
    if (state.perspective === "patient") {
      if (!state.filters.patient && state.schedule?.appointments.length) {
        state.filters.patient = String(state.schedule.appointments[0].patientId);
      }
    }
    populatePerspectivePicker();
    await loadSchedule();
  }));
  $("#perspectiveEntityPicker").addEventListener("change", (event) => {
    state.filters[state.perspective] = event.target.value;
    const drawerSelect = state.perspective === "therapist" ? $("#therapistFilter") : $("#patientFilter");
    drawerSelect.value = event.target.value;
    renderSchedule();
  });
  $("#applyFilters").addEventListener("click", (event) => { event.preventDefault(); readFilters(); closeDrawers(); renderSchedule(); });
  $("#clearFilters").addEventListener("click", (event) => { event.preventDefault(); clearFilters(); closeDrawers(); renderSchedule(); });
  $("#globalSearch").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const query = event.currentTarget.value.trim();
    if (state.activeView === "schedule") {
      $("#scheduleSearch").value = query;
      readFilters();
      renderSchedule();
    } else {
      state.patientQuery = query;
      setView("patients");
    }
  });
  $("#hidePreview").addEventListener("click", async () => {
    state.showPreview = false;
    await loadSchedule();
  });
  document.addEventListener("keydown", async (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#globalSearch").focus();
      return;
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (event.key === "Escape" && !$(".patient-status-dialog[open]")) { closeSearchableSelects(); closeDrawers(); }
    if (state.activeView !== "schedule") return;
    const shortcut = event.key.toLowerCase();
    if (shortcut === "t") {
      state.currentDate = parseLocalDate(state.overview.today);
      await loadSchedule();
    }
  });
}

async function stepDate(direction) {
  state.currentDate = addDays(state.currentDate, direction * 7);
  await loadSchedule();
}

async function loadSchedule() {
  const start = startOfWeek(state.currentDate);
  const days = 6;
  $("#calendarSurface").innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading the practice schedule…</div>`;
  try {
    state.schedule = await api(`/api/schedule?start=${isoDate(start)}&days=${days}`);
    if (state.showPreview && state.latestPreview) {
      const end = addDays(start, days - 1);
      const existing = new Set(state.schedule.appointments.map((item) => `${item.patientId}-${item.therapistId}-${item.date}-${item.slot}`));
      const previewAppointments = state.latestPreview.appointments
        .filter((item) => item.date >= isoDate(start) && item.date <= isoDate(end))
        .filter((item) => !existing.has(`${item.patientId}-${item.therapistId}-${item.date}-${item.slot}`))
        .map((item, index) => ({ ...item, id: `preview-${index}`, preview: true }));
      state.schedule.appointments.push(...previewAppointments);
    }
    populateFilterOptions();
    renderSchedule();
    if (workflowWorkspace) window.therapyWhatsapp.mountCalendar({
      root: $(".schedule-heading .heading-actions"), date: isoDate(state.currentDate), api,
      onChanged: refreshAfterWhatsapp,
      onOpen: () => setView("operations", {operationsTab:"whatsapp"}),
    });
  } catch (error) {
    $("#calendarSurface").innerHTML = errorState(error.message);
  }
}

function populateFilterOptions() {
  const selected = state.filters;
  $("#therapistFilter").innerHTML = `<option value="">All clinicians</option>${state.schedule.therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  $("#patientFilter").innerHTML = `<option value="">All patients</option>${state.schedule.patients.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  $("#departmentFilter").innerHTML = `<option value="">All therapies</option>${state.schedule.departments.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("#locationFilter").innerHTML = `<option value="">All locations</option>${state.schedule.locations.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("#therapistFilter").value = selected.therapist;
  $("#patientFilter").value = selected.patient;
  $("#departmentFilter").value = selected.department;
  $("#locationFilter").value = selected.location;
  $("#statusFilter").value = selected.status;
  $("#scheduleSearch").value = selected.search;
  makeSearchableSelect($("#therapistFilter"), "Search clinicians");
  makeSearchableSelect($("#patientFilter"), "Search patients");
  makeSearchableSelect($("#departmentFilter"), "Search therapies");
  makeSearchableSelect($("#locationFilter"), "Search branches");
  populatePerspectivePicker();
}

function populatePerspectivePicker() {
  if (!state.schedule) return;
  const picker = $("#perspectiveEntityPicker");
  const label = $("#perspectivePickerLabel");
  const showingTherapists = state.perspective === "therapist";
  const entities = showingTherapists
    ? state.schedule.therapists.filter((item) => item.active)
    : state.schedule.patients;
  label.textContent = showingTherapists ? "Doctor" : "Patient";
  picker.setAttribute("aria-label", showingTherapists ? "Choose doctor" : "Choose patient");
  picker.innerHTML = `<option value="">All ${showingTherapists ? "doctors" : "patients"}</option>${entities.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  picker.value = state.filters[state.perspective];
  makeSearchableSelect(picker, `Search ${showingTherapists ? "doctors" : "patients"}`);
}

function readFilters() {
  state.filters = {
    therapist: $("#therapistFilter").value,
    patient: $("#patientFilter").value,
    department: $("#departmentFilter").value,
    location: $("#locationFilter").value,
    status: $("#statusFilter").value,
    search: $("#scheduleSearch").value.trim(),
  };
}

function clearFilters() {
  state.filters = { therapist: "", patient: "", department: "", location: "", status: "", search: "" };
  populateFilterOptions();
}

function filteredAppointments() {
  const search = state.filters.search.toLowerCase();
  return state.schedule.appointments.filter((item) => {
    if (state.filters.therapist && String(item.therapistId) !== state.filters.therapist) return false;
    if (state.filters.patient && String(item.patientId) !== state.filters.patient) return false;
    if (state.filters.department && String(item.departmentId) !== state.filters.department) return false;
    if (state.filters.location && String(item.locationId) !== state.filters.location) return false;
    if (state.filters.status && item.status !== state.filters.status) return false;
    if (search && !`${item.patientCode} ${item.patientName} ${item.therapistCode} ${item.therapistName} ${item.therapy}`.toLowerCase().includes(search)) return false;
    return true;
  });
}

function renderFilterChips() {
  const labels = [];
  const therapist = state.schedule.therapists.find((item) => String(item.id) === state.filters.therapist);
  const patient = state.schedule.patients.find((item) => String(item.id) === state.filters.patient);
  const department = state.schedule.departments.find((item) => String(item.id) === state.filters.department);
  const location = state.schedule.locations.find((item) => String(item.id) === state.filters.location);
  if (therapist) labels.push(["therapist", therapist.name]);
  if (patient) labels.push(["patient", patient.name]);
  if (department) labels.push(["department", department.name]);
  if (location) labels.push(["location", location.name]);
  if (state.filters.status) labels.push(["status", formatStatus(state.filters.status)]);
  if (state.filters.search) labels.push(["search", `“${state.filters.search}”`]);
  const row = $("#activeFilters");
  const count = $("#filterCount");
  row.hidden = labels.length === 0;
  count.hidden = labels.length === 0;
  count.textContent = labels.length;
  row.innerHTML = labels.map(([key, label]) => `<span class="filter-chip">${escapeHtml(label)}<button data-remove-filter="${key}" aria-label="Remove ${escapeHtml(label)}">${icon("close")}</button></span>`).join("");
  $$('[data-remove-filter]', row).forEach((button) => button.addEventListener("click", () => {
    state.filters[button.dataset.removeFilter] = "";
    populateFilterOptions();
    renderSchedule();
  }));
}

function renderSchedule() {
  if (!state.schedule) return;
  const appointments = filteredAppointments();
  const visibleEntities = new Set(appointments.map((item) => (
    state.perspective === "therapist" ? item.therapistId : item.patientId
  )));
  $("#visibleAppointmentCount").textContent = appointments.length.toLocaleString("en-IN");
  $("#visibleTherapistCount").textContent = visibleEntities.size.toLocaleString("en-IN");
  $("#visibleEntityLabel").textContent = state.perspective === "therapist" ? "doctors scheduled" : "patients scheduled";
  $("#previewBanner").hidden = !(state.showPreview && state.latestPreview);
  renderFilterChips();
  updateDateDisplay();
  renderWeekCalendar(appointments);
}

function updateDateDisplay() {
  const display = $("#dateDisplay");
  const start = startOfWeek(state.currentDate);
  const end = addDays(start, 5);
  const sameMonth = start.getMonth() === end.getMonth();
  display.textContent = sameMonth
    ? `${start.getDate()}–${formatDate(end, { day: "numeric", month: "long", year: "numeric" })}`
    : `${formatDate(start, { day: "numeric", month: "short" })} – ${formatDate(end, { day: "numeric", month: "short", year: "numeric" })}`;
}

function calendarBreakRow(afterSlot, view) {
  return (state.schedule.calendarBreaks || [])
    .filter((item) => item.afterSlot === afterSlot)
    .map((item) => `<div class="${view}-grid-row calendar-break-row"><div class="time-label">${escapeHtml(item.start)}</div><div class="calendar-break-label">${escapeHtml(item.label)} · ${escapeHtml(item.start)}–${escapeHtml(item.end)}</div></div>`)
    .join("");
}

function renderWeekCalendar(appointments) {
  const start = startOfWeek(state.currentDate);
  const days = Array.from({ length: 6 }, (_, index) => addDays(start, index));
  const today = parseLocalDate(state.overview.today);
  const headers = days.map((day) => `<div class="day-header ${sameDate(day, today) ? "is-today" : ""}"><span>${formatDate(day, { weekday: "short" })}</span><strong>${day.getDate()}</strong></div>`).join("");
  const rows = state.schedule.slotLabels.map((label, slot) => {
    const cells = days.map((day) => {
      const items = appointments.filter((item) => item.slot === slot && item.date === isoDate(day));
      return `<div class="week-cell" ${!items.length ? emptySlotAttributes(isoDate(day), slot, state.filters.therapist || null) : ""}>${items.map(appointmentCard).join("")}</div>`;
    }).join("");
    return `<div class="week-grid-row"><div class="time-label">${escapeHtml(label.split("–")[0])}</div>${cells}</div>${calendarBreakRow(slot, "week")}`;
  }).join("");
  $("#calendarSurface").innerHTML = `<div class="week-calendar"><div class="week-grid-header"><div class="time-corner">Time</div>${headers}</div>${rows}</div>`;
  bindAppointmentCards();
  bindEmptySlots();
}

function emptySlotAttributes(date, slot, therapistId = null, patientId = null) {
  return `data-empty-date="${date}" data-empty-slot="${slot}" ${therapistId ? `data-empty-therapist="${therapistId}"` : ""} ${patientId ? `data-empty-patient="${patientId}"` : ""} tabindex="0" role="button" aria-label="Add appointment on ${date} at ${escapeHtml(state.schedule.slotLabels[slot])}" title="Double-click to add an appointment (or press Enter)"`;
}

function bindEmptySlots() {
  $$('[data-empty-slot]', $("#calendarSurface")).forEach(cell => {
    const open = () => {
      const doctor = state.schedule.therapists.find(item => String(item.id) === cell.dataset.emptyTherapist);
      const opening = {
        date: cell.dataset.emptyDate, slot: Number(cell.dataset.emptySlot),
        therapistId: doctor?.id || null,
        departmentId: Number(state.filters.department) || doctor?.departmentId || state.schedule.departments[0]?.id,
        locationId: Number(state.filters.location) || doctor?.locationId || state.schedule.locations[0]?.id,
      };
      showReplacementCandidates([{
        emptySlot: opening, preselectedPatientId: Number(cell.dataset.emptyPatient) || null,
        date: opening.date, time: state.schedule.slotLabels[opening.slot], candidates: [],
      }]);
    };
    cell.addEventListener("dblclick", event => { if(event.target.closest('[data-appointment-id]')) return; open(); });
    cell.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
  });
}

function appointmentCard(item) {
  const primary = state.perspective === "therapist" ? item.patientName : item.therapistName;
  const secondary = state.perspective === "therapist" ? item.patientCode : item.therapistCode;
  return `<button class="appointment-card ${therapyClass(item.therapy)} status-${item.status.toLowerCase()}" data-appointment-id="${item.id}"><strong>${escapeHtml(primary)}</strong><small>${escapeHtml(secondary)} · ${escapeHtml(item.therapy)}</small><span class="card-time">${escapeHtml(item.time.split("–")[0])}</span></button>`;
}

function bindAppointmentCards() {
  $$('[data-appointment-id]').forEach((card) => card.addEventListener("click", () => {
    const item = state.schedule.appointments.find((appointment) => String(appointment.id) === card.dataset.appointmentId);
    if (item) openAppointment(item);
  }));
}

function emptySchedule() {
  const filtersActive = Object.values(state.filters).some(Boolean);
  const perspective = state.perspective === "therapist" ? "doctor" : "patient";
  return `<div class="empty-state"><span class="empty-state-icon">${icon(filtersActive ? "filter" : "calendar")}</span><h3>${filtersActive ? `No ${perspective} schedules match these filters` : "This schedule is ready to plan"}</h3><p>${filtersActive ? "Clear or adjust the calendar filters to broaden the view." : "Generate tomorrow’s draft to place patient sessions into available clinician time."}</p><button class="${filtersActive ? "secondary-button" : "primary-button"}" id="emptyAction">${filtersActive ? "Clear filters" : "Plan tomorrow"}</button></div>`;
}

function errorState(message) {
  return `<div class="empty-state"><span class="empty-state-icon">${icon("alert")}</span><h3>We couldn’t load this view</h3><p>${escapeHtml(message)}</p><button class="secondary-button" onclick="location.reload()">Try again</button></div>`;
}

function bindEmptyAction() {
  const action = $("#emptyAction");
  if (!action) return;
  action.addEventListener("click", () => {
    if (Object.values(state.filters).some(Boolean)) { clearFilters(); renderSchedule(); }
    else { setView("generator"); }
  });
}

const scheduleObserver = new MutationObserver(bindEmptyAction);
scheduleObserver.observe($("#calendarSurface"), { childList: true });

async function loadOverviewView() {
  const root = $("#view-overview");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading today’s overview…</div>`;
  try {
    state.overview = await api("/api/overview");
    const todaySchedule = await api(`/api/schedule?start=${state.overview.today}&days=1`);
    const todayAppointments = todaySchedule.appointments;
    const filterOptions = (field) => [...new Set(todayAppointments.map((item) => item[field]).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right))
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    root.innerHTML = `${pageHeading("Practice pulse", "Today at EIC", `<button class="primary-button" data-overview-schedule>${icon("calendar")}Open calendar</button>`)}
      <div class="metric-grid">
        ${metricCard("Appointments today", state.overview.appointmentsToday, "Across the clinical team")}
        ${metricCard("Active patients", state.overview.patients, `${state.overview.activeRequirements.toLocaleString("en-IN")} active therapy requirements`)}
        ${metricCard("Clinical team", state.overview.therapists, "Across all therapy departments", "accent")}
        ${metricCard("Needs attention", state.overview.needsAttention, "Released or disrupted appointments", state.overview.needsAttention ? "warn" : "")}
      </div>
      <div class="content-grid">
        <section class="panel"><div class="panel-header"><div><h2>Today’s sessions</h2><p>${formatDate(parseLocalDate(state.overview.today), { weekday: "long", day: "numeric", month: "long" })}</p></div><span class="soft-pill" id="overviewSessionCount">${todayAppointments.length} appointments</span></div>
          <div class="overview-session-filters" aria-label="Filter today’s sessions">
            <label>Branch<select id="overviewBranchFilter"><option value="">All branches</option>${filterOptions("location")}</select></label>
            <label>Therapist<select id="overviewTherapistFilter"><option value="">All therapists</option>${filterOptions("therapistName")}</select></label>
            <label>Patient<select id="overviewPatientFilter"><option value="">All patients</option>${filterOptions("patientName")}</select></label>
            <button class="secondary-button overview-filter-clear" type="button" id="clearOverviewFilters">Clear filters</button>
          </div>
          <div id="overviewSessionList"></div>
        </section>
        <aside class="panel"><div class="panel-header"><div><h2>Planning readiness</h2><p>Current local workspace</p></div></div><div class="panel-body">
          <div class="detail-list">
            <div class="detail-item"><span>${icon("patients")}</span><div><small>Patient records</small><strong>${state.overview.patients.toLocaleString("en-IN")} ready</strong></div></div>
            <div class="detail-item"><span>${icon("team")}</span><div><small>Active therapists</small><strong>${state.overview.therapists.toLocaleString("en-IN")} available</strong></div></div>
            <div class="detail-item"><span>${icon("activity")}</span><div><small>Import warnings</small><strong>${state.overview.importWarnings.toLocaleString("en-IN")} recorded for review</strong></div></div>
          </div>
          <button class="primary-button" data-overview-generate style="width:100%;margin-top:18px">Plan tomorrow</button>
        </div></aside>
      </div>`;
    $('[data-overview-schedule]').addEventListener("click", () => setView("schedule"));
    $('[data-overview-generate]').addEventListener("click", () => setView("generator"));
    makeSearchableSelect($("#overviewBranchFilter", root), "Search branches");
    makeSearchableSelect($("#overviewTherapistFilter", root), "Search therapists");
    makeSearchableSelect($("#overviewPatientFilter", root), "Search patients");
    const renderTodaySessions = () => {
      const branch = $("#overviewBranchFilter", root).value;
      const therapist = $("#overviewTherapistFilter", root).value;
      const patient = $("#overviewPatientFilter", root).value;
      const matching = todayAppointments.filter((item) => (
        (!branch || item.location === branch)
        && (!therapist || item.therapistName === therapist)
        && (!patient || item.patientName === patient)
      ));
      const visible = matching.slice(0, 8);
      $("#overviewSessionCount", root).textContent = `${matching.length} appointment${matching.length === 1 ? "" : "s"}`;
      $("#overviewSessionList", root).innerHTML = visible.length
        ? `<div>${visible.map((item) => `<button class="agenda-row" data-overview-appointment="${item.id}"><span class="agenda-time">${escapeHtml(item.time.split("–")[0])}</span><span class="agenda-person"><strong>${escapeHtml(item.patientName)}</strong><small>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</small></span><span class="agenda-clinician"><strong>${escapeHtml(item.therapistName)}</strong><small>${escapeHtml(item.therapistCode)}</small></span><span class="soft-pill">${escapeHtml(item.location)}</span><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span>${icon("chevron-right")}</button>`).join("")}</div>${matching.length > visible.length ? `<p class="overview-session-note">Showing the first ${visible.length} matching sessions.</p>` : ""}`
        : `<div class="panel-empty overview-empty">${icon("calendar")}<strong>No sessions match these filters</strong><span>Clear one or more filters to see today’s appointments.</span></div>`;
      $$('[data-overview-appointment]', root).forEach((button) => button.addEventListener("click", () => {
        const item = todayAppointments.find((appointment) => String(appointment.id) === button.dataset.overviewAppointment);
        if (item) openAppointment(item);
      }));
    };
    ["#overviewBranchFilter", "#overviewTherapistFilter", "#overviewPatientFilter"].forEach((selector) => {
      $(selector, root).addEventListener("change", renderTodaySessions);
    });
    $("#clearOverviewFilters", root).addEventListener("click", () => {
      ["#overviewBranchFilter", "#overviewTherapistFilter", "#overviewPatientFilter"].forEach((selector) => {
        $(selector, root).value = "";
        makeSearchableSelect($(selector, root), $(selector, root).closest(".searchable-select")?.dataset.placeholder || "Search");
      });
      renderTodaySessions();
    });
    renderTodaySessions();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function metricCard(label, value, note, style = "") {
  return `<article class="metric-card ${style}"><span class="metric-label">${escapeHtml(label)}</span><div class="metric-value">${Number(value).toLocaleString("en-IN")}</div><span class="metric-note">${escapeHtml(note)}</span></article>`;
}

async function loadPatientsView() {
  const root = $("#view-patients");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading patient directory…</div>`;
  try {
    const payload = await api(`/api/patients?q=${encodeURIComponent(state.patientQuery)}`);
    state.patients = payload.patients;
    state.patientPage = 1;
    renderPatientsView();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function renderPatientsView() {
  const root = $("#view-patients");
  const pageSize = 25;
  const pages = Math.max(1, Math.ceil(state.patients.length / pageSize));
  state.patientPage = Math.min(state.patientPage, pages);
  const start = (state.patientPage - 1) * pageSize;
  const rows = state.patients.slice(start, start + pageSize);
  root.innerHTML = `${pageHeading("Care directory", "Patients")}
    <div class="directory-toolbar"><label class="search-field">${icon("search")}<input id="patientSearch" type="search" value="${escapeHtml(state.patientQuery)}" placeholder="Search by patient name or code" aria-label="Search patients"></label><span class="soft-pill">${state.patients.length.toLocaleString("en-IN")} patients</span></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>Patient ID</th><th>Patient</th><th>Preference day</th><th>Preferred time</th><th>Status</th><th></th></tr></thead><tbody>
      ${rows.map((item) => `<tr><td><strong>${escapeHtml(item.code)}</strong></td><td><div class="person-cell"><span class="avatar">${escapeHtml(initials(item.name === "Name not provided" ? item.code : item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${item.therapies} active therap${item.therapies === 1 ? "y" : "ies"}</small></span></div></td><td>${escapeHtml(item.preferenceDay)}</td><td>${escapeHtml(item.preferencePeriod)}</td><td><button class="status-pill patient-status-button ${item.active ? "status-confirmed" : "status-needs_rescheduling"}" type="button" data-patient-status="${item.id}" aria-haspopup="dialog" aria-label="${item.active ? "Active" : "Inactive"} — change scheduling status for ${escapeHtml(item.code)}">${item.active ? "Active" : "Inactive"} ${icon("chevron-right")}</button></td><td><button class="ghost-button" type="button" data-patient-preferences="${item.id}">Edit</button></td></tr>`).join("")}
    </tbody></table><div class="table-footer"><span>Showing ${state.patients.length ? start + 1 : 0}–${Math.min(start + pageSize, state.patients.length)} of ${state.patients.length.toLocaleString("en-IN")}</span><div><button class="ghost-button" id="patientPrev" ${state.patientPage === 1 ? "disabled" : ""}>Previous</button><button class="ghost-button" id="patientNext" ${state.patientPage === pages ? "disabled" : ""}>Next</button></div></div></div>`;
  let searchTimer;
  $("#patientSearch").addEventListener("input", (event) => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { state.patientQuery = event.target.value.trim(); loadPatientsView(); }, 280);
  });
  $("#patientPrev").addEventListener("click", () => { state.patientPage--; renderPatientsView(); });
  $("#patientNext").addEventListener("click", () => { state.patientPage++; renderPatientsView(); });
  $$("[data-patient-status]", root).forEach(button => button.addEventListener("click", () => {
    const patient = rows.find(item => String(item.id) === button.dataset.patientStatus);
    openPatientStatusDialog(patient, () => {
      renderPatientsView();
      $(`[data-patient-status="${patient.id}"]`, root)?.focus();
    });
  }));
  $$('[data-patient-preferences]', root).forEach((button) => button.addEventListener("click", () => {
    const patient = state.patients.find((item) => String(item.id) === button.dataset.patientPreferences);
    if (patient) openPatientPreferences(patient);
  }));
}

function ensurePatientPreferencesDrawer() {
  let drawer = $("#patientPreferencesDrawer");
  if (drawer) return drawer;
  drawer = document.createElement("aside");
  drawer.id = "patientPreferencesDrawer";
  drawer.className = "drawer";
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("aria-labelledby", "patientPreferencesTitle");
  document.body.append(drawer);
  return drawer;
}

async function openPatientPreferences(patient) {
  const drawer = ensurePatientPreferencesDrawer();
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">Loading availability…</h2></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div><div class="drawer-body"><div class="loading-state"><span class="spinner"></span>Loading preference table…</div></div>`;
  $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
  openDrawer("patientPreferencesDrawer");
  try {
    const payload = await api(`/api/patients/${patient.id}/preferences`);
    drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">${escapeHtml(payload.patient.name)}</h2><p>${escapeHtml(payload.patient.code)}</p></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div>
      <div class="drawer-body">
      <section class="patient-status-editor" aria-label="Scheduling status">
        <div><h3>Scheduling status</h3><span id="patientCurrentStatus">${payload.patient.active ? "Active" : "Inactive"}</span></div>
        <button class="secondary-button" id="patientStatusAction" type="button">${payload.patient.active ? "Mark inactive" : "Reactivate"}</button>
      </section>
      <form class="preference-form" id="patientPreferencesForm"><p class="candidate-intro">The scheduler ranks the selected day and time band ahead of other available slots. This is a preference, not a restriction: it can use another time when needed to complete therapy safely.</p>
        <label>Preferred day<select id="patientPreferenceDay"><option value="">No priority</option>${["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"].map((label, day) => `<option value="${day}" ${payload.preference.day === day ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <label>Preferred time<select id="patientPreferencePeriod"><option value="">No priority</option><option value="morning" ${payload.preference.period === "morning" ? "selected" : ""}>Morning · 09:00–12:35</option><option value="afternoon" ${payload.preference.period === "afternoon" ? "selected" : ""}>Afternoon · 13:15–16:50</option></select></label>
        <button class="primary-button" type="submit">Save preference</button>
      </form>
      </div>`;
    $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
    const statusAction = $("#patientStatusAction", drawer);
    statusAction.addEventListener("click", () => openPatientStatusDialog(payload.patient, () => {
      $("#patientCurrentStatus", drawer).textContent = payload.patient.active ? "Active" : "Inactive";
      statusAction.textContent = payload.patient.active ? "Mark inactive" : "Reactivate";
      renderPatientsView();
    }));
    $("#patientPreferencesForm", drawer).addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true;
      button.textContent = "Saving…";
      try {
        await api(`/api/patients/${patient.id}/preferences`, { method: "POST", body: JSON.stringify({ day: $("#patientPreferenceDay", drawer).value, period: $("#patientPreferencePeriod", drawer).value }) });
        toast("Preference saved", "The next schedule will prioritize this day and time.");
        closeDrawers();
        await loadPatientsView();
      } catch (error) {
        toast("Couldn’t save preferences", error.message, "error");
        button.disabled = false;
        button.textContent = "Save preference";
      }
    });
  } catch (error) {
    drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">Availability unavailable</h2></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div><div class="drawer-body">${errorState(error.message)}</div>`;
    $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
  }
}

function openPatientStatusDialog(patient, onSaved) {
  const active = !patient.active;
  let saving = false;
  let canActivate = !active;
  const dialog = document.createElement("dialog");
  dialog.className = "patient-status-dialog";
  dialog.setAttribute("aria-labelledby", "patientStatusTitle");
  dialog.setAttribute("aria-describedby", "patientStatusWarning");
  dialog.innerHTML = `
    <form class="preference-form">
      <h3 id="patientStatusTitle">${active ? "Mark this patient active?" : "Mark this patient as inactive?"}</h3>
      <p id="patientStatusWarning"><strong>${escapeHtml(patient.name)}</strong> ${active
        ? "can be marked active once their details are complete."
        : "will be excluded from new scheduling. Unsent offers will be withdrawn. Review any existing offers and confirmed visits separately."}</p>
      ${active ? "" : '<label>Reason for marking inactive <textarea name="reason" rows="3" maxlength="1000" required></textarea></label>'}
      ${active ? '<section class="patient-activation-check" aria-live="polite" aria-busy="true"><p>Checking patient details…</p></section><button type="button" class="secondary-button" data-recheck-activation hidden>Check again</button>' : ''}
      <p class="patient-status-error" role="alert" hidden></p>
      <div class="patient-status-actions">
        <button type="button" class="secondary-button" data-cancel-status>Cancel</button>
        <button type="submit" class="primary-button" ${active ? 'disabled' : ''}>${active ? "Mark active" : "Mark inactive"}</button>
      </div>
    </form>`;
  const form = $("form", dialog);
  const reason = $("textarea", form);
  const cancel = $("[data-cancel-status]", form);
  const confirm = $('button[type="submit"]', form);
  const errorNote = $(".patient-status-error", form);
  const check = $(".patient-activation-check", form);
  const recheck = $("[data-recheck-activation]", form);
  async function checkActivation() {
    canActivate = false;
    confirm.disabled = true;
    recheck.disabled = true;
    check.setAttribute("aria-busy", "true");
    check.innerHTML = '<p>Checking patient details…</p>';
    try {
      const review = await api(`/api/patients/${patient.id}/activation`);
      if (!dialog.isConnected) return;
      canActivate = review.canActivate;
      check.innerHTML = review.canActivate
        ? '<p>Patient details are complete. You can mark this patient active.</p>'
        : `<strong>Cannot mark active yet</strong><ul>${review.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join('')}</ul><p>${escapeHtml(review.nextStep || 'Run the Zoho sync, then check again.')}</p>`;
      if (review.checkedAt) check.insertAdjacentHTML('beforeend', `<p class="activation-check-date">Checked from the latest Zoho sync: ${escapeHtml(new Date(review.checkedAt).toLocaleString('en-IN'))}</p>`);
      confirm.disabled = !canActivate;
    } catch (error) {
      check.textContent = 'Patient details could not be checked. Check again before marking active.';
    } finally {
      check.setAttribute("aria-busy", "false");
      recheck.hidden = false;
      recheck.disabled = false;
    }
  }
  recheck?.addEventListener("click", checkActivation);

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("cancel", event => {
    if (saving) event.preventDefault();
  });
  reason?.addEventListener("input", () => reason.setCustomValidity(""));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (saving || (active && !canActivate)) return;
    const explanation = reason?.value.trim() ?? "";
    if (reason && !explanation) {
      reason.setCustomValidity("Enter a reason for marking this patient inactive.");
      reason.reportValidity();
      return;
    }
    confirm.disabled = cancel.disabled = true;
    saving = true;
    errorNote.hidden = true;
    let result;
    try {
      result = await api(`/api/patients/${patient.id}/status`, {
        method: "POST", body: JSON.stringify({ active, reason: explanation }),
      });
    } catch (error) {
      errorNote.textContent = error.message;
      errorNote.hidden = false;
      saving = false;
      cancel.disabled = false;
      if (active) await checkActivation();
      else confirm.disabled = false;
      return;
    }
    patient.active = result.active;
    const listedPatient = state.patients.find(item => item.id === patient.id);
    if (listedPatient) listedPatient.active = result.active;
    dialog.close();
    onSaved();
    toast(result.active ? "Patient reactivated" : "Patient marked inactive", "Scheduling status saved.");
  });

  document.body.append(dialog);
  dialog.showModal();
  (reason || cancel).focus();
  if (active) checkActivation();
}

async function loadTeamView() {
  const root = $("#view-team");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading clinical team…</div>`;
  try {
    const week = isoDate(startOfWeek(state.currentDate));
    const payload = await api(`/api/therapists?start=${week}`);
    root.innerHTML = `${pageHeading("Capacity & coverage", "Clinical team", `<button class="secondary-button" data-team-calendar>${icon("calendar")}View team calendar</button>`)}
      <div class="table-shell"><table class="data-table"><thead><tr><th>Clinician</th><th>Department</th><th>Location</th><th>Weekly workload</th><th>Status</th></tr></thead><tbody>
      ${payload.therapists.map((item) => { const capacityLabel = item.capacity == null ? "Not supplied" : item.capacity; const utilization = item.capacity ? Math.min(100, Math.round(item.appointments / item.capacity * 100)) : 0; return `<tr><td><div class="person-cell"><span class="avatar">${escapeHtml(initials(item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></span></div></td><td>${escapeHtml(item.department)}</td><td>${escapeHtml(item.location)}</td><td><div style="display:flex;align-items:center;gap:10px"><div class="progress-track"><div class="progress-fill" style="width:${utilization}%"></div></div><span>${item.appointments} / ${capacityLabel}</span></div></td><td><span class="status-pill ${item.active ? "status-confirmed" : "status-needs_rescheduling"}">${item.active ? "Active" : "Inactive"}</span></td></tr>`; }).join("")}
      </tbody></table><div class="table-footer"><span>${payload.therapists.length} therapists · week of ${formatDate(parseLocalDate(payload.weekStart), { day: "numeric", month: "long", year: "numeric" })}</span><span>Capacity assumes 10 daily slots</span></div></div>`;
    $('[data-team-calendar]').addEventListener("click", () => setView("schedule"));
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function renderReleasedAppointments(operations) {
  return `<section class="panel" id="operationsRescheduling"><div class="panel-header"><div><h2>Needs rescheduling</h2><p>Appointments released by an absence or disruption.</p></div><span class="soft-pill">${operations.needsRescheduling.length}</span></div>
            ${operations.needsRescheduling.length ? `<div>${operations.needsRescheduling.map((item) => `<button class="agenda-row" data-operation-appointment="${item.id}"><span class="agenda-time">${escapeHtml(item.time.split("–")[0])}</span><span class="agenda-person"><strong>${escapeHtml(item.patientName)}</strong><small>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</small></span><span class="agenda-clinician"><strong>${escapeHtml(item.therapistName)}</strong><small>${formatDate(parseLocalDate(item.date), { day: "numeric", month: "short" })}</small></span><span class="soft-pill">${escapeHtml(item.location)}</span><span class="status-pill status-needs_rescheduling">Needs rescheduling</span>${icon("chevron-right")}</button>`).join("")}</div>` : `<div class="panel-empty">${icon("check")}<strong>No released appointments</strong><span>The schedule has no appointments waiting for repair.</span></div>`}
          </section>`;
}

function bindReleasedAppointments(root, operations) {
    $$('[data-operation-appointment]', root).forEach((button) => button.addEventListener("click", async () => {
      const item = operations.needsRescheduling.find((appointment) => String(appointment.id) === button.dataset.operationAppointment);
      if (!item) return;
      showReplacementCandidates([{...item, appointmentId: item.id, branch: item.location}]);
    }));
}

function selectOperationsTab(tab, focus = false) {
  const root = $("#view-operations");
  const buttons = $$('[data-operations-tab]', root);
  const selected = buttons.find(button => button.dataset.operationsTab === tab) || buttons[0];
  if (!selected) return;
  state.operationsTab = selected.dataset.operationsTab;
  buttons.forEach(button => {
    const active = button === selected;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $$('.operations-tab-panel', root).forEach(panel => {
    panel.hidden = panel.id !== selected.getAttribute("aria-controls");
  });
  if (focus) selected.focus({preventScroll:true});
}

async function loadOperationsView() {
  const root = $("#view-operations");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading operations…</div>`;
  try {
    const [operations, patients, assistant] = await Promise.all([
      api("/api/operations"), api("/api/patients"),
      api("/api/assistant/status").catch(() => ({ configured: false, unavailable: true })),
    ]);
    const therapists = state.schedule?.therapists || [];
    const tabs = [
      {id:"assistant", label:"Scheduling assistant", icon:"sparkles"},
      ...(workflowWorkspace ? [{id:"whatsapp", label:"WhatsApp & attendance", icon:"bell"}] : []),
      {id:"absence", label:"Record an absence", icon:"calendar"},
      {id:"rescheduling", label:"Needs rescheduling", icon:"operations"},
    ];
    root.innerHTML = `${pageHeading("Schedule continuity", "Operations")}
      <div class="operations-tabs" role="tablist" aria-label="Operations sections">
        ${tabs.map(tab => `<button type="button" role="tab" id="operations-tab-${tab.id}" aria-controls="operations-panel-${tab.id}" aria-selected="false" tabindex="-1" data-operations-tab="${tab.id}">${icon(tab.icon)}<span>${tab.label}</span>${tab.id === "rescheduling" ? `<span class="operations-tab-count" data-rescheduling-count>${operations.needsRescheduling.length}</span>` : ""}</button>`).join("")}
      </div>
      <div class="operations-tab-panel" role="tabpanel" id="operations-panel-assistant" aria-labelledby="operations-tab-assistant" tabindex="0" hidden>
      <section class="panel assistant-panel">
        <div class="panel-header"><div><h2 class="assistant-title"><span class="assistant-ai-icon" aria-label="AI assistant">${icon("sparkles")}</span>Scheduling assistant</h2></div><div class="assistant-header-actions"><button class="secondary-button compact-button" id="assistantClearMemory" type="button">Clear memory</button></div></div>
        <div class="assistant-messages" id="assistantMessages" aria-live="polite"><div class="assistant-message">${assistant.configured ? "Type a request to get started." : (assistant.unavailable ? "The assistant endpoint is not available in this running server. Restart the scheduler; use the absence and rescheduling tabs for those actions." : "Add the Azure OpenAI settings to .env, then restart the scheduler to use this assistant.")}</div></div>
        <form class="assistant-form" id="assistantForm"><textarea id="assistantInput" rows="2" maxlength="1000" placeholder="Give me an overall summary for today" ${assistant.configured ? "" : "disabled"} required></textarea><button class="primary-button" type="submit" ${assistant.configured ? "" : "disabled"}>Send request</button></form>
      </section>
      </div>
      ${workflowWorkspace ? '<div class="operations-tab-panel" role="tabpanel" id="operations-panel-whatsapp" aria-labelledby="operations-tab-whatsapp" tabindex="0" hidden></div>' : ""}
      <div class="operations-tab-panel" role="tabpanel" id="operations-panel-absence" aria-labelledby="operations-tab-absence" tabindex="0" hidden>
        <div class="operations-absence-grid">
        <section class="panel"><div class="panel-header"><div><h2>Record an absence</h2><p>Affected appointments will be released for safe rescheduling.</p></div></div><form class="panel-body form-grid" id="absenceForm">
          <div class="segmented-field"><label><input type="radio" name="absenceKind" value="therapist" checked><span>Therapist</span></label><label><input type="radio" name="absenceKind" value="patient"><span>Patient</span></label></div>
          <label id="absenceEntityLabel">Therapist<select id="absenceEntity" required>${therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}</select></label>
          <label>Date<input id="absenceDate" type="date" value="${state.overview.today}" required></label>
          <label>Reason<textarea id="absenceReason" placeholder="Optional operational note"></textarea></label>
          <button class="primary-button" type="submit">Record full-day absence</button>
        </form></section>
          <section class="panel"><div class="panel-header"><div><h2>Recent absences</h2><p>Latest therapist and patient availability changes.</p></div></div>
            ${operations.absences.length ? `<div>${operations.absences.map((item) => `<div class="absence-row"><span class="soft-pill">${escapeHtml(item.kind)}</span><span><strong>${escapeHtml(item.name)}</strong><br>${escapeHtml(item.code)}</span><span>${formatDate(parseLocalDate(item.date), { day: "numeric", month: "short", year: "numeric" })}</span><span>${escapeHtml(item.reason)}</span></div>`).join("")}</div>` : `<div class="panel-empty">${icon("operations")}<strong>No absences recorded</strong><span>Recent changes will appear here.</span></div>`}
          </section>
        </div>
      </div>
      <div class="operations-tab-panel" role="tabpanel" id="operations-panel-rescheduling" aria-labelledby="operations-tab-rescheduling" tabindex="0" hidden>
        ${renderReleasedAppointments(operations)}
      </div>`;
    $$('[data-operations-tab]', root).forEach(button => {
      button.addEventListener("click", () => selectOperationsTab(button.dataset.operationsTab));
      button.addEventListener("keydown", event => {
        const buttons = $$('[data-operations-tab]', root);
        const index = buttons.indexOf(button);
        const next = {ArrowRight:(index+1)%buttons.length, ArrowLeft:(index+buttons.length-1)%buttons.length, Home:0, End:buttons.length-1}[event.key];
        if(next === undefined) return;
        event.preventDefault();
        selectOperationsTab(buttons[next].dataset.operationsTab, true);
      });
    });
    selectOperationsTab(state.operationsTab);
    const entitySelect = $("#absenceEntity");
    const label = $("#absenceEntityLabel");
    makeSearchableSelect(entitySelect, "Search therapists");
    $$('input[name="absenceKind"]').forEach((radio) => radio.addEventListener("change", () => {
      const kind = $('input[name="absenceKind"]:checked').value;
      label.firstChild.textContent = kind === "therapist" ? "Therapist" : "Patient";
      entitySelect.innerHTML = kind === "therapist"
        ? therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")
        : patients.patients.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("");
      makeSearchableSelect(entitySelect, `Search ${kind === "therapist" ? "therapists" : "patients"}`);
    }));
    $("#absenceForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true; button.textContent = "Recording…";
      try {
        const result = await api("/api/absences", { method: "POST", body: JSON.stringify({ kind: $('input[name="absenceKind"]:checked').value, entityId: Number(entitySelect.value), date: $("#absenceDate").value, reason: $("#absenceReason").value.trim() }) });
        const candidates = (result.replacementGroups || []).reduce((total, group) => total + group.candidates.length, 0);
        toast("Absence recorded", candidates ? `${candidates} continuity-preserving replacement option${candidates === 1 ? "" : "s"} found.` : `${result.affected} appointment${result.affected === 1 ? "" : "s"} now need rescheduling.`);
        state.overview = await api("/api/overview");
        await loadOperationsView();
      } catch (error) { toast("Couldn’t record absence", error.message, "error"); button.disabled = false; button.textContent = "Record full-day absence"; }
    });
    $("#assistantClearMemory").addEventListener("click", async () => {
      const button = $("#assistantClearMemory");
      const sessionId = rememberedAssistantSessionId();
      button.disabled = true;
      try {
        if (sessionId) await api("/api/assistant/memory/clear", { method: "POST", body: JSON.stringify({ sessionId }) });
        try { sessionStorage.removeItem("therapySchedulerAssistantSession"); } catch (_) { /* Browser storage is optional. */ }
        state.assistantSessionId = null;
        saveAssistantMemoryContext(null);
        $("#assistantMessages").innerHTML = "<div class=\"assistant-message\">Type a request to get started.</div>";
      } catch (error) {
        toast("Could not clear assistant memory", error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    $("#assistantForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("#assistantInput");
      const button = $('button[type="submit"]', event.currentTarget);
      const requestText = input.value.trim();
      if (!requestText) return;
      const messages = $("#assistantMessages");
      messages.insertAdjacentHTML("beforeend", `<div class="assistant-message user-message">${escapeHtml(requestText)}</div>`);
      input.value = "";
      button.disabled = true; button.textContent = "Working…";
      try {
        const result = await api("/api/assistant/messages", { method: "POST", body: JSON.stringify({
          message: requestText,
          sessionId: assistantMemorySessionId(),
          memory: assistantMemoryContext(),
        }) });
        if (result.memoryContext) saveAssistantMemoryContext(result.memoryContext);
        const replacements = (result.replacements || []).slice(0, 5);
        const pullForward = (result.pullForward || []).slice(0, 3);
        const replacementList = replacements.length ? `<div class="assistant-options"><strong>Possible replacements</strong><ul>${replacements.map((item) => `<li>${escapeHtml(item.patient_name)} · ${escapeHtml(item.weekly_progress)} weekly sessions</li>`).join("")}</ul></div>` : "";
        const pullForwardList = pullForward.length ? `<div class="assistant-options"><strong>Possible pull-forwards</strong><ul>${pullForward.map((item) => `<li>${escapeHtml(item.patient_name || item.patient_code)} · currently ${escapeHtml(String(item.days_later))} day(s) later</li>`).join("")}</ul></div>` : "";
        const summary = result.summary;
        const summaryDetails = summary ? `<div class="assistant-summary"><div class="assistant-metrics"><span><strong>${summary.scheduled}</strong>Scheduled</span><span><strong>${summary.patients}</strong>Patients</span><span><strong>${summary.therapists}</strong>Therapists</span><span><strong>${summary.needsRescheduling}</strong>Need repair</span><span><strong>${summary.cancelled}</strong>Cancelled</span></div>${summary.byTherapy?.length ? `<div class="assistant-options"><strong>Sessions by therapy</strong><ul>${summary.byTherapy.map((item) => `<li>${escapeHtml(item.name)} · ${escapeHtml(String(item.count))}</li>`).join("")}</ul></div>` : ""}</div>` : "";
        const itemList = result.items?.length ? `<div class="assistant-options"><strong>${escapeHtml(result.itemsLabel || "Details")}</strong><ul>${result.items.slice(0, 20).map((item) => `<li>${escapeHtml(item.kind ? `${item.kind}: ` : "")}${escapeHtml(item.name)}${item.detail ? ` · ${escapeHtml(item.detail)}` : ""}${item.reason ? ` · ${escapeHtml(item.reason)}` : ""}</li>`).join("")}</ul></div>` : "";
        const choices = result.choices?.length ? `<div class="assistant-options"><strong>Choose one</strong><div class="assistant-choice-list">${result.choices.map((choice) => `<button type="button" data-assistant-choice="${escapeHtml(String(choice.number))}">${escapeHtml(`${choice.number}. ${choice.label}`)}</button>`).join("")}</div></div>` : "";
        messages.insertAdjacentHTML("beforeend", `<div class="assistant-message">${escapeHtml(result.message)}${summaryDetails}${itemList}${replacementList}${pullForwardList}${choices}</div>`);
        $$('[data-assistant-choice]', messages).forEach((choice) => {
          if (choice.dataset.bound) return;
          choice.dataset.bound = "true";
          choice.addEventListener("click", () => {
            input.value = choice.dataset.assistantChoice;
            input.focus();
          });
        });
        if (["cancelled", "absence_recorded"].includes(result.outcome)) {
          state.overview = await api("/api/overview");
          const badge = $("#attentionBadge");
          badge.textContent = state.overview.needsAttention;
          badge.hidden = !state.overview.needsAttention;
        }
      } catch (error) {
        messages.insertAdjacentHTML("beforeend", `<div class="assistant-message error-message">${escapeHtml(error.message)}</div>`);
      } finally {
        messages.scrollTop = messages.scrollHeight;
        button.disabled = false; button.textContent = "Send request";
        input.focus();
      }
    });
    $$('[data-assistant-command]').forEach((command) => command.addEventListener("click", () => {
      const input = $("#assistantInput");
      input.value = command.dataset.assistantCommand;
      input.focus();
    }));
    if (workflowWorkspace) window.therapyWhatsapp.mountOperations({
      root, date: isoDate(state.currentDate), api, onChanged: refreshAfterWhatsapp,
    });
    bindReleasedAppointments(root, operations);
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function loadGeneratorView() {
  const root = $("#view-generator");
  const target = state.overview.nextPlanningDate;
  if (state.latestPreview && state.latestPreview.start !== target) {
    state.latestPreview = null;
    state.showPreview = false;
  }
  root.innerHTML = `${pageHeading("Daily therapy planning", "Schedule generator")}
    <section class="generator-hero">
      <div><p class="eyebrow" style="color:#5ed5cd">Daily planning</p><h2>Build a safe schedule around every child and therapist.</h2><p>Plan tomorrow’s sessions. Saved visits stay fixed and count toward weekly demand.</p><div class="rule-strip"><span>${icon("check")}Patient availability</span><span>${icon("check")}Therapist coverage</span><span>${icon("check")}Saved visits</span><span>${icon("check")}Weekly targets</span></div></div>
      <form class="generator-form" id="generatorForm">
        <label>Planning date<input id="generatorStart" type="date" value="${escapeHtml(target)}" readonly></label>
        <label>Search time per centre<select id="generatorSearchSeconds">${workflowWorkspace ? '<option value="300">Up to 5 minutes per centre</option>' : '<option value="20">Up to 20 seconds per centre</option><option value="60">Up to 1 minute per centre</option><option value="120">Up to 2 minutes per centre</option><option value="300" selected>Up to 5 minutes per centre</option>'}</select></label>
        <button class="primary-button" type="submit">Generate validated preview</button>
        <p class="fine-print">Review tomorrow’s draft before saving. ${workflowWorkspace ? "Saving queues tomorrow’s WhatsApp offers." : ""}</p>
      </form>
    </section>
    <div id="generatorResults">${state.latestPreview ? renderGeneratorResults(state.latestPreview) : `<section class="panel"><div class="panel-empty">${icon("activity")}<strong>No preview generated yet</strong><span>Generate tomorrow’s draft to see proposed sessions and remaining weekly demand.</span></div></section>`}</div>`;
  $("#generatorForm").addEventListener("submit", generateSchedule);
  bindSavePreview();
  if (workflowWorkspace && !state.latestPreview) loadAutomaticTestDraft(root);
}

async function loadAutomaticTestDraft(root) {
  try {
    const response = await api("/api/zoho/draft");
    const draft = response.draft || (response.planning ? await waitForPlanning(response.planning) : null);
    if (!draft || draft.start !== state.overview.nextPlanningDate || !root.isConnected || state.activeView !== "generator" || state.latestPreview) return;
    state.latestPreview = draft;
    state.currentDate = parseLocalDate(draft.start);
    $("#generatorStart").value = draft.start;
    $("#generatorResults").innerHTML = renderGeneratorResults(draft);
    bindSavePreview();
    toast("Daily test draft ready", `Review ${draft.newSessions} proposed sessions for ${draft.start}.`);
  } catch (error) {
    toast("Daily test draft unavailable", error.message, "error");
  }
}

async function waitForPlanning(job) {
  while (job.status === "QUEUED" || job.status === "PROCESSING") {
    await new Promise(resolve => setTimeout(resolve, 1500));
    job = await api(`/api/schedules/jobs/${job.jobId}`);
  }
  if (job.status !== "DONE") throw new Error(job.error || "Planning did not complete. Generate a fresh preview.");
  return job.preview;
}

async function generateSchedule(event) {
  event.preventDefault();
  const button = $('button[type="submit"]', event.currentTarget);
  const start = $("#generatorStart").value;
  const searchSeconds = Number($("#generatorSearchSeconds").value);
  const searchLabel = searchSeconds >= 60 ? `${searchSeconds / 60} minute${searchSeconds === 60 ? "" : "s"}` : `${searchSeconds} seconds`;
  button.disabled = true;
  $("#generatorSearchSeconds").disabled = true;
  button.innerHTML = `<span class="spinner" style="width:16px;height:16px;border-color:rgba(255,255,255,.35);border-top-color:white"></span>Planning tomorrow…`;
  $("#generatorResults").innerHTML = `<section class="panel"><div class="loading-state" role="status" aria-live="polite" style="min-height:260px"><span class="spinner"></span><strong>Planning tomorrow’s sessions, one centre at a time</strong><span>Up to ${searchLabel} per centre or connected group. Each search finishes early when the best result is proven. Review the validated draft before saving.</span></div></section>`;
  try {
    const response = await api("/api/schedules/generate", { method: "POST", body: JSON.stringify({ start, searchSeconds, planningMode: "day", background: workflowWorkspace }) });
    const result = response.jobId ? await waitForPlanning(response) : response;
    state.latestPreview = result;
    $("#generatorResults").innerHTML = renderGeneratorResults(result);
    bindSavePreview();
    toast("Schedule preview ready", `${result.newSessions} new sessions proposed for ${result.start}; ${result.unscheduled} weekly sessions remain unplaced.`);
  } catch (error) {
    $("#generatorResults").innerHTML = errorState(error.message);
    toast("Scheduling didn’t complete", error.message, "error");
  } finally {
    button.disabled = false;
    const search = $("#generatorSearchSeconds");
    if (search) search.disabled = false;
    button.textContent = "Generate validated preview";
  }
}

function renderGeneratorResults(result) {
  const validationTotal = Object.values(result.validation || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `<div class="result-banner ${result.canSave ? "" : "error"}"><div><h3>${result.canSave ? "Preview passed every validation check" : "Preview needs review before it can be saved"}</h3><p>${result.status} · ${validationTotal} validation issue${validationTotal === 1 ? "" : "s"} · New visits from ${escapeHtml(result.planningStart || result.start)} to ${escapeHtml(result.end)}</p>${workflowWorkspace ? `<p>${result.autoOfferOnSave ? (result.messagingPaused ? "Saving holds tomorrow’s WhatsApp offers while messaging is off." : result.liveMessaging ? "Saving queues WhatsApp offers for tomorrow’s proposed visits. Existing offers are not resent." : "Saving queues tomorrow’s preview offers. No real messages are sent.") : "Saving this plan queues no WhatsApp offers. Automatic offers apply only to tomorrow in India time."}</p>` : ""}</div><div class="heading-actions"><button class="secondary-button" id="reviewPreview">${icon("calendar")}Review calendar</button>${result.canSave ? `<button class="primary-button" id="savePreview">${savePreviewLabel(result)}</button>` : ""}</div></div>
    <div class="metric-grid">
      ${metricCard("Weekly requested", result.requested, "Whole-week targets for the chosen roster")}
      ${metricCard("Sessions tomorrow", result.scheduledOnSelectedDay, `${result.newSessions} new proposals, including saved visits in the total`)}
      ${metricCard("Remaining this week", result.unscheduled, "Demand still available for later working days", result.unscheduled ? "accent" : "")}
      ${metricCard("Solver status", 0, result.status).replace('<div class="metric-value">0</div>', `<div class="metric-value" style="font-size:1.15rem;margin-top:20px">${escapeHtml(result.status)}</div>`)}
    </div>
    <section class="panel"><div class="panel-header"><div><h2>Unmet demand</h2><p>Clear reasons for every session the planner could not place.</p></div><span class="soft-pill">${result.unmet.length.toLocaleString("en-IN")} requirements</span></div>
      ${result.unmet.length ? `<div class="unmet-list">${result.unmet.map((item) => `<div class="unmet-row"><strong>${escapeHtml(item.patientCode)}</strong><span>${escapeHtml(item.therapy)}</span><span class="unmet-reasons"><strong>Primary: ${escapeHtml(item.reasonLabel)}</strong>${item.secondaryReasonLabels?.length ? `<small>Also affected by: ${item.secondaryReasonLabels.map(escapeHtml).join(" · ")}</small>` : `<small>No additional blocking reason detected</small>`}</span><strong>${item.missing_sessions}</strong></div>`).join("")}</div>` : `<div class="panel-empty">${icon("check")}<strong>All requested sessions were placed</strong><span>No unmet therapy demand remains in this planning window.</span></div>`}
    </section>`;
}

function bindSavePreview() {
  const reviewButton = $("#reviewPreview");
  if (reviewButton && state.latestPreview) {
    reviewButton.addEventListener("click", async () => {
      state.showPreview = true;
      state.currentDate = parseLocalDate(state.latestPreview.planningStart || state.latestPreview.start);
      setView("schedule");
      await loadSchedule();
    });
  }
  const button = $("#savePreview");
  if (!button || !state.latestPreview) return;
  button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Saving…";
    try {
      const result = await api(`/api/schedules/${state.latestPreview.previewId}/save`, { method: "POST", body: "{}" });
      if (workflowWorkspace && result.offers > 0 && !state.latestPreview.liveMessaging && !state.latestPreview.messagingPaused) {
        try { await api("/api/process", {method:"POST", body:"{}"}); }
        catch (error) { toast("Preview queue needs attention", error.message, "error"); }
      }
      toast("Schedule saved", result.message);
      if (state.latestPreview.planningMode === "day") state.currentDate = parseLocalDate(state.latestPreview.start);
      state.latestPreview = null;
      state.showPreview = false;
      await loadSchedule();
      setView("schedule");
    } catch (error) { toast("Couldn’t save preview", error.message, "error"); button.disabled = false; button.textContent = savePreviewLabel(state.latestPreview); }
  });
}

async function refreshAfterWhatsapp(appointmentId = null) {
  state.overview = await api("/api/overview");
  const badge = $("#attentionBadge");
  badge.textContent = state.overview.needsAttention;
  badge.hidden = !state.overview.needsAttention;
  await loadSchedule();
  if (state.activeView === "operations" && $("#operationsRescheduling")) {
    const operations = await api("/api/operations");
    $("#operationsRescheduling").outerHTML = renderReleasedAppointments(operations);
    bindReleasedAppointments($("#view-operations"), operations);
    const count = $("[data-rescheduling-count]");
    if(count) count.textContent = operations.needsRescheduling.length;
  }
  if (appointmentId && $("#appointmentDrawer")?.classList.contains("open")) {
    const updated = state.schedule.appointments.find(a => a.id === appointmentId);
    if (updated && !["NEEDS_RESCHEDULING", "CANCELLED_PATIENT", "CANCELLED_OTHER"].includes(updated.status)) {
      await openAppointment(updated);
    } else closeDrawers();
  }
}

function openDrawer(id) {
  closeDrawers();
  const drawer = document.getElementById(id);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").hidden = false;
  document.body.style.overflow = "hidden";
}

function closeDrawers() {
  const activeDrawer = document.activeElement?.closest(".drawer.open");
  if (activeDrawer) document.activeElement.blur();
  $$(".drawer.open").forEach((drawer) => { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); });
  $("#drawerBackdrop").hidden = true;
  document.body.style.overflow = "";
}

function ensureAppointmentDrawer() {
  let drawer = $("#appointmentDrawer");
  if (drawer) return drawer;
  drawer = document.createElement("aside");
  drawer.id = "appointmentDrawer";
  drawer.className = "drawer";
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("aria-labelledby", "appointmentDrawerTitle");
  document.body.append(drawer);
  return drawer;
}

async function openAppointment(item) {
  if (!item.preview && ["NEEDS_RESCHEDULING", "CANCELLED_PATIENT", "CANCELLED_OTHER"].includes(item.status)) {
    showReplacementCandidates([{...item, appointmentId: item.id, branch: item.location}]);
    return;
  }
  const drawer = ensureAppointmentDrawer();
  const canChange = !item.preview && ["PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED"].includes(item.status);
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Appointment details</p><h2 id="appointmentDrawerTitle">${formatDate(parseLocalDate(item.date), { weekday: "long", day: "numeric", month: "long" })}</h2></div><button class="icon-button" data-close-appointment aria-label="Close appointment">${icon("close")}</button></div>
    <div class="drawer-body"><div class="appointment-hero"><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span><h3>${escapeHtml(item.patientName)}</h3><p>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</p></div>
      <div class="detail-list">
        <div class="detail-item"><span>${icon("clock")}</span><div><small>Time</small><strong>${escapeHtml(item.time)}</strong></div></div>
        <div class="detail-item"><span>${icon("user")}</span><div><small>Clinician</small><strong>${escapeHtml(item.therapistName)} · ${escapeHtml(item.therapistCode)}</strong></div></div>
        <div class="detail-item"><span>${icon("location")}</span><div><small>Location</small><strong>${escapeHtml(item.location)}</strong></div></div>
        <div class="detail-item"><span>${icon("activity")}</span><div><small>Schedule source</small><strong>${escapeHtml(item.source)}${item.bundle ? ` · bundle ${escapeHtml(item.bundle)}` : ""}</strong></div></div>
      </div>
      ${canChange ? `<section class="drawer-section"><h3>Move this visit</h3><form class="form-grid" id="moveAppointmentForm"><label>New date<input type="date" id="moveDate" value="${item.date}" required></label><label>New starting time<select id="moveSlot">${state.overview.slotLabels.map((label, index) => `<option value="${index}" ${index === item.slot ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select></label><button class="secondary-button" type="submit">Validate and move</button></form></section><div class="drawer-actions"><button class="danger-button" id="cancelAppointment">Record patient cancellation</button></div>` : ""}
    </div>`;
  $('[data-close-appointment]', drawer).addEventListener("click", closeDrawers);
  if (canChange) {
    $("#moveAppointmentForm", drawer).addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget); button.disabled = true; button.textContent = "Validating…";
      try {
        const result = await api(`/api/appointments/${item.id}/move`, { method: "POST", body: JSON.stringify({ date: $("#moveDate").value, slot: Number($("#moveSlot").value) }) });
        toast("Appointment moved", result.message); closeDrawers(); await loadSchedule();
      } catch (error) { toast("Move blocked by scheduling rules", error.message, "error"); button.disabled = false; button.textContent = "Validate and move"; }
    });
    $("#cancelAppointment", drawer).addEventListener("click", async () => {
      if (!window.confirm(`Record a patient cancellation for ${item.patientName} on ${formatDate(parseLocalDate(item.date), { day: "numeric", month: "long" })}?`)) return;
      const button = $("#cancelAppointment", drawer); button.disabled = true; button.textContent = "Recording…";
      try {
        const result = await api(`/api/appointments/${item.id}/cancel`, { method: "POST", body: "{}" });
        const candidates = (result.replacementGroups || []).reduce((total, group) => total + group.candidates.length, 0);
        toast("Cancellation recorded", candidates ? `${candidates} continuity-preserving replacement option${candidates === 1 ? "" : "s"} found.` : "The opening is ready for the next repair run.");
        closeDrawers(); await loadSchedule();
        if (result.replacementGroups?.length) showReplacementCandidates(result.replacementGroups);
      } catch (error) { toast("Couldn’t record cancellation", error.message, "error"); button.disabled = false; button.textContent = "Record patient cancellation"; }
    });
  }
  openDrawer("appointmentDrawer");
  if (workflowWorkspace && !item.preview) {
    const section = document.createElement("section");
    section.className = "drawer-section";
    $(".detail-list", drawer).after(section);
    window.therapyWhatsapp.mountAppointment({
      root: section, item, api,
      onChanged: () => refreshAfterWhatsapp(item.id),
    });
  }
}

function showReplacementCandidates(groups) {
  if (!groups.length) return;
  const drawer = ensureAppointmentDrawer();
  const instance = newRequestId();
  drawer.dataset.reservationInstance = instance;
  let group = groups[0], selectedPatient = null, preview = null, requestId = null;
  const emptyBooking = Boolean(group.emptySlot);
  const bookingUrl = action => emptyBooking ? `/api/calendar/${action}` : `/api/appointments/${group.appointmentId}/${action}`;
  let version = 0, searchTimer = null, suggestionsMode = !emptyBooking, saving = false, submittedMode = null;
  let slotVersion = 0, contextReady = emptyBooking, choosingPatientId = null;
  const canMessage = workflowWorkspace && !emptyBooking;
  const current = () => drawer.dataset.reservationInstance === instance && drawer.classList.contains("open") && Boolean($("#replacementConfirm",drawer));
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">${emptyBooking ? "Calendar appointment" : "Released appointment"}</p><h2 id="appointmentDrawerTitle">${emptyBooking ? "Add appointment" : "Possible replacements"}</h2></div><button class="icon-button" data-close-appointment aria-label="Close">${icon("close")}</button></div>
    <div class="drawer-body"><p class="candidate-intro">Choose a patient to reserve this opening. Scheduling mismatches are warnings; the admin may proceed after reviewing them.</p>
      ${emptyBooking ? `<div class="form-grid empty-slot-fields"><label>Doctor<select id="emptySlotDoctor"><option value="">Choose doctor</option>${state.schedule.therapists.map(item => `<option value="${item.id}" ${item.id === group.emptySlot.therapistId ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}</select></label><label>Therapy<select id="emptySlotTherapy">${state.schedule.departments.map(item => `<option value="${item.id}" ${item.id === group.emptySlot.departmentId ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label><label>Branch<select id="emptySlotBranch">${state.schedule.locations.map(item => `<option value="${item.id}" ${item.id === group.emptySlot.locationId ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label></div>` : ""}
      ${groups.length > 1 ? `<label class="replacement-opening-label">Released slot<select id="replacementOpening">${groups.map((item, index) => `<option value="${index}">${escapeHtml(item.time)} · ${escapeHtml(item.therapy)}</option>`).join("")}</select></label>` : ""}
      <div id="replacementSlotSummary" class="replacement-slot-summary"></div>
      <label class="replacement-whole"><input type="checkbox" id="replacementWhole" checked><span id="replacementWholeLabel">Reserve complete visit</span></label>
      <div class="replacement-picker-tabs"><button id="replacementSuggested" class="${emptyBooking ? "" : "active"}" type="button" ${emptyBooking ? "hidden" : ""}>Suggested patients</button><button id="replacementAll" class="${emptyBooking ? "active" : ""}" type="button">Choose patient</button></div>
      <input type="search" id="replacementSearch" placeholder="Search patient name or code" aria-label="Search replacement patients">
      <div id="replacementPatientList" class="replacement-patient-list" aria-live="polite"></div>
      <div id="replacementReview" class="replacement-review"><p class="candidate-intro">Select any patient to see warnings and confirmation options.</p></div>
      <div id="replacementError" class="replacement-error" role="alert" hidden></div>
    </div><div class="drawer-footer replacement-footer"><button class="primary-button" id="replacementConfirm" disabled>Confirm without messaging</button><button class="secondary-button" id="replacementMessage" disabled title="${canMessage ? "Queue a WhatsApp offer and wait for the parent to confirm" : "Messaging is available for released appointments in the test workspace"}">${canMessage ? "Send offer & wait for confirmation" : "Confirm with messaging"}</button><p>Without messaging confirms immediately.${canMessage ? " Sending an offer holds the slot as pending until the parent replies Yes. In preview mode, no real message is sent." : " No message is sent."}</p></div>`;
  $('[data-close-appointment]', drawer).addEventListener("click", closeDrawers);
  openDrawer("appointmentDrawer");

  async function choosePatient(item) {
    if (saving || !item || !contextReady || choosingPatientId === item.id) return;
    selectedPatient = item; preview = null; requestId = null; submittedMode = null;
    choosingPatientId = item.id;
    $$('[data-replacement-patient]', $("#replacementPatientList",drawer)).forEach(button => button.setAttribute("aria-pressed", String(button.dataset.replacementPatient === String(item.id))));
    const latest = ++version;
    $("#replacementConfirm", drawer).disabled = true;
    $("#replacementMessage", drawer).disabled = true;
    $("#replacementError", drawer).hidden = true;
    $("#replacementReview", drawer).innerHTML = `<h3>${escapeHtml(item.name)}</h3><p>Checking reservation details…</p>`;
    if(emptyBooking && !group.emptySlot.therapistId) {
      choosingPatientId = null;
      $("#replacementReview",drawer).innerHTML='<p class="candidate-intro">Choose a doctor to review this reservation.</p>';
      return;
    }
    try {
      const data = await api(bookingUrl("reservation-preview"), {method:"POST",body:JSON.stringify({...group.emptySlot,patientId:item.id, wholeVisit:$("#replacementWhole",drawer).checked})});
      if (!current() || latest !== version) return;
      preview = data; requestId = newRequestId();
      $("#replacementReview", drawer).innerHTML = `<h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.code)} · ${data.slots.length} session(s) · ${data.slots.map(slot => escapeHtml(slot.time)).join(", ")}</p>${data.warnings.length ? `<div class="replacement-warnings" role="status"><strong>${data.warnings.length} warning(s) — admin can override</strong><ul>${data.warnings.map(warning => `<li>${escapeHtml(warning.message)}</li>`).join("")}</ul><p>Reserve anyway confirms your choice and records these warnings.</p></div>` : '<div class="replacement-match">No scheduling mismatches found.</div>'}`;
      $("#replacementConfirm", drawer).textContent = data.warnings.length ? "Reserve anyway without messaging" : "Confirm without messaging";
      $("#replacementConfirm", drawer).disabled = false;
      if(canMessage) $("#replacementMessage", drawer).textContent = data.warnings.length ? "Send offer anyway & wait for confirmation" : "Send offer & wait for confirmation";
      $("#replacementMessage", drawer).disabled = !canMessage;
      $("#replacementReview", drawer).scrollIntoView({behavior:"smooth",block:"nearest"});
    } catch (error) {if(current() && latest === version) {$("#replacementError",drawer).hidden=false; $("#replacementError",drawer).textContent=error.message;}}
    finally { if (latest === version) choosingPatientId = null; }
  }

  let listVersion = 0;
  async function loadPatientChoices(cachedCandidates = null) {
    if (!current()) return;
    const useSuggestions = suggestionsMode;
    const appointmentId = group.appointmentId;
    const wholeVisit = $("#replacementWhole",drawer).checked;
    const query = $("#replacementSearch", drawer).value.trim();
    const latest = ++listVersion;
    const root = $("#replacementPatientList", drawer);
    $("#replacementAll",drawer).classList.toggle("active", !suggestionsMode);
    $("#replacementSuggested",drawer).classList.toggle("active", suggestionsMode);
    root.innerHTML = '<p class="candidate-intro" role="status">Loading patients…</p>';
    try {
      const suggestion = useSuggestions ? (cachedCandidates !== null ? {candidates: cachedCandidates} : await api(`/api/appointments/${appointmentId}/replacement-candidates?wholeVisit=${wholeVisit}`)) : null;
      if (!current() || latest !== listVersion) return;
      if (useSuggestions && wholeVisit && !emptyBooking && (group.visitSessions || 1) > 1 && !suggestion.candidates.length) {
        // Nobody can take the whole visit: show who can take this session, best fit first.
        $("#replacementWhole",drawer).checked = false;
        $("#replacementPatientList",drawer).innerHTML = '<p class="candidate-intro" role="status">No child can take the complete visit. Showing children for this session…</p>';
        return loadPatientChoices();
      }
      const fitText = item => item.joins_existing_visit ? "joins their visit today" : item.visit_sessions > 1 ? (item.sessions_covered === item.visit_sessions ? `can take all ${item.visit_sessions} sessions` : `can take ${item.sessions_covered} of ${item.visit_sessions} sessions`) : "";
      const people = useSuggestions ? suggestion.candidates.map(item => ({id:item.patient_id,name:item.patient_name || item.patient_code,code:item.patient_code,active:true,progress:item.weekly_progress,fit:fitText(item)})) : (await api(`/api/patients?q=${encodeURIComponent(query)}`)).patients;
      if (!current() || latest !== listVersion) return;
      const filtered = people.filter(item => item.active && (!query || `${item.name} ${item.code}`.toLowerCase().includes(query.toLowerCase())));
      const emptyMessage = useSuggestions ? "No matching suggestions. Choose patient searches all active patients." : "No matching active patients. Try searching by name or patient ID.";
      root.innerHTML = filtered.length ? filtered.slice(0,50).map(item => `<button class="replacement-patient" data-replacement-patient="${item.id}"><span class="avatar">${escapeHtml(initials(item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}${item.progress ? ` · ${escapeHtml(item.progress)} weekly sessions` : ""}${item.fit ? ` · ${escapeHtml(item.fit)}` : ""}</small></span>${icon("chevron-right")}</button>`).join("") + (filtered.length>50 ? '<p class="candidate-intro">Showing 50 matches. Refine your search to find another patient.</p>' : "") : `<p class="candidate-intro">${emptyMessage}</p>`;
      $$('[data-replacement-patient]',root).forEach(button => {
        button.disabled = !contextReady;
        button.setAttribute("aria-pressed", String(String(selectedPatient?.id) === button.dataset.replacementPatient));
        button.addEventListener("click", () => choosePatient(filtered.find(item => String(item.id) === button.dataset.replacementPatient)));
      });
    } catch(error) {if(current() && latest === listVersion) {
      root.innerHTML = `${errorState(error.message)}<button type="button" data-retry-patients>Retry loading patients</button>`;
      $('[data-retry-patients]', root).addEventListener("click", () => loadPatientChoices());
    }}
  }

  async function loadSlot() {
    if (saving) return;
    const latestSlot = ++slotVersion;
    const appointmentId = group.appointmentId;
    clearTimeout(searchTimer); listVersion++; choosingPatientId=null;
    contextReady=emptyBooking;
    version++; preview=null; selectedPatient=null;
    $("#replacementError",drawer).hidden=true;
    $("#replacementPatientList",drawer).innerHTML='<p class="candidate-intro" role="status">Loading suggestions…</p>';
    $("#replacementWhole",drawer).disabled=true;
    $("#replacementConfirm",drawer).disabled=true;
    $("#replacementMessage",drawer).disabled=true;
    $("#replacementReview",drawer).innerHTML='<p class="candidate-intro">Select any patient to review this reservation.</p>';
    $("#replacementSlotSummary",drawer).innerHTML=`<strong>${escapeHtml(group.therapy || "New appointment")}</strong><p>${escapeHtml(group.branch || "")} · ${formatDate(parseLocalDate(group.date), {day:"numeric",month:"short"})} · ${escapeHtml(group.time)}</p>`;
    try {
      if (emptyBooking) {
        $("#replacementWhole",drawer).checked=false;
        $("#replacementWhole",drawer).disabled=true;
        $("#replacementWholeLabel",drawer).textContent="Reserve this session";
        await loadPatientChoices();
        if (group.preselectedPatientId) {
          const patient = state.schedule.patients.find(item => item.id === group.preselectedPatientId);
          if(patient && current()) await choosePatient(patient);
        }
        return;
      }
      const context = await api(`/api/appointments/${appointmentId}/reservation-context`);
      if(!current() || latestSlot !== slotVersion) return;
      group=context;
      contextReady=true;
      $("#replacementWhole",drawer).checked=true;
      $("#replacementWhole",drawer).disabled=context.visitSessions<=1;
      $("#replacementWholeLabel",drawer).textContent=context.visitSessions>1 ? `Reserve complete visit (${context.visitSessions} sessions)` : "Reserve this session";
      await loadPatientChoices(context.candidates);
    } catch(error) {if(current() && latestSlot === slotVersion) {
      $("#replacementPatientList",drawer).innerHTML='<button type="button" data-retry-opening>Retry loading this opening</button>';
      $('[data-retry-opening]',drawer).addEventListener("click",loadSlot);
      $("#replacementError",drawer).hidden=false;$("#replacementError",drawer).textContent=error.message;
    }}
  }
  $("#replacementAll",drawer).addEventListener("click",()=>{suggestionsMode=false;loadPatientChoices();});
  $("#replacementSuggested",drawer).addEventListener("click",()=>{suggestionsMode=true;loadPatientChoices();});
  $("#replacementSearch",drawer).addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadPatientChoices,180);});
  $("#replacementWhole",drawer).addEventListener("change",()=>{choosingPatientId=null;loadPatientChoices();if(selectedPatient) choosePatient(selectedPatient);});
  $("#replacementOpening",drawer)?.addEventListener("change",event=>{group=groups[Number(event.target.value)];loadSlot();});
  if (emptyBooking) {
    ["#emptySlotDoctor", "#emptySlotTherapy", "#emptySlotBranch"].forEach(selector => $(selector,drawer).addEventListener("change", () => {
      if(selector === "#emptySlotDoctor") {
        const doctor = state.schedule.therapists.find(item => item.id === Number($(selector,drawer).value));
        if(doctor) {$("#emptySlotTherapy",drawer).value=doctor.departmentId;$("#emptySlotBranch",drawer).value=doctor.locationId;}
      }
      group.emptySlot={...group.emptySlot,therapistId:Number($("#emptySlotDoctor",drawer).value),departmentId:Number($("#emptySlotTherapy",drawer).value),locationId:Number($("#emptySlotBranch",drawer).value)};
      choosingPatientId=null;
      if(selectedPatient) choosePatient(selectedPatient);
    }));
  }
  async function reserve(mode) {
    if(!preview || !selectedPatient || saving) return;
    if(submittedMode && submittedMode !== mode) {
      $("#replacementError",drawer).hidden=false;
      $("#replacementError",drawer).textContent="Retry the same action to check the previous request, or refresh this opening before changing modes.";
      return;
    }
    const bookingPatient=selectedPatient, bookingPreview=preview, bookingVersion=version;
    const message = mode === "with_messaging";
    submittedMode=mode; saving=true;
    $("#replacementError",drawer).hidden=true;
    const inputs=$$("button, input, select",drawer).filter(input=>!input.hasAttribute("data-close-appointment"));
    const disabled=inputs.map(input=>input.disabled);
    inputs.forEach(input=>input.disabled=true);
    let saved=false;
    try {
      const result=await api(bookingUrl("reserve"),{method:"POST",body:JSON.stringify({...group.emptySlot,patientId:bookingPatient.id,wholeVisit:bookingPreview.whole_visit,previewToken:bookingPreview.preview_token,requestId,overrideWarnings:bookingPreview.warnings.length>0,mode})});
      saved=true;
      if(message && result.mode === "preview") {
        try { await api("/api/process",{method:"POST",body:"{}"}); }
        catch { toast("Offer queued", "Open Operations to check queue processing."); }
      }
      if(current()) closeDrawers();
      toast(message ? "Offer saved" : "Slot reserved", message
        ? `${bookingPatient.name}: ${formatStatus(result.status)}. Track the offer and reply in WhatsApp controls.`
        : `${bookingPatient.name}: ${result.appointment_ids.length} session(s) confirmed without messaging.`);
      state.overview=await api("/api/overview");await loadSchedule();
      if(state.activeView==="operations") await loadOperationsView();
      if(message) {
        const appointment=state.schedule.appointments.find(item=>item.id===result.appointment_ids[0]);
        if(appointment) await openAppointment(appointment);
      }
    } catch(error) {
      if(saved) toast("Booking saved", "Refresh the calendar to view its current status.", "error");
      else if(current() && version===bookingVersion) {
        $("#replacementError",drawer).hidden=false;
        $("#replacementError",drawer).textContent=error.message;
      }
    } finally {
      saving=false;
      if(current() && version===bookingVersion && !saved) inputs.forEach((input,index)=>input.disabled=disabled[index]);
    }
  }
  $("#replacementConfirm",drawer).addEventListener("click",()=>reserve("without_messaging"));
  $("#replacementMessage",drawer).addEventListener("click",()=>reserve("with_messaging"));
  loadSlot();
}

initialize();

if (!workflowWorkspace) {
  fetch('/api/workflow/status').then(response => response.ok ? response.json() : null).then(status => {
    if (!status?.enabled) return;
    const link = document.createElement('a');
    link.className = 'nav-item';
    link.href = status.path || '/';
    link.innerHTML = `${icon('operations')}<span>Daily operations</span>`;
    document.querySelector('[aria-label="Planning navigation"]').append(link);
  }).catch(() => { /* The original workspace remains usable if operations are unavailable. */ });
}
