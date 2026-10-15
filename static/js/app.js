// Comportamiento común de SISEM (menú lateral, sombra al hacer scroll y utilidades).
(function () {
    'use strict';

    // ---- utilidades compartidas -------------------------------------------------------
    const csrf = () => {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    };

    // Descarga un PDF con fetch para poder mostrar los errores sin salir de la página.
    async function descargarPDF(url, nombre) {
        const respuesta = await fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        if (respuesta.status === 401) {
            window.location.assign('/login');
            throw new Error('Tu sesión expiró. Inicia sesión de nuevo.');
        }
        if (!respuesta.ok) {
            let mensaje = 'No se pudo generar el PDF.';
            try { mensaje = (await respuesta.json()).error || mensaje; } catch (_) { /* no era JSON */ }
            throw new Error(mensaje);
        }
        const blob = await respuesta.blob();
        const enlace = document.createElement('a');
        enlace.href = URL.createObjectURL(blob);
        enlace.download = nombre;
        document.body.appendChild(enlace);
        enlace.click();
        enlace.remove();
        setTimeout(() => URL.revokeObjectURL(enlace.href), 10000);
    }

    window.SISEM = { csrf, descargarPDF };

    // ---- sombra del menú al hacer scroll -----------------------------------------------
    window.addEventListener('scroll', () => {
        document.body.classList.toggle('scrolled', window.scrollY > 8);
    }, { passive: true });

    // ---- menú lateral ------------------------------------------------------------------
    const botonMenu = document.querySelector('.menu-btn');
    if (botonMenu) {
        botonMenu.addEventListener('click', () => {
            const abierto = document.body.classList.toggle('menu-abierto');
            botonMenu.setAttribute('aria-expanded', String(abierto));
        });
    }

    document.querySelectorAll('.submenu-toggle').forEach((boton) => {
        boton.addEventListener('click', () => {
            const item = boton.closest('.has-submenu');
            const abierto = item.classList.toggle('abierto');
            boton.setAttribute('aria-expanded', String(abierto));
        });
    });
})();
