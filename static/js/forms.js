// Selector de formulario principal AJAX
const seleccion = document.getElementById('formSelector');
const container = document.getElementById('dynamicform');

// funcion reutilizable
function selector(selectorElement, containerElement){
    if(!selectorElement){
        return;
    }
    
    selectorElement.addEventListener('change', async () => {
    const valor = selectorElement.value;

    if(!valor){
        containerElement.innerHTML = '';
        return;
    }

    try {
        const response = await fetch(`/load_form/${valor}`);
        if(!response.ok){
            throw new Error(`HTTP error: ${response.status}`);
        }
        const html = await response.text();
        containerElement.innerHTML = html;
        // incializando formularios anidados
        formulario_anidado();
        //inicializar boton
        guardarProtocoloMantenimiento();
    }
    
    catch (error) {
        // console.error('Error al cargar el formulario:', error);
        containerElement.innerHTML = '<p>Error al cargar el formulario. Por favor, inténtalo de nuevo.</p>';
    }
    });
}


// funcion para inicializar formulario anidado
function formulario_anidado(){
    // Selector de tipo de equipo   
    const selector_equipo = document.getElementById('equipos');
    const container_equipo = document.getElementById('formulario_especifico'); 

    if(selector_equipo){
        selector(selector_equipo, container_equipo);
    }
}

selector(seleccion, container);

// Guardar informe AJAX
function guardarProtocoloMantenimiento(){
    const boton = document.getElementById('guardarInforme');
    if(!boton){
        // console.warn('Guardar informe button not found');
        alert('Error: Botón de guardar informe no encontrado');
        return;
    }

    boton.onclick = async (e)=>{
        e.preventDefault();
        const formulario = document.getElementById('protocoloForm');
        
        if(!formulario){
            // console.error('Protocol form not found');
            alert('Error: Formulario no encontrado');
            return;
        }
        
        const formData = new FormData(formulario);
        const datos = {};
        
        formData.forEach((valor, clave)=>{
            if(datos[clave]){
                if(!Array.isArray(datos[clave])){
                    datos[clave] = [datos[clave]];
                }
                datos[clave].push(valor);
            }else{
                datos[clave] = valor;
            }
    });
        try{
            const response = await fetch("/mantenimiento/reportes/guardar_informe",{
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify(datos)
            });
            // console.log(datos)
            
            if(!response.ok){
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            
            const resultado = await response.json();
            alert(resultado.mensaje || 'Informe guardado correctamente');
            // Si el servidor devuelve el id, iniciar la descarga automática
            if(resultado.id){
                alert(`Informe guardado con ID: ${resultado.id}. Iniciando descarga, por favor espera...`);
                const downloadUrl = `/mantenimiento/reportes/${resultado.id}/descargar`;
                // abrir en la misma ventana para que el navegador gestione la descarga
                window.location = downloadUrl;
            }
        }catch(error){
            // console.error('Error al guardar:', error);
            alert('Error al guardar el informe. Revisa la consola.');
        }
    };
}

