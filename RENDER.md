# Publicación en Render

1. Crear un repositorio privado en GitHub y subir el contenido de esta carpeta a la raíz (no subir node_modules ni archivos .env con secretos).
2. En Render, seleccionar New > Web Service, conectar el repositorio y escoger Node.
3. Configurar Build Command: `npm ci && npm run build`; Start Command: `npm start`.
4. En Environment agregar `UPSTASH_REDIS_REST_URL` y `UPSTASH_REDIS_REST_TOKEN` de una base de datos Upstash Redis, y `AGENT_TOKEN` y `CONTROL_TOKEN` como dos secretos aleatorios largos y distintos. No subirlos a GitHub.
5. Crear el servicio. Para usar el conector local, seguir las instrucciones de README.md usando la URL HTTPS de Render como BRIDGE_URL y el mismo AGENT_TOKEN. Se necesita un adaptador HTTP local compatible; no está incluido.

La aplicación necesita Upstash Redis REST; sin sus credenciales las rutas API no funcionarán. Render no permite acceder a localhost de tu computadora; el conector Python debe ejecutarse en ella. La URL de Render será pública: conserva los tokens en secreto.
