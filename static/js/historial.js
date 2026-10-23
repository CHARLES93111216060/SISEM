// Historial de reportes y consulta de hojas de vida: vuelve a descargar el PDF guardado.
(function () {
    'use strict';
    const aviso = document.getElementById('mensajeHistorial');

    function mostrar(texto, tipo) {
        if (!aviso) return;
        aviso.textContent = texto;
        aviso.className = 'alert alert-' + tipo;
        aviso.hidden = !texto;
    }

    document.querySelectorAll('.js-descargar').forEach((boton) => {
        boton.addEventListener('click', async () => {
            const textoOriginal = boton.textContent;
            boton.disabled = true;
            boton.textContent = 'Generando…';
            mostrar('', 'info');
            try {
                await window.SISEM.descargarPDF(
                    boton.dataset.url, boton.dataset.nombre || `reporte_${boton.dataset.id}.pdf`);
            } catch (error) {
                mostrar(error.message, 'error');
            } finally {
                boton.disabled = false;
                boton.textContent = textoOriginal;
            }
        });
    });
})();
