import "./busy.js";

const form = document.getElementById("sc-survey");
const missing = form.querySelector("[data-sc-missing]");
const groups = () => [...new Set([...form.querySelectorAll("input[type='radio']")].map(input => input.name))];

function renderMissing() {
    const open = groups().filter(name => !form.querySelector(`input[name="${name}"]:checked`)).length;
    missing.textContent = open
        ? missing.dataset.scMissing.replace("{count}", open)
        : missing.dataset.scDone;
    missing.classList.toggle("text-danger", open > 0);
}

// Setting a whole column at once keeps long periods manageable.
for (const button of form.querySelectorAll(".sc-fill-button")) {
    button.addEventListener("click", () => {
        for (const cell of form.querySelectorAll(`td[data-column="${CSS.escape(button.dataset.column)}"]`)) {
            const radio = cell.querySelector(`input[value="${button.dataset.rating}"]`);
            if (radio && !radio.disabled) radio.checked = true;
        }
        renderMissing();
    });
}

const fillRow = form.querySelector(".sc-fill-row");
if (fillRow) fillRow.hidden = false;
form.addEventListener("change", renderMissing);
renderMissing();
