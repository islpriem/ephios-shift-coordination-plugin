import { DraftState } from "./draft-state.js";

const editor = document.getElementById("planning-editor");
const data = JSON.parse(document.getElementById("planning-data").textContent);
const words = editor.dataset;
const language = document.documentElement.lang;
const state = new DraftState(data.assignments, data.observer_assignments ?? []);
const people = new Map(data.people.map(person => [person.id, person]));
const shifts = new Map(data.shifts.map(shift => [shift.id, shift]));
const answers = new Map(data.availability.map(answer => [`${answer.person_id}:${answer.shift_id}`, answer]));
const wishes = new Set((data.observers ?? []).map(pair => pair.join(":")));
const calendar = document.getElementById("service-calendar");
const stats = document.getElementById("planning-stats");
const status = document.getElementById("draft-status");
const save = document.getElementById("save-draft");
const validation = document.getElementById("planning-validation");
const calculate = document.getElementById("calculate-proposal");
const adopt = document.getElementById("adopt-proposal");
const preview = document.getElementById("proposal-preview");
const proposalStatus = document.getElementById("proposal-status");
const proposalContent = document.getElementById("proposal-content");
const ICONS = {
    unavailable: "fa-times", if_needed: "fa-exclamation", available: "fa-check",
    preferred: "fa-star", missing: "fa-question", observer: "fa-chair",
};
// Violations of these codes belong to the shift as a whole, all others to one person in it.
const SHIFT_CODES = new Set(["shift_incomplete", "regular_minimum", "shift_maximum"]);
const candidateTotals = new Map();
const pairProblems = new Map(), shiftProblems = new Map();
let exceptions = 0, notesOnly = false;
let proposal = null, calculating = false;
const signature = pairs => pairs.map(pair => pair.join(":")).sort().join(",");
let savedSignature = signature(state.all());
let revision = 0, timer, saving = false, conflict = false, validated = false;

function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.append(node);
    return node;
}
function tip(node, text) {
    // ephios only wires up the tooltips present at load, so ours need their own instance.
    if (!text) return node;
    node.setAttribute("data-bs-toggle", "tooltip");
    node.setAttribute("title", text);
    window.bootstrap?.Tooltip?.getOrCreateInstance(node);
    return node;
}
function dropTips(root) {
    // A shown tooltip lives on the body, so it has to go before its element disappears.
    for (const node of root.querySelectorAll('[data-bs-toggle="tooltip"]')) {
        window.bootstrap?.Tooltip?.getInstance(node)?.dispose();
    }
}
function icon(name, parent, title) {
    const node = element("i", undefined, parent);
    node.className = `fas ${name}`;
    // Decorative: the surrounding element carries the text, also for the accessible name.
    node.setAttribute("aria-hidden", "true");
    return tip(node, title);
}
function ratingLabel(uid, sid) {
    const answer = answers.get(`${uid}:${sid}`);
    const rating = data.ratings[answer?.rating] ?? words.noResponse;
    return answer?.eligible ? rating : `${rating} · ${words.unqualified}`;
}
function personLabel(uid, sid) {
    return `${people.get(uid).name} · ${ratingLabel(uid, sid)}`;
}
function shiftLabel(sid) {
    const shift = shifts.get(sid);
    if (!shift) return String(sid);
    const day = localDate(shift.date).toLocaleDateString(language, { weekday: "short", day: "numeric", month: "short" });
    return `${day} · ${shift.label}`;
}
function pairLabel(uid, sid) {
    return `${people.get(uid)?.name ?? uid} · ${shiftLabel(sid)}`;
}
function localDate(iso) {
    const [year, month, day] = iso.split("-").map(Number);
    return new Date(year, month - 1, day, 12);
}
function isoDate(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
const weekdayIndex = date => (date.getDay() + 6) % 7;
const format = (template, values) =>
    template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? values[key] : match));

function changed() {
    revision++;
    validated = false;
    save.disabled = true;
    status.textContent = words.dirty;
    validation.replaceChildren();
    // The old marks describe the previous selection, so they go until the next check returns.
    pairProblems.clear();
    shiftProblems.clear();
    exceptions = 0;
    applyProblems();
    renderCounts();
    clearTimeout(timer);
    timer = setTimeout(check, 350);
}

function choice(uid, shift, chosen, list, observer) {
    const answer = answers.get(`${uid}:${shift.id}`);
    const label = element("label", undefined, state.has(uid, shift.id) ? chosen : list);
    label.className = `sc-choice ${observer ? "sc-observer" : `sc-r-${answer?.rating ?? "missing"}`}`;
    label.title = observer ? words.observersLabel : ratingLabel(uid, shift.id);
    const checkbox = element("input", undefined, label);
    checkbox.type = "checkbox";
    checkbox.dataset.person = uid;
    checkbox.dataset.shift = shift.id;
    if (observer) checkbox.dataset.observer = "1";
    checkbox.checked = state.has(uid, shift.id);
    icon(observer ? ICONS.observer : ICONS[answer?.rating ?? "missing"], label);
    element("span", people.get(uid).name, label).className = "sc-choice-name";
    element("span", ` (${label.title})`, label).className = "visually-hidden";
    if (people.get(uid).notes) icon("fa-comment-dots", label, words.notesTitle).classList.add("sc-note-mark");
    element("span", "", label).className = "sc-problem-slot";
    checkbox.addEventListener("change", () => {
        state.toggle(uid, shift.id, checkbox.checked, observer);
        (checkbox.checked ? chosen : list).append(label);
        checkbox.focus();
        changed();
    });
    return label;
}

function candidatesFor(shift) {
    const rows = [];
    for (const answer of data.availability) {
        if (answer.shift_id !== shift.id) continue;
        const person = people.get(answer.person_id);
        const sitting = wishes.has(`${answer.person_id}:${shift.id}`);
        if (state.has(answer.person_id, shift.id)) {
            rows.push({ uid: answer.person_id, observer: state.role(answer.person_id, shift.id) === "observer" });
        } else if (answer.eligible && person.complete && answer.rating && answer.rating !== "unavailable") {
            rows.push({ uid: answer.person_id, observer: false });
        } else if (sitting && person.complete && answer.rating !== "unavailable") {
            rows.push({ uid: answer.person_id, observer: true });
        }
    }
    return rows;
}

function shiftCard(shift, parent) {
    const card = element("section", undefined, parent);
    card.className = "sc-shift";
    card.dataset.shiftCard = shift.id;
    const head = element("div", undefined, card);
    head.className = "sc-shift-head";
    element("span", shift.label, head).className = "sc-shift-label";
    element("span", `${shift.start.slice(11, 16)}–${shift.end.slice(11, 16)}`, head).className = "sc-shift-time";
    element("span", "", head).dataset.shiftProblem = shift.id;
    element("span", "", head).dataset.shiftCount = shift.id;
    const chosen = element("div", undefined, card);
    chosen.className = "sc-chosen";
    const more = element("details", undefined, card);
    more.className = "sc-candidates";
    element("summary", "", more).dataset.candidateCount = shift.id;
    const list = element("div", undefined, more);
    list.className = "sc-candidate-list";
    const rows = candidatesFor(shift);
    candidateTotals.set(shift.id, rows.length);
    for (const row of rows) choice(row.uid, shift, chosen, list, row.observer);
    const pickerLabel = element("label", words.addPerson, more);
    pickerLabel.className = "sc-add";
    const picker = element("select", undefined, pickerLabel);
    picker.className = "form-select form-select-sm";
    element("option", "—", picker).value = "";
    for (const person of data.people) {
        if (rows.some(row => row.uid === person.id) || !answers.has(`${person.id}:${shift.id}`)) continue;
        element("option", personLabel(person.id, shift.id), picker).value = person.id;
    }
    const buttons = element("div", undefined, more);
    buttons.className = "sc-add-buttons";
    const insert = observer => {
        if (!picker.value) return;
        const uid = Number(picker.value);
        state.toggle(uid, shift.id, true, observer);
        choice(uid, shift, chosen, list, observer);
        candidateTotals.set(shift.id, candidateTotals.get(shift.id) + 1);
        picker.selectedOptions[0].remove();
        picker.value = "";
        changed();
    };
    const add = element("button", words.add, buttons);
    add.type = "button";
    add.className = "btn btn-sm btn-outline-secondary";
    add.addEventListener("click", () => insert(false));
    if (data.allow_observers) {
        const sit = element("button", words.addObserver, buttons);
        sit.type = "button";
        sit.className = "btn btn-sm btn-outline-secondary";
        sit.addEventListener("click", () => insert(true));
    }
}

function renderCalendar() {
    dropTips(calendar);
    calendar.replaceChildren();
    candidateTotals.clear();
    const dates = [...new Set(data.shifts.map(shift => shift.date))].sort();
    if (!dates.length) return;
    // Only weekdays that actually carry shifts get a column, so the cells stay wide.
    const columns = [...new Set(dates.map(iso => weekdayIndex(localDate(iso))))].sort((a, b) => a - b);
    calendar.style.setProperty("--sc-columns", columns.length);
    const byDate = new Map();
    for (const shift of data.shifts) {
        if (!byDate.has(shift.date)) byDate.set(shift.date, []);
        byDate.get(shift.date).push(shift);
    }
    const monday = localDate(dates[0]);
    monday.setDate(monday.getDate() - weekdayIndex(monday));
    const last = localDate(dates.at(-1));
    for (let week = monday; week <= last; week.setDate(week.getDate() + 7)) {
        const days = columns.map(index => {
            const day = new Date(week);
            day.setDate(day.getDate() + index);
            return day;
        });
        if (!days.some(day => byDate.has(isoDate(day)))) continue;
        const label = element("div", undefined, calendar);
        label.className = "sc-week-label";
        element("span", days[0].toLocaleDateString(language, { day: "numeric", month: "short" }), label);
        for (const day of days) {
            const iso = isoDate(day);
            const cell = element("div", undefined, calendar);
            cell.className = byDate.has(iso) ? "sc-day-box" : "sc-day-box sc-day-empty";
            element("div", day.toLocaleDateString(language, { weekday: "short", day: "numeric", month: "short" }), cell)
                .className = "sc-day-title";
            for (const shift of byDate.get(iso) ?? []) shiftCard(shift, cell);
        }
    }
    renderCounts();
}

function renderCounts() {
    const pairs = state.all();
    for (const node of editor.querySelectorAll("[data-shift-count]")) {
        const shift = shifts.get(Number(node.dataset.shiftCount));
        const count = pairs.filter(pair => pair[1] === shift.id).length;
        const sitting = state.observers().filter(pair => pair[1] === shift.id).length;
        node.replaceChildren();
        element("span", `${count}/${shift.minimum}`, node);
        if (sitting) icon(ICONS.observer, node, words.observersLabel);
        tip(node, `${words.minimum} ${shift.minimum}${shift.maximum === null ? "" : ` · ${words.maximum} ${shift.maximum}`}`);
        node.className = `sc-shift-count ${count < shift.minimum ? "sc-missing"
            : shift.maximum !== null && count > shift.maximum ? "sc-over" : "sc-ok"}`;
    }
    for (const node of editor.querySelectorAll("[data-candidate-count]")) {
        const id = Number(node.dataset.candidateCount);
        const open = (candidateTotals.get(id) ?? 0) - pairs.filter(pair => pair[1] === id).length;
        node.textContent = open > 0 ? `${words.candidates} (${open})` : words.nobody;
    }
    for (const node of editor.querySelectorAll("[data-person-count]")) {
        const uid = Number(node.dataset.personCount);
        const existing = data.counts[uid].existing;
        const draft = pairs.filter(pair => pair[0] === uid).length;
        const maximum = people.get(uid).maximum;
        node.textContent = `${existing + draft}/${maximum ?? "—"}`;
        tip(node, `${words.draft}: ${draft} · ${words.existing}: ${existing}`);
        node.classList.toggle("sc-over", maximum !== null && existing + draft > maximum);
    }
    renderStats();
}

function stat(value, label, tone, action) {
    const chip = element(action ? "button" : "span", undefined, stats);
    chip.className = `sc-stat ${tone ?? ""}`;
    if (action) {
        chip.type = "button";
        chip.addEventListener("click", action);
    }
    element("b", String(value), chip);
    element("span", label, chip);
    return chip;
}

function renderStats() {
    stats.replaceChildren();
    const pairs = state.all();
    const missing = data.shifts.map(shift => Math.max(0, shift.minimum - pairs.filter(pair => pair[1] === shift.id).length));
    const open = missing.reduce((sum, value) => sum + value, 0);
    stat(`${missing.filter(value => value === 0).length}/${data.shifts.length}`, words.filled, open ? "" : "sc-r-available");
    stat(open, words.openPlaces, open ? "sc-r-unavailable" : "sc-r-available");
    if (data.allow_observers) stat(state.observers().length, words.sitting, "sc-observer");
    if (exceptions) {
        stat(exceptions, words.exceptions, "sc-r-if_needed", () => {
            validation.querySelector(".sc-banner")?.scrollIntoView({ block: "center" });
        });
    }
    const notes = data.people.filter(person => person.notes).length;
    // Free texts are easy to miss: the chip filters the sidebar down to them.
    if (notes) {
        const chip = stat(notes, words.notesLabel, notesOnly ? "sc-r-if_needed" : "", () => {
            notesOnly = !notesOnly;
            filterPeople();
            renderStats();
        });
        chip.setAttribute("aria-pressed", String(notesOnly));
    }
}

function renderPeople() {
    const sidebar = document.getElementById("planning-people");
    dropTips(sidebar);
    sidebar.replaceChildren();
    for (const person of data.people) {
        const row = element(person.notes ? "details" : "div", undefined, sidebar);
        row.className = "sc-person";
        row.dataset.personName = person.name.toLowerCase();
        if (person.notes) row.dataset.hasNotes = "1";
        const summary = element(person.notes ? "summary" : "div", undefined, row);
        summary.className = "sc-person-head";
        element("span", person.name, summary).className = "sc-person-name";
        if (person.notes) icon("fa-comment-dots", summary, words.notesTitle).classList.add("sc-note-mark");
        if (!person.complete) icon(ICONS.missing, summary, words.noResponse);
        const count = element("span", "", summary);
        count.className = "sc-person-count";
        count.dataset.personCount = person.id;
        if (person.notes) element("p", person.notes, row).className = "sc-notes";
    }
}

function filterPeople() {
    const term = document.getElementById("people-filter").value.trim().toLowerCase();
    for (const row of document.querySelectorAll(".sc-person")) {
        row.hidden = (Boolean(term) && !row.dataset.personName.includes(term)) ||
            (notesOnly && !row.dataset.hasNotes);
    }
}

function applyProblems() {
    for (const label of editor.querySelectorAll(".sc-choice")) {
        const input = label.querySelector("input");
        const slot = label.querySelector(".sc-problem-slot");
        const messages = input.checked && pairProblems.get(`${input.dataset.person}:${input.dataset.shift}`);
        dropTips(slot);
        slot.replaceChildren();
        label.classList.toggle("sc-flagged", Boolean(messages));
        if (messages) problemIcon(messages, slot);
    }
    for (const node of editor.querySelectorAll("[data-shift-problem]")) {
        const messages = shiftProblems.get(Number(node.dataset.shiftProblem));
        dropTips(node);
        node.replaceChildren();
        if (messages) problemIcon(messages, node);
    }
}

function problemIcon(messages, parent) {
    const mark = icon("fa-exclamation-circle", parent, messages.join(" · "));
    mark.className = "fas fa-exclamation-circle sc-problem";
    mark.setAttribute("aria-hidden", "false");
    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", `${words.problem}: ${messages.join(" · ")}`);
    return mark;
}

function renderValidation(result) {
    validation.replaceChildren();
    pairProblems.clear();
    shiftProblems.clear();
    exceptions = result.violations.length;
    for (const violation of result.violations) {
        for (const sid of violation.shift_ids) {
            if (SHIFT_CODES.has(violation.code)) {
                shiftProblems.set(sid, [...(shiftProblems.get(sid) ?? []), violation.message]);
                continue;
            }
            for (const uid of violation.person_ids) {
                const key = `${uid}:${sid}`;
                pairProblems.set(key, [...(pairProblems.get(key) ?? []), violation.message]);
            }
        }
    }
    if (exceptions) {
        // One collected confirmation: the calendar shows where each exception sits.
        const box = element("section", undefined, validation);
        box.className = "sc-banner";
        box.dataset.tokens = result.violations.map(violation => violation.token).join(" ");
        const title = element("p", undefined, box);
        title.className = "sc-banner-title";
        icon("fa-exclamation-triangle", title);
        element("span", ` ${words.exceptions} (${exceptions})`, title);
        const list = element("ul", undefined, box);
        list.className = "sc-banner-list";
        for (const violation of result.violations) {
            const who = violation.person_ids.map(uid => people.get(uid)?.name ?? uid).join(", ");
            const where = violation.shift_ids.map(shiftLabel).join(", ");
            element("li", `${violation.message} — ${[who, where].filter(Boolean).join(" · ")}`, list);
        }
        const label = element("label", undefined, box);
        label.className = "form-check";
        const confirm = element("input", undefined, label);
        confirm.type = "checkbox";
        confirm.className = "form-check-input";
        confirm.required = true;
        element("span", ` ${words.confirm}`, label);
        const reasonLabel = element("label", words.reason, box);
        reasonLabel.className = "form-label mt-2 w-100";
        const reason = element("textarea", undefined, reasonLabel);
        reason.className = "form-control form-control-sm";
        reason.rows = 2;
        reason.maxLength = 2000;
    }
    if (result.existing_violations.length) {
        const box = element("details", undefined, validation);
        box.className = "sc-details";
        element("summary", `${words.baseline} (${result.existing_violations.length})`, box);
        const list = element("ul", undefined, box);
        list.className = "sc-details-body";
        for (const violation of result.existing_violations) {
            element("li", `${violation.person_ids.map(uid => people.get(uid).name).join(", ")}: ${violation.message}`, list);
        }
    }
    applyProblems();
    renderStats();
}

function payload() {
    return {
        expected_version: data.version,
        fingerprint: data.fingerprint,
        assignments: state.assignments(),
        observers: state.observers(),
    };
}

async function request(url, body) {
    const response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": editor.querySelector('[name="csrfmiddlewaretoken"]').value,
        },
        body: JSON.stringify(body),
    });
    const result = await response.json().catch(() => { throw new Error(words.failed); });
    if (!response.ok) {
        const error = new Error(result.error ?? words.failed);
        error.conflict = response.status === 409;
        throw error;
    }
    return result;
}

async function check() {
    if (saving || conflict) return;
    const currentRevision = ++revision;
    validated = false;
    save.disabled = true;
    status.textContent = words.checking;
    try {
        const result = await request(words.validateUrl, payload());
        if (currentRevision !== revision) return;
        renderValidation(result);
        validated = true;
        save.disabled = calculating;
        status.textContent = exceptions ? words.checkedExceptions : words.checked;
    } catch (error) {
        if (currentRevision !== revision) return;
        conflict = Boolean(error.conflict);
        status.textContent = error.message || words.failed;
    }
}

editor.addEventListener("submit", async event => {
    event.preventDefault();
    if (!validated || saving || calculating || conflict || !editor.reportValidity()) return;
    const banner = validation.querySelector("[data-tokens]");
    const body = {
        ...payload(),
        // Every exception is recorded, but the coordinator confirms them together once.
        confirmations: (banner?.dataset.tokens.split(" ").filter(Boolean) ?? []).map(token => ({
            token,
            confirmed: banner.querySelector("input").checked,
            reason: banner.querySelector("textarea").value,
        })),
    };
    saving = true;
    const controls = [...editor.querySelectorAll("input, select, textarea, button")].map(control => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    try {
        const result = await request(words.saveUrl, body);
        data.version = result.version;
        data.fingerprint = result.fingerprint;
        savedSignature = signature(state.all());
        proposal = null;
        preview.hidden = true;
        status.textContent = words.saved;
    } catch (error) {
        conflict = Boolean(error.conflict);
        status.textContent = error.message || words.failed;
    } finally {
        saving = false;
        controls.forEach(([control, wasDisabled]) => { control.disabled = wasDisabled; });
        save.disabled = conflict;
    }
});

document.getElementById("validate-draft").addEventListener("click", () => { clearTimeout(timer); check(); });

document.getElementById("people-filter").addEventListener("input", filterPeople);

calculate.addEventListener("click", async () => {
    if (calculating || saving || conflict) return;
    calculating = true;
    calculate.disabled = true;
    save.disabled = true;
    adopt.disabled = true;
    proposal = null;
    preview.hidden = false;
    proposalContent.replaceChildren();
    proposalStatus.textContent = words.calculating;
    try {
        const result = await request(words.proposeUrl, { expected_version: data.version, fingerprint: data.fingerprint });
        proposalStatus.textContent = result.message;
        if (!["optimal_primary", "feasible_timeout"].includes(result.status)) return;
        const observers = result.observers ?? [];
        if (result.version !== data.version || result.fingerprint !== data.fingerprint ||
            ![...result.assignments, ...observers].every(([uid, sid]) => people.has(uid) && shifts.has(sid))) {
            throw new Error(words.failed);
        }
        const score = result.score;
        const pairs = state.all();
        const current = data.shifts.filter(
            shift => pairs.filter(pair => pair[1] === shift.id).length >= shift.minimum
        ).length;
        proposalStatus.textContent = format(words.proposalSummary, {
            proposed: score.filled, total: data.shifts.length, current,
        });
        proposalContent.replaceChildren();
        const detail = [format(words.proposalDetail, {
            yellow: score.yellow, preferred: score.preferred, partners: score.partner_repeats,
        })];
        // Sitting in is only worth a word where the period offers it at all.
        if (data.allow_observers) detail.push(format(words.proposalSitting, { sitting: score.sitting ?? 0 }));
        element("p", detail.join(" "), proposalContent).className = "text-body-secondary mb-0";
        if (!result.assignments.length && !observers.length) {
            element("p", words.emptyProposal, proposalContent).className = "mb-0";
        }
        proposal = result;
        adopt.disabled = false;
    } catch (error) {
        conflict = Boolean(error.conflict);
        proposalStatus.textContent = error.message || words.failed;
    } finally {
        calculating = false;
        calculate.disabled = conflict;
        save.disabled = !validated || conflict;
    }
});

adopt.addEventListener("click", () => {
    if (!proposal || calculating || saving || conflict) return;
    if (signature(state.all()) !== savedSignature && !window.confirm(words.replaceDraft)) return;
    state.replace(proposal.assignments, proposal.observers ?? []);
    document.getElementById("invalid-assignments").replaceChildren();
    renderCalendar();
    changed();
    proposal = null;
    preview.hidden = true;
    status.textContent = words.proposalApplied;
});

document.getElementById("dismiss-proposal").addEventListener("click", () => {
    proposal = null;
    preview.hidden = true;
});

for (const [uid, sid] of data.invalid_assignments) {
    const row = element("p", pairLabel(uid, sid), document.getElementById("invalid-assignments"));
    row.className = "alert alert-warning";
    const remove = element("button", words.remove, row);
    remove.type = "button";
    remove.className = "btn btn-sm btn-outline-danger ms-2";
    remove.addEventListener("click", () => { state.toggle(uid, sid, false); row.remove(); changed(); });
}
renderPeople();
renderCalendar();
check();
