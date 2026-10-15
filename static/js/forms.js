// Formulario de reportes de mantenimiento (carga dinámica, guardado y descarga del PDF).
(function () {
    'use strict';

    const selectorPrincipal = document.getElementById('formSelector');
    const contenedor = document.getElementById('dynamicform');
    if (!selectorPrincipal || !contenedor) return;

    // ---- mensajes en pantalla (en lugar de alert()) -------------------------------------
    function mensaje(texto, tipo) {
        let caja = document.getElementById('mensajeFormulario');
        if (!caja) {
            caja = document.createElement('div');
            caja.id = 'mensajeFormulario';
            caja.setAttribute('role', 'status');
            contenedor.prepend(caja);
        }
        caja.className = 'alert alert-' + tipo;
        caja.textContent = texto;
        caja.hidden = !texto;
        if (texto) caja.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // ---- carga de formularios por AJAX -------------------------------------------------
    async function cargarFormulario(valor, destino) {
        if (!valor) {
            destino.innerHTML = '';
            return false;
        }
        try {
            const respuesta = await fetch(`/load_form/${encodeURIComponent(valor)}`, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            if (respuesta.status === 401) {
                window.location.assign('/login');
                return false;
            }
            if (!respuesta.ok) throw new Error(`HTTP ${respuesta.status}`);
            destino.innerHTML = await respuesta.text();   // HTML generado por el servidor
            return true;
        } catch (error) {
            destino.textContent = 'Error al cargar el formulario. Por favor, inténtalo de nuevo.';
            return false;
        }
    }

    // Enlaza el selector de tipo de equipo, que carga el formulario específico.
    function enlazarSelectorEquipo() {
        const equipos = document.getElementById('equipos');
        const destino = document.getElementById('formulario_especifico');
        if (!equipos || !destino) return;
        equipos.addEventListener('change', () => cargarFormulario(equipos.value, destino));
    }

    // ---- guardado ----------------------------------------------------------------------
    function recolectarDatos(formulario) {
        const datos = {};
        new FormData(formulario).forEach((valor, clave) => {
            if (typeof valor !== 'string') return;
            if (Object.prototype.hasOwnProperty.call(datos, clave)) {
                if (!Array.isArray(datos[clave])) datos[clave] = [datos[clave]];
                datos[clave].push(valor);
            } else {
                datos[clave] = valor;
            }
        });
        return datos;
    }

    function enlazarGuardado() {
        const boton = document.getElementById('guardarInforme');
        const formulario = document.getElementById('protocoloForm');
        if (!boton || !formulario) return;   // los otros formularios no tienen botón de guardar

        boton.addEventListener('click', async (evento) => {
            evento.preventDefault();
            if (!formulario.reportValidity()) return;   // respeta los campos "required"

            const textoOriginal = boton.textContent;
            boton.disabled = true;
            boton.textContent = 'Guardando…';
            mensaje('', 'info');

            let id = null;
            try {
                const respuesta = await fetch('/mantenimiento/reportes/guardar_informe', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': window.SISEM.csrf(),
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify(recolectarDatos(formulario)),
                });
                if (respuesta.status === 401) {
                    window.location.assign('/login');
                    return;
                }
                const resultado = await respuesta.json().catch(() => ({}));
                if (!respuesta.ok) throw new Error(resultado.error || 'No se pudo guardar el informe.');
                id = resultado.id;
            } catch (error) {
                mensaje(error.message, 'error');
                boton.disabled = false;
                boton.textContent = textoOriginal;
                return;
            }

            mensaje(`Informe N° ${id} guardado. Generando el PDF…`, 'info');
            try {
                await window.SISEM.descargarPDF(
                    `/mantenimiento/reportes/${id}/descargar`, `reporte_${id}.pdf`);
                mensaje(`Informe N° ${id} guardado y PDF descargado.`, 'ok');
            } catch (error) {
                // El informe ya está guardado: solo falló el PDF.
                mensaje(`El informe N° ${id} se guardó, pero no se pudo generar el PDF (${error.message}). ` +
                        'Puedes descargarlo más tarde desde el Historial.', 'error');
            } finally {
                boton.disabled = false;
                boton.textContent = textoOriginal;
            }
        });
    }

    selectorPrincipal.addEventListener('change', async () => {
        const cargado = await cargarFormulario(selectorPrincipal.value, contenedor);
        if (cargado) {
            enlazarSelectorEquipo();
            enlazarGuardado();
        }
    });
})();
