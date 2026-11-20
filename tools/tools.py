"""Herramientas del agente de reconocimiento.

Todas las herramientas devuelven un diccionario con la misma forma, para que el
modelo (y los tests) puedan razonar sobre el resultado sin sorpresas:

    {"ok": True,  "tool": "<nombre>", "data": {...}}
    {"ok": False, "tool": "<nombre>", "error": "<qué falló>", "hint": "<qué hacer>"}

`ok=False` significa que la herramienta no pudo completar su trabajo. Cuando eso
pasa el agente debe informar el error, nunca inventar un resultado.

La configuración se lee del entorno en el momento de cada llamada (ver
`agent/.env.example`), así se puede reconfigurar el agente sin tocar el código.
"""

import ipaddress
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import chromadb
from ddgs import DDGS
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

MAX_TEXT_CHARS = 100_000
MAX_SEARCH_RESULTS = 20


def _cfg(name, default):
    """Lee una variable de entorno de texto, con fallback."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _cfg_int(name, default):
    """Lee una variable de entorno numérica, con fallback si no es válida."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Sobre de respuesta
# --------------------------------------------------------------------------- #


def _ok(tool, data, note=None):
    payload = {"ok": True, "tool": tool, "data": data}
    if note:
        payload["note"] = note
    return payload


def _err(tool, error, hint=None):
    payload = {"ok": False, "tool": tool, "error": error}
    if hint:
        payload["hint"] = hint
    return payload


# --------------------------------------------------------------------------- #
# Validación de argumentos
# --------------------------------------------------------------------------- #

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,59}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,59}[a-zA-Z0-9])?$"
)
_PORTS_SPEC_RE = re.compile(r"^\d{1,5}(?:-\d{1,5})?(?:,\d{1,5}(?:-\d{1,5})?)*$")
_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$")


def _validate_host(value):
    """Devuelve el host normalizado o lanza ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("el objetivo (target/host) está vacío")

    host = value.strip()
    if "://" in host or "/" in host:
        raise ValueError(
            "'%s' parece una URL; esta herramienta espera solo un host o una IP"
            % value
        )
    if len(host) > 253:
        raise ValueError("el host supera los 253 caracteres permitidos")

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if not _HOSTNAME_RE.match(host):
        raise ValueError(
            "'%s' no es una IP ni un nombre de host válido" % host
        )
    return host


def _validate_port(value, name="puerto"):
    """Devuelve el puerto como int o lanza ValueError."""
    if isinstance(value, bool):
        raise ValueError("%s inválido: se esperaba un número" % name)
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s inválido: '%s' no es un número" % (name, value))
    if not 1 <= port <= 65535:
        raise ValueError("%s fuera de rango: %s (válido: 1-65535)" % (name, port))
    return port


def _validate_ports_spec(value):
    """Valida un rango de Nmap tipo '22,80,443' o '1-1000'."""
    if value is None or not str(value).strip():
        return "1-1000"

    spec = str(value).strip()
    if not _PORTS_SPEC_RE.match(spec):
        raise ValueError(
            "rango de puertos inválido: '%s' (ejemplos: '80', '22,80,443', '1-1000')"
            % spec
        )

    for part in spec.split(","):
        start, _, end = part.partition("-")
        start_port = int(start)
        end_port = int(end) if end else start_port
        if not 1 <= start_port <= 65535 or not 1 <= end_port <= 65535:
            raise ValueError(
                "los puertos deben estar entre 1 y 65535 (rango recibido: '%s')" % part
            )
        if start_port > end_port:
            raise ValueError(
                "rango de puertos invertido: '%s' (el inicio es mayor que el fin)" % part
            )

    return spec


def _validate_url(value):
    """Valida que sea una URL http/https con host."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("la URL está vacía")

    url = value.strip()
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            "esquema no soportado: '%s' (solo se aceptan http y https)"
            % (parsed.scheme or "(ninguno)")
        )
    if not parsed.hostname:
        raise ValueError("la URL no tiene host: '%s'" % url)

    return url


def _validate_collection(value):
    """Valida el nombre de una colección de Chroma."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("el nombre de la colección está vacío")

    name = value.strip()
    if not _COLLECTION_RE.match(name):
        raise ValueError(
            "nombre de colección inválido: '%s' (3-63 caracteres alfanuméricos, "
            "punto, guion o guion bajo; debe empezar y terminar alfanumérico)" % name
        )
    return name


def _validate_text(value):
    """Valida el texto a indexar."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("el texto a indexar está vacío")

    text = value.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            "el texto supera el máximo de %s caracteres (recibidos: %s)"
            % (MAX_TEXT_CHARS, len(text))
        )
    return text


def _validate_query(value, name="consulta"):
    """Valida una consulta de búsqueda."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("la %s está vacía" % name)

    query = value.strip()
    if len(query) > 500:
        raise ValueError("la %s supera los 500 caracteres" % name)
    return query


def _validate_limit(value, name, default, maximum=MAX_SEARCH_RESULTS):
    """Valida un límite numérico (k, max_results, timeout)."""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s inválido: '%s' no es un número" % (name, value))
    if not 1 <= number <= maximum:
        raise ValueError("%s fuera de rango: %s (válido: 1-%s)" % (name, number, maximum))
    return number


# --------------------------------------------------------------------------- #
# Helpers de Chroma / Ollama
# --------------------------------------------------------------------------- #


def _chroma_dir():
    return _cfg("CHROMA_DIR", "./vector_store")


def _embeddings():
    return OllamaEmbeddings(
        model=_cfg("EMBED_MODEL", "nomic-embed-text"),
        base_url=_cfg("OLLAMA_API_BASE", "http://localhost:11434"),
    )


def _vector_store(collection_name):
    return Chroma(
        embedding_function=_embeddings(),
        persist_directory=_chroma_dir(),
        collection_name=collection_name,
    )


def _ollama_error(exc):
    """Traduce un fallo de embeddings a un mensaje accionable."""
    message = str(exc).lower()
    if isinstance(exc, ConnectionError) or "connect" in message:
        return (
            "no se pudo conectar con Ollama en %s"
            % _cfg("OLLAMA_API_BASE", "http://localhost:11434"),
            "ejecutá 'ollama serve' y verificá que el servicio esté levantado",
        )
    if "not found" in message or "404" in message:
        return (
            "el modelo de embeddings '%s' no está descargado"
            % _cfg("EMBED_MODEL", "nomic-embed-text"),
            "ejecutá 'ollama pull %s'" % _cfg("EMBED_MODEL", "nomic-embed-text"),
        )
    return ("falló la generación de embeddings: %s" % exc, None)


def _collection_names():
    """Nombres de las colecciones existentes (compatible con distintas versiones)."""
    client = chromadb.PersistentClient(path=_chroma_dir())
    return sorted(
        collection.name if hasattr(collection, "name") else str(collection)
        for collection in client.list_collections()
    )


def _collection_count(collection_name):
    """Cantidad de documentos indexados en una colección."""
    client = chromadb.PersistentClient(path=_chroma_dir())
    return client.get_collection(collection_name).count()


# --------------------------------------------------------------------------- #
# Herramienta 1: búsqueda web
# --------------------------------------------------------------------------- #


def web_search(query: str, max_results: int = 10) -> dict:
    """Busca en internet y devuelve los resultados más relevantes.

    Usala cuando el usuario pregunta por información pública y actualizada que no
    está en la base de conocimiento (noticias, documentación, precios, versiones).

    Args:
        query: Texto a buscar en internet.
        max_results: Cantidad máxima de resultados (1-20).
    """
    tool = "web_search"

    try:
        clean_query = _validate_query(query, "consulta")
        limit = _validate_limit(max_results, "max_results", default=10)
    except ValueError as exc:
        return _err(tool, str(exc), "pasá una consulta de texto y max_results entre 1 y 20")

    try:
        results = DDGS().text(clean_query, max_results=limit) or []
    except Exception as exc:
        return _err(
            tool,
            "la búsqueda web falló: %s" % exc,
            "puede ser un problema de red o un bloqueo temporal del buscador; "
            "probá de nuevo o respondé con lo que ya sabés",
        )

    normalized = [
        {
            "title": item.get("title") or "",
            "url": item.get("href") or item.get("url") or "",
            "snippet": item.get("body") or item.get("snippet") or "",
        }
        for item in results
        if isinstance(item, dict)
    ]

    if not normalized:
        return _ok(tool, {"query": clean_query, "results": [], "count": 0},
                   note="la búsqueda no devolvió resultados")

    return _ok(tool, {"query": clean_query, "count": len(normalized),
                      "results": normalized})


# --------------------------------------------------------------------------- #
# Herramienta 2: escaneo de puertos
# --------------------------------------------------------------------------- #


def nmap_scan(target: str, ports: str = "1-1000") -> dict:
    """Escanea puertos TCP de un objetivo AUTORIZADO con Nmap.

    Usala cuando el usuario pide relevar qué servicios expone un host.

    Args:
        target: IP o nombre de host del objetivo (sin http:// ni rutas).
        ports: Rango de puertos, por ejemplo "80", "22,80,443" o "1-1000".
    """
    tool = "nmap_scan"

    try:
        host = _validate_host(target)
        ports_spec = _validate_ports_spec(ports)
    except ValueError as exc:
        return _err(
            tool, str(exc),
            "ejemplo válido: target='127.0.0.1', ports='22,80,443'",
        )

    timeout = _cfg_int("NMAP_TIMEOUT", 120)

    try:
        completed = subprocess.run(
            ["nmap", "-sV", "-p", ports_spec, "--", host],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _err(
            tool,
            "el escaneo superó el tiempo límite de %s segundos" % timeout,
            "achicá el rango de puertos o subí NMAP_TIMEOUT en el .env",
        )
    except FileNotFoundError:
        return _err(
            tool,
            "Nmap no está instalado o no se encuentra en el PATH",
            "instalalo con 'sudo apt install nmap'",
        )

    open_ports = [
        {"port": int(match.group(1)),
         "protocol": match.group(2),
         "service": match.group(3),
         "version": (match.group(4) or "").strip()}
        for match in re.finditer(
            r"^(\d+)/(tcp|udp)\s+open\s+(\S+)\s*(.*)$", completed.stdout, re.MULTILINE
        )
    ]

    if completed.returncode != 0 and not open_ports:
        return _err(
            tool,
            "Nmap terminó con código %s" % completed.returncode,
            (completed.stderr or "sin salida de error").strip()[:300],
        )

    data = {
        "target": host,
        "ports_scanned": ports_spec,
        "open_ports": open_ports,
        "count": len(open_ports),
        "raw_output": completed.stdout[:4000],
    }

    if not open_ports:
        return _ok(tool, data,
                   note="el objetivo no respondió con puertos abiertos en el rango pedido")

    return _ok(tool, data)


# --------------------------------------------------------------------------- #
# Herramienta 3: banner de servicio TCP
# --------------------------------------------------------------------------- #


def tcp_banner_grab(host: str, port: int, timeout: int = 0) -> dict:
    """Se conecta a un puerto TCP y captura el banner inicial del servicio.

    Usala para identificar la versión de un servicio ya descubierto por nmap_scan.

    Args:
        host: IP o nombre de host del objetivo.
        port: Puerto TCP a consultar (1-65535).
        timeout: Segundos de espera. 0 usa TCP_TIMEOUT del .env.
    """
    tool = "tcp_banner_grab"

    try:
        target = _validate_host(host)
        port_number = _validate_port(port)
        wait = (
            _cfg_int("TCP_TIMEOUT", 3)
            if not timeout
            else _validate_limit(timeout, "timeout", default=3, maximum=60)
        )
    except ValueError as exc:
        return _err(
            tool, str(exc),
            "ejemplo válido: host='127.0.0.1', port=22",
        )

    try:
        with socket.create_connection((target, port_number), timeout=wait) as connection:
            connection.settimeout(wait)
            try:
                raw = connection.recv(1024)
            except socket.timeout:
                raw = b""
    except socket.timeout:
        return _err(
            tool,
            "el puerto %s de %s no respondió en %s segundos" % (port_number, target, wait),
            "el puerto puede estar filtrado o el servicio tardar más; probá con timeout mayor",
        )
    except ConnectionRefusedError:
        return _err(
            tool,
            "conexión rechazada en %s:%s" % (target, port_number),
            "el puerto está cerrado; confirmá con nmap_scan antes de reintentar",
        )
    except socket.gaierror:
        return _err(
            tool,
            "no se pudo resolver el host '%s'" % target,
            "revisá que el nombre esté bien escrito o usá una IP",
        )
    except OSError as exc:
        return _err(tool, "no se pudo conectar a %s:%s: %s" % (target, port_number, exc))

    banner = raw.decode("utf-8", errors="replace").strip()
    data = {"host": target, "port": port_number, "banner": banner, "bytes": len(raw)}

    if not banner:
        return _ok(tool, data,
                   note="el puerto está abierto pero el servicio no envió banner")

    return _ok(tool, data)


# --------------------------------------------------------------------------- #
# Herramienta 4: sonda HTTP
# --------------------------------------------------------------------------- #


def http_probe(url: str, timeout: int = 0) -> dict:
    """Hace una petición GET y devuelve status, headers y un fragmento del cuerpo.

    Usala para verificar si un sitio responde y qué tecnología expone en headers.

    Args:
        url: URL http o https a consultar.
        timeout: Segundos de espera. 0 usa HTTP_TIMEOUT del .env.
    """
    tool = "http_probe"

    try:
        clean_url = _validate_url(url)
        wait = (
            _cfg_int("HTTP_TIMEOUT", 8)
            if not timeout
            else _validate_limit(timeout, "timeout", default=8, maximum=60)
        )
    except ValueError as exc:
        return _err(
            tool, str(exc),
            "ejemplo válido: url='https://example.com'",
        )

    request = urllib.request.Request(
        clean_url, method="GET", headers={"User-Agent": "ADK-Recon-Agent/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=wait) as response:
            body = response.read(500)
            data = {
                "url": clean_url,
                "status": response.status,
                "headers": dict(response.headers),
                "body_snippet": body.decode("utf-8", errors="replace"),
            }
            return _ok(tool, data)
    except urllib.error.HTTPError as exc:
        # Un 4xx/5xx también es información: el servidor respondió.
        body = exc.read(500) if exc.fp else b""
        data = {
            "url": clean_url,
            "status": exc.code,
            "headers": dict(exc.headers or {}),
            "body_snippet": body.decode("utf-8", errors="replace"),
        }
        return _ok(tool, data, note="el servidor respondió con un estado de error HTTP")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return _err(
                tool,
                "no se pudo resolver el host de '%s'" % clean_url,
                "revisá el dominio o usá una IP",
            )
        return _err(
            tool,
            "no se pudo conectar con '%s': %s" % (clean_url, reason),
            "verificá que el sitio esté arriba y que la URL sea correcta",
        )
    except socket.timeout:
        return _err(
            tool,
            "la petición a '%s' superó los %s segundos" % (clean_url, wait),
            "el sitio puede estar lento o filtrando la conexión",
        )


# --------------------------------------------------------------------------- #
# Herramienta 5: indexar texto en la base de conocimiento
# --------------------------------------------------------------------------- #


def index_text(text: str, collection_name: str = "") -> dict:
    """Guarda un texto en la base vectorial para poder consultarlo después.

    Usala cuando el usuario pide "recordar", "guardar" o "indexar" un contenido.

    Args:
        text: Texto a indexar (se parte en fragmentos automáticamente).
        collection_name: Colección donde guardarlo. Vacío usa DEFAULT_COLLECTION.
    """
    tool = "index_text"

    try:
        clean_text = _validate_text(text)
        collection = _validate_collection(
            collection_name or _cfg("DEFAULT_COLLECTION", "reconocimiento")
        )
    except ValueError as exc:
        return _err(
            tool, str(exc),
            "ejemplo válido: text='<contenido>', collection_name='apuntes'",
        )

    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    chunks = splitter.create_documents([clean_text])

    try:
        store = _vector_store(collection)
        ids = store.add_documents(documents=chunks)
    except Exception as exc:
        message, hint = _ollama_error(exc)
        return _err(tool, message, hint)

    return _ok(
        tool,
        {
            "collection": collection,
            "chunks": len(ids),
            "characters": len(clean_text),
        },
    )


# --------------------------------------------------------------------------- #
# Herramienta 6: búsqueda semántica
# --------------------------------------------------------------------------- #


def search_knowledge(query: str, collection_name: str = "", k: int = 5) -> dict:
    """Busca los fragmentos más parecidos a una consulta dentro de la base vectorial.

    Usala cuando el usuario pregunta sobre algo que ya se indexó previamente o
    cuando necesita recuperar apuntes, documentos o notas guardadas.

    Args:
        query: Consulta en lenguaje natural.
        collection_name: Colección a consultar. Vacío usa DEFAULT_COLLECTION.
        k: Cantidad de fragmentos a devolver (1-20).
    """
    tool = "search_knowledge"

    try:
        clean_query = _validate_query(query)
        collection = _validate_collection(
            collection_name or _cfg("DEFAULT_COLLECTION", "reconocimiento")
        )
        limit = _validate_limit(k, "k", default=5)
    except ValueError as exc:
        return _err(
            tool, str(exc),
            "ejemplo válido: query='¿qué es un banner?', collection_name='apuntes', k=3",
        )

    try:
        available = _collection_names()
    except Exception as exc:
        return _err(tool, "no se pudo abrir la base vectorial: %s" % exc,
                    "revisá CHROMA_DIR en el .env")

    if collection not in available:
        return _err(
            tool,
            "la colección '%s' no existe" % collection,
            "colecciones disponibles: %s" % (", ".join(available) or "(ninguna)"),
        )

    try:
        store = _vector_store(collection)
        if _collection_count(collection) == 0:
            return _ok(
                tool,
                {"query": clean_query, "collection": collection, "results": [], "count": 0},
                note="la colección existe pero todavía no tiene documentos indexados",
            )
        pairs = store.similarity_search_with_relevance_scores(clean_query, k=limit)
    except Exception as exc:
        message, hint = _ollama_error(exc)
        return _err(tool, message, hint)

    results = [
        {
            "content": document.page_content.strip(),
            "score": round(float(score), 4),
            "metadata": dict(document.metadata or {}),
        }
        for document, score in pairs
    ]

    if not results:
        return _ok(
            tool,
            {"query": clean_query, "collection": collection, "results": [], "count": 0},
            note="no se encontraron fragmentos relevantes para la consulta",
        )

    return _ok(
        tool,
        {"query": clean_query, "collection": collection,
         "count": len(results), "results": results},
    )


# --------------------------------------------------------------------------- #
# Herramienta 7: listar colecciones
# --------------------------------------------------------------------------- #


def list_collections() -> dict:
    """Lista las colecciones disponibles en la base vectorial.

    Usala antes de indexar o buscar, para saber qué nombres de colección existen.
    """
    tool = "list_collections"

    try:
        names = _collection_names()
    except Exception as exc:
        return _err(tool, "no se pudo abrir la base vectorial: %s" % exc,
                    "revisá CHROMA_DIR en el .env")

    if not names:
        return _ok(tool, {"collections": [], "count": 0},
                   note="todavía no hay colecciones creadas")

    return _ok(tool, {"collections": names, "count": len(names)})
