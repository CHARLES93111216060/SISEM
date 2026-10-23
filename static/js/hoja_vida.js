// Hoja de vida: autollenado desde el inventario, guardado y descarga automática del PDF.
(function () {
    'use strict';

    const formulario = document.getElementById('hojaVidaForm');
    if (!formulario) return;

    const $ = (id) => document.getElementById(id);
    const boton = $('guardarHojaVida');
    const equipo = $('hv-equipo');
    const responsable = $('hv-responsable');
    const foto = $('hv-foto');
    const vista = $('hv-foto-vista');

    const LADO_MAX = 1200;          // px: igual que en el servidor
    const PESO_MAX = 700 * 1024;    // el servidor acepta hasta 1 MB por envío

    function mensaje(texto, tipo) {
        const caja = $('mensajeHojaVida');
        caja.className = 'alert alert-' + tipo;
        caja.textContent = texto;
        caja.hidden = !texto;
        if (texto) caja.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // ---- autollenado: el mismo registro de inventario da serie, inventario, marca… ------
    function llenarDesdeEquipo() {
        const d = (equipo.selectedOptions[0] || {}).dataset || {};
        $('hv-inventario').value = d.inventario || '';
        $('hv-nombre').value = d.nombre || '';
        $('hv-marca').value = d.marca || '';
        $('hv-entidad').value = d.entidad || '';
        $('hv-riesgo').value = d.riesgo || '';
        // El INVIMA se propone desde el inventario, pero se puede corregir.
        const invima = $('hv-invima');
        if (!invima.value || invima.dataset.auto === '1') {
            invima.value = d.invima || '';
            invima.dataset.auto = '1';
        }
    }
    $('hv-invima').addEventListener('input', (e) => { e.target.dataset.auto = '0'; });
    equipo.addEventListener('change', llenarDesdeEquipo);

    function llenarResponsable() {
        const d = (responsable.selectedOptions[0] || {}).dataset || {};
        $('hv-contacto').value = d.telefono || '';
        $('hv-email').value = d.correo || '';
    }
    responsable.addEventListener('change', llenarResponsable);

    // ---- foto: vista previa y compresión antes de enviar ---------------------------------
    foto.addEventListener('change', () => {
        const archivo = foto.files[0];
        vista.hidden = true;
        vista.removeAttribute('src');
        if (!archivo) return;
        const lector = new FileReader();         // data: (permitido por la CSP)
        lector.onload = () => { vista.src = lector.result; vista.hidden = false; };
        lector.readAsDataURL(archivo);
    });

    async function comprimir(archivo) {
        if (!window.createImageBitmap) return archivo;
        const imagen = await createImageBitmap(archivo);
        const escala = Math.min(1, LADO_MAX / Math.max(imagen.width, imagen.height));
        const lienzo = document.createElement('canvas');
        lienzo.width = Math.round(imagen.width * escala);
        lienzo.height = Math.round(imagen.height * escala);
        const ctx = lienzo.getContext('2d');
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, lienzo.width, lienzo.height);
        ctx.drawImage(imagen, 0, 0, lienzo.width, lienzo.height);
        let calidad = 0.85;
        let blob = null;
        for (let i = 0; i < 5; i++) {
            blob = await new Promise((ok) => lienzo.toBlob(ok, 'image/jpeg', calidad));
            if (!blob || blob.size <= PESO_MAX) break;
            calidad -= 0.15;
        }
        return blob || archivo;
    }

    async function datosDelFormulario() {
        const datos = new FormData(formulario);
        const archivo = foto.files[0];
        if (archivo) {
            try {
                const comprimida = await comprimir(archivo);
                datos.set('foto', comprimida, (archivo.name || 'foto').replace(/\.\w+$/, '') + '.jpg');
            } catch (_) {
                throw new Error('No se pudo leer la foto. Prueba con otra imagen JPG o PNG.');
            }
        }
        return datos;
    }

    // ---- guardar + descargar PDF -----------------------------------------------------------
    formulario.addEventListener('submit', async (evento) => {
        evento.preventDefault();
        if (!formulario.reportValidity()) return;

        const textoOriginal = boton.textContent;
        boton.disabled = true;
        boton.textContent = 'Guardando…';
        mensaje('', 'info');

        let resultado;
        try {
            const respuesta = await fetch('/mantenimiento/hojas_vida_equipos/guardar', {
                method: 'POST',
                headers: { 'X-CSRF-Token': window.SISEM.csrf(), 'X-Requested-With': 'XMLHttpRequest' },
                body: await datosDelFormulario(),
            });
            if (respuesta.status === 401) {
                window.location.assign('/login');
                return;
            }
            resultado = await respuesta.json().catch(() => ({}));
            if (!respuesta.ok) throw new Error(resultado.error || 'No se pudo guardar la hoja de vida.');
        } catch (error) {
            mensaje(error.message, 'error');
            boton.disabled = false;
            boton.textContent = textoOriginal;
            return;
        }

        // Guardada: el equipo ya no está disponible para otra hoja de vida.
        const inventario = $('hv-inventario').value;
        equipo.selectedOptions[0].remove();
        formulario.reset();
        llenarDesdeEquipo();
        llenarResponsable();
        vista.hidden = true;

        mensaje(`Hoja de vida de ${inventario} guardada. Generando el PDF…`, 'info');
        try {
            await window.SISEM.descargarPDF(
                `/mantenimiento/hojas_vida_equipos/${resultado.id}/descargar`, resultado.archivo);
            mensaje(`Hoja de vida de ${inventario} guardada y PDF descargado.`, 'ok');
        } catch (error) {
            mensaje(`La hoja de vida se guardó, pero no se pudo generar el PDF (${error.message}).`, 'error');
        } finally {
            boton.textContent = textoOriginal;
            boton.disabled = equipo.options.length <= 1;   // sin equipos pendientes
        }
    });
})();
