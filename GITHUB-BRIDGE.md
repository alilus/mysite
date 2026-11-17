# Puente GitHub API ↔ OpenCode Local (Windows / Linux / macOS)

Este canal permite que **el agente en Arena** le mande mensajes/tareas directamente a tu **OpenCode local** y reciba sus respuestas en **~2–4 segundos**, sin abrir puertos entrantes en tu red y sin depender de Render, Vercel ni Redis.

## Cómo funciona

1. **Canal dedicado:** Issue bloqueado [`alilus/mysite#1`](https://github.com/alilus/mysite/issues/1) (solo el propietario `@alilus` puede publicar; además el script verifica autor y rol `OWNER`/`COLLABORATOR`).
2. **En tu PC (Windows):** `tools\github_bridge.py` consulta `api.github.com` cada 2 s usando `ETag` (`304 Not Modified`, no gasta cuota cuando está en reposo). Cuando llega una tarea:
   - Publica un acuse de recibo (`ack`) de inmediato.
   - Ejecuta el mensaje en tu **OpenCode** local (detecta automáticamente `opencode` CLI o `opencode serve` en `127.0.0.1:4096`, manteniendo la misma sesión entre mensajes).
   - Publica la respuesta (`result`, fragmentada automáticamente si supera 58 KB).
3. **En Arena:** el agente usa `python3 tools/bridge_client.py send "..."` para enviarte un mensaje y leer la respuesta en el mismo turno.

## Requisitos en Windows

- **Python 3.9+** (`python --version` o `py -3 --version`). No requiere `pip install` (solo librería estándar).
- **OpenCode** instalado (`opencode --version`) o corriendo con `opencode serve --port 4096`.
- **Sesión de GitHub local** en tu PC con la cuenta `alilus` (el script la detecta solo desde `gh auth login`, Git Credential Manager de Git para Windows, o variable `BRIDGE_GITHUB_TOKEN` en `.env.local`). **Nunca pegues tokens en el chat.**

## Inicio rápido en Windows (PowerShell)

### Opción A — Ejecutar directo en la carpeta de tu proyecto (2 comandos)

Abre PowerShell en la carpeta del proyecto donde quieres que trabaje OpenCode y ejecuta:

```powershell
Invoke-WebRequest "https://raw.githubusercontent.com/alilus/mysite/arena/61a88cb7-mysite/tools/github_bridge.py" -OutFile "$env:TEMP\github_bridge.py"
python "$env:TEMP\github_bridge.py"
```

*(Si una vez fusionado a `main` prefieres usar `main`, cambia `arena/61a88cb7-mysite` por `main`).*

### Opción B — Desde el repositorio clonado

```powershell
git fetch origin
git checkout arena/61a88cb7-mysite
python tools\github_bridge.py --workdir "C:\ruta\a\tu\proyecto"
```

O con doble clic / consola usando `tools\start_bridge.bat`.

### Si te pide iniciar sesión en GitHub

Configúralo **solo en tu PC**:

```powershell
# Con GitHub CLI (recomendado):
winget install --id GitHub.cli
gh auth login

# O usando la ventana de inicio de sesión de Git para Windows:
python tools\github_bridge.py --login
```

También puedes guardar un Fine-Grained Personal Access Token (limitado a `alilus/mysite` con permiso **Issues: Read and write**) en un archivo `.env.local` (ignorado por Git):

```ini
BRIDGE_GITHUB_TOKEN=github_pat_...
```

## Opciones útiles de `tools\github_bridge.py`

| Opción | Descripción |
| --- | --- |
| `--doctor` | Comprueba autenticación de GitHub, acceso al Issue y detección de OpenCode sin quedarse escuchando. |
| `--workdir "C:\ruta"` | Carpeta local donde trabajará OpenCode (por defecto: carpeta actual). |
| `--opencode-auto` | Pasa `--auto` a `opencode run` para que OpenCode apruebe permisos de herramientas automáticamente sin bloquearse. |
| `--mode opencode-http` | Fuerza conexión a `opencode serve` (por defecto `http://127.0.0.1:4096`). |
| `--mode opencode-cli` | Fuerza ejecución mediante `opencode run`. |
| `--opencode-bin "C:\...\opencode.exe"` | Ruta explícita a `opencode` si no está en `PATH`. |
| `--model provider/model` | Modelo por defecto para OpenCode. |
| `--agent plan` | Agente de OpenCode (p. ej. `plan` para solo lectura o `build` para edición). |
| `--poll 2` | Intervalo de consulta en segundos (1 a 30). |

## Comandos del cliente (`tools/bridge_client.py` — usado por el agente en Arena)

```sh
# Ver estado del canal y si el puente local está activo
python3 tools/bridge_client.py status

# Probar conexión ida y vuelta en ~3 segundos (sin gastar llamada al modelo)
python3 tools/bridge_client.py ping

# Enviar mensaje a OpenCode local y esperar la respuesta (mantiene la sesión)
python3 tools/bridge_client.py send "Resume la estructura del proyecto actual"

# Iniciar una sesión nueva de OpenCode
python3 tools/bridge_client.py send --new-session "Nueva tarea..."

# Enviar y borrar los comentarios del Issue apenas se lea la respuesta
python3 tools/bridge_client.py send --cleanup "Consulta rápida..."

# Esperar una tarea ya enviada si el puente estaba apagado
python3 tools/bridge_client.py wait <task_id>

# Ver historial o limpiar todos los comentarios del Issue
python3 tools/bridge_client.py history --limit 10
python3 tools/bridge_client.py cleanup
```

## Notas de seguridad

- Como `alilus/mysite` es público, el Issue `#1` está **bloqueado** para que nadie externo pueda escribir, y ambos scripts ignoran cualquier comentario que no sea de `@alilus` con rol `OWNER`/`MEMBER`/`COLLABORATOR`.
- Aun así, al ser un repositorio público, los comentarios del Issue son legibles mientras existan: **no envíes contraseñas, claves API ni datos sensibles**, y usa `--cleanup` (o `bridge_client.py cleanup`) para borrar los mensajes cuando quieras. Si más adelante pones el repositorio en privado, todo seguirá funcionando exactamente igual pero con privacidad total.
