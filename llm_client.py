"""
llm_client.py — Capa de LLM intercambiable para los generadores de Basketmática.

Todos los proveedores gratuitos relevantes exponen una API compatible con
OpenAI (chat/completions), así que un único cliente sirve para todos:

    Proveedor    base_url                                      ejemplo de modelo
    ─────────    ───────────────────────────────────────────   ─────────────────────────────
    groq         https://api.groq.com/openai/v1                llama-3.3-70b-versatile
    openrouter   https://openrouter.ai/api/v1                  meta-llama/llama-3.3-70b-instruct:free
    cerebras     https://api.cerebras.ai/v1                    gpt-oss-120b
    mistral      https://api.mistral.ai/v1                     mistral-small-latest
    gemini       https://generativelanguage.googleapis.com/v1beta/openai/   gemini-3-flash

Se configura por secrets/env, con cadena de fallback opcional:

    LLM_PROVIDERS = "groq,openrouter"      # orden de intento
    GROQ_API_KEY = "..."
    OPENROUTER_API_KEY = "..."
    # Overrides opcionales:
    GROQ_MODEL = "llama-3.3-70b-versatile"
    OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

El cliente pide SIEMPRE una respuesta JSON (los generadores ya no piden HTML
al modelo: el HTML lo monta Python). Eso reduce la salida a ~1K tokens y hace
viable cualquier free tier.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Modelos por defecto por proveedor (elegidos por calidad en español + free tier).
PRESETS: Dict[str, Dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_name": "GROQ_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "key_name": "OPENROUTER_API_KEY",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
        "key_name": "CEREBRAS_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "key_name": "MISTRAL_API_KEY",
    },
    "gemini": {
        # Endpoint de compatibilidad OpenAI de la API de Gemini.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "key_name": "API_KEY",  # tu clave de AI Studio de siempre
    },
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Esperas cortas ante 429: los límites por minuto se liberan en segundos; si el
# proveedor pide esperar más (p. ej. cupo diario agotado), pasamos al siguiente.
MAX_REINTENTOS_429 = 2
ESPERA_MAX_429_S = 12.0


def _espera_429(r: httpx.Response, reintento: int) -> Optional[float]:
    try:
        espera = float(r.headers.get("retry-after", ""))
    except ValueError:
        espera = 2.0 * (reintento + 1)
    return espera + 0.5 if espera <= ESPERA_MAX_429_S else None


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str


def cargar_proveedores(secrets: Dict[str, Any]) -> List[ProviderConfig]:
    """
    Construye la cadena de proveedores desde secrets/env.

    `secrets` puede ser st.secrets (Mapping) o os.environ. Se intentan en el
    orden de LLM_PROVIDERS (por defecto: "groq,openrouter,gemini") y se
    descartan los que no tengan clave configurada.
    """
    orden = str(secrets.get("LLM_PROVIDERS", "groq,openrouter,gemini"))
    provs: List[ProviderConfig] = []
    for nombre in [p.strip().lower() for p in orden.split(",") if p.strip()]:
        preset = PRESETS.get(nombre)
        if not preset:
            logger.warning("Proveedor desconocido en LLM_PROVIDERS: %s", nombre)
            continue
        api_key = str(secrets.get(preset["key_name"], "")).strip()
        if not api_key:
            continue
        model = str(
            secrets.get(f"{nombre.upper()}_MODEL", preset["model"])
        ).strip()
        base_url = str(
            secrets.get(f"{nombre.upper()}_BASE_URL", preset["base_url"])
        ).strip().rstrip("/")
        provs.append(ProviderConfig(nombre, base_url, model, api_key))
    return provs


def _extraer_json(texto: str) -> Dict[str, Any]:
    """Parsea JSON tolerando fences de Markdown y texto alrededor."""
    texto = _FENCE_RE.sub("", (texto or "").strip()).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        # Último recurso: primer bloque {...} balanceado.
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def generar_json(
    prompt: str,
    proveedores: List[ProviderConfig],
    system: Optional[str] = None,
    max_tokens: int = 3000,
    temperature: float = 0.35,
) -> Dict[str, Any]:
    """
    Pide una respuesta JSON al primer proveedor disponible; si falla
    (rate limit, red, JSON inválido), pasa al siguiente de la cadena.

    Raises
    ------
    EnvironmentError  Ningún proveedor configurado.
    RuntimeError      Todos los proveedores fallaron.
    """
    if not proveedores:
        raise EnvironmentError(
            "Ningún proveedor de LLM configurado. Añade en Secrets al menos "
            "una clave: GROQ_API_KEY (gratis, sin tarjeta, console.groq.com), "
            "OPENROUTER_API_KEY (openrouter.ai) o API_KEY (Gemini)."
        )

    errores: List[str] = []
    for prov in proveedores:
        payload: Dict[str, Any] = {
            "model": prov.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": (
                [{"role": "system", "content": system}] if system else []
            )
            + [{"role": "user", "content": prompt}],
            # La mayoría de proveedores OpenAI-compatibles lo soportan; si no,
            # reintentamos sin él más abajo.
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {prov.api_key}",
            "Content-Type": "application/json",
        }
        if prov.name == "openrouter":
            headers["HTTP-Referer"] = "https://basketmatica.com"
            headers["X-Title"] = "Basketmatica Report Generator"

        usar_response_format = True
        reintentos_429 = 0
        while True:
            body = payload if usar_response_format else {
                k: v for k, v in payload.items() if k != "response_format"
            }
            try:
                r = httpx.post(
                    f"{prov.base_url}/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(90.0, connect=10.0),
                )
            except httpx.HTTPError as exc:
                errores.append(f"{prov.name}: red ({exc.__class__.__name__})")
                break  # siguiente proveedor
            if r.status_code == 200:
                try:
                    data = r.json()
                    contenido = data["choices"][0]["message"]["content"]
                    resultado = _extraer_json(contenido)
                    logger.info(
                        "✓ Análisis generado con %s (%s)", prov.name, prov.model
                    )
                    resultado["_modelo"] = f"{prov.name}/{prov.model}"
                    return resultado
                except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                    errores.append(f"{prov.name}: respuesta inválida ({exc})")
                    break
            if r.status_code == 400 and usar_response_format:
                usar_response_format = False
                continue
            if r.status_code == 429:
                espera = _espera_429(r, reintentos_429)
                if espera is not None and reintentos_429 < MAX_REINTENTOS_429:
                    reintentos_429 += 1
                    logger.warning(
                        "%s: rate limit (429), reintento %d en %.1f s",
                        prov.name, reintentos_429, espera,
                    )
                    time.sleep(espera)
                    continue
                errores.append(f"{prov.name}: rate limit (429)")
            elif r.status_code in (401, 403):
                errores.append(f"{prov.name}: clave inválida (HTTP {r.status_code})")
            else:
                errores.append(f"{prov.name}: HTTP {r.status_code}")
            break  # siguiente proveedor

    raise RuntimeError(
        "Todos los proveedores de LLM fallaron: " + " · ".join(errores)
    )
