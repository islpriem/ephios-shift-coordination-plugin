// Long running submits (creating services, opening a survey, publishing) tell the user
// that the request is still running and prevent a second submit.
export function markBusy(button) {
    const form = button.form;
    if (!form || form.dataset.scBusyActive) return;
    form.dataset.scBusyActive = "1";
    const note = document.createElement("p");
    note.className = "sc-busy";
    note.setAttribute("role", "status");
    note.textContent = button.dataset.scBusy;
    button.after(note);
    for (const control of form.querySelectorAll("button[type='submit']")) control.disabled = true;
}

document.addEventListener("click", event => {
    const button = event.target.closest("[data-sc-busy]");
    if (!button || button.form === null || !button.form.checkValidity()) return;
    // Keep the clicked button's value in the submitted data.
    window.setTimeout(() => markBusy(button), 0);
});
