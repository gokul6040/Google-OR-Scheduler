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
  patientStatusFilter: "all",
  patientCentreFilter: "",
  latestPreview: null,
  showPreview: false,
  assistantSessionId: null,
  assistantMemoryContext: null,
  operationsTab: "assistant",
  shiftView: "",
  shiftLocation: "",
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
  if (!response.ok) {
    const error = new Error(payload.error || "Something went wrong. Please try again.");
    error.status = response.status;
    error.details = payload;
    throw error;
  }
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
  return `<div class="page-heading"><div>${eyebrow ? `<p class="eyebrow">${escapeHtml(eyebrow)}</p>` : ""}<h1>${escapeHtml(title)}</h1></div>${action ? `<div class="heading-actions">${action}</div>` : ""}</div>`;
}

function setView(view, options = {}) {
  if (view === "generator") view = "schedule";
  state.activeView = view;
  if (view === "operations") state.operationsTab = options.operationsTab || "assistant";
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#mainContent").focus({ preventScroll: true });
  if (view === "overview") loadOverviewView();
  if (view === "patients") loadPatientsView();
  if (view === "team") loadTeamView();
  if (view === "shifts") loadShiftsView();
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
    const requestedView = new URLSearchParams(location.search).get("view");
    if (["overview", "patients", "team", "operations"].includes(requestedView)) setView(requestedView);
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
  // The calendar shows exactly one of two days: today (default) or tomorrow.
  $$('[data-calendar-day]').forEach((button) => button.addEventListener("click", async () => {
    state.currentDate = calendarDay(button.dataset.calendarDay);
    await loadSchedule();
  }));
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
      state.currentDate = calendarDay("today");
      await loadSchedule();
    }
  });
}

function calendarDay(which) {
  const today = parseLocalDate(state.overview.today);
  return which === "tomorrow" ? addDays(today, 1) : today;
}

async function loadSchedule() {
  // Only today or tomorrow; anything else (e.g. an old preview date) falls back to today.
  if (!["today", "tomorrow"].some((which) => sameDate(calendarDay(which), state.currentDate))) {
    state.currentDate = calendarDay("today");
  }
  const showingTomorrow = sameDate(calendarDay("tomorrow"), state.currentDate);
  $$('[data-calendar-day]').forEach((button) => {
    const active = (button.dataset.calendarDay === "tomorrow") === showingTomorrow;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const start = state.currentDate;
  const days = 1;
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
  populateShiftPicker();
}

// Calendar Shift picker: the default shift first, then the others, then All shifts.
// Shown only when some centre has more than one shift. It changes the view only.
function populateShiftPicker() {
  const picker = $("#shiftViewPicker"), select = $("#shiftViewSelect");
  if (!picker || !select) return;
  const options = state.schedule.shiftOptions || [];
  picker.hidden = options.length < 2;
  if (!options.includes(state.shiftView) && state.shiftView !== "all") state.shiftView = options[0] || "";
  select.innerHTML = options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")
    + (options.length > 1 ? '<option value="all">All shifts</option>' : "");
  select.value = state.shiftView;
  if (!select.dataset.bound) {
    select.dataset.bound = "1";
    select.addEventListener("change", () => { state.shiftView = select.value; renderSchedule(); });
  }
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
  const booked = appointments.filter(item => ["PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED", "COMPLETED"].includes(item.status));
  const visibleDoctors = new Set(booked.map((item) => item.therapistId));
  $("#visibleAppointmentCount").textContent = booked.length.toLocaleString("en-IN");
  $("#visibleTherapistCount").textContent = visibleDoctors.size.toLocaleString("en-IN");
  $("#visibleEntityLabel").textContent = "doctors scheduled";
  $("#previewBanner").hidden = !(state.showPreview && state.latestPreview);
  renderFilterChips();
  updateDateDisplay();
  renderDoctorCalendar(appointments);
}

function updateDateDisplay() {
  $("#dateDisplay").textContent = formatDate(state.currentDate, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

// A gap between two shown sessions is a break; it is "Lunch" when a shown doctor lunches then.
function calendarGapRow(session, next, doctors) {
  if (!next || next.start <= session.end) return "";
  const lunch = doctors.some((doctor) => (doctor.breaks || []).some((item) => item.kind === "lunch" && item.start < next.start && item.end > session.end));
  return `<div class="doctor-grid-row calendar-break-row"><div class="time-label">${escapeHtml(session.end)}</div><div class="calendar-break-label">${lunch ? "Lunch break" : "Break"} · ${escapeHtml(session.end)}–${escapeHtml(next.start)}</div></div>`;
}

// What a doctor is doing in a session they do not work: their lunch, a break, or off shift.
function offShiftLabel(doctor, session) {
  const inBreak = (doctor.breaks || []).find((item) => item.start < session.end && item.end > session.start);
  if (inBreak) return inBreak.kind === "lunch" ? "Lunch" : "Break";
  return "Off shift";
}

function calendarDoctors(appointments) {
  // Columns: every active doctor (busy ones first), plus any doctor holding a visit that day.
  // A doctor filter shows that doctor; patient, therapy, status or search shows only doctors with matches.
  const booked = new Set(appointments.map((item) => item.therapistId));
  const narrowedToVisits = Boolean(state.filters.patient || state.filters.department || state.filters.status || state.filters.search);
  const onShift = (doctor) => !state.shiftView || state.shiftView === "all"
    || (state.schedule.shiftOptions || []).length < 2 || doctor.shiftName === state.shiftView;
  return state.schedule.therapists.filter((doctor) => {
    if (state.filters.therapist) return String(doctor.id) === state.filters.therapist;
    if (!onShift(doctor)) return false;
    if (booked.has(doctor.id)) return true;
    if (narrowedToVisits || !doctor.active) return false;
    return !state.filters.location || String(doctor.locationId) === state.filters.location;
  }).sort((a, b) => (booked.has(b.id) - booked.has(a.id)) || a.name.localeCompare(b.name));
}

function renderDoctorCalendar(appointments) {
  const day = isoDate(state.currentDate);
  const saved = state.schedule.appointments.filter((item) => !item.preview);
  if (state.currentDate.getDay() === 0) {
    $("#calendarSurface").innerHTML = `<div class="empty-state calendar-day-notice"><span class="empty-state-icon">${icon("calendar")}</span><h3>Closed on Sunday</h3><p>There are no therapy sessions on Sundays.</p></div>`;
    return;
  }
  const isTomorrow = sameDate(calendarDay("tomorrow"), state.currentDate);
  if (isTomorrow && !saved.length && !(state.showPreview && state.latestPreview)) {
    $("#calendarSurface").innerHTML = `<div class="empty-state calendar-day-notice not-scheduled"><span class="empty-state-icon">${icon("calendar")}</span><h3>NOT SCHEDULED YET</h3><p>Tomorrow's schedule appears here after automatic planning saves it.</p></div>`;
    return;
  }
  const doctors = calendarDoctors(appointments);
  if (!doctors.length) {
    $("#calendarSurface").innerHTML = emptySchedule();
    return;
  }
  const headers = doctors.map((doctor) => `<div class="doctor-header${doctor.active ? "" : " is-inactive"}" title="${escapeHtml(doctor.zohoDepartment ? `${doctor.name} · ${doctor.zohoDepartment}` : doctor.name)}"><span class="doctor-header-name">${escapeHtml(doctor.name)}</span>${doctor.zohoDepartment ? `<span class="doctor-header-department">${escapeHtml(doctor.zohoDepartment)}</span>` : ""}</div>`).join("");
  // Rows: the sessions these doctors work (plus any session holding a booking), in clock order.
  const shown = new Set(doctors.flatMap((doctor) => doctor.sessions || state.schedule.defaultSessions || []));
  appointments.forEach((item) => { if (doctors.some((doctor) => doctor.id === item.therapistId)) shown.add(item.slot); });
  const sessions = (state.schedule.sessions || []).filter((session) => shown.has(session.id));
  const rows = sessions.map((session, index) => {
    const cells = doctors.map((doctor) => {
      const items = appointments.filter((item) => item.slot === session.id && item.therapistId === doctor.id);
      const works = (doctor.sessions || state.schedule.defaultSessions || []).includes(session.id);
      if (!items.length && !works) return `<div class="doctor-cell off-shift">${escapeHtml(offShiftLabel(doctor, session))}</div>`;
      return `<div class="doctor-cell" ${!items.length ? emptySlotAttributes(day, session.id, doctor.id) : ""}>${items.map(appointmentCard).join("")}</div>`;
    }).join("");
    return `<div class="doctor-grid-row"><div class="time-label">${escapeHtml(session.start)}</div>${cells}</div>${calendarGapRow(session, sessions[index + 1], doctors)}`;
  }).join("");
  $("#calendarSurface").innerHTML = `<div class="doctor-calendar" style="--doctor-columns:${doctors.length}"><div class="doctor-grid-header"><div class="time-corner">Time</div>${headers}</div>${rows}</div>`;
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
  const primary = item.patientName;
  const secondary = item.patientCode;
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
  const perspective = "doctor";
  return `<div class="empty-state"><span class="empty-state-icon">${icon(filtersActive ? "filter" : "calendar")}</span><h3>${filtersActive ? `No ${perspective} schedules match these filters` : "No sessions scheduled"}</h3><p>${filtersActive ? "Clear or adjust the calendar filters to broaden the view." : "Saved sessions appear here after automatic planning."}</p>${filtersActive ? '<button class="secondary-button" id="emptyAction">Clear filters</button>' : ''}</div>`;
}

function errorState(message) {
  return `<div class="empty-state"><span class="empty-state-icon">${icon("alert")}</span><h3>We couldn’t load this view</h3><p>${escapeHtml(message)}</p><button class="secondary-button" onclick="location.reload()">Try again</button></div>`;
}

function bindEmptyAction() {
  const action = $("#emptyAction");
  if (!action) return;
  action.addEventListener("click", () => {
    if (Object.values(state.filters).some(Boolean)) { clearFilters(); renderSchedule(); }
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
    root.innerHTML = `${pageHeading("", "Today at EIC", `<button class="primary-button" data-overview-schedule>${icon("calendar")}Open calendar</button>`)}
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
        </div></aside>
      </div>`;
    $('[data-overview-schedule]').addEventListener("click", () => setView("schedule"));
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

let patientDirectoryRequest = 0;
let zohoRefreshTimer = null;

// "Refresh from Zoho" (Patients and Clinical team pages): runs the nightly import now
// (server-side, in the background), then reloads whichever list is open.
// The last-refresh summary sits behind an (i) beside the button: shown on hover, focus or click.
function zohoRefreshControls(scope) {
  return `<span class="zoho-refresh-group"><button type="button" class="primary-button" data-zoho-refresh>${icon("activity")}Refresh from Zoho</button>`
    + `<span class="zoho-info" data-zoho-info hidden><button type="button" class="zoho-info-button" aria-label="Last refresh from Zoho" aria-expanded="false">${icon("info")}</button>`
    + `<span class="zoho-info-tip" role="status" aria-live="polite" data-zoho-refresh-status="${scope}"></span></span></span>`;
}

// Open the popover toward whichever side has room (the toolbar wraps on narrow screens).
function placeZohoInfo(info) {
  const tip = $(".zoho-info-tip", info);
  const room = info.getBoundingClientRect().right - ($("#mainContent")?.getBoundingClientRect().left || 0);
  info.classList.toggle("tip-right", room < tip.offsetWidth + 16);
}
["mouseover", "focusin"].forEach((type) => document.addEventListener(type, (event) => {
  const info = event.target.closest?.("[data-zoho-info]");
  if (info) placeZohoInfo(info);
}));

document.addEventListener("click", (event) => {
  const button = event.target.closest(".zoho-info-button");
  $$("[data-zoho-info].open").forEach((info) => {
    if (!button || info !== button.parentElement) {
      info.classList.remove("open");
      $(".zoho-info-button", info).setAttribute("aria-expanded", "false");
    }
  });
  if (button) {
    placeZohoInfo(button.parentElement);
    const open = button.parentElement.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  $$("[data-zoho-info].open").forEach((info) => {
    info.classList.remove("open");
    $(".zoho-info-button", info).setAttribute("aria-expanded", "false");
  });
});

function showZohoRefresh(status) {
  const running = status.state === "RUNNING";
  const when = (value) => value ? new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "";
  $$("[data-zoho-refresh]").forEach((button) => {
    button.disabled = running;
    button.innerHTML = `${icon("activity")}${running ? "Refreshing…" : "Refresh from Zoho"}`;
  });
  $$("[data-zoho-refresh-status]").forEach((line) => {
    if (running) {
      line.textContent = "Refreshing from Zoho… this takes about 1–2 minutes. You can keep working.";
    } else if (status.state === "FAILED") {
      line.textContent = `Last refresh failed (${when(status.finishedAt)}): ${status.error} Nothing was changed.`;
    } else if (status.state === "DONE" && line.dataset.zohoRefreshStatus === "team") {
      line.textContent = `Updated from Zoho ${when(status.finishedAt)} · New clinicians are added with their Zoho status; `
        + "existing clinicians keep the Active/Inactive set here.";
    } else if (status.state === "DONE") {
      const s = status.summary || {};
      line.textContent = `Updated from Zoho ${when(status.finishedAt)} · ${(s.newActive || []).length} new active · `
        + `${(s.newInactive || []).length} new inactive · ${(s.readyToActivate || []).length} inactive ready to activate · `
        + `${s.stillNeedFixing ?? 0} need fixing in Zoho.`;
    } else {
      line.textContent = "";
    }
    const info = line.closest("[data-zoho-info]");
    if (info) {
      info.hidden = !line.textContent;
      info.classList.toggle("failed", status.state === "FAILED");
    }
  });
}

async function pollZohoRefresh(reloadWhenDone = true) {
  window.clearTimeout(zohoRefreshTimer);
  try {
    const status = await api("/api/zoho/refresh");
    showZohoRefresh(status);
    if (status.state === "RUNNING") {
      zohoRefreshTimer = window.setTimeout(() => pollZohoRefresh(true), 3000);
    } else if (reloadWhenDone && status.state === "DONE") {
      const onTeam = state.activeView === "team";
      if ($("#patientSearch")) await loadPatientsView();
      if ($("#teamSearch")) await loadTeamView();
      toast(onTeam ? "Clinicians updated" : "Patients updated", onTeam
        ? "The latest Zoho data is loaded. New clinicians start with their Zoho status."
        : "The latest Zoho data is loaded. Inactive patients still need Mark active.");
    } else if (reloadWhenDone && status.state === "FAILED") {
      toast("Refresh didn’t finish", status.error, "error");
    }
  } catch (_) { /* Status is optional; the list and button keep working. */ }
}

async function startZohoRefresh() {
  try {
    showZohoRefresh(await api("/api/zoho/refresh", { method: "POST", body: "{}" }));
    zohoRefreshTimer = window.setTimeout(() => pollZohoRefresh(true), 3000);
  } catch (error) {
    toast("Couldn’t refresh", error.message, "error");
  }
}

async function loadPatientsView() {
  const root = $("#view-patients");
  const request = ++patientDirectoryRequest;
  if (!$("#patientSearch", root)) {
    root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading patient directory…</div>`;
  }
  try {
    const payload = await api("/api/patients");
    if (request !== patientDirectoryRequest) return;
    state.patients = payload.patients;
    if (payload.cancelStreakWarning) state.cancelStreakWarning = payload.cancelStreakWarning;
    renderPatientsView();
  } catch (error) {
    if (request !== patientDirectoryRequest) return;
    if ($("#patientSearch", root)) toast("Couldn’t refresh patients", error.message, "error");
    else root.innerHTML = errorState(error.message);
  }
}

// "6/16 sessions": saved sessions count until cancelled, declined or marked no-show.
function courseLabel(course) {
  if (course.target == null) return `${course.sessions} ${course.sessions === 1 ? "session" : "sessions"}`;
  return `${course.sessions}/${course.target} sessions${course.complete ? " · complete" : ""}`;
}

function cancelStreakBadge(count) {
  return count && count >= (state.cancelStreakWarning || 4) ? `<span class="cancel-streak-warning" title="The last ${count} sessions were cancelled, declined or missed. Please check with the family.">${icon("alert")}${count} cancelled in a row</span>` : "";
}

// Centre filter: every centre a child has a plan at (25+ centres, so it is searchable).
function syncPatientCentreFilter(root) {
  const select = $("#patientCentreFilter", root);
  const centres = [...new Set(state.patients.flatMap((item) => item.centres || []))].sort((a, b) => a.localeCompare(b));
  if (state.patientCentreFilter && !centres.includes(state.patientCentreFilter)) state.patientCentreFilter = "";
  const key = centres.join("|");
  if (select.dataset.centres !== key) {
    select.innerHTML = `<option value="">All centres</option>${centres.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}`;
    select.dataset.centres = key;
  }
  select.value = state.patientCentreFilter;
  makeSearchableSelect(select, "Search centres");
}

function renderPatientsView() {
  const root = $("#view-patients");
  const query = state.patientQuery.trim().toLowerCase();
  const patients = state.patients.filter(item =>
    (!query || `${item.name} ${item.code}`.toLowerCase().includes(query)) &&
    (state.patientStatusFilter === "all" || item.active === (state.patientStatusFilter === "active")) &&
    (!state.patientCentreFilter || (item.centres || []).includes(state.patientCentreFilter)));
  const pageSize = 25;
  const pages = Math.max(1, Math.ceil(patients.length / pageSize));
  state.patientPage = Math.max(1, Math.min(state.patientPage, pages));
  const start = (state.patientPage - 1) * pageSize;
  const rows = patients.slice(start, start + pageSize);
  if (!$("#patientSearch", root)) {
    root.innerHTML = `${pageHeading("Care directory", "Patients")}
    <div class="directory-toolbar patient-directory-toolbar"><label class="search-field">${icon("search")}<input id="patientSearch" type="search" value="${escapeHtml(state.patientQuery)}" placeholder="Search by patient name or code" aria-label="Search patients"></label><label class="patient-status-filter">Status<select id="patientStatusFilter"><option value="all">All</option><option value="active">Active</option><option value="inactive">Inactive</option></select></label><label class="patient-status-filter patient-centre-filter">Centre<select id="patientCentreFilter" aria-label="Centre"><option value="">All centres</option></select></label><span class="soft-pill" id="patientMatchCount" role="status" aria-live="polite"></span>${mainWorkflowWorkspace ? zohoRefreshControls("patients") : ""}</div>
    <div class="table-shell patient-directory-table"><table class="data-table"><thead><tr><th>Patient ID</th><th>Patient</th><th>Preference day</th><th>Preferred time</th><th>Status</th><th></th></tr></thead><tbody>
    </tbody></table><div class="table-footer"><span id="patientPageSummary"></span><div><button class="ghost-button" id="patientPrev">Previous</button><button class="ghost-button" id="patientNext">Next</button></div></div></div>`;
    $("#patientSearch", root).addEventListener("input", event => {
      state.patientQuery = event.target.value;
      state.patientPage = 1;
      renderPatientsView();
    });
    $("#patientStatusFilter", root).addEventListener("change", event => {
      state.patientStatusFilter = event.target.value;
      state.patientPage = 1;
      renderPatientsView();
    });
    $("#patientCentreFilter", root).addEventListener("change", event => {
      state.patientCentreFilter = event.target.value;
      state.patientPage = 1;
      renderPatientsView();
    });
    $("#patientPrev", root).addEventListener("click", () => { state.patientPage--; renderPatientsView(); });
    $("#patientNext", root).addEventListener("click", () => { state.patientPage++; renderPatientsView(); });
    if ($("[data-zoho-refresh]", root)) {
      $("[data-zoho-refresh]", root).addEventListener("click", startZohoRefresh);
      pollZohoRefresh(false);  // Show the last refresh, or resume watching one already running.
    }
  }
  const search = $("#patientSearch", root);
  if (search.value !== state.patientQuery) search.value = state.patientQuery;
  $("#patientStatusFilter", root).value = state.patientStatusFilter;
  syncPatientCentreFilter(root);
  $("#patientMatchCount", root).textContent = `${patients.length.toLocaleString("en-IN")} patients`;
  $("#patientPageSummary", root).textContent = `Showing ${patients.length ? start + 1 : 0}–${Math.min(start + pageSize, patients.length)} of ${patients.length.toLocaleString("en-IN")}`;
  $("#patientPrev", root).disabled = state.patientPage === 1;
  $("#patientNext", root).disabled = state.patientPage === pages;
  $("tbody", root).innerHTML = rows.length ? rows.map((item) => `<tr data-open-patient="${item.id}"><td><strong>${escapeHtml(item.code)}</strong></td><td><button type="button" class="patient-open-button person-cell" data-patient-preferences="${item.id}" aria-label="View plan and preferences for ${escapeHtml(item.name)}"><span class="avatar">${escapeHtml(initials(item.name === "Name not provided" ? item.code : item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${item.therapies} active therap${item.therapies === 1 ? "y" : "ies"}</small>${cancelStreakBadge(item.cancelStreak)}</span></button></td><td>${escapeHtml(item.preferenceDay)}</td><td>${escapeHtml(item.preferencePeriod)}</td><td><button class="status-pill patient-status-button ${item.active ? "status-confirmed" : "status-needs_rescheduling"}" type="button" data-patient-status="${item.id}" aria-haspopup="dialog" aria-label="${item.active ? "Active" : "Inactive"} — change scheduling status for ${escapeHtml(item.code)}">${item.active ? "Active" : "Inactive"} ${icon("chevron-right")}</button></td><td><button class="ghost-button" type="button" data-patient-preferences="${item.id}">Edit</button></td></tr>`).join("") : '<tr><td colspan="6">No patients match your search and filters.</td></tr>';
  $$("[data-open-patient]", root).forEach(row => row.addEventListener("click", event => {
    if (event.target.closest("button, a, input, select, textarea")) return;
    const patient = rows.find(item => String(item.id) === row.dataset.openPatient);
    if (patient) openPatientPreferences(patient);
  }));
  $$("[data-patient-status]", root).forEach(button => button.addEventListener("click", () => {
    const patient = rows.find(item => String(item.id) === button.dataset.patientStatus);
    openPatientStatusDialog(patient, () => {
      renderPatientsView();
      ($(`[data-patient-status="${patient.id}"]`, root) || $("#patientStatusFilter", root)).focus();
    });
  }));
  $$('[data-patient-preferences]', root).forEach((button) => button.addEventListener("click", () => {
    const patient = state.patients.find((item) => String(item.id) === button.dataset.patientPreferences);
    if (patient) openPatientPreferences(patient);
  }));
}

function pickedPreferenceDays(root) {
  return $$("#patientPreferenceDays input:checked", root).map((input) => Number(input.value));
}

function pickedPreferenceTimes(root) {
  return $$("#patientPreferenceTimes input:checked", root).map((input) => input.value);
}

// Morning / Afternoon / Clear quickly tick or untick a group of start times.
function wirePreferenceTimeShortcuts(root) {
  $$("[data-time-shortcut]", root).forEach((button) => button.addEventListener("click", () => {
    const boxes = $$("#patientPreferenceTimes input", root);
    const group = button.dataset.timeShortcut === "clear" ? [] : boxes.filter((box) => (box.value < "12:30") === (button.dataset.timeShortcut === "morning"));
    if (button.dataset.timeShortcut === "clear") boxes.forEach((box) => { box.checked = false; });
    else { const on = !group.every((box) => box.checked); group.forEach((box) => { box.checked = on; }); }
  }));
}

// "09:40" -> "9:40 AM"
function preferenceTimeLabel(time) {
  const [hours, minutes] = time.split(":").map(Number);
  return `${hours % 12 || 12}:${String(minutes).padStart(2, "0")} ${hours < 12 ? "AM" : "PM"}`;
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
      <section class="patient-plan-section" aria-labelledby="patientPlanTitle"><h3 id="patientPlanTitle">Plan</h3>${cancelStreakBadge(payload.cancelStreak)}
        ${payload.plan?.length ? `<ul class="patient-plan-list">${payload.plan.map(therapy => `<li><h4>${escapeHtml(therapy.therapy)}</h4><dl><div><dt>Clinician</dt><dd>${escapeHtml(therapy.clinician || "Not assigned")}</dd></div><div><dt>Frequency</dt><dd>${escapeHtml(therapy.sessionsPerWeek)} ${therapy.sessionsPerWeek === 1 ? "session" : "sessions"}/week</dd></div><div><dt>Duration</dt><dd>${therapy.durationMonths ? `${escapeHtml(therapy.durationMonths)} ${therapy.durationMonths === 1 ? "month" : "months"}` : "Not recorded"}</dd></div><div><dt>Sessions</dt><dd>${escapeHtml(courseLabel({sessions: therapy.sessions || 0, target: therapy.target, complete: therapy.complete}))}</dd></div></dl></li>`).join("")}</ul>` : '<p class="patient-plan-empty">No active plan recorded.</p>'}
      </section>
      <form class="preference-form" id="patientPreferencesForm">
        <fieldset class="preference-days" id="patientPreferenceDays"><legend>Preferred days</legend>
          <div class="preference-day-options">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((label, day) => `<label class="preference-day"><input type="checkbox" value="${day}" ${(payload.preference.days || []).includes(day) ? "checked" : ""}><span>${label}</span></label>`).join("")}</div>
        </fieldset>
        <fieldset class="preference-days" id="patientPreferenceTimes"><legend><span>Preferred times</span><span class="preference-time-shortcuts"><button type="button" data-time-shortcut="morning">Morning</button><button type="button" data-time-shortcut="afternoon">Afternoon</button><button type="button" data-time-shortcut="clear">Clear</button></span></legend>
          <div class="preference-day-options preference-time-options">${(payload.startTimes || []).map((time) => `<label class="preference-day"><input type="checkbox" value="${escapeHtml(time)}" ${(payload.preference.times || []).includes(time) ? "checked" : ""}><span>${escapeHtml(preferenceTimeLabel(time))}</span></label>`).join("")}</div>
        </fieldset>
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
    wirePreferenceTimeShortcuts(drawer);
    $("#patientPreferencesForm", drawer).addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true;
      button.textContent = "Saving…";
      try {
        const days = pickedPreferenceDays(drawer);
        const times = pickedPreferenceTimes(drawer);
        await api(`/api/patients/${patient.id}/preferences`, { method: "POST", body: JSON.stringify({ days, times }) });
        toast("Preference saved", days.length || times.length ? "The next schedules will use the picked days and times first."
          : "Preference cleared: any day, any time.");
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

const teamFilters = { query: "", status: "all", department: "", location: "", shift: "" };
const TEAM_PAGE_SIZE = 30;
let teamDirectory = null;
let teamPage = 1;
let teamMatches = [];  // Clinicians the current search and filters show (all pages), for Select all.

// Status, department and location live in one Filters drawer, like the Schedule page.
function teamFilterCount() {
  return (teamFilters.status !== "all") + Boolean(teamFilters.department) + Boolean(teamFilters.location) + Boolean(teamFilters.shift);
}

// Shift filter value: "" (all), "none" (no active shift, at any centre the Location filter allows),
// or "centreId:shiftId" (shiftId empty for a centre still on its original timetable).
function teamShiftKey(item) {
  return item.noShift ? "none" : `${item.locationId}:${item.shiftId ?? ""}`;
}

// Active clinicians at the location (every location when empty) with no active shift: not scheduled.
function teamNoShiftCount(location) {
  return teamDirectory.therapists.filter((item) => item.active && item.noShift && (!location || item.location === location)).length;
}

// The Shift filter follows Location, so it stays short with many centres: with no location it offers
// only "No shift (n)"; with one, that centre's shifts. Counts are active clinicians.
function teamShiftFilterOptions(location) {
  const options = [];
  if (location) {
    const ids = [...new Set(teamDirectory.therapists.filter((item) => item.location === location).map((item) => String(item.locationId)))];
    ids.forEach((id) => (teamDirectory.centreShifts?.[id] || []).forEach((option) => {
      const value = `${id}:${option.id ?? ""}`;
      const count = teamDirectory.therapists.filter((item) => item.active && teamShiftKey(item) === value).length;
      options.push({ value, name: `${option.name} (${count})` });
    }));
  }
  options.push({ value: "none", name: `No shift (${teamNoShiftCount(location)})` });
  return options;
}

function teamShiftSelectHtml(location) {
  return `<option value="">All shifts</option>${teamShiftFilterOptions(location).map((shift) => `<option value="${escapeHtml(shift.value)}">${escapeHtml(shift.name)}</option>`).join("")}`;
}

function ensureTeamFilterDrawer(departments, locations) {
  $("#teamFilterDrawer")?.remove();
  const options = (values, all) => `<option value="">${all}</option>${values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}`;
  const drawer = document.createElement("aside");
  drawer.id = "teamFilterDrawer";
  drawer.className = "drawer filter-drawer";
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("aria-labelledby", "teamFilterTitle");
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Clinical team</p><h2 id="teamFilterTitle">Filters</h2></div><button class="icon-button" data-close-team-filters aria-label="Close filters">${icon("close")}</button></div>
    <form id="teamFilterForm" class="drawer-body filter-form">
      <label>Status<select id="teamStatusFilter"><option value="all">All</option><option value="active">Active</option><option value="inactive">Inactive</option></select></label>
      <label>Department<select id="teamDepartmentFilter">${options(departments, "All departments")}</select></label>
      <label>Location<select id="teamLocationFilter">${options(locations, "All locations")}</select></label>
      <label>Shift<select id="teamShiftFilter"></select><span class="filter-hint" id="teamShiftHint">Pick a location to filter by a specific shift.</span></label>
    </form>
    <div class="drawer-footer"><button class="ghost-button" type="button" id="clearTeamFilters">Clear all</button><button class="primary-button" type="button" id="applyTeamFilters">Show clinicians</button></div>`;
  document.body.append(drawer);
  const fields = [["#teamStatusFilter", "status"], ["#teamDepartmentFilter", "department"], ["#teamLocationFilter", "location"], ["#teamShiftFilter", "shift"]];
  // Rebuild the Shift list for a location; a shift from another centre resets to All shifts.
  const refreshShifts = (location, wanted) => {
    const select = $("#teamShiftFilter", drawer);
    select.innerHTML = teamShiftSelectHtml(location);
    select.value = teamShiftFilterOptions(location).some((option) => option.value === wanted) ? wanted : "";
    $("#teamShiftHint", drawer).hidden = Boolean(location);
  };
  const sync = () => {
    fields.forEach(([selector, key]) => { if (key !== "shift") $(selector, drawer).value = teamFilters[key]; });
    refreshShifts(teamFilters.location, teamFilters.shift);
  };
  sync();
  $("#teamLocationFilter", drawer).addEventListener("change", (event) => refreshShifts(event.target.value, $("#teamShiftFilter", drawer).value));
  drawer.syncTeamFilters = sync;
  $("[data-close-team-filters]", drawer).addEventListener("click", closeDrawers);
  $("#teamFilterForm", drawer).addEventListener("submit", (event) => event.preventDefault());
  $("#applyTeamFilters", drawer).addEventListener("click", () => {
    fields.forEach(([selector, key]) => { teamFilters[key] = $(selector, drawer).value; });
    teamPage = 1;
    closeDrawers();
    renderTeamView();
  });
  $("#clearTeamFilters", drawer).addEventListener("click", () => {
    Object.assign(teamFilters, { status: "all", department: "", location: "", shift: "" });
    sync();
    teamPage = 1;
    renderTeamView();
  });
  return drawer;
}

async function loadTeamView() {
  const root = $("#view-team");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading clinical team…</div>`;
  try {
    const week = isoDate(startOfWeek(state.currentDate));
    teamDirectory = await api(`/api/therapists?start=${week}`);
    const departments = [...new Set(teamDirectory.therapists.map((item) => item.department))].sort((a, b) => a.localeCompare(b));
    const locations = [...new Set(teamDirectory.therapists.map((item) => item.location))].sort((a, b) => a.localeCompare(b));
    if (!departments.includes(teamFilters.department)) teamFilters.department = "";
    if (!locations.includes(teamFilters.location)) teamFilters.location = "";
    if (!teamShiftFilterOptions(teamFilters.location).some((shift) => shift.value === teamFilters.shift)) teamFilters.shift = "";
    const drawer = ensureTeamFilterDrawer(departments, locations);
    // The toolbar is built once; typing and filters only re-render the rows, so focus is never lost.
    root.innerHTML = `${pageHeading("Capacity & coverage", "Clinical team", `<button class="secondary-button" data-team-calendar>${icon("calendar")}View team calendar</button>`)}
      <div class="directory-toolbar patient-directory-toolbar">
        <label class="search-field">${icon("search")}<input id="teamSearch" type="search" placeholder="Search by clinician name or code" aria-label="Search clinicians"></label>
        <button class="secondary-button" type="button" id="teamFilterButton" aria-haspopup="dialog" aria-controls="teamFilterDrawer">${icon("filter")}Filters <span class="filter-count" id="teamFilterCount" hidden>0</span></button>
        <span class="soft-pill" id="teamMatchCount" role="status" aria-live="polite"></span>
        ${mainWorkflowWorkspace ? zohoRefreshControls("team") : ""}
      </div>
      <div class="team-noshift-banner" id="teamNoShiftBanner" role="status" hidden></div>
      <div class="team-bulk-bar" id="teamBulkBar" role="region" aria-label="Selected clinicians" hidden></div>
      <div class="table-shell patient-directory-table"><table class="data-table"><thead><tr><th class="team-pick-cell"><input type="checkbox" id="teamPickAll" aria-label="Select all clinicians shown"></th><th>Clinician</th><th>Department</th><th>Location</th><th>Shift</th><th>Weekly workload</th><th>Status</th></tr></thead><tbody></tbody></table>
      <div class="table-footer"><span id="teamFooter"></span><div><button class="ghost-button" id="teamPrev">Previous</button><button class="ghost-button" id="teamNext">Next</button></div></div></div>`;
    $("#teamSearch", root).value = teamFilters.query;
    $("#teamSearch", root).addEventListener("input", (event) => { teamFilters.query = event.target.value; teamPage = 1; renderTeamView(); });
    $("#teamFilterButton", root).addEventListener("click", () => { drawer.syncTeamFilters(); openDrawer("teamFilterDrawer"); });
    $("#teamPrev", root).addEventListener("click", () => { teamPage--; renderTeamView(); });
    $("#teamNext", root).addEventListener("click", () => { teamPage++; renderTeamView(); });
    // Select all = every clinician the search and filters show, on every page.
    $("#teamPickAll", root).addEventListener("change", (event) => {
      teamMatches.forEach((item) => { if (event.target.checked) teamSelection.add(item.id); else teamSelection.delete(item.id); });
      renderTeamView();
    });
    // "Show them" on the No shift banner: list exactly the active clinicians the banner counts.
    // The search and department filters are cleared so the list never comes up empty; location stays.
    $("#teamNoShiftBanner", root).addEventListener("click", (event) => {
      if (!event.target.closest("[data-show-no-shift]")) return;
      Object.assign(teamFilters, { shift: "none", status: "active", query: "", department: "" });
      $("#teamSearch", root).value = "";
      teamPage = 1;
      renderTeamView();
    });
    $('[data-team-calendar]', root).addEventListener("click", () => setView("schedule"));
    if ($("[data-zoho-refresh]", root)) {
      $("[data-zoho-refresh]", root).addEventListener("click", startZohoRefresh);
      pollZohoRefresh(false);  // Show the last refresh, or resume watching one already running.
    }
    renderTeamView();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function openTherapistStatusDialog(therapist, onSaved) {
  const active = !therapist.active;
  let saving = false;
  const dialog = document.createElement("dialog");
  dialog.className = "patient-status-dialog";
  dialog.setAttribute("aria-labelledby", "therapistStatusTitle");
  dialog.setAttribute("aria-describedby", "therapistStatusWarning");
  dialog.innerHTML = `
    <form class="preference-form">
      <h3 id="therapistStatusTitle">${active ? "Mark this clinician active?" : "Mark this clinician as inactive?"}</h3>
      <p id="therapistStatusWarning"><strong>${escapeHtml(therapist.name)}</strong> ${active
        ? "will be included when new sessions are scheduled."
        : "will get no new sessions. Visits already booked stay on the calendar for you to review."}</p>
      ${active ? "" : '<label>Reason for marking inactive <textarea name="reason" rows="3" maxlength="1000" required></textarea></label>'}
      <p class="patient-status-error" role="alert" hidden></p>
      <div class="patient-status-actions">
        <button type="button" class="secondary-button" data-cancel-status>Cancel</button>
        <button type="submit" class="primary-button">${active ? "Mark active" : "Mark inactive"}</button>
      </div>
    </form>`;
  const form = $("form", dialog);
  const reason = $("textarea", form);
  const cancel = $("[data-cancel-status]", form);
  const confirm = $('button[type="submit"]', form);
  const errorNote = $(".patient-status-error", form);
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
  reason?.addEventListener("input", () => reason.setCustomValidity(""));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (saving) return;
    const explanation = reason?.value.trim() ?? "";
    if (reason && !explanation) {
      reason.setCustomValidity("Enter a reason for marking this clinician inactive.");
      reason.reportValidity();
      return;
    }
    confirm.disabled = cancel.disabled = true;
    saving = true;
    errorNote.hidden = true;
    let result;
    try {
      result = await api(`/api/therapists/${therapist.id}/status`, {
        method: "POST", body: JSON.stringify({ active, reason: explanation }),
      });
    } catch (error) {
      errorNote.textContent = error.message;
      errorNote.hidden = false;
      saving = false;
      confirm.disabled = cancel.disabled = false;
      return;
    }
    therapist.active = result.active;
    // Keep the calendar's clinician list in step without a reload.
    const scheduled = state.schedule?.therapists?.find(item => item.id === therapist.id);
    if (scheduled) scheduled.active = result.active;
    dialog.close();
    onSaved();
    const kept = result.upcomingBookings || 0;
    toast(result.active ? "Clinician marked active" : "Clinician marked inactive",
      !result.active && kept ? `${kept} upcoming visit${kept === 1 ? " is" : "s are"} still booked. Review ${kept === 1 ? "it" : "them"} in the calendar.` : "Scheduling status saved.");
  });
  document.body.append(dialog);
  dialog.showModal();
  (reason || cancel).focus();
}

function renderTeamView() {
  const root = $("#view-team");
  if (!teamDirectory || !$("#teamSearch", root)) return;
  const query = teamFilters.query.trim().toLowerCase();
  const matches = teamDirectory.therapists.filter((item) =>
    (!query || `${item.name} ${item.code}`.toLowerCase().includes(query)) &&
    (teamFilters.status === "all" || item.active === (teamFilters.status === "active")) &&
    (!teamFilters.department || item.department === teamFilters.department) &&
    (!teamFilters.location || item.location === teamFilters.location) &&
    (!teamFilters.shift || teamShiftKey(item) === teamFilters.shift));
  teamMatches = matches;
  syncTeamPickAll();
  const pages = Math.max(1, Math.ceil(matches.length / TEAM_PAGE_SIZE));
  teamPage = Math.max(1, Math.min(teamPage, pages));
  const start = (teamPage - 1) * TEAM_PAGE_SIZE;
  const rows = matches.slice(start, start + TEAM_PAGE_SIZE);
  const filters = teamFilterCount();
  $("#teamFilterCount", root).hidden = filters === 0;
  $("#teamFilterCount", root).textContent = filters;
  $("#teamMatchCount", root).textContent = `${matches.length.toLocaleString("en-IN")} clinician${matches.length === 1 ? "" : "s"}`;
  // Banner while active clinicians (at the chosen location) have no shift; hidden at 0 or once they are listed.
  const unassigned = teamNoShiftCount(teamFilters.location);
  const banner = $("#teamNoShiftBanner", root);
  banner.hidden = !unassigned || teamFilters.shift === "none";
  if (!banner.hidden) {
    banner.innerHTML = `<span aria-hidden="true">⚠</span><span><strong>${unassigned} clinician${unassigned === 1 ? "" : "s"}</strong>${teamFilters.location ? ` at ${escapeHtml(teamFilters.location)}` : ""} ${unassigned === 1 ? "has" : "have"} no shift and won't be scheduled.</span><button class="team-noshift-link" type="button" data-show-no-shift>Show them</button>`;
  }
  $("#teamFooter", root).textContent = `Showing ${matches.length ? start + 1 : 0}–${Math.min(start + TEAM_PAGE_SIZE, matches.length)} of ${matches.length.toLocaleString("en-IN")} · week of ${formatDate(parseLocalDate(teamDirectory.weekStart), { day: "numeric", month: "long", year: "numeric" })}`;
  $("#teamPrev", root).disabled = teamPage === 1;
  $("#teamNext", root).disabled = teamPage === pages;
  $("tbody", root).innerHTML = rows.length ? rows.map((item) => { const capacityLabel = item.capacity == null ? "Not supplied" : item.capacity; const utilization = item.capacity ? Math.min(100, Math.round(item.appointments / item.capacity * 100)) : 0; return `<tr><td class="team-pick-cell"><input type="checkbox" data-team-pick="${item.id}" ${teamSelection.has(item.id) ? "checked" : ""} aria-label="Select ${escapeHtml(item.name)}"></td><td><div class="person-cell"><span class="avatar">${escapeHtml(initials(item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></span></div></td><td>${escapeHtml(item.department)}</td><td>${escapeHtml(item.location)}</td><td class="team-shift-cell">${teamShiftCell(item)}</td><td><div style="display:flex;align-items:center;gap:10px"><div class="progress-track"><div class="progress-fill" style="width:${utilization}%"></div></div><span style="white-space:nowrap">${item.appointments} / ${capacityLabel}</span></div></td><td><button class="status-pill patient-status-button ${item.active ? "status-confirmed" : "status-needs_rescheduling"}" type="button" data-therapist-status="${item.id}" aria-haspopup="dialog" aria-label="${item.active ? "Active" : "Inactive"} — change scheduling status for ${escapeHtml(item.name)}">${item.active ? "Active" : "Inactive"} ${icon("chevron-right")}</button></td></tr>`; }).join("")
    : '<tr><td colspan="7">No clinicians match your search and filters.</td></tr>';
  bindTeamShiftControls(root, rows);
  $$("[data-therapist-status]", root).forEach(button => button.addEventListener("click", () => {
    const therapist = rows.find(item => String(item.id) === button.dataset.therapistStatus);
    if (!therapist) return;
    openTherapistStatusDialog(therapist, () => {
      renderTeamView();
      ($(`[data-therapist-status="${therapist.id}"]`, root) || $("#teamSearch", root)).focus();
    });
  }));
}

// ---------------------------------------------------------------------------
// Shifts: centre timetables, assigning clinicians, and the shared confirmations.
// ---------------------------------------------------------------------------

// The start times a visit at this centre can use (its shifts' sessions), in clock order.
function centreSessionChoices(locationId) {
  const ids = state.schedule?.centreSessions?.[String(locationId)] || state.schedule?.defaultSessions || [];
  const byId = new Map((state.schedule?.sessions || []).map((session) => [session.id, session]));
  return ids.map((id) => byId.get(id)).filter(Boolean);
}

function formatClock(value) {
  const [hours, minutes] = String(value).split(":").map(Number);
  return `${hours % 12 || 12}:${String(minutes).padStart(2, "0")} ${hours < 12 ? "AM" : "PM"}`;
}

// Ask before saving a change that leaves upcoming bookings outside a clinician's shift.
function confirmAffectedBookings(error) {
  const details = error.details || {};
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "patient-status-dialog shift-affected-dialog";
    const listed = (details.affected || []).slice(0, 8);
    const rows = listed.map((item) => `<li><strong>${escapeHtml(formatDate(parseLocalDate(item.date), { day: "numeric", month: "short" }))} · ${escapeHtml(item.time)}</strong> ${escapeHtml(item.clinician)} · ${escapeHtml(item.patientCode)}${item.clashesWith?.length ? `<span class="shift-clash">Overlaps new ${escapeHtml(item.clashesWith.join(", "))}: not booked then that day</span>` : ""}${item.splitVisit ? `<span class="shift-clash">Back-to-back visit would be split by a lunch break or gap</span>` : ""}</li>`).join("");
    const more = (details.affectedCount || 0) - listed.length;
    const list = rows || more > 0 ? `<ul class="shift-affected-list">${rows}${more > 0 ? `<li>and ${more} more</li>` : ""}</ul>` : "";
    dialog.innerHTML = `<form method="dialog" class="shift-affected-form"><h3>${escapeHtml(details.title || "Bookings affected by this change")}</h3>${String(error.message).split("\n").map((line) => `<p>${escapeHtml(line)}</p>`).join("")}${list}<div class="dialog-actions"><button class="ghost-button" value="cancel" type="submit">Cancel</button><button class="primary-button" value="ok" type="submit">${escapeHtml(details.confirmLabel || "Save anyway")}</button></div></form>`;
    document.body.append(dialog);
    // Settle from the button itself (the dialog's own close event is not fired reliably everywhere).
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve(ok);
      if (dialog.open) dialog.close();
      dialog.remove();
    };
    $$("button[value]", dialog).forEach((button) => button.addEventListener("click", (event) => {
      event.preventDefault();
      finish(button.value === "ok");
    }));
    dialog.addEventListener("cancel", () => finish(false));
    dialog.addEventListener("close", () => finish(dialog.returnValue === "ok"));
    dialog.showModal();
  });
}

// POST a shift change; if bookings are affected, confirm and resend with acknowledgement.
async function postShiftChange(path, body) {
  try {
    return await api(path, { method: "POST", body: JSON.stringify(body) });
  } catch (error) {
    if (error.status === 409 && error.details?.needsAcknowledgement) {
      if (!(await confirmAffectedBookings(error))) return null;
      return api(path, { method: "POST", body: JSON.stringify({ ...body, acknowledgeAffected: true }) });
    }
    throw error;
  }
}

async function assignTherapistShift(therapistIds, shiftId) {
  try {
    const result = await postShiftChange("/api/therapists/shift", { therapistIds, shiftId: shiftId === "" ? null : Number(shiftId) });
    if (!result) return false;
    const kept = result.affected ? ` ${result.affected} existing booking${result.affected === 1 ? "" : "s"} kept as booked.` : "";
    toast("Shift updated", `${result.assigned} clinician${result.assigned === 1 ? "" : "s"} now on ${result.shiftName}.${kept}`);
    return true;
  } catch (error) {
    toast("Couldn’t change the shift", error.message, "error");
    return false;
  }
}

function teamShiftCell(item) {
  const options = teamDirectory.centreShifts?.[String(item.locationId)] || [{ id: null, name: item.shiftName, isDefault: true }];
  if (options.length < 2) return `<span class="shift-name">${escapeHtml(item.shiftName)}</span>`;
  const select = `<select class="shift-select" data-therapist-shift="${item.id}" aria-label="Shift for ${escapeHtml(item.name)}">${options.map((option) => `<option value="${option.id ?? ""}" ${String(option.id ?? "") === String(item.shiftId ?? "") ? "selected" : ""}>${escapeHtml(option.name)}</option>`).join("")}</select>`;
  return select + (item.shiftNeedsCheck ? `<span class="shift-check" title="New clinician on the default shift. Check their shift.">Check shift</span>` : "");
}

const teamSelection = new Set();

// Header box: ticked when every clinician shown is selected, part-ticked when some are.
function syncTeamPickAll() {
  const pickAll = $("#teamPickAll");
  if (!pickAll) return;
  const picked = teamMatches.filter((item) => teamSelection.has(item.id)).length;
  pickAll.checked = Boolean(teamMatches.length) && picked === teamMatches.length;
  pickAll.indeterminate = picked > 0 && picked < teamMatches.length;
  pickAll.disabled = !teamMatches.length;
}

function bindTeamShiftControls(root, rows) {
  $$("[data-therapist-shift]", root).forEach((select) => select.addEventListener("change", async () => {
    const therapist = rows.find((item) => String(item.id) === select.dataset.therapistShift);
    select.disabled = true;
    const done = await assignTherapistShift([therapist.id], select.value);
    if (done) await loadTeamView(); else { select.value = String(therapist.shiftId ?? ""); select.disabled = false; }
  }));
  $$("[data-team-pick]", root).forEach((box) => box.addEventListener("change", () => {
    if (box.checked) teamSelection.add(Number(box.dataset.teamPick)); else teamSelection.delete(Number(box.dataset.teamPick));
    syncTeamPickAll();
    renderTeamBulkBar();
  }));
  renderTeamBulkBar();
}

// Bulk assign: tick clinicians of one centre, pick a shift, apply.
function renderTeamBulkBar() {
  const bar = $("#teamBulkBar");
  if (!bar || !teamDirectory) return;
  const picked = teamDirectory.therapists.filter((item) => teamSelection.has(item.id));
  bar.hidden = !picked.length;
  if (!picked.length) return;
  const centres = [...new Set(picked.map((item) => item.locationId))];
  const options = centres.length === 1 ? (teamDirectory.centreShifts?.[String(centres[0])] || []) : [];
  const assign = centres.length > 1
    ? `<span class="fine-print">Pick clinicians from one centre to assign a shift together.</span>`
    : `<label>Move to<select id="teamBulkShift">${options.map((option) => `<option value="${option.id ?? ""}">${escapeHtml(option.name)}</option>`).join("")}</select></label><button class="primary-button" type="button" id="teamBulkApply">Assign shift</button>`;
  bar.innerHTML = `<span><strong>${picked.length}</strong> selected</span>${assign}<button class="ghost-button" type="button" id="teamBulkClear">Clear</button>`;
  $("#teamBulkClear", bar).addEventListener("click", () => { teamSelection.clear(); renderTeamView(); });
  $("#teamBulkApply", bar)?.addEventListener("click", async () => {
    const button = $("#teamBulkApply", bar);
    button.disabled = true;
    if (await assignTherapistShift([...teamSelection], $("#teamBulkShift", bar).value)) { teamSelection.clear(); await loadTeamView(); }
    else button.disabled = false;
  });
}

// ---- Shifts screen --------------------------------------------------------------

let shiftsData = null;
let shiftEditor = null;  // {id, isDefault, locationId, name, status, items:[{kind, breakKind, start, end}], warnings:[]}

function addMinutes(value, minutes) {
  const [hours, mins] = value.split(":").map(Number);
  const total = Math.min(23 * 60 + 59, hours * 60 + mins + minutes);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function minutesOf(value) {
  const [hours, mins] = String(value).split(":").map(Number);
  return hours * 60 + mins;
}

// Session length for the shift being edited: the centre's length, or, when the centre has no
// active shift, the length of this shift's first session (it sets the length for all the others).
function editorSessionLength(data, items) {
  if (data.lengthLocked) return data.sessionMinutes;
  const first = items.filter((item) => item.kind === "session").sort((a, b) => minutesOf(a.start) - minutesOf(b.start))[0];
  const length = first ? minutesOf(first.end) - minutesOf(first.start) : data.sessionMinutes;
  return length > 0 ? length : data.sessionMinutes;
}

function isFirstSession(items, index) {
  const sessions = items.map((item, at) => ({ ...item, at })).filter((item) => item.kind === "session")
    .sort((a, b) => minutesOf(a.start) - minutesOf(b.start));
  return sessions.length > 0 && sessions[0].at === index;
}

function shiftSummary(shift) {
  const sessions = shift.items.filter((item) => item.kind === "session");
  if (!sessions.length) return "No sessions";
  return `${formatClock(sessions[0].start)} – ${formatClock(sessions[sessions.length - 1].end)} · ${sessions.length} session${sessions.length === 1 ? "" : "s"}`;
}

async function loadShiftsView() {
  const root = $("#view-shifts");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading shifts…</div>`;
  try {
    shiftsData = await api(`/api/shifts${state.shiftLocation ? `?location=${encodeURIComponent(state.shiftLocation)}` : ""}`);
    state.shiftLocation = String(shiftsData.locationId ?? "");
    if (shiftEditor && String(shiftEditor.locationId) !== state.shiftLocation) shiftEditor = null;
    renderShiftsView();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function isEditing(shift) {
  return shiftEditor && (shift.id !== null ? shiftEditor.id === shift.id : shiftEditor.isDefault && shiftEditor.id === null);
}

function renderShiftsView() {
  const root = $("#view-shifts");
  const data = shiftsData;
  const centrePicker = `<label class="shift-centre-picker">Centre<select id="shiftCentre" aria-label="Centre">${data.locations.map((item) => `<option value="${item.id}" ${String(item.id) === state.shiftLocation ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>`;
  const cards = data.shifts.map((shift, index) => `<li><button type="button" class="shift-card${isEditing(shift) ? " is-selected" : ""}" data-shift-index="${index}">
      <span class="shift-card-top"><strong>${escapeHtml(shift.name)}</strong>${shift.isDefault && shift.status !== "inactive" ? `<span class="soft-pill">Default</span>` : ""}${shift.status === "draft" ? `<span class="status-pill status-pending_confirmation">Draft</span>` : ""}${shift.status === "inactive" ? `<span class="status-pill status-needs_rescheduling">Inactive</span>` : ""}</span>
      <span class="shift-card-meta">${escapeHtml(shiftSummary(shift))}</span>
      <span class="shift-card-meta">${shift.doctors} clinician${shift.doctors === 1 ? "" : "s"}</span>
    </button></li>`).join("");
  const empty = `<div class="panel-empty">${icon("clock")}<strong>Choose a shift</strong><span>Pick a shift to view or edit its sessions and breaks, or create a new one.</span></div>`;
  root.innerHTML = `${pageHeading("Timetables", "Shifts", centrePicker)}
    <div class="shifts-layout">
      <section class="panel shift-list-panel" aria-labelledby="shiftListTitle">
        <div class="panel-header"><div><h2 id="shiftListTitle">Shifts</h2><p>${data.lengthLocked ? `${data.sessionMinutes}-minute sessions` : "No active shift · the next shift’s first session sets the session length"} · ${escapeHtml(formatClock(data.dayStart))}–${escapeHtml(formatClock(data.dayEnd))}</p>${data.withoutShift ? `<p class="shift-check-note">${data.withoutShift} clinician${data.withoutShift === 1 ? " is" : "s are"} not scheduled until a shift is in use.</p>` : ""}</div><button class="secondary-button compact-button" type="button" id="newShift">${icon("plus")}New shift</button></div>
        <ul class="shift-card-list">${cards}</ul>
      </section>
      <section class="panel shift-editor-panel" aria-labelledby="shiftEditorTitle">${shiftEditor ? shiftEditorMarkup(data) : empty}</section>
    </div>`;
  $("#shiftCentre", root).addEventListener("change", (event) => { state.shiftLocation = event.target.value; shiftEditor = null; loadShiftsView(); });
  // Type to find a centre (there can be 25+), like the other branch pickers.
  makeSearchableSelect($("#shiftCentre", root), "Search centres");
  $("#newShift", root).addEventListener("click", () => {
    const start = "09:00";
    shiftEditor = { id: null, isDefault: false, locationId: state.shiftLocation, name: "", status: "draft", warnings: [],
      items: [{ kind: "session", breakKind: null, start, end: addMinutes(start, data.sessionMinutes) }] };
    renderShiftsView();
    $("#shiftName")?.focus();
  });
  $$("[data-shift-index]", root).forEach((button) => button.addEventListener("click", () => {
    const shift = data.shifts[Number(button.dataset.shiftIndex)];
    shiftEditor = { id: shift.id, isDefault: shift.isDefault, standard: Boolean(shift.standard), locationId: state.shiftLocation,
      name: shift.name, status: shift.status, warnings: [], items: shift.items.map((item) => ({ ...item })) };
    renderShiftsView();
  }));
  if (shiftEditor) bindShiftEditor(root, data);
}

function shiftEditorMarkup(data) {
  const editor = shiftEditor;
  const rows = editor.items.map((item, index) => `<tr>
      <td><select data-row-kind="${index}" aria-label="Row ${index + 1} type"><option value="session" ${item.kind === "session" ? "selected" : ""}>Session</option><option value="short" ${item.kind === "break" && item.breakKind === "short" ? "selected" : ""}>Short break</option><option value="lunch" ${item.kind === "break" && item.breakKind === "lunch" ? "selected" : ""}>Lunch</option></select></td>
      <td><input type="time" step="300" data-row-start="${index}" value="${escapeHtml(item.start)}" aria-label="Row ${index + 1} start"></td>
      <td><input type="time" step="300" data-row-end="${index}" value="${escapeHtml(item.end)}" aria-label="Row ${index + 1} end" ${item.kind === "session" && (data.lengthLocked || !isFirstSession(editor.items, index)) ? "readonly" : ""}></td>
      <td><button class="icon-button" type="button" data-row-remove="${index}" aria-label="Remove row ${index + 1}">${icon("close")}</button></td>
    </tr>`).join("");
  const warnings = editor.warnings.length ? `<div class="shift-warnings" role="alert">${icon("alert")}<div><strong>Overlapping times</strong><ul>${editor.warnings.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul><span>Saved as a draft. Fix these to use the shift for scheduling.</span></div></div>` : "";
  const inactive = editor.status === "inactive";
  const canDeactivate = !inactive && (editor.id !== null || editor.isDefault);
  const isNew = editor.id === null && !editor.isDefault;
  const length = editorSessionLength(data, editor.items);
  const note = inactive ? "Deactivated · kept here, not used for scheduling. Reactivate it to use it again."
    : !data.lengthLocked ? `The first session sets the session length for this centre: now ${length} minutes.`
    : editor.isDefault ? "Default shift · clinicians without another shift work these hours."
    : editor.status === "draft" ? "Draft · not used for scheduling yet." : "In use for scheduling.";
  const saveButtons = inactive ? `<button class="primary-button" type="button" id="reactivateShift">Reactivate</button>`
    : editor.isDefault || editor.status === "active"
    ? `<button class="primary-button" type="submit" data-activate="true">Save</button>`
    : `<button class="secondary-button" type="submit" data-activate="false">Save draft</button><button class="primary-button" type="submit" data-activate="true">Save and use</button>`;
  return `<div class="panel-header"><div><h2 id="shiftEditorTitle">${isNew ? "New shift" : escapeHtml(editor.name)}</h2><p>${note}</p></div>${canDeactivate ? `<button class="danger-button compact-button" type="button" id="archiveShift">${editor.standard || editor.id === null ? "Deactivate shift" : "Delete shift"}</button>` : ""}</div>
    <form class="shift-editor" id="shiftEditorForm">
      <label class="shift-name-field">Name<input id="shiftName" maxlength="80" value="${escapeHtml(editor.name)}" ${editor.isDefault ? "readonly" : ""} required></label>
      <div class="table-shell shift-rows"><table class="data-table"><thead><tr><th>Type</th><th>Start</th><th>End</th><th><span class="visually-hidden">Remove</span></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="shift-row-actions"><button class="ghost-button" type="button" id="addSession">${icon("plus")}Add session</button><button class="ghost-button" type="button" id="addBreak">${icon("plus")}Add break</button></div>
      ${warnings}
      <p class="replacement-error" id="shiftError" role="alert" hidden></p>
      <div class="shift-save-actions">${saveButtons}</div>
    </form>`;
}

function readShiftRows(root) {
  shiftEditor.name = $("#shiftName", root)?.value ?? shiftEditor.name;
  const raw = shiftEditor.items.map((item, index) => {
    const kind = $(`[data-row-kind="${index}"]`, root).value;
    const isSession = kind === "session";
    return { kind: isSession ? "session" : "break", breakKind: isSession ? null : kind,
      start: $(`[data-row-start="${index}"]`, root).value || item.start,
      end: $(`[data-row-end="${index}"]`, root).value || item.end };
  });
  // Locked centres keep their length; otherwise the first session's own end sets it.
  const length = editorSessionLength(shiftsData, raw);
  shiftEditor.items = raw.map((item, index) => (item.kind === "session" && (shiftsData.lengthLocked || !isFirstSession(raw, index))
    ? { ...item, end: addMinutes(item.start, length) } : item));
}

function bindShiftEditor(root, data) {
  if (shiftEditor.status === "inactive") {
    // A deactivated Shift 1 is shown as it was; it can only be reactivated.
    $$("#shiftEditorForm input, #shiftEditorForm select, [data-row-remove], #addSession, #addBreak", root)
      .forEach((element) => { element.disabled = true; });
    $("#reactivateShift", root)?.addEventListener("click", async () => {
      try {
        // Warns first: shifts of another session length are deleted, bookings stay as booked.
        const result = await postShiftChange(`/api/shifts/${shiftEditor.id}/reactivate`, {});
        if (!result) return;
        toast("Shift reactivated", result.deleted?.length
          ? `${shiftEditor.name} is in use again. Deleted: ${result.deleted.join(", ")}.`
          : `${shiftEditor.name} is in use again.`);
        shiftEditor = null;
        await loadShiftsView();
      } catch (error) { toast("Couldn’t reactivate the shift", error.message, "error"); }
    });
    return;
  }
  const rerender = () => { readShiftRows(root); renderShiftsView(); };
  $$("[data-row-kind], [data-row-start], [data-row-end]", root).forEach((input) => input.addEventListener("change", rerender));
  $$("[data-row-remove]", root).forEach((button) => button.addEventListener("click", () => {
    readShiftRows(root);
    shiftEditor.items.splice(Number(button.dataset.rowRemove), 1);
    renderShiftsView();
  }));
  const addRow = (kind) => {
    readShiftRows(root);
    const last = shiftEditor.items[shiftEditor.items.length - 1];
    const start = last ? last.end : "09:00";  // A new row starts where the previous one ended.
    shiftEditor.items.push(kind === "session"
      ? { kind: "session", breakKind: null, start, end: addMinutes(start, editorSessionLength(data, shiftEditor.items)) }
      : { kind: "break", breakKind: "short", start, end: addMinutes(start, 15) });
    renderShiftsView();
  };
  $("#addSession", root).addEventListener("click", () => addRow("session"));
  $("#addBreak", root).addEventListener("click", () => addRow("break"));
  $("#archiveShift", root)?.addEventListener("click", async () => {
    try {
      const result = shiftEditor.id === null
        ? await postShiftChange("/api/shifts/default/archive", { locationId: Number(state.shiftLocation) })
        : await postShiftChange(`/api/shifts/${shiftEditor.id}/archive`, {});
      if (!result) return;
      toast(result.kept ? "Shift deactivated" : "Shift deleted",
        result.kept ? `${shiftEditor.name} is kept as Inactive.` : `${shiftEditor.name} was deleted.`);
      shiftEditor = null;
      await loadShiftsView();
    } catch (error) { toast("Couldn’t deactivate the shift", error.message, "error"); }
  });
  $("#shiftEditorForm", root).addEventListener("submit", async (event) => {
    event.preventDefault();
    readShiftRows(root);
    const activate = event.submitter?.dataset.activate === "true";
    $$("button", event.currentTarget).forEach((button) => { button.disabled = true; });
    try {
      const result = await postShiftChange("/api/shifts", { id: shiftEditor.id, isDefault: shiftEditor.isDefault,
        locationId: Number(state.shiftLocation), name: shiftEditor.name, items: shiftEditor.items, activate });
      if (!result) { renderShiftsView(); return; }
      shiftEditor = { ...shiftEditor, id: result.shift.id, status: result.shift.status, warnings: result.warnings || [],
        items: result.shift.items.map((item) => ({ ...item })) };
      toast(result.shift.status === "active" ? "Shift saved" : "Draft saved",
        result.warnings?.length ? "Fix the overlapping times before using it." : `${result.shift.name} · ${shiftSummary(result.shift)}`);
      await loadShiftsView();
    } catch (error) {
      if (error.details?.warnings) shiftEditor.warnings = error.details.warnings;
      renderShiftsView();
      const line = $("#shiftError");
      if (line) { line.textContent = error.message; line.hidden = false; }
    }
  });
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
      {id:"rescheduling", label:"Needs rescheduling", icon:"operations"},
      {id:"absence", label:"Record an absence", icon:"calendar"},
      ...(workflowWorkspace ? [{id:"whatsapp", label:"WhatsApp replies & follow-ups", icon:"whatsapp"}] : []),
    ];
    root.innerHTML = `${pageHeading("Schedule continuity", "Operations")}
      <div class="operations-tabs" role="tablist" aria-label="Operations sections">
        ${tabs.map(tab => `<button type="button" role="tab" id="operations-tab-${tab.id}" aria-controls="operations-panel-${tab.id}" aria-selected="false" tabindex="-1" data-operations-tab="${tab.id}">${icon(tab.icon)}<span>${tab.label}</span>${tab.id === "rescheduling" ? `<span class="operations-tab-count" data-rescheduling-count>${operations.needsRescheduling.length}</span>` : ""}</button>`).join("")}
      </div>
      <div class="operations-tab-panel" role="tabpanel" id="operations-panel-assistant" aria-labelledby="operations-tab-assistant" tabindex="0" hidden>
      <section class="panel assistant-panel">
        <div class="panel-header"><div><h2 class="assistant-title">Scheduling assistant</h2></div><div class="assistant-header-actions"><button class="secondary-button compact-button" id="assistantClearMemory" type="button">Clear memory</button></div></div>
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
      root, date: isoDate(state.currentDate), api, onChanged: refreshAfterWhatsapp, patients: patients.patients,
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
  const plan = state.schedule?.patientPlans?.[String(item.patientId)] || [];
  const staffCancellationEnabled = state.schedule.staffCancellationEnabled === true;
  const visit = state.schedule.appointments.filter(other => !other.preview && other.patientId === item.patientId && other.date === item.date && (item.bundle ? other.bundle === item.bundle : other.id === item.id));
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Appointment details</p><h2 id="appointmentDrawerTitle">${formatDate(parseLocalDate(item.date), { weekday: "long", day: "numeric", month: "long" })}</h2></div><button class="icon-button" data-close-appointment aria-label="Close appointment">${icon("close")}</button></div>
    <div class="drawer-body"><div class="appointment-hero"><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span><h3>${escapeHtml(item.patientName)}</h3><p>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</p></div>
      <dl class="appointment-facts">
        <div><dt>Time</dt><dd>${escapeHtml(item.time)}</dd></div>
        <div><dt>Clinician</dt><dd>${escapeHtml(item.therapistName)}</dd></div>
        <div><dt>Location</dt><dd>${escapeHtml(item.location)}</dd></div>
        <div><dt>Plan</dt><dd>${plan.length ? `<ul class="appointment-plan">${plan.map(therapy => `<li><span>${escapeHtml(therapy.therapy)}</span><span>${escapeHtml(therapy.sessionsPerWeek)} ${therapy.sessionsPerWeek === 1 ? "session" : "sessions"}/week</span></li>`).join("")}</ul>` : '<span class="appointment-plan-empty">No active plan recorded.</span>'}</dd></div>
      </dl>
      ${canChange ? `<section class="drawer-section"><h3>Cancel appointment</h3><form class="preference-form" id="cancelAppointmentForm"><label>Cancel<select id="cancelAppointmentScope"><option value="session">This session</option><option value="visit">Entire visit (${visit.length} ${visit.length === 1 ? "session" : "sessions"})</option></select></label><ul id="cancelScopeSummary" class="cancel-scope-summary"></ul><label>Reason<textarea id="cancelAppointmentReason" rows="2" maxlength="1000" required></textarea></label><p class="fine-print">The opening will be released for staff to choose a replacement. No replacement is booked automatically.</p><p id="cancelAppointmentError" class="replacement-error" role="alert" hidden></p>${staffCancellationEnabled ? "" : '<p class="fine-print" role="status">This action needs the updated Thanal backend. It is not available in this session yet.</p>'}<button class="danger-button" type="submit" ${staffCancellationEnabled ? "" : "disabled"}>Confirm cancellation</button></form></section><section class="drawer-section"><h3>Move this visit</h3><form class="form-grid" id="moveAppointmentForm"><label>New date<input type="date" id="moveDate" value="${item.date}" required></label><label>New starting time<select id="moveSlot">${centreSessionChoices(item.locationId).map((session) => `<option value="${session.id}" ${session.id === item.slot ? "selected" : ""}>${escapeHtml(session.label)}</option>`).join("")}</select></label><button class="secondary-button" type="submit">Validate and move</button></form></section>` : ""}
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
    const cancelScope = $("#cancelAppointmentScope", drawer);
    const renderCancellationScope = () => {
      const items = cancelScope.value === "visit" ? visit : [item];
      $("#cancelScopeSummary", drawer).innerHTML = items.map(session => `<li>${escapeHtml(session.time)} · ${escapeHtml(session.therapy)}${session.status === "COMPLETED" ? " (completed — cannot cancel)" : ""}</li>`).join("");
    };
    cancelScope.addEventListener("change", renderCancellationScope);
    renderCancellationScope();
    $("#cancelAppointmentForm", drawer).addEventListener("submit", async event => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      const inputs = $$("input, select, textarea, button", drawer);
      const disabled = inputs.map(input => input.disabled);
      inputs.forEach(input => input.disabled = true);
      button.textContent = "Cancelling…";
      const errorNote = $("#cancelAppointmentError", drawer);
      errorNote.hidden = true;
      let result;
      try {
        result = await api(`/api/appointments/${item.id}/staff-cancel`, {method: "POST", body: JSON.stringify({scope: cancelScope.value, reason: $("#cancelAppointmentReason", drawer).value})});
      } catch (error) {
        errorNote.textContent = error.status === 404 ? "Staff cancellation requires the updated Thanal backend. Nothing was changed." : error.message;
        errorNote.hidden = false;
        inputs.forEach((input, index) => input.disabled = disabled[index]);
        button.textContent = "Confirm cancellation";
        return;
      }
      closeDrawers();
      toast(result.message, `${result.cancelledIds.length} session(s) released. Choose a replacement or leave the opening for later.`);
      try { await loadSchedule(); }
      catch (error) { toast("Cancellation saved", "Refresh the calendar to see the updated counts.", "error"); }
      if (result.replacementGroups?.length) showReplacementCandidates(result.replacementGroups);
    });
  }
  openDrawer("appointmentDrawer");
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
    document.querySelector('[aria-label="Primary navigation"]').append(link);
  }).catch(() => { /* The original workspace remains usable if operations are unavailable. */ });
}
