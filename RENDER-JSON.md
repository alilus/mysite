# Render con almacenamiento JSON

Esta versión reemplaza Upstash por un archivo JSON local. Para una sola instancia y un solo proceso Node. No sirve para varias instancias ni despliegues serverless. El almacenamiento predeterminado `/tmp/local-agent-bridge/relay.json` es efímero: tareas y resultados se pierden cuando Render reinicia o reemplaza la instancia. Los registros expiran tras una hora.

Actualiza el repositorio con TODOS los archivos de esta carpeta en su raíz, sin subir secretos. En Render usa Node, build `npm ci && npm run build`, start `npm start`. Conserva CONTROL_TOKEN y AGENT_TOKEN (largos y distintos) en Environment. Las variables UPSTASH_REDIS_REST_URL y UPSTASH_REDIS_REST_TOKEN ya no se necesitan. Espera el nuevo deploy antes de probar.

Si configuras un disco persistente en Render, opcionalmente define RELAY_FILE como una ruta dentro del punto de montaje, por ejemplo `/var/data/relay.json`. Consulta los límites y costos de Render. Un disco no convierte el sistema en apto para múltiples instancias/procesos. El JSON contiene prompts y respuestas: no debe publicarse ni subirse a GitHub.

El conector local y su API siguen funcionando igual; consulta README.md para el contrato. Algunas instrucciones de ese README mencionan Vercel y Upstash porque corresponden a la versión original; usa este documento para Render.
