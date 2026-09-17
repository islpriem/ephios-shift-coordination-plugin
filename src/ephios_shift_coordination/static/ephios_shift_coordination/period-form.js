import "./busy.js";

const form = document.getElementById("sc-period-form");
const counter = form.querySelector("[data-sc-count]");
const create = form.querySelector("[data-sc-create]");
const dates = () => [...form.querySelectorAll("input[name='dates']")];

function renderCount() {
    if (!counter) return;
    const chosen = dates().filter(input => input.checked).length;
    counter.textContent = `${chosen} / ${dates().length}`;
    counter.dataset.scChosen = String(chosen);
    if (create) {
        create.disabled = chosen === 0;
        create.querySelector("[data-sc-create-label]").textContent = `${form.dataset.create} (${chosen})`;
    }
}

function markStale() {
    if (!create || create.disabled) return;
    create.disabled = true;
    const note = document.createElement("p");
    note.className = "sc-stale";
    note.setAttribute("role", "status");
    note.textContent = form.dataset.stale;
    create.after(note);
}

form.addEventListener("change", event => {
    if (event.target.name === "dates") renderCount();
    else if (event.target.closest("[data-sc-basics]")) markStale();
});
renderCount();
