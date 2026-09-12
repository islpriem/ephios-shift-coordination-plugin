document.getElementById("add-shift").addEventListener("click", () => {
    const total = document.getElementById("id_shifts-TOTAL_FORMS");
    const html = document.getElementById("empty-shift-form").innerHTML.replaceAll("__prefix__", total.value);
    document.getElementById("shift-forms").insertAdjacentHTML("beforeend", html);
    total.value = String(Number(total.value) + 1);
});
