import { DraftState } from "./draft-state.js";

const editor = document.getElementById("planning-editor");
const data = JSON.parse(document.getElementById("planning-data").textContent);
const words = editor.dataset;
const state = new DraftState(data.assignments);
const people = new Map(data.people.map(person => [person.id, person]));
const shifts = new Map(data.shifts.map(shift => [shift.id, shift]));
const answers = new Map(data.availability.map(answer => [`${answer.person_id}:${answer.shift_id}`, answer]));
const calendar = document.getElementById("service-calendar");
const month = document.getElementById("planning-month");
const status = document.getElementById("draft-status");
const save = document.getElementById("save-draft");
const validation = document.getElementById("planning-validation");
const calculate = document.getElementById("calculate-proposal");
const adopt = document.getElementById("adopt-proposal");
const preview = document.getElementById("proposal-preview");
const proposalStatus = document.getElementById("proposal-status");
const proposalContent = document.getElementById("proposal-content");
let proposal = null, calculating = false;
const signature = pairs => pairs.map(pair => pair.join(":")).sort().join(",");
let savedSignature = signature(data.assignments);
let revision = 0, timer, saving = false, conflict = false, validated = false;

function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.append(node);
    return node;
}
function pairLabel(uid, sid) {
    const shift = shifts.get(sid);
    return `${people.get(uid)?.name ?? uid} · ${shift?.date ?? ""} ${shift?.label ?? sid}`;
}
function changed() {
    revision++;
    validated = false;
    save.disabled = true;
    status.textContent = words.dirty;
    validation.replaceChildren();
    renderCounts();
    clearTimeout(timer);
    timer = setTimeout(check, 350);
}
function choice(uid, shift, parent) {
    const answer = answers.get(`${uid}:${shift.id}`);
    const label = element("label", undefined, parent);
    label.className = `planning-choice rating-${answer?.rating ?? "missing"}`;
    const checkbox = element("input", undefined, label);
    checkbox.type = "checkbox";
    checkbox.dataset.person = uid;
    checkbox.dataset.shift = shift.id;
    checkbox.checked = state.has(uid, shift.id);
    const rating = data.ratings[answer?.rating] ?? words.noResponse;
    element("span", ` ${people.get(uid).name} · ${rating}${answer?.eligible ? "" : ` · ${words.unqualified}`}`, label);
    checkbox.addEventListener("change", () => {
        state.toggle(uid, shift.id, checkbox.checked);
        changed();
    });
}
function renderCalendar() {
    calendar.replaceChildren();
    const [year, index] = month.value.split("-").map(Number);
    for (let weekday = 0; weekday < 7; weekday++) {
        element("div", new Date(2026, 0, 5 + weekday).toLocaleDateString(document.documentElement.lang, {weekday: "short"}), calendar);
    }
    const days = new Date(year, index, 0).getDate();
    for (let day = 1; day <= days; day++) {
        const local = new Date(year, index - 1, day, 12);
        const iso = `${month.value}-${String(day).padStart(2, "0")}`;
        const cell = element("section", undefined, calendar);
        cell.className = "service-day";
        if (day === 1) cell.style.gridColumnStart = ((local.getDay() + 6) % 7) + 1;
        element("h3", String(day), cell);
        for (const shift of data.shifts.filter(item => item.date === iso)) {
            const card = element("fieldset", undefined, cell);
            card.dataset.shiftCard = shift.id;
            element("legend", `${shift.label} · ${shift.start.slice(11, 16)}–${shift.end.slice(11, 16)}${shift.end.slice(0, 10) !== iso ? ` (${shift.end.slice(0, 10)})` : ""}`, card);
            const count = element("p", undefined, card);
            count.dataset.shiftCount = shift.id;
            const choices = element("div", undefined, card);
            choices.className = "planning-choices";
            const candidates = data.availability.filter(a => a.shift_id === shift.id);
            const listed = new Set();
            for (const answer of candidates) {
                if (state.has(answer.person_id, shift.id) ||
                    (answer.eligible && people.get(answer.person_id).complete && answer.rating && answer.rating !== "unavailable")) {
                    choice(answer.person_id, shift, choices);
                    listed.add(answer.person_id);
                }
            }
            const pickerLabel = element("label", words.addPerson, card);
            const picker = element("select", undefined, pickerLabel);
            element("option", "—", picker).value = "";
            for (const answer of candidates) {
                if (!listed.has(answer.person_id)) {
                    const option = element("option", `${people.get(answer.person_id).name} · ${data.ratings[answer.rating] ?? words.noResponse}${answer.eligible ? "" : ` · ${words.unqualified}`}`, picker);
                    option.value = answer.person_id;
                }
            }
            const add = element("button", words.add, card);
            add.type = "button";
            add.className = "btn btn-sm btn-secondary";
            add.addEventListener("click", () => {
                if (!picker.value) return;
                const uid = Number(picker.value);
                state.toggle(uid, shift.id, true);
                choice(uid, shift, choices);
                picker.selectedOptions[0].remove();
                picker.value = "";
                changed();
            });
        }
    }
    renderCounts();
}
function renderCounts() {
    const pairs = state.assignments();
    for (const node of editor.querySelectorAll("[data-shift-count]")) {
        const shift = shifts.get(Number(node.dataset.shiftCount));
        const count = pairs.filter(pair => pair[1] === shift.id).length;
        node.textContent = `${words.selected}: ${count} · ${words.minimum}: ${shift.minimum} · ${words.maximum}: ${shift.maximum ?? "∞"} · ${words.free}: ${shift.maximum === null ? "∞" : Math.max(0, shift.maximum - count)}`;
        node.classList.toggle("text-danger", count < shift.minimum || (shift.maximum !== null && count > shift.maximum));
    }
    for (const node of editor.querySelectorAll("[data-person-count]")) {
        const uid = Number(node.dataset.personCount);
        const existing = data.counts[uid].existing;
        const count = pairs.filter(pair => pair[0] === uid).length;
        node.textContent = `${words.existing}: ${existing} · ${words.draft}: ${count} · ${words.total}: ${existing + count} / ${people.get(uid).maximum ?? "—"}`;
    }
}
function renderValidation(result) {
    validation.replaceChildren();
    for (const violation of result.violations) {
        const box = element("fieldset", undefined, validation);
        box.className = "planning-exception";
        box.dataset.token = violation.token;
        element("legend", violation.message, box);
        element("p", violation.person_ids.map(uid => people.get(uid)?.name ?? uid).join(", ") +
            " · " + violation.shift_ids.map(sid => `${shifts.get(sid).date} ${shifts.get(sid).label}`).join(", "), box);
        const label = element("label", undefined, box);
        const confirm = element("input", undefined, label);
        confirm.type = "checkbox";
        confirm.required = true;
        element("span", ` ${words.confirm}`, label);
        const reasonLabel = element("label", words.reason, box);
        const reason = element("textarea", undefined, reasonLabel);
        reason.required = true;
        reason.maxLength = 2000;
    }
    if (result.underfilled.length) {
        element("h2", words.underfilled, validation);
        const list = element("ul", undefined, validation);
        for (const [sid, count, minimum] of result.underfilled) {
            const shift = shifts.get(sid);
            element("li", `${shift.date} ${shift.label}: ${count} / ${minimum}`, list);
        }
    }
    if (result.existing_violations.length) {
        element("h2", words.baseline, validation);
        const list = element("ul", undefined, validation);
        for (const violation of result.existing_violations) {
            element("li", `${violation.person_ids.map(uid => people.get(uid).name).join(", ")}: ${violation.message}`, list);
        }
    }
}
function payload() {
    return {expected_version: data.version, fingerprint: data.fingerprint, assignments: state.assignments()};
}
async function request(url, body) {
    const response = await fetch(url, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRFToken": editor.querySelector('[name="csrfmiddlewaretoken"]').value},
        body: JSON.stringify(body)});
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
        status.textContent = words.checked;
    } catch (error) {
        if (currentRevision !== revision) return;
        conflict = Boolean(error.conflict);
        status.textContent = error.message || words.failed;
    }
}
editor.addEventListener("submit", async event => {
    event.preventDefault();
    if (!validated || saving || calculating || conflict || !editor.reportValidity()) return;
    const body = {...payload(), confirmations: [...validation.querySelectorAll("[data-token]")].map(box => ({
        token: box.dataset.token, confirmed: box.querySelector("input").checked,
        reason: box.querySelector("textarea").value}))};
    saving = true;
    const controls = [...editor.querySelectorAll("input, select, textarea, button")].map(control => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    try {
        const result = await request(words.saveUrl, body);
        data.version = result.version;
        data.fingerprint = result.fingerprint;
        savedSignature = signature(state.assignments());
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
month.addEventListener("change", renderCalendar);

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
        const result = await request(words.proposeUrl, {expected_version: data.version, fingerprint: data.fingerprint});
        proposalStatus.textContent = result.message;
        if (!["optimal_primary", "feasible_timeout"].includes(result.status)) return;
        if (result.version !== data.version || result.fingerprint !== data.fingerprint ||
            !result.assignments.every(([uid, sid]) => people.has(uid) && shifts.has(sid))) {
            throw new Error(words.failed);
        }
        const score = result.score;
        element("p", `${words.filled}: ${score.filled} · ${words.yellow}: ${score.yellow} · ${words.preferred}: ${score.preferred} · ${words.partners}: ${score.partner_repeats}`, proposalContent);
        if (!result.assignments.length) element("p", words.emptyProposal, proposalContent);
        const list = element("ul", undefined, proposalContent);
        for (const shift of data.shifts) {
            const names = result.assignments.filter(pair => pair[1] === shift.id).map(pair => people.get(pair[0]).name);
            element("li", `${shift.date} ${shift.label}: ${names.join(", ") || "—"} (${names.length} / ${shift.minimum})`, list);
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
    if (signature(state.assignments()) !== savedSignature && !window.confirm(words.replaceDraft)) return;
    state.replace(proposal.assignments);
    document.getElementById("invalid-assignments").replaceChildren();
    renderCalendar();
    changed();
    proposalStatus.textContent = words.proposalApplied;
    adopt.disabled = true;
});

const start = words.start.slice(0, 7), end = words.end.slice(0, 7);
for (let value = start; value <= end;) {
    const [year, number] = value.split("-").map(Number);
    element("option", new Date(year, number - 1, 1).toLocaleDateString(document.documentElement.lang, {month: "long", year: "numeric"}), month).value = value;
    value = number === 12 ? `${year + 1}-01` : `${year}-${String(number + 1).padStart(2, "0")}`;
}
const sidebar = document.getElementById("planning-people");
for (const person of data.people) {
    const details = element("details", undefined, sidebar);
    const summary = element("summary", `${person.name}${person.complete ? "" : ` · ${words.noResponse}`}`, details);
    element("span", undefined, summary).dataset.personCount = person.id;
    element("p", person.notes, details).className = "planning-notes";
}
for (const [uid, sid] of data.invalid_assignments) {
    const row = element("p", pairLabel(uid, sid), document.getElementById("invalid-assignments"));
    const remove = element("button", words.remove, row);
    remove.type = "button";
    remove.addEventListener("click", () => { state.toggle(uid, sid, false); row.remove(); changed(); });
}
renderCalendar();
check();
