"""Herramientas expuestas al agente.

Cada función tiene una responsabilidad distinta y devuelve el mismo sobre
`{"ok": bool, ...}`. Ver `tools/tools.py` para el detalle de cada una.
"""

from .tools import (
    http_probe,
    index_text,
    list_collections,
    nmap_scan,
    search_knowledge,
    tcp_banner_grab,
    web_search,
)

__all__ = [
    "http_probe",
    "index_text",
    "list_collections",
    "nmap_scan",
    "search_knowledge",
    "tcp_banner_grab",
    "web_search",
]
