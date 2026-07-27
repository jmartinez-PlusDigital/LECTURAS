/* Plus Digital — feedback visual en formularios de acciones largas
 * (facturar ahora, importar lecturas/equipos). Marca el botón como
 * "cargando" al enviar el formulario, para que el usuario no dude si
 * el clic tuvo efecto mientras el servidor procesa la petición.
 * Ver core/templates/admin/base.html (include) y las clases
 * "pd-form-loading" / "pd-btn-loading" en plusdigital.css.
 */
document.addEventListener("submit", function (evento) {
    var form = evento.target;
    if (!form.classList || !form.classList.contains("pd-form-loading")) return;

    var boton = form.querySelector('[type="submit"]');
    if (!boton) return;

    if (boton.dataset.pdLoading === "1") {
        evento.preventDefault();
        return;
    }
    boton.dataset.pdLoading = "1";
    boton.disabled = true;
    boton.classList.add("pd-btn-loading");

    var textoCarga = boton.dataset.pdTextoCarga || "Procesando…";
    if (boton.tagName === "INPUT") {
        boton.value = textoCarga;
    } else {
        boton.textContent = textoCarga;
    }
});
