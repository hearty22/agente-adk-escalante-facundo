"""Configuración del agente raíz para `adk web` / `adk run`."""

import os
from pathlib import Path

from dotenv import load_dotenv

# ADK carga `agent/.env` al iniciar su interfaz, pero adelantarlo acá permite
# ejecutar el agente desde un script o desde los tests con la misma configuración.
load_dotenv(Path(__file__).resolve().parent / ".env")

from google.adk.agents.llm_agent import Agent  # noqa: E402
from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from tools import (  # noqa: E402
    http_probe,
    index_text,
    list_collections,
    nmap_scan,
    search_knowledge,
    tcp_banner_grab,
    web_search,
)

from .prompts import INSTRUCTION  # noqa: E402

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def build_model():
    """Devuelve el modelo configurado en MODEL_NAME.

    Sin "/" se usa el proveedor nativo de ADK (Gemini). Con "/" se enruta por
    LiteLLM, por ejemplo "ollama_chat/minimax-m2.1:cloud".
    """
    name = (os.getenv("MODEL_NAME") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return LiteLlm(name) if "/" in name else name


root_agent = Agent(
    name="root_agent",
    model=build_model(),
    description=(
        "Agente de reconocimiento asistido: busca en internet, escanea puertos, "
        "consulta servicios HTTP y recupera conocimiento indexado."
    ),
    instruction=INSTRUCTION,
    tools=[
        web_search,
        nmap_scan,
        tcp_banner_grab,
        http_probe,
        index_text,
        search_knowledge,
        list_collections,
    ],
)
