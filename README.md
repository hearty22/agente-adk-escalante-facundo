# Agente de Reconocimiento Asistido (ADK + Gemini)

Agente de inteligencia artificial construido con **Python** y **Google Agent Development Kit (ADK)**. Interpreta pedidos en lenguaje natural, decide qué herramienta usar, la ejecuta y arma la respuesta con los resultados obtenidos.

No es un chatbot: si la respuesta depende de un dato que se puede obtener con una herramienta, el agente la usa.

---

## 1. Problema que resuelve

Cuando alguien necesita reconocer un objetivo (un sitio, un host o un cuerpo de documentación), normalmente tiene que encadenar herramientas distintas y leer salidas crudas de cada una: buscar en internet, escanear puertos, leer banners, consultar una base de notas.

Este agente centraliza ese trabajo: recibe el pedido en lenguaje natural, **elige la herramienta adecuada**, la ejecuta con argumentos validados y devuelve un resumen en tres partes (resultado, evidencia y límites). Si una herramienta falla, informa el error en vez de inventar un resultado.

---

## 2. Arquitectura

```
                         ┌─────────────────────────┐
   Usuario ─── lenguaje  │   ADK Web (interfaz)    │
   natural  ───────────► │   http://localhost:8000 │
                         └────────────┬────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │  root_agent             │
                         │  Gemini 3.5 Flash Lite  │
                         │  + INSTRUCTION          │
                         └────────────┬────────────┘
                                      │ elige herramienta según el pedido
        ┌──────────────┬──────────────┼──────────────┬──────────────────┐
        ▼              ▼              ▼              ▼                  ▼
   web_search     nmap_scan    tcp_banner_grab   http_probe      index_text
   (internet)     (puertos)     (servicios)      (HTTP)         search_knowledge
                                                                 list_collections
                                                                       │
                                                                       ▼
                                                              Chroma + Ollama
                                                              (embeddings)
```

El agente se declara en `agent/agent.py` y sus instrucciones viven en `agent/prompts.py`. Las herramientas están en `tools/tools.py`.

---

## 3. Modelo de IA

El modelo se configura con la variable `MODEL_NAME` del archivo `agent/.env`:

| `MODEL_NAME` | Cómo se resuelve |
|---|---|
| `gemini-3.5-flash-lite` (por defecto) | Proveedor nativo de ADK (`google-genai`) |
| `ollama_chat/minimax-m2.1:cloud` | Se enruta por **LiteLLM** automáticamente |
| `openrouter/google/gemini-2.5-flash` | Se enruta por **LiteLLM** automáticamente |

La regla es simple: si el nombre contiene `/`, se envuelve en `LiteLlm(...)`; si no, se usa como modelo nativo. La lógica está en `build_model()` (`agent/agent.py`).

---

## 4. Herramientas

Siete herramientas con responsabilidades distintas. Todas devuelven el mismo sobre de respuesta:

```json
{"ok": true,  "tool": "nmap_scan", "data": { }}
{"ok": false, "tool": "nmap_scan", "error": "qué falló", "hint": "qué hacer"}
```

| Herramienta | Responsabilidad | Valida |
|---|---|---|
| `web_search` | Busca información pública y actualizada en internet | consulta no vacía, `max_results` entre 1 y 20, resultado vacío |
| `nmap_scan` | Escanea puertos TCP de un host autorizado | IP o hostname válido, rango de puertos, rangos invertidos, timeout, Nmap instalado |
| `tcp_banner_grab` | Lee el banner de un servicio TCP ya descubierto | puerto 1–65535, host, conexión rechazada, DNS inexistente, puerto sin banner |
| `http_probe` | Consulta una URL y devuelve status, headers y cuerpo | esquema `http`/`https`, host presente, errores SSL, 4xx/5xx |
| `index_text` | Guarda un texto en la base vectorial | texto no vacío, límite de tamaño, nombre de colección válido |
| `search_knowledge` | Búsqueda semántica sobre lo indexado | `k` entre 1 y 20, colección inexistente, colección vacía |
| `list_collections` | Lista las colecciones disponibles | base vacía |

---

## 5. Selección de herramientas

El agente no sigue un flujo fijo: elige según el pedido. Casos verificados:

| Pedido del usuario | Herramienta que ejecuta |
|---|---|
| "Listá las colecciones que hay en la base de conocimiento" | `list_collections` |
| "¿Qué puertos abiertos tiene 127.0.0.1? Escaneá del 1 al 200" | `nmap_scan` |
| "Buscá en la colección `apuntes_2026` qué dice sobre banners" | `search_knowledge` → falla → `list_collections` → `search_knowledge` (reintento) |
| "¿Qué versión de SSH corre en ese host?" | `tcp_banner_grab` |
| "¿Responde `https://example.com`? ¿Qué headers devuelve?" | `http_probe` |
| "Guardá estas notas en la colección `apuntes`" | `index_text` |
| "¿Cuál es la última versión de Python?" | `web_search` |

---

## 6. Validación

Todos los argumentos se validan **antes** de tocar la red o el modelo de embeddings:

| Caso | Respuesta de la herramienta |
|---|---|
| `nmap_scan("")` | `ok: false` — "el objetivo (target/host) está vacío" |
| `nmap_scan("https://example.com")` | `ok: false` — "parece una URL; esta herramienta espera solo un host o una IP" |
| `nmap_scan("127.0.0.1", "443-80")` | `ok: false` — "rango de puertos invertido" |
| `tcp_banner_grab("127.0.0.1", 70000)` | `ok: false` — "puerto fuera de rango: 70000 (válido: 1-65535)" |
| `http_probe("ftp://example.com")` | `ok: false` — "esquema no soportado" |
| `index_text("")` | `ok: false` — "el texto a indexar está vacío" |
| `search_knowledge(..., "no_existe")` | `ok: false` — "la colección 'no_existe' no existe" + lista de disponibles |
| Ollama apagado | `ok: false` — "no se pudo conectar con Ollama en …" + hint para levantarlo |

Los errores de red se traducen a mensajes accionables (`hint`), y los **resultados vacíos** se devuelven como `ok: true` con `count: 0` y una `note`, para que el modelo no los confunda con fallas.

### Ejemplo real: error → recuperación → respuesta

Transcripción de una sesión de prueba (el texto de `apuntes` se cargó antes con el
prompt de indexado de la sección 9):

```
Usuario> Buscá en la coleccion apuntes_2026 que dice sobre banners de servicios.

CALL  search_knowledge {"collection_name": "apuntes_2026", "query": "banners de servicios"}
RESP  {"ok": false, "tool": "search_knowledge",
       "error": "la colección 'apuntes_2026' no existe",
       "hint": "colecciones disponibles: apuntes, default, indigno_ser_humano, reconocimiento"}

CALL  list_collections {}
RESP  {"ok": true, "data": {"collections": ["apuntes", "default",
       "indigno_ser_humano", "reconocimiento"], "count": 4}}

CALL  search_knowledge {"collection_name": "apuntes", "query": "banners de servicios"}
RESP  {"ok": true, "data": {"count": 1, "results": [{"content": "Un banner TCP es el
       texto que un servicio envía al establecerse la conexión...", "score": 0.7975}]}}

Agente> Resultado: Los apuntes definen al banner TCP como el texto que un servicio
         envía al conectarse para identificar de forma pasiva su producto y versión,
         aclarando que es una técnica de reconocimiento autorizada.

         Evidencia: Usé la herramienta search_knowledge en la colección "apuntes"
         (ya que la colección "apuntes_2026" no existía), la cual devolvió el
         fragmento: "Un banner TCP es el texto que un servicio envía...".

         Límites: No se encontró información adicional sobre banners en otras
         colecciones...
```

En un solo turno el agente detectó el error, leyó el `hint`, corrigió el argumento y
respondió con evidencia. Nada de eso está programado como flujo fijo: lo decide el
modelo a partir del sobre de respuesta.

---

## 7. Requisitos

- Python 3.11+
- `nmap` instalado (`sudo apt install nmap`)
- Ollama con el modelo de embeddings:
  ```bash
  ollama serve
  ollama pull nomic-embed-text
  ```
- Una API key de Google AI Studio: <https://aistudio.google.com/apikey>

---

## 8. Instalación

```bash
git clone git@github.com:hearty22/agente-adk-escalante-facundo.git
cd agente-adk-escalante-facundo

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # o: uv pip install -r requirements.txt

cp agent/.env.example agent/.env
# editá agent/.env y completá GOOGLE_API_KEY
```

Contenido típico de `agent/.env`:

```env
GOOGLE_API_KEY=tu_api_key
MODEL_NAME=gemini-3.5-flash-lite
CHROMA_DIR=./vector_store
DEFAULT_COLLECTION=reconocimiento
OLLAMA_API_BASE=http://localhost:11434
EMBED_MODEL=nomic-embed-text
NMAP_TIMEOUT=120
HTTP_TIMEOUT=8
TCP_TIMEOUT=3
```

Ninguna configuración está hardcodeada: todo sale del entorno.

---

## 9. Cómo usarlo

Desde la raíz del repositorio:

```bash
adk web .
```

Abrí <http://localhost:8000>, elegí la app `agent` y escribí el pedido. También se puede usar por consola:

```bash
adk run agent
```

### Prompts de prueba sugeridos

Los tres primeros van en orden: el segundo crea la colección `apuntes` y el tercero
la consulta.

```
Listá las colecciones que hay en la base de conocimiento.
```

```
Guardá este texto en la colección apuntes:

Un banner TCP es el texto que un servicio envía al conectarse. Sirve para
identificar producto y versión de forma pasiva, sin explotar nada.
```

```
Buscá en la colección apuntes qué es un banner TCP.
```

```
¿Qué puertos abiertos tiene 127.0.0.1? Escaneá del 1 al 200.
```

```
¿Responde https://example.com? Decime el status y los headers principales.
```

```
Buscá en internet cuál es la última versión estable de Python.
```

---

## 10. Estructura del proyecto

```
.
├── agent/
│   ├── __init__.py
│   ├── agent.py           # root_agent: modelo + tools (wiring)
│   ├── prompts.py         # INSTRUCTION: rol, objetivo, restricciones, formato
│   ├── .env               # configuración local (no se versiona)
│   └── .env.example       # plantilla versionada
├── tools/
│   ├── __init__.py        # exporta las herramientas
│   └── tools.py           # implementación + validación
├── tests/
│   └── test_tools.py      # 42 tests de validación, offline
├── pytest.ini
├── requirements.txt
└── README.md
```

---

## 11. Tests

```bash
python -m pytest -v
```

42 tests que cubren argumentos inválidos, rangos fuera de límite, esquemas no soportados y el formato del sobre de respuesta. **No requieren red, ni Ollama, ni API key**: todos fallan en la validación antes de salir a buscar datos.

---

## 12. Decisiones de diseño

- **Sobre de respuesta uniforme.** Devolver siempre `{ok, tool, data|error, hint}` hace que el modelo pueda razonar sobre el resultado y que los tests sean triviales de escribir.
- **Validación en la herramienta, no en el prompt.** El prompt ayuda, pero un modelo puede pasar cualquier cosa. Cada tool se defiende sola.
- **Configuración por entorno.** Sin rutas ni credenciales en el código: se puede correr en otra máquina clonando el repo y completando `.env`.
- **Resultados vacíos ≠ error.** Un `count: 0` con `note` evita que el agente invente datos para "llenar" la respuesta.
- **`--` antes del objetivo en Nmap.** Evita que un host que empieza con `-` se interprete como una opción del comando.

---

## 13. Limitaciones

- Las herramientas de red (`nmap_scan`, `tcp_banner_grab`, `http_probe`) deben usarse **solo contra objetivos propios o de laboratorio**. El prompt lo declara como restricción y el agente rechaza pedidos contra terceros sin autorización, pero la garantía real es la validación de argumentos, no el modelo.
- El RAG necesita Ollama corriendo con `nomic-embed-text`. Si no está disponible, las herramientas devuelven un error explicativo en lugar de fallar en silencio.
- `web_search` depende de DuckDuckGo y puede devolver resultados vacíos o limitados por rate limiting.
- El proveedor de Gemini puede responder `503 UNAVAILABLE` en momentos de alta demanda; es transitorio, se resuelve reintentando el mensaje.
- El agente no persiste memoria entre conversaciones más allá de la sesión de ADK.

---

## 14. Autor

**Facundo Escalante** — Trabajo Práctico de la materia, desarrollado con Google ADK.
