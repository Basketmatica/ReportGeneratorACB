"""
report_acb.py — Nombre → datos ACB → análisis (LLM, JSON) → HTML (Python) → PDF.

Misma arquitectura que el generador NBA v2:

  * El LLM NO genera HTML. Solo redacta el análisis (desempeño, FODA,
    proyección, similares) como JSON compacto (~1K tokens) vía llm_client
    (Groq / OpenRouter / Cerebras / Gemini, con fallback en cadena).
  * Las tablas las renderiza Python desde los datos scrapeados de acb.com →
    los números del PDF no pasan por el modelo (imposible alucinarlos) y
    cabemos en los límites de cualquier free tier.
  * Devuelve bytes (st.download_button), sin ficheros temporales.
  * Design tokens reales de basketmatica.com (teal/espresso/court).
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from weasyprint import HTML

from acb_data import obtener_datos_jugador_acb
from llm_client import ProviderConfig, generar_json

logger = logging.getLogger(__name__)

# ─── Design tokens Basketmática (espejo de :root en global.css) ───────────────
BG = "#F4EFE5"         # --bg
SURFACE = "#FBF8F0"    # --surface
LINE = "#E2D8C4"       # --line
INK = "#221A10"        # --ink
INK_SOFT = "#6E5E46"   # --ink-soft
BRAND = "#583C14"      # --brand (espresso: titulares e identidad)
ACCENT = "#1F8A74"     # --accent (teal: filetes, highlights de datos)
ACCENT_600 = "#176B5A"
SPOT = "#E8772E"       # --spot (uso puntual)
COURT = "#1B140D"      # --court (cabeceras de tabla oscuras)
COURT_INK = "#EFE8DA"  # --court-ink

# FODA: teal = funciona · spot = potencial · rojo (único hex fuera de tokens,
# candidato a --negative en global.css) = debilidades · ink-soft = contexto.
FODA_FORTALEZAS = ACCENT
FODA_OPORTUNIDADES = SPOT
FODA_DEBILIDADES = "#A8442F"
FODA_AMENAZAS = INK_SOFT

FONT_BODY = "Georgia,'Times New Roman',serif"

LOGO_URL = "https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"


# ─── Prompt de análisis (solo JSON) ───────────────────────────────────────────

_SYSTEM = (
    "Eres un analista profesional de baloncesto europeo especializado en "
    "estadística avanzada y scouting de la Liga Endesa (ACB). Escribes para "
    "Basketmática: registro sobrio y técnico, sin épica. Respondes SIEMPRE "
    "con un único objeto JSON válido, sin Markdown."
)


def _prompt_analisis(player_data: Dict[str, Any]) -> str:
    return f"""A partir de este JSON con datos REALES de un jugador de la Liga Endesa (ACB), extraídos de acb.com, redacta el análisis en ESPAÑOL.

=== DATOS ===
{json.dumps(player_data, ensure_ascii=False)}
=============

CONTEXTO DE MÉTRICAS (para interpretar, NO para inventar valores):
- "Valoración" es el índice oficial europeo (PIR), LA métrica de referencia en ACB.
- "avanzadas" son estadísticas avanzadas OFICIALES de acb.com: Cuatro Factores (eFG%, ORB%, TOV%, FTr), manejo de balón (AST%, STL%, BLK%, TOV%), lanzamiento (TS%, eFG%, 3PAr, PPT), puntos por 100 posesiones y ritmo.
- "per40" es la normalización europea a 40 minutos, calculada sobre promedios reales.
- "_rankings_liga" son los puestos del jugador en los rankings oficiales de la Liga Endesa: úsalos, dan mucho contexto.

Devuelve EXACTAMENTE este esquema JSON (sin campos extra, sin Markdown):
{{
  "resumen_desempeno": "Párrafo de 110-160 palabras. Compara temporada vs carrera si ambas existen. Apóyate en Valoración, TS%/eFG% oficiales, Cuatro Factores y per-40. Cita números concretos del JSON.",
  "foda": {{
    "fortalezas": ["2-3 puntos, máx. 25 palabras cada uno"],
    "oportunidades": ["2-3 puntos"],
    "debilidades": ["2-3 puntos"],
    "amenazas": ["2-3 puntos"]
  }},
  "proyeccion": "Párrafo de 80-100 palabras sobre rol y sostenibilidad, coherente con la edad (fecha de nacimiento) y los datos.",
  "similares": [
    {{"nombre": "Jugador de perfil ESTADÍSTICO comparable, prioriza trayectoria ACB/Europa", "razon": "justificación técnica de una línea; preséntalo como perfil comparable, no equivalencia de nivel"}},
    {{"nombre": "...", "razon": "..."}}
  ]
}}

Reglas: básate al 100% en los datos del JSON; no inventes lesiones, contratos ni contexto de mercado; si un dato es "–", ignóralo."""


# ─── Render HTML determinista ─────────────────────────────────────────────────


def _e(v: Any) -> str:
    s = str(v if v is not None else "—").strip()
    return html.escape("—" if s in ("", "–") else s)


def _tabla(
    titulo: str, filas: List[List[str]], cabecera: Optional[List[str]] = None
) -> str:
    if not filas:
        return ""
    th = ""
    if cabecera:
        celdas = "".join(
            f'<th style="background-color:{COURT};color:{COURT_INK};padding:9px;'
            f'text-align:center;font-size:12.5px;letter-spacing:1px;">{_e(c)}</th>'
            for c in cabecera
        )
        th = f"<tr>{celdas}</tr>"
    trs = ""
    for fila in filas:
        tds = "".join(
            f'<td style="padding:8px;border-bottom:1px solid {LINE};'
            f'text-align:center;font-size:13px;color:{INK};">{_e(c)}</td>'
            for c in fila
        )
        trs += f"<tr>{tds}</tr>"
    t = (
        f'<h3 style="color:{INK};font-size:14.5px;margin:18px 0 8px;'
        f'font-family:{FONT_BODY};">{_e(titulo)}</h3>'
        if titulo
        else ""
    )
    return t + f'<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{th}{trs}</table>'


def _h2(texto: str) -> str:
    return (
        f'<h2 style="color:{BRAND};border-bottom:2px solid {ACCENT};'
        f'padding-bottom:8px;font-size:19px;margin-top:28px;'
        f'font-family:{FONT_BODY};">{_e(texto)}</h2>'
    )


# Orden y etiquetas de los promedios ACB.
_ORDEN_STATS: List[Tuple[str, str]] = [
    ("Partidos", "PJ"), ("Minutos", "MIN"), ("Puntos", "PTS"),
    ("%2P", "%2P"), ("%3P", "%3P"), ("%TL", "%TL"),
    ("Rebotes", "REB"), ("Asistencias", "AST"),
    ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Valoración", "VAL"),
]

_ORDEN_P40: List[Tuple[str, str]] = [
    ("Puntos", "PTS"), ("Rebotes", "REB"), ("Asistencias", "AST"),
    ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Valoración", "VAL"),
]

# Nombres visibles de los rankings de liga.
_NOMBRE_RANK = {
    "Puntos": "puntos", "Rebotes": "rebotes", "Asistencias": "asistencias",
    "Recuperaciones": "recuperaciones", "Tapones": "tapones",
    "Valoración": "valoración", "Minutos": "minutos",
    "%2P": "%2P", "%3P": "%3P", "%TL": "%TL", "Partidos": "partidos",
}


def _fila_stats(stats: Dict[str, Any], orden: List[Tuple[str, str]]):
    presentes = [(k, et) for k, et in orden if stats.get(k) not in (None, "", "–")]
    cab = [et for _, et in presentes]
    fila = [str(stats.get(k)) for k, _ in presentes]
    return cab, fila


def _linea_rankings(stats: Dict[str, Any]) -> str:
    ranks = stats.get("_rankings_liga") or {}
    if not isinstance(ranks, dict) or not ranks:
        return ""
    partes = []
    for k, v in ranks.items():
        num = str(v).replace("#", "").strip().split()[0] if v else ""
        if num:
            partes.append(f"#{num} en {_NOMBRE_RANK.get(k, k)}")
    if not partes:
        return ""
    return (
        f'<p style="font-size:12.5px;color:{ACCENT_600};margin:-10px 0 16px;">'
        f"Top de liga: {_e(' · '.join(partes[:6]))}</p>"
    )


def _tabla_avanzadas(avanz: Dict[str, Any]) -> str:
    """Aplana los bloques oficiales de acb.com en una tabla métrica→valor."""
    filas: List[List[str]] = []
    for bloque in ("Cuatro Factores", "Lanzamiento", "Manejo de Balón", "Puntos", "Rebotes"):
        contenido = avanz.get(bloque)
        if isinstance(contenido, dict):
            for metrica, valor in contenido.items():
                filas.append([f"{metrica} ({bloque})", str(valor)])
    if avanz.get("PTS_100_posesiones"):
        filas.append(["Puntos por 100 posesiones", str(avanz["PTS_100_posesiones"])])
    if avanz.get("Posesiones_40min"):
        filas.append(["Posesiones por 40'", str(avanz["Posesiones_40min"])])
    if not filas:
        return ""
    return _tabla(
        "Estadísticas avanzadas (oficiales acb.com)", filas, ["Métrica", "Valor"]
    )


def _seccion_foda(foda: Dict[str, List[str]]) -> str:
    bloques = [
        ("Fortalezas", FODA_FORTALEZAS, foda.get("fortalezas") or []),
        ("Oportunidades", FODA_OPORTUNIDADES, foda.get("oportunidades") or []),
        ("Debilidades", FODA_DEBILIDADES, foda.get("debilidades") or []),
        ("Amenazas", FODA_AMENAZAS, foda.get("amenazas") or []),
    ]
    secciones = ""
    for titulo, color, puntos in bloques:
        lis = "".join(f"<li>{_e(p)}</li>" for p in puntos) or "<li>—</li>"
        secciones += (
            f'<section style="background-color:{SURFACE};border:1px solid {LINE};'
            f'border-left:4px solid {color};padding:14px 16px;border-radius:6px;">'
            f'<h3 style="color:{color};margin:0 0 8px;font-size:13.5px;'
            f'text-transform:uppercase;letter-spacing:1px;">{titulo}</h3>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;">{lis}</ul>'
            f"</section>"
        )
    return (
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;'
        f'margin-bottom:24px;">{secciones}</div>'
    )


def render_html(player_data: Dict[str, Any], analisis: Dict[str, Any]) -> str:
    bio = player_data.get("Datos personales", {})
    est = player_data.get("Estadísticas", {})
    temporada_label = est.get("temporada_label", "")

    # ── Cabecera ──
    foto = bio.get("Foto") or ""
    img = (
        f'<img src="{html.escape(foto)}" alt="" '
        f'style="max-width:150px;border-radius:8px;"/>' if foto else ""
    )
    cabecera = f"""
    <div style="display:flex;align-items:center;gap:24px;border-bottom:3px solid {ACCENT};
                padding-bottom:20px;margin-bottom:26px;">
      {img}
      <div>
        <h1 style="color:{BRAND};margin:0 0 8px;font-size:28px;letter-spacing:.5px;
                   font-family:{FONT_BODY};">{_e(bio.get("Nombre"))}</h1>
        <p style="margin:0;font-size:16px;color:{INK_SOFT};">
          {_e(bio.get("Posición"))} · {_e(bio.get("Equipo"))} · Dorsal {_e(bio.get("Dorsal"))}
        </p>
        <p style="margin:6px 0 0;font-size:11px;color:{ACCENT_600};
                  text-transform:uppercase;letter-spacing:2px;">
          Informe de scouting · Liga Endesa{f" · Temporada {_e(temporada_label)}" if temporada_label else ""}
        </p>
      </div>
    </div>"""

    # ── Perfil ──
    campos_bio = [
        "Equipo", "Posición", "Altura", "Fecha nacimiento", "Lugar nacimiento",
        "Nacionalidad", "Licencia", "Dorsal",
    ]
    filas_bio = [
        [c, str(bio.get(c))] for c in campos_bio if bio.get(c) not in (None, "", "–")
    ]
    perfil = _h2("Perfil del jugador") + _tabla("", filas_bio)

    # ── Métricas ──
    stats_html = _h2("Métricas de rendimiento")
    temp = est.get("temporada") or {}
    if temp:
        cab, fila = _fila_stats(temp, _ORDEN_STATS)
        etiqueta = f"Promedios {temporada_label}" if temporada_label else "Promedios de la temporada"
        stats_html += _tabla(etiqueta, [fila], cab)
        stats_html += _linea_rankings(temp)
        p40 = temp.get("per40") or {}
        if p40:
            cab, fila = _fila_stats(p40, _ORDEN_P40)
            stats_html += _tabla("Per-40 minutos (calculado)", [fila], cab)

    carrera = est.get("carrera") or {}
    if carrera:
        cab, fila = _fila_stats(carrera, _ORDEN_STATS)
        stats_html += _tabla("Promedios de carrera en ACB", [fila], cab)

    avanz = est.get("avanzadas") or {}
    if avanz:
        stats_html += _tabla_avanzadas(avanz)

    records = est.get("records") or {}
    if records:
        filas = [
            [met, str(d.get("valor", "—")), str(d.get("partido", "—"))]
            for met, d in records.items()
            if isinstance(d, dict)
        ]
        stats_html += _tabla(
            "Récords en un partido", filas, ["Métrica", "Valor", "Partido"]
        )

    # ── Análisis del LLM ──
    analisis_html = (
        _h2("Análisis de desempeño")
        + f'<p style="font-size:13.5px;line-height:1.6;">{_e(analisis.get("resumen_desempeno"))}</p>'
        + _h2("Análisis FODA")
        + _seccion_foda(analisis.get("foda") or {})
        + _h2("Proyección")
        + f'<p style="font-size:13.5px;line-height:1.6;">{_e(analisis.get("proyeccion"))}</p>'
        + _h2("Perfiles similares")
        + "<ul style='font-size:13.5px;line-height:1.7;'>"
        + "".join(
            f"<li><strong>{_e(s.get('nombre'))}</strong>: {_e(s.get('razon'))}</li>"
            for s in (analisis.get("similares") or [])
            if isinstance(s, dict)
        )
        + "</ul>"
    )

    modelo = _e(analisis.get("_modelo", ""))
    pie = (
        f'<p style="margin-top:32px;padding-top:12px;border-top:1px solid {ACCENT};'
        f'font-size:10.5px;color:{INK_SOFT};text-transform:uppercase;letter-spacing:2px;">'
        f"Basketmática · basketmatica.com · Datos: acb.com · Análisis: {modelo}</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"></head>
<body style="background-color:{BG};margin:0;
             font-family:{FONT_BODY};line-height:1.55;color:{INK};">
  <div style="max-width:850px;margin:0 auto;padding:40px;background-color:{BG};position:relative;">
    <img src="{LOGO_URL}" alt="Basketmática"
         style="position:absolute;top:40px;right:40px;width:90px;opacity:.85;"/>
    {cabecera}
    {perfil}
    {stats_html}
    {analisis_html}
    {pie}
  </div>
</body></html>"""


# ─── Pipeline principal ───────────────────────────────────────────────────────


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

    analisis = generar_json(
        _prompt_analisis(player_data),
        proveedores,
        system=_SYSTEM,
        max_tokens=2500,
    )

    html_doc = render_html(player_data, analisis)
    pdf: bytes = HTML(string=html_doc).write_pdf()
    logger.info("✓ PDF generado (%d KB).", len(pdf) // 1024)
    return pdf
