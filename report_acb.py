"""
report_acb.py — Datos de acb.com → vista del informe → PDF.

Solo contiene lo propio de la Liga Endesa: la configuración de la competición,
la vista construida desde acb_data y los ratios calculados. El prompt, la
normalización del texto de la IA y el render son comunes (informe_comun.py,
idéntico en el generador NBA).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from acb_data import obtener_datos_jugador_acb
from informe_comun import Competicion, generar_pdf
from llm_client import ProviderConfig

logger = logging.getLogger(__name__)

COMPETICION = Competicion(
    nombre="Liga Endesa",
    fuente="acb.com",
    ambito_similares="la ACB y Europa",
    minutos_normalizacion=40,
    pj_minimo=10,
    metricas_clave="Valoración, TS% y eFG% oficiales, Cuatro Factores y per-40",
    contexto_metricas=(
        '- "Valoración" es el índice oficial europeo (PIR), LA métrica de referencia en ACB.\n'
        '- "avanzadas_oficiales" son estadísticas avanzadas OFICIALES de acb.com de la temporada de '
        "referencia: Cuatro Factores (eFG%, ORB%, TOV%, FTr), manejo de balón (AST%, STL%, BLK%), "
        "lanzamiento (TS%, 3PAr, PPT), puntos por 100 posesiones y ritmo.\n"
        '- "rankings_liga" son los puestos del jugador en los rankings oficiales de la Liga Endesa: '
        "úsalos, dan mucho contexto.\n"
        '- "trayectoria" es la serie temporada a temporada de su carrera ACB, con el club de cada año: '
        "úsala para el arco de carrera (evolución, picos, cambios de equipo y tendencia reciente).\n"
        '- "records" son sus máximos en un partido de la Liga Endesa.'
    ),
    col_promedios=[
        ("Partidos", "PJ"), ("Minutos", "MIN"), ("Puntos", "PTS"),
        ("%2P", "%2P"), ("%3P", "%3P"), ("%TL", "%TL"),
        ("Rebotes", "REB"), ("Asistencias", "AST"),
        ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Valoración", "VAL"),
    ],
    col_normalizado=[
        ("Puntos", "PTS"), ("Rebotes", "REB"), ("Asistencias", "AST"),
        ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Valoración", "VAL"),
    ],
    col_trayectoria=[
        ("Temporada", "Temp"), ("Club", "Club"), ("PJ", "PJ"), ("Minutos", "MIN"),
        ("Puntos", "PTS"), ("%2P", "%2P"), ("%3P", "%3P"), ("%TL", "%TL"),
        ("Rebotes", "REB"), ("Asistencias", "AST"), ("Valoración", "VAL"),
    ],
    titulo_avanzadas="Estadísticas avanzadas (oficiales acb.com)",
    leyenda_avanzadas=(
        "eFG%: % de tiro efectivo · TS%: % de tiro real · FTr: tiros libres intentados por "
        "tiro de campo · 3PAr: proporción de intentos de 3 · TOV%: % de pérdidas por posesión · "
        "AST%/ORB%/DRB%/STL%/BLK%: % de asistencias, rebotes of./def., robos y tapones del "
        "equipo generados por el jugador · PPT: puntos por tiro · PPFT/PP2PS/PP3PS: puntos por "
        "tiro libre / lanzamiento de 2 / lanzamiento de 3."
    ),
    leyenda_ratios=(
        "AST/BP: asistencias por pérdida, calculado por Basketmática. Pérdidas y triples "
        "intentados por partido: datos oficiales de acb.com."
    ),
)

_CAMPOS_PERFIL = [
    "Posición", "Equipo", "Dorsal", "Altura", "Fecha nacimiento",
    "Lugar nacimiento", "Nacionalidad", "Licencia",
]


def _num_es(v: Any) -> Optional[float]:
    """'4,3' -> 4.3 | '89,9%' -> 89.9 | '22:05' -> 22.08 (min decimales)."""
    s = str(v or "").strip().replace("%", "").strip()
    if ":" in s:
        try:
            mm, ss = s.split(":")
            return round(int(mm) + int(ss) / 60.0, 2)
        except ValueError:
            return None
    try:
        return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)
    except ValueError:
        return None


def _ratios(est: Dict[str, Any]) -> Dict[str, str]:
    """Ratios de la fila de la temporada de referencia en la trayectoria (única fuente de pérdidas e intentos)."""
    label = str(est.get("temporada_label", ""))  # '2025-26' -> '25-26'
    corto = label[2:] if len(label) >= 7 else label
    fila = next((t for t in est.get("trayectoria") or [] if t.get("Temporada") == corto), None)
    if fila is None:
        return {}
    out: Dict[str, str] = {}
    ast = _num_es(fila.get("Asistencias"))
    bp = _num_es(fila.get("Pérdidas"))
    if ast is not None and bp and bp > 0:
        out["AST/BP (calculado)"] = f"{ast / bp:.1f}"
    if fila.get("Pérdidas") not in (None, ""):
        out["Pérdidas/partido"] = str(fila["Pérdidas"])
    if fila.get("T3_int") not in (None, ""):
        out["Triples intentados/partido"] = str(fila["T3_int"])
    return out


def _avanzadas_planas(avanz: Dict[str, Any]) -> Dict[str, str]:
    """Aplana los bloques oficiales SIN duplicados (eFG%, TOV% y ORB% aparecen en dos bloques del sitio)."""
    out: Dict[str, str] = {}
    for bloque in ("Cuatro Factores", "Lanzamiento", "Manejo de Balón", "Puntos", "Rebotes"):
        contenido = avanz.get(bloque)
        if isinstance(contenido, dict):
            for metrica, valor in contenido.items():
                out.setdefault(metrica, str(valor))
    if avanz.get("PTS_100_posesiones"):
        out["PTS/100 posesiones"] = str(avanz["PTS_100_posesiones"])
    if avanz.get("Posesiones_40min"):
        out["Posesiones por 40'"] = str(avanz["Posesiones_40min"])
    return out


def construir_vista(player_data: Dict[str, Any]) -> Dict[str, Any]:
    bio = player_data.get("Datos personales", {})
    est = player_data.get("Estadísticas", {})
    norm = COMPETICION.clave_normalizado
    temp = est.get("temporada") or {}
    carrera = est.get("carrera") or {}
    slug = re.search(r"/jugadores/([a-z0-9-]+)-\d+/?$", str(bio.get("Ficha_acb", "")))
    return {
        "jugador": {"Nombre": bio.get("Nombre"), **{c: bio.get(c) for c in _CAMPOS_PERFIL}},
        "alias": [slug.group(1).replace("-", " ")] if slug else [],
        "foto": bio.get("Foto"),
        "temporada": {
            "etiqueta": est.get("temporada_label", ""),
            "promedios": temp,
            "rankings_liga": temp.get("_rankings_liga"),
            norm: temp.get("per40"),
            "ratios": _ratios(est),
        },
        "carrera": {"promedios": carrera, norm: carrera.get("per40")},
        "avanzadas_oficiales": _avanzadas_planas(est.get("avanzadas") or {}),
        "trayectoria": est.get("trayectoria"),
        "records": est.get("records"),
        "nota_temporada": est.get("_nota_temporada"),
    }


def generar_pdf_jugador_acb(
    nombre_jugador: str,
    proveedores: List[ProviderConfig],
    indice: Optional[list] = None,
    player_data: Optional[Dict[str, Any]] = None,
) -> bytes:
    """
    Pipeline completo. Devuelve el PDF como bytes.

    Raises
    ------
    ValueError        Jugador no encontrado (con sugerencias).
    EnvironmentError  Ningún proveedor de LLM configurado.
    RuntimeError      Errores transitorios de red / API.
    """
    logger.info("=== Informe ACB para: '%s' ===", nombre_jugador)
    if player_data is None:
        player_data = obtener_datos_jugador_acb(nombre_jugador, indice=indice)
    return generar_pdf(construir_vista(player_data), COMPETICION, proveedores)
