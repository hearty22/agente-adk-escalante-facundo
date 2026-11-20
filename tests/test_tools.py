"""Tests de validación de las herramientas.

Todos los casos son offline: apuntan a argumentos inválidos que deben rechazarse
ANTES de tocar la red o de llamar a Ollama. Así los tests corren en cualquier
máquina, sin servicios levantados.

Ejecución:  python -m pytest -v
"""

import pytest

from tools import (
    http_probe,
    index_text,
    list_collections,
    nmap_scan,
    search_knowledge,
    tcp_banner_grab,
    web_search,
)


def assert_error(result, fragment):
    """Verifica el sobre de error y que el mensaje mencione el problema."""
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "tool" in result
    assert "error" in result
    assert fragment.lower() in result["error"].lower()


# --------------------------------------------------------------------------- #
# Sobre de respuesta
# --------------------------------------------------------------------------- #


def test_todas_las_herramientas_devuelven_el_mismo_sobre():
    results = [
        nmap_scan(""),
        tcp_banner_grab("", 0),
        http_probe(""),
        web_search(""),
        index_text(""),
        search_knowledge("", "reconocimiento"),
    ]
    for result in results:
        assert result["ok"] is False
        assert set(result) >= {"ok", "tool", "error"}


def test_list_collections_devuelve_estructura_valida():
    result = list_collections()
    assert result["ok"] is True
    assert isinstance(result["data"]["collections"], list)
    assert result["data"]["count"] == len(result["data"]["collections"])


# --------------------------------------------------------------------------- #
# nmap_scan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", ["", "   ", None, 123])
def test_nmap_target_vacio_o_invalido(target):
    assert_error(nmap_scan(target), "vacío")


def test_nmap_rechaza_url_como_target():
    assert_error(nmap_scan("https://example.com"), "parece una url")


def test_nmap_rechaza_host_con_ruta():
    assert_error(nmap_scan("example.com/admin"), "parece una url")


@pytest.mark.parametrize("ports", ["ochenta", "80;rm", "1-1000-2000"])
def test_nmap_rechaza_rango_de_puertos_mal_formado(ports):
    assert_error(nmap_scan("127.0.0.1", ports), "rango de puertos inválido")


def test_nmap_rechaza_puerto_fuera_de_rango():
    assert_error(nmap_scan("127.0.0.1", "80-70000"), "entre 1 y 65535")


def test_nmap_rechaza_rango_invertido():
    assert_error(nmap_scan("127.0.0.1", "443-80"), "invertido")


# --------------------------------------------------------------------------- #
# tcp_banner_grab
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port", [70000, 0, -1, "abc", None, True])
def test_tcp_rechaza_puerto_invalido(port):
    assert_error(tcp_banner_grab("127.0.0.1", port), "puerto")


def test_tcp_rechaza_host_vacio():
    assert_error(tcp_banner_grab("", 80), "vacío")


# --------------------------------------------------------------------------- #
# http_probe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "example.com"])
def test_http_rechaza_esquema_no_soportado(url):
    assert_error(http_probe(url), "esquema no soportado")


def test_http_rechaza_url_vacia():
    assert_error(http_probe(""), "vacía")


# --------------------------------------------------------------------------- #
# web_search
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("query", ["", "   ", None])
def test_web_search_rechaza_consulta_vacia(query):
    assert_error(web_search(query), "vacía")


@pytest.mark.parametrize("limit", [0, 21, 999, "muchos"])
def test_web_search_rechaza_max_results_invalido(limit):
    assert_error(web_search("adk", limit), "max_results")


# --------------------------------------------------------------------------- #
# index_text / search_knowledge
# --------------------------------------------------------------------------- #


def test_index_rechaza_texto_vacio():
    assert_error(index_text(""), "vacío")


def test_index_rechaza_texto_demasiado_largo():
    assert_error(index_text("a" * 100_001), "supera el máximo")


@pytest.mark.parametrize("collection", ["ab", "  ", "con espacio", "-empieza-mal"])
def test_index_rechaza_coleccion_invalida(collection):
    assert_error(index_text("contenido", collection), "colección")


def test_search_rechaza_consulta_vacia():
    assert_error(search_knowledge("", "reconocimiento"), "vacía")


@pytest.mark.parametrize("k", [0, 21, "cinco"])
def test_search_rechaza_k_invalido(k):
    assert_error(search_knowledge("apuntes", "reconocimiento", k), "k")


def test_search_avisa_si_la_coleccion_no_existe():
    result = search_knowledge("apuntes", "coleccion_que_no_existe")
    assert_error(result, "no existe")
    assert "hint" in result
