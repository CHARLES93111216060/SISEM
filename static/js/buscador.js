// Buscador de tablas: filtra las filas mientras se escribe.
// Uso en la plantilla:  <input data-filtro-tabla="id_de_la_tabla">  (y opcionalmente un
// elemento con id "<id_de_la_tabla>_vacio" que se muestra cuando no hay coincidencias).
(function () {
    'use strict';

    // Quita tildes y mayúsculas: "clínica" encuentra "CLINICA" y viceversa.
    const normalizar = (texto) => texto.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

    function filtrar(input) {
        const tabla = document.getElementById(input.dataset.filtroTabla);
        if (!tabla || !tabla.tBodies[0]) return;

        const filtro = normalizar(input.value.trim());
        let visibles = 0;
        for (const fila of tabla.tBodies[0].rows) {        // solo el cuerpo: el encabezado no se oculta
            const coincide = !filtro || normalizar(fila.textContent).includes(filtro);
            fila.hidden = !coincide;
            if (coincide) visibles += 1;
        }

        const aviso = document.getElementById(input.dataset.filtroTabla + '_vacio');
        if (aviso) aviso.hidden = visibles > 0;
    }

    document.querySelectorAll('input[data-filtro-tabla]').forEach((input) => {
        input.addEventListener('input', () => filtrar(input));
        filtrar(input);     // por si el navegador recuerda el texto al volver a la página
    });
})();
