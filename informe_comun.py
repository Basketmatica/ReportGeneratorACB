"""
informe_comun.py — Núcleo común de los generadores de informes de Basketmática.

Este archivo es IDÉNTICO en ReportGeneratorACB y ReportGeneratorNBA. Tras
editarlo en uno, cópialo al otro y comprueba:

    diff ../ReportGeneratorACB/informe_comun.py ../ReportGeneratorNBA/informe_comun.py

Cada generador aporta solo lo propio de su fuente:

  * un `Competicion` (textos, columnas y contexto de métricas);
  * `construir_vista(player_data)`: los datos de su fuente con este esquema

        {
          "jugador":   {"Nombre", "Posición", "Equipo", "Dorsal", …perfil},
          "alias":     ["nombre legal", …],          # solo para filtrar similares
          "foto":      "https://…",
          "temporada": {"etiqueta", "promedios", "rankings_liga",
                        "per{N}_calculado", "ratios"},
          "carrera":   {"promedios", "per{N}_calculado"},
          "avanzadas_oficiales": {métrica: valor},
          "trayectoria": [{columna: valor}, …],       # más reciente primero
          "records":   {métrica: {"valor", "partido"}},
          "nota_temporada": "…",
        }

Todo lo demás es común. En particular, el LLM recibe exactamente los datos que
pinta el PDF: nunca puede citar una cifra que el lector no encuentre en una tabla.
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from weasyprint import HTML

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

# El razonamiento de gpt-oss-120b cuenta como salida y varía mucho entre llamadas
# (medido: 1.500-4.200 tokens). Un límite justo corta el JSON; Groq no penaliza
# pedir de más, solo cuenta lo que se genera.
MAX_TOKENS_ANALISIS = 8000

Columnas = List[Tuple[str, str]]  # (clave en la vista, etiqueta de la tabla)

_HUECO = object()  # celda de relleno: se pinta vacía, no como "—"


@dataclass(frozen=True)
class Competicion:
    nombre: str                  # "Liga Endesa" · "NBA"
    fuente: str                  # pie del PDF
    ambito_similares: str        # "la ACB y Europa" · "la NBA"
    minutos_normalizacion: int   # 40 (FIBA) · 36 (NBA)
    pj_minimo: int               # por debajo, la temporada no cuenta para tendencias
    metricas_clave: str          # en qué se apoya el resumen
    contexto_metricas: str       # viñetas del prompt propias de la fuente
    col_promedios: Columnas
    col_normalizado: Columnas
    col_trayectoria: Columnas
    titulo_avanzadas: str
    leyenda_avanzadas: str
    leyenda_ratios: str
    max_trayectoria: int = 12

    @property
    def clave_normalizado(self) -> str:
        return f"per{self.minutos_normalizacion}_calculado"


# ─── Formato español ──────────────────────────────────────────────────────────

_VACIOS = (None, "", "–", "-", "—")
_DECIMAL_PUNTO = re.compile(r"(?<=\d)\.(?=\d)")
_PORCENTAJE = re.compile(r"(?<=\d)\s*%")


def formato_es(texto: str) -> str:
    """'47.3%' → '47,3 %'. Coma decimal y porcentaje con espacio, como acb.com."""
    return _PORCENTAJE.sub(" %", _DECIMAL_PUNTO.sub(",", texto))


def _mapear_textos(obj: Any, fn: Callable[[str], str], excluir: Tuple[str, ...] = ()) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_mapear_textos(x, fn, excluir) for x in obj]
    if isinstance(obj, dict):
        return {
            k: v if (k in excluir or str(k).startswith("_")) else _mapear_textos(v, fn, excluir)
            for k, v in obj.items()
        }
    return obj


def _sin_vacios(obj: Any) -> Any:
    if isinstance(obj, dict):
        limpio = {k: _sin_vacios(v) for k, v in obj.items()}
        return {k: v for k, v in limpio.items() if v not in _VACIOS and v != {} and v != []}
    if isinstance(obj, list):
        return [x for x in (_sin_vacios(v) for v in obj) if x not in _VACIOS and x != {}]
    if isinstance(obj, (int, float)):
        return str(obj)
    return obj


def _columnas(stats: Optional[Dict[str, Any]], cols: Columnas) -> Dict[str, Any]:
    stats = stats or {}
    return {k: stats[k] for k, _ in cols if stats.get(k) not in _VACIOS}


def preparar_vista(vista: Dict[str, Any], comp: Competicion) -> Dict[str, Any]:
    """Recorta la vista a lo que pinta el PDF y la pasa a formato español."""
    v = json.loads(json.dumps(vista, ensure_ascii=False))
    norm = comp.clave_normalizado
    for bloque in ("temporada", "carrera"):
        b = v.get(bloque) or {}
        b["promedios"] = _columnas(b.get("promedios"), comp.col_promedios)
        b[norm] = _columnas(b.get(norm), comp.col_normalizado)
    tray = v.get("trayectoria") or []
    v["trayectoria"] = [_columnas(t, comp.col_trayectoria) for t in tray[: comp.max_trayectoria]]
    if len(tray) > comp.max_trayectoria:
        v["trayectoria_temporadas_totales"] = str(len(tray))
    return _mapear_textos(_sin_vacios(v), formato_es, excluir=("foto",))


# ─── Texto de la IA: normalización determinista ───────────────────────────────

# Latinoamericanismos frecuentes en los modelos → baloncesto en España.
_TERMINOS: List[Tuple[str, str]] = [
    (r"desde la banca", "desde el banquillo"),
    (r"de la banca", "del banquillo"),
    (r"a la banca", "al banquillo"),
    (r"la banca", "el banquillo"),
    (r"banca", "banquillo"),
    (r"guardia armador", "base"),
    (r"guardia tirador", "escolta"),
    (r"base armador", "base"),
    (r"guardias", "exteriores"),
    (r"guardia", "exterior"),
    (r"canchas", "pistas"),
    (r"cancha", "pista"),
    (r"las clavadas", "los mates"),
    (r"la clavada", "el mate"),
    (r"una clavada", "un mate"),
    (r"clavadas", "mates"),
    (r"clavada", "mate"),
    (r"por juego", "por partido"),
    (r"un centro", "un pívot"),
    (r"centro con", "pívot con"),
]
_TERMINOS_RE = [(re.compile(rf"\b{p}\b", re.IGNORECASE), r) for p, r in _TERMINOS]


def _reemplazo(destino: str) -> Callable[[re.Match], str]:
    def fn(m: re.Match) -> str:
        return destino[0].upper() + destino[1:] if m.group(0)[0].isupper() else destino
    return fn


def normalizar_texto_ia(texto: str) -> str:
    texto = formato_es(texto)
    for patron, destino in _TERMINOS_RE:
        texto = patron.sub(_reemplazo(destino), texto)
    return texto


def _tokens_nombre(nombre: str) -> set:
    s = unicodedata.normalize("NFKD", re.sub(r"\(.*?\)", "", nombre or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return set(re.sub(r"[^a-z]+", " ", s).split())


def filtrar_similares(similares: Any, nombres_jugador: List[str]) -> List[Dict[str, str]]:
    """Quita al propio jugador ('Facundo Campazzo (versión 2015-16)', 'Walter Tavares')."""
    propios = [_tokens_nombre(n) for n in nombres_jugador if n]
    out = []
    for s in similares if isinstance(similares, list) else []:
        if not isinstance(s, dict):
            continue
        t = _tokens_nombre(str(s.get("nombre", "")))
        if t and any(t <= p or len(t & p) >= 2 for p in propios):
            logger.info("Similar descartado por ser el propio jugador: %s", s.get("nombre"))
            continue
        out.append(s)
    return out


# ─── Prompt ───────────────────────────────────────────────────────────────────


def _system(comp: Competicion) -> str:
    return (
        "Eres un analista profesional de baloncesto especializado en estadística "
        f"avanzada y scouting de la {comp.nombre}. Escribes en español de España "
        "para Basketmática: registro sobrio y técnico, sin épica. Respondes SIEMPRE "
        "con un único objeto JSON válido, sin Markdown."
    )


def construir_prompt(vista: Dict[str, Any], comp: Competicion) -> str:
    datos = {k: v for k, v in vista.items() if k not in ("foto", "alias")}
    m = comp.minutos_normalizacion
    norm = comp.clave_normalizado
    return f"""A partir de este JSON con datos REALES de un jugador de la {comp.nombre}, redacta el análisis en español de España. El JSON contiene exactamente las cifras que aparecen en las tablas del informe.

=== DATOS ===
{json.dumps(datos, ensure_ascii=False)}
=============

CONTEXTO DE MÉTRICAS (para interpretar, NO para inventar valores):
- "temporada" es la temporada de referencia del informe (ver "etiqueta"); "carrera" son sus promedios de toda la carrera en la {comp.nombre}.
- "{norm}" es la normalización a {m} minutos (valor por partido × {m} ÷ minutos por partido), CALCULADA por Basketmática a partir de datos oficiales.
- En "ratios", las métricas marcadas "(calculado)" las calcula Basketmática; el resto son datos oficiales.
{comp.contexto_metricas}
- Definiciones (las mismas que la leyenda del informe): {comp.leyenda_avanzadas} {comp.leyenda_ratios}

Devuelve EXACTAMENTE este esquema JSON (sin campos extra, sin Markdown):
{{
  "resumen_desempeno": "Párrafo de 110-160 palabras. Compara temporada vs carrera si ambas existen. Apóyate en {comp.metricas_clave}. Cita números concretos del JSON y usa la trayectoria para señalar la tendencia (mejora, meseta o declive) con temporadas concretas.",
  "foda": {{
    "fortalezas": ["2-3 puntos, máx. 25 palabras cada uno"],
    "oportunidades": ["2-3 puntos"],
    "debilidades": ["2-3 puntos"],
    "amenazas": ["2-3 puntos"]
  }},
  "proyeccion": "Párrafo de 80-100 palabras sobre rol y sostenibilidad, coherente con la edad y los datos. Si la edad no consta en el JSON, no la menciones.",
  "similares": [
    {{"nombre": "Jugador de perfil ESTADÍSTICO comparable (NUNCA el propio jugador ni una versión anterior de él), prioriza trayectoria en {comp.ambito_similares}", "razon": "justificación técnica de una línea basada en el perfil del jugador analizado, sin cifras del jugador comparado (no están en el JSON); preséntalo como perfil comparable, no equivalencia de nivel"}},
    {{"nombre": "...", "razon": "..."}}
  ]
}}

REGLAS DE RIGOR (obligatorias, prevalecen sobre todo lo demás):
1. Compara SOLO pares de valores que estén AMBOS en el JSON y sean la MISMA métrica (TS% con TS%, nunca TS% con %TL). Si el homólogo de carrera de una métrica no existe, NO compares: describe el valor en solitario. Prohibido citar cualquier número que no aparezca literalmente en el JSON.
2. Direccionalidad: TOV%, Pérdidas y "Pérdidas/partido" significan mejor cuanto MÁS BAJOS. TS%, eFG%, AST%, %TC, %2P, %3P, %TL, AST/BP y AST/TO significan mejor cuanto más altos. Un TOV% bajo (<13) en un exterior con AST% alto es seguridad de balón de élite: FORTALEZA, jamás debilidad.
3. Ancla todo juicio de nivel ("élite", "top", "pobre") en "rankings_liga" si existe; sin ranking que lo respalde, describe el dato sin calificarlo.
4. Temporadas con PJ < {comp.pj_minimo} son muestra no significativa: exclúyelas de tendencias y no cites sus porcentajes.
5. Prohibido mencionar defensa, lesiones, contratos, vestuario o minutos futuros si el JSON no contiene un dato que lo respalde, y prohibidas las predicciones numéricas (puntos, minutos o porcentajes futuros). Cada punto del FODA debe citar al menos un número del JSON.
6. Estilo de juego: no atribuyas rasgos (juego sin balón, tiro tras bote, bloqueo y continuación, liderazgo, físico…) que no se deduzcan directamente de un número del JSON, y sé coherente con la posición: un base con asistencias o AST% altos es un generador con balón, nunca un jugador "sin balón".
7. En la tendencia de la trayectoria: di meseta, descenso o mejora según los números reales, no la narrativa amable. Un pico anterior seguido de valores menores es meseta o leve descenso, no "mejora".
8. Si existe "nota_temporada", el jugador no tiene partidos en la temporada más reciente: no lo interpretes como rendimiento ni especules con la causa.
9. Métricas calculadas: cada vez que cites un valor de "{norm}" o de un ratio "(calculado)", indícalo en la misma frase, p. ej. "16,6 puntos por {m} minutos (calculado)".

ESTILO (obligatorio):
- Decimales con coma y porcentajes con espacio: "11,2", "57,8 %". Nunca punto decimal.
- Posiciones: base, escolta, alero, ala-pívot, pívot; "exterior" e "interior" para agrupar. Nunca "guardia", "armador", "delantero" ni "centro".
- Vocabulario: banquillo (no banca), pista (no cancha), mate (no clavada), tapón, recuperación, pérdida, triple, partido (no juego), "por partido".
- %TL es el porcentaje de acierto en tiros libres; FTr es tiros libres intentados por tiro de campo. No llames "tasa" al %TL."""


def generar_analisis(
    vista: Dict[str, Any], comp: Competicion, proveedores: List[ProviderConfig]
) -> Dict[str, Any]:
    analisis = generar_json(
        construir_prompt(vista, comp),
        proveedores,
        system=_system(comp),
        max_tokens=MAX_TOKENS_ANALISIS,
    )
    analisis = _mapear_textos(analisis, normalizar_texto_ia)
    nombres = [(vista.get("jugador") or {}).get("Nombre", "")] + list(vista.get("alias") or [])
    analisis["similares"] = filtrar_similares(analisis.get("similares"), nombres)
    return analisis


# ─── Render HTML determinista ─────────────────────────────────────────────────

_NOMBRE_RANK = {
    "Puntos": "puntos", "Rebotes": "rebotes", "Asistencias": "asistencias",
    "Recuperaciones": "recuperaciones", "Tapones": "tapones",
    "Valoración": "valoración", "Minutos": "minutos", "Partidos": "partidos",
}


def _e(v: Any) -> str:
    s = str(v if v is not None else "—").strip()
    return html.escape("—" if s in ("", "–") else s)


def _leyenda(texto: str) -> str:
    if not texto:
        return ""
    return (
        f'<p style="font-size:10.5px;color:{INK_SOFT};margin:-14px 0 20px;'
        f'line-height:1.5;">{_e(texto)}</p>'
    )


def tabla(
    titulo: str,
    filas: List[List[Any]],
    cabecera: Optional[List[str]] = None,
    leyenda: str = "",
    extra: str = "",
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
        th = f"<thead><tr>{celdas}</tr></thead>"
    trs = ""
    for fila in filas:
        tds = "".join(
            f'<td style="padding:8px;border-bottom:1px solid {LINE};'
            f'text-align:center;font-size:13px;color:{INK};">{"" if c is _HUECO else _e(c)}</td>'
            for c in fila
        )
        trs += f"<tr>{tds}</tr>"
    t = (
        f'<h3 style="color:{INK};font-size:14.5px;margin:18px 0 8px;'
        f'font-family:{FONT_BODY};">{_e(titulo)}</h3>'
        if titulo
        else ""
    )
    # Las tablas cortas no se parten entre páginas; las largas sí (la cabecera se repite).
    clase = "bloque" if len(filas) <= 10 else "bloque-largo"
    return (
        f'<div class="{clase}">{t}'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{th}<tbody>{trs}</tbody></table>'
        f"{extra}{_leyenda(leyenda)}</div>"
    )


def h2(texto: str) -> str:
    return (
        f'<h2 style="color:{BRAND};border-bottom:2px solid {ACCENT};'
        f'padding-bottom:8px;font-size:19px;margin-top:28px;'
        f'font-family:{FONT_BODY};">{_e(texto)}</h2>'
    )


def _en_dos_columnas(filas: List[List[Any]]) -> List[List[Any]]:
    mitad = (len(filas) + 1) // 2
    return [a + (filas[mitad + i] if mitad + i < len(filas) else [_HUECO, _HUECO]) for i, a in enumerate(filas[:mitad])]


def _fila_stats(stats: Dict[str, Any], cols: Columnas) -> Tuple[List[str], List[str]]:
    presentes = [(k, et) for k, et in cols if stats.get(k) not in _VACIOS]
    return [et for _, et in presentes], [str(stats[k]) for k, _ in presentes]


def _linea_rankings(ranks: Dict[str, Any]) -> str:
    partes = []
    for k, v in (ranks or {}).items():
        num = str(v).replace("#", "").strip().split()[0] if v else ""
        if num:
            partes.append(f"#{num} en {_NOMBRE_RANK.get(k, k)}")
    if not partes:
        return ""
    return (
        f'<p style="font-size:12.5px;color:{ACCENT_600};margin:-10px 0 16px;">'
        f"Top de liga: {_e(' · '.join(partes[:6]))}</p>"
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


def _parrafo(texto: Any) -> str:
    return f'<p style="font-size:13.5px;line-height:1.6;">{_e(texto)}</p>'


def render_html(vista: Dict[str, Any], analisis: Dict[str, Any], comp: Competicion) -> str:
    jugador = vista.get("jugador") or {}
    temp = vista.get("temporada") or {}
    carrera = vista.get("carrera") or {}
    etiqueta = temp.get("etiqueta", "")
    norm = comp.clave_normalizado
    m = comp.minutos_normalizacion

    # ── Cabecera ──
    foto = vista.get("foto") or ""
    img = (
        f'<img src="{html.escape(foto)}" alt="" style="max-width:150px;border-radius:8px;"/>'
        if foto else ""
    )
    linea = " · ".join(
        _e(x) for x in (
            jugador.get("Posición"), jugador.get("Equipo"),
            f"Dorsal {jugador['Dorsal']}" if jugador.get("Dorsal") else None,
        ) if x
    )
    cabecera = f"""
    <div style="display:flex;align-items:center;gap:24px;border-bottom:3px solid {ACCENT};
                padding-bottom:20px;margin-bottom:26px;padding-right:110px;">
      {img}
      <div>
        <h1 style="color:{BRAND};margin:0 0 8px;font-size:28px;letter-spacing:.5px;
                   font-family:{FONT_BODY};">{_e(jugador.get("Nombre"))}</h1>
        <p style="margin:0;font-size:16px;color:{INK_SOFT};">{linea}</p>
        <p style="margin:6px 0 0;font-size:11px;color:{ACCENT_600};
                  text-transform:uppercase;letter-spacing:2px;">
          Informe de scouting · {_e(comp.nombre)}{f" · Temporada {_e(etiqueta)}" if etiqueta else ""}
        </p>
      </div>
    </div>"""

    # ── Perfil ──
    perfil = h2("Perfil del jugador") + tabla(
        "", [[k, v] for k, v in jugador.items() if k != "Nombre"]
    )

    # ── Métricas ──
    stats_html = h2("Métricas de rendimiento")
    if temp.get("promedios"):
        cab, fila = _fila_stats(temp["promedios"], comp.col_promedios)
        stats_html += tabla(
            f"Promedios por partido · {etiqueta}" if etiqueta else "Promedios por partido",
            [fila], cab, extra=_linea_rankings(temp.get("rankings_liga") or {}),
        )

    periodos = [
        (f"Temporada {etiqueta}" if etiqueta else "Temporada", temp.get(norm) or {}),
        ("Carrera", carrera.get(norm) or {}),
    ]
    cols_norm = [(k, et) for k, et in comp.col_normalizado if any(p.get(k) for _, p in periodos)]
    filas_norm = [[nombre] + [p.get(k, "—") for k, _ in cols_norm] for nombre, p in periodos if p]
    if cols_norm and filas_norm:
        stats_html += tabla(
            f"Per-{m} minutos (calculado)", filas_norm, ["Periodo"] + [et for _, et in cols_norm],
            leyenda=f"Calculado por Basketmática: valor por partido × {m} ÷ minutos por partido.",
        )

    ratios = temp.get("ratios") or {}
    if ratios:
        stats_html += tabla(
            f"Ratios y volumen · {etiqueta}" if etiqueta else "Ratios y volumen",
            [[k, v] for k, v in ratios.items()], ["Métrica", "Valor"],
            leyenda=comp.leyenda_ratios,
        )

    if carrera.get("promedios"):
        cab, fila = _fila_stats(carrera["promedios"], comp.col_promedios)
        stats_html += tabla(f"Promedios de carrera en la {comp.nombre}", [fila], cab)

    avanz = vista.get("avanzadas_oficiales") or {}
    if avanz:
        filas = [[k, v] for k, v in avanz.items()]
        cab = ["Métrica", "Valor"]
        if len(filas) > 6:
            filas, cab = _en_dos_columnas(filas), cab * 2
        stats_html += tabla(comp.titulo_avanzadas, filas, cab, leyenda=comp.leyenda_avanzadas)

    trayectoria = vista.get("trayectoria") or []
    if len(trayectoria) >= 2:
        cols = [(k, et) for k, et in comp.col_trayectoria if any(t.get(k) for t in trayectoria)]
        titulo = "Trayectoria temporada a temporada"
        total = vista.get("trayectoria_temporadas_totales")
        if total:
            titulo += f" (últimas {len(trayectoria)} de {total})"
        stats_html += tabla(
            titulo, [[t.get(k, "—") for k, _ in cols] for t in trayectoria],
            [et for _, et in cols],
        )

    records = vista.get("records") or {}
    if records:
        stats_html += tabla(
            "Récords en un partido",
            [[met, d.get("valor", "—"), d.get("partido", "—")]
             for met, d in records.items() if isinstance(d, dict)],
            ["Métrica", "Valor", "Partido"],
        )

    # ── Análisis del LLM ──
    analisis_html = (
        h2("Análisis de desempeño")
        + _parrafo(analisis.get("resumen_desempeno"))
        + h2("Análisis FODA")
        + _seccion_foda(analisis.get("foda") or {})
        + h2("Proyección")
        + _parrafo(analisis.get("proyeccion"))
    )
    similares = analisis.get("similares") or []
    cierre = ""
    if similares:
        cierre = (
            h2("Perfiles similares")
            + "<ul style='font-size:13.5px;line-height:1.7;'>"
            + "".join(
                f"<li><strong>{_e(s.get('nombre'))}</strong>: {_e(s.get('razon'))}</li>"
                for s in similares
            )
            + "</ul>"
        )

    pie = (
        f'<p style="margin-top:32px;padding-top:12px;border-top:1px solid {ACCENT};'
        f'font-size:10.5px;color:{INK_SOFT};text-transform:uppercase;letter-spacing:2px;">'
        f"Basketmática · basketmatica.com · Datos: {_e(comp.fuente)} · "
        f"Análisis: {_e(analisis.get('_modelo', ''))}</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 16mm 0; background: {BG}; }}
  h2, h3, thead {{ break-after: avoid; }}
  .bloque, section, li {{ break-inside: avoid; }}
  p {{ orphans: 3; widows: 3; }}
</style></head>
<body style="background-color:{BG};margin:0;
             font-family:{FONT_BODY};line-height:1.55;color:{INK};">
  <div style="margin:0 auto;padding:0 40px;background-color:{BG};position:relative;">
    <img src="{LOGO_URL}" alt="Basketmática"
         style="position:absolute;top:0;right:40px;width:90px;opacity:.85;"/>
    {cabecera}
    {perfil}
    {stats_html}
    {analisis_html}
    <div class="bloque">{cierre}{pie}</div>
  </div>
</body></html>"""


# ─── Pipeline ─────────────────────────────────────────────────────────────────


def generar_pdf(
    vista: Dict[str, Any], comp: Competicion, proveedores: List[ProviderConfig]
) -> bytes:
    vista = preparar_vista(vista, comp)
    analisis = generar_analisis(vista, comp, proveedores)
    pdf: bytes = HTML(string=render_html(vista, analisis, comp)).write_pdf()
    logger.info("✓ PDF generado (%d KB).", len(pdf) // 1024)
    return pdf
