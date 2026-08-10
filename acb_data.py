"""
acb_data.py — Extracción de datos de jugadores desde acb.com (sitio nuevo, Next.js).

ESTRATEGIA (verificada contra el HTML servido por acb.com en agosto 2026):

  * Las fichas de jugador (/es/liga/jugadores/{slug}-{id}) se sirven RENDERIZADAS
    en el servidor (SSR): bio, promedios de temporada con ranking de liga,
    promedios de carrera, récords y — desde la 25/26 — ESTADÍSTICAS AVANZADAS
    OFICIALES (Cuatro Factores, TS%, eFG%, AST%, ritmo…) en la subpágina
    /estadisticas-avanzadas.
  * Las tablas grandes (estadisticas-de-jugador/detalle) se rellenan por JS,
    así que NO se usan para datos; solo para descubrir el editionId vigente.
  * La resolución nombre → URL se hace construyendo un índice con las
    plantillas de los 18 equipos (SSR también). Se cachea.

FILOSOFÍA DE PARSING:
  Los parsers se anclan a ETIQUETAS DE TEXTO visibles ("Posición", "Altura",
  "TEMPORADA", "CUATRO FACTORES"…), no a clases CSS de Next.js (que cambian
  con cada build). Es la opción más resistente a rediseños.

  Ningún dato se inventa: todo lo que aparece en el informe sale del HTML de
  acb.com. La única métrica DERIVADA es per-40 (normalización estándar europea,
  calculada a partir de minutos y promedios reales, y etiquetada como tal).
"""

from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

BASE = "https://www.acb.com"
URL_EQUIPOS = f"{BASE}/es/liga/equipos"
URL_LIDERES = f"{BASE}/es/liga/estadisticas/estadisticas-de-jugador"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

# Cortesía con acb.com: mínimo entre peticiones.
_MIN_INTERVAL_SEC = 0.5
_last_request = 0.0

# Patrón de enlace a ficha de jugador. Verificado:
#   /es/liga/jugadores/facundo-campazzo-20211331
_PLAYER_HREF_RE = re.compile(r"/es/liga/jugadores/([a-z0-9\-]+)-(\d{6,})")

# Patrón de enlace a ficha de equipo. Verificado:
#   /es/liga/equipos/real-madrid-9
_TEAM_HREF_RE = re.compile(r"/es/liga/equipos/([a-z0-9\-]+-\d+)$")

_EDITION_RE = re.compile(r"editionId=(\d+)")

# Valores tipo: "39", "23:06", "11,7", "55,6 %", "1,81 m"
_VALUE_RE = re.compile(r"^-?\d[\d.,:]*\s*(?:%|m)?$")
_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


# ─── HTTP ─────────────────────────────────────────────────────────────────────


def _throttle() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_request = time.monotonic()


def _get_html(url: str, retries: int = 3) -> str:
    """GET con reintentos exponenciales. Devuelve el HTML como str."""
    delay = 1.5
    last: Optional[str] = None
    for intento in range(retries):
        _throttle()
        try:
            r = httpx.get(
                url, headers=HEADERS, timeout=DEFAULT_TIMEOUT, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            last = f"red: {exc.__class__.__name__}"
            logger.warning("acb.com %s → %s (intento %d/%d)", url, last, intento + 1, retries)
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            raise ValueError(f"acb.com devolvió 404 para {url}")
        if r.status_code in (429, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"acb.com {url} → HTTP {r.status_code}")
    raise RuntimeError(f"acb.com no respondió tras {retries} intentos ({last}): {url}")


# ─── Utilidades de texto ──────────────────────────────────────────────────────


def normalizar(s: str) -> str:
    """minúsculas + sin tildes + espacios colapsados. Para matching de nombres."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower().strip())


def _lineas(soup: BeautifulSoup) -> List[str]:
    """Texto visible de la página como lista de líneas no vacías."""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    texto = soup.get_text("\n")
    return [ln.strip() for ln in texto.split("\n") if ln.strip()]


def _dedupe_consecutivo(lineas: List[str]) -> List[str]:
    """La web duplica etiquetas (span desktop + span móvil): 'Partidos','Partidos','39'."""
    out: List[str] = []
    for ln in lineas:
        if not out or out[-1] != ln:
            out.append(ln)
    return out


def _num(s: str) -> Optional[float]:
    """'55,6 %' → 55.6 · '23:06' → 23.1 (minutos decimales) · '11,7' → 11.7"""
    s = (s or "").strip()
    if ":" in s:
        try:
            mm, ss = s.split(":")
            return round(int(mm) + int(ss) / 60.0, 2)
        except ValueError:
            return None
    m = _NUM_RE.search(s.replace(".", "").replace(",", "."))
    # Ojo: '1.234,5' es raro en estas páginas; los valores son cortos.
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# ─── Índice de jugadores (nombre → URL) ───────────────────────────────────────


def descubrir_edition_id() -> Optional[int]:
    """
    La página de líderes SIN parámetro redirige/enlaza a la última edición CON
    datos (verificado: los enlaces internos llevan editionId=N). Devolvemos el
    editionId más frecuente encontrado en esos enlaces.
    """
    try:
        html = _get_html(URL_LIDERES)
    except Exception as exc:
        logger.warning("No se pudo descubrir editionId: %s", exc)
        return None
    ids = [int(x) for x in _EDITION_RE.findall(html)]
    if not ids:
        return None
    return max(set(ids), key=ids.count)


def _urls_equipos() -> List[str]:
    html = _get_html(URL_EQUIPOS)
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    vistos: set = set()
    for a in soup.find_all("a", href=True):
        m = _TEAM_HREF_RE.search(urlparse(a["href"]).path)
        if m and m.group(1) not in vistos:
            vistos.add(m.group(1))
            urls.append(f"{BASE}/es/liga/equipos/{m.group(1)}")
    return urls


def _jugadores_de_plantilla(team_url: str, edition_id: Optional[int]) -> List[Dict[str, str]]:
    """Enlaces a jugadores en la plantilla de un equipo (con fallback de edición)."""
    candidatos_url = [f"{team_url}/plantilla"]
    if edition_id:
        # En pretemporada la plantilla nueva está vacía → probamos la edición con datos.
        candidatos_url.append(f"{team_url}/plantilla?editionId={edition_id}")

    for url in candidatos_url:
        try:
            html = _get_html(url)
        except Exception as exc:
            logger.warning("Plantilla no accesible (%s): %s", url, exc)
            continue
        soup = BeautifulSoup(html, "html.parser")
        jugadores: Dict[str, Dict[str, str]] = {}
        for a in soup.find_all("a", href=True):
            m = _PLAYER_HREF_RE.search(a["href"])
            if not m:
                continue
            slug, pid = m.group(1), m.group(2)
            display = " ".join(a.get_text(" ").split()).strip()
            prev = jugadores.get(pid)
            if prev is None or (display and len(display) > len(prev.get("display", ""))):
                jugadores[pid] = {
                    "id": pid,
                    "slug": slug,
                    "display": display,
                    "url": f"{BASE}/es/liga/jugadores/{slug}-{pid}",
                }
        if jugadores:
            return list(jugadores.values())
    return []


def _urls_desde_sitemap_xml(xml: str) -> List[str]:
    """Extrae las URLs <loc> de un sitemap (o sub-sitemaps de un índice)."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def _jugadores_desde_sitemap() -> List[Dict[str, str]]:
    """
    Fuente independiente de la temporada: el sitemap oficial de jugadores
    (mismo patrón verificado que /sitemaps/entrenadores/sitemap.xml).
    Cubre TODOS los jugadores con ficha en acb.com, históricos incluidos.
    Sin nombre visible ni equipo (solo slug); se completan después.
    """
    indices_candidatos = [
        f"{BASE}/sitemaps/jugadores/sitemap.xml",
        "https://acb.com/sitemaps/jugadores/sitemap.xml",
    ]
    paginas: List[str] = []
    for idx_url in indices_candidatos:
        try:
            xml = _get_html(idx_url)
        except Exception as exc:
            logger.warning("Sitemap no accesible (%s): %s", idx_url, exc)
            continue
        locs = _urls_desde_sitemap_xml(xml)
        if not locs:
            continue
        # ¿Índice de sub-sitemaps o sitemap de URLs finales?
        if any(".xml" in u for u in locs):
            paginas = [u for u in locs if ".xml" in u]
        else:
            paginas = [idx_url]  # las URLs finales están aquí mismo
        break
    if not paginas:
        return []

    jugadores: Dict[str, Dict[str, str]] = {}
    for pag in paginas:
        try:
            xml = _get_html(pag)
        except Exception as exc:
            logger.warning("Página de sitemap no accesible (%s): %s", pag, exc)
            continue
        for loc in _urls_desde_sitemap_xml(xml):
            m = _PLAYER_HREF_RE.search(loc)
            if not m:
                continue
            slug, pid = m.group(1), m.group(2)
            jugadores[pid] = {
                "id": pid,
                "slug": slug,
                "display": "",
                "url": f"{BASE}/es/liga/jugadores/{slug}-{pid}",
            }
    logger.info("Sitemap de jugadores: %d fichas", len(jugadores))
    return list(jugadores.values())


def construir_indice_jugadores() -> List[Dict[str, str]]:
    """
    Índice de jugadores con dos fuentes en cascada:

      1. Plantillas de los 18 equipos (~19 peticiones). La mejor fuente en
         temporada: aporta nombre visible y equipo. En PRETEMPORADA las
         plantillas están vacías ("La plantilla se publicará cuando haya
         datos oficiales") y el parámetro editionId es ignorado, así que…
      2. Sitemap oficial de jugadores (2-10 peticiones). Independiente de la
         temporada y cubre también históricos; solo aporta el slug (el
         equipo se completa después desde la propia ficha).
    """
    edition_id = descubrir_edition_id()
    logger.info("editionId con datos: %s", edition_id)

    indice: Dict[str, Dict[str, str]] = {}
    for team_url in _urls_equipos():
        for j in _jugadores_de_plantilla(team_url, edition_id):
            j["equipo_url"] = team_url
            indice[j["id"]] = j

    if indice:
        logger.info("Índice construido desde plantillas: %d jugadores", len(indice))
        return list(indice.values())

    logger.info("Plantillas vacías (pretemporada) → usando sitemap de jugadores…")
    desde_sitemap = _jugadores_desde_sitemap()
    if desde_sitemap:
        logger.info("Índice construido desde sitemap: %d jugadores", len(desde_sitemap))
        return desde_sitemap

    raise RuntimeError(
        "No se pudo construir el índice de jugadores desde acb.com "
        "(ni plantillas ni sitemap). La estructura del sitio puede haber cambiado."
    )


def resolver_jugador(
    nombre: str, indice: List[Dict[str, str]]
) -> Dict[str, str]:
    """
    Resuelve un nombre a una entrada del índice.

    Puntúa contra dos claves por jugador: el nombre del slug (nombre legal:
    'guillermo-hernangomez') y el texto visible del enlace ('W. Hernangómez'),
    para cubrir apodos (Willy, Marcelinho…).
    """
    q = normalizar(nombre)
    if not q:
        raise ValueError("El nombre del jugador no puede estar vacío.")
    q_tokens = set(q.split())

    def claves(j: Dict[str, str]) -> List[str]:
        ks = [normalizar(j["slug"].replace("-", " "))]
        if j.get("display"):
            ks.append(normalizar(j["display"].replace(".", " ")))
        return ks

    mejor: Optional[Dict[str, str]] = None
    mejor_score = 0.0
    for j in indice:
        for k in claves(j):
            k_tokens = set(k.split())
            if not k_tokens:
                continue
            # Coincidencia exacta o de todos los tokens de la consulta.
            if q == k or (q_tokens and q_tokens <= k_tokens):
                return j
            solape = len(q_tokens & k_tokens) / max(len(q_tokens), 1)
            fuzzy = difflib.SequenceMatcher(None, q, k).ratio()
            # El apellido (último token) pesa más — es lo más estable.
            apellido = 0.35 if (q.split()[-1] in k_tokens) else 0.0
            score = 0.45 * solape + 0.45 * fuzzy + apellido
            if score > mejor_score:
                mejor_score, mejor = score, j
    if mejor is None or mejor_score < 0.55:
        sugerencias = difflib.get_close_matches(
            q,
            [normalizar(j["slug"].replace("-", " ")) for j in indice],
            n=3,
            cutoff=0.4,
        )
        extra = f" ¿Quisiste decir: {', '.join(sugerencias)}?" if sugerencias else ""
        raise ValueError(
            f"Jugador '{nombre}' no encontrado en las plantillas de la Liga Endesa."
            f"{extra} (v1: solo jugadores de la última temporada con datos)."
        )
    return mejor


# ─── Parsers de la ficha del jugador ──────────────────────────────────────────

_BIO_LABELS = [
    "Posición", "Altura", "Fecha nacimiento", "Lugar nacimiento",
    "Nacionalidad", "Licencia",
]

# Alias (desktop / móvil) → clave canónica.
_STAT_LABELS: Dict[str, str] = {
    "Partidos": "Partidos",
    "Minutos": "Minutos",
    "Puntos": "Puntos",
    "% 2 Puntos": "%2P", "% 2 PT": "%2P",
    "% 3 Puntos": "%3P", "% 3 PT": "%3P",
    "% Tiros libres": "%TL", "% TL": "%TL",
    "Rebotes": "Rebotes",
    "Asistencias": "Asistencias",
    "Tapones": "Tapones",
    "Recuperaciones": "Recuperaciones", "Recuper.": "Recuperaciones",
    "Valoración": "Valoración",
}

_RECORD_LABELS = [
    "Puntos", "Triples", "Asistencias", "Recuperaciones",
    "Tapones", "Rebotes", "Valoración",
]


# Validadores de forma por etiqueta (rechazan falsos positivos del esqueleto).
_BIO_VALIDADORES = {
    "Posición": lambda v: bool(re.match(r"^[A-Za-zÁÉÍÓÚáéíóúÑñ /-]{3,30}$", v)),
    "Altura": lambda v: bool(re.match(r"^\d,\d{2}\s*m$", v)),
    "Fecha nacimiento": lambda v: "/" in v,
    "Lugar nacimiento": lambda v: len(v) >= 2 and "·" not in v,
    "Nacionalidad": lambda v: len(v) >= 2 and "·" not in v and not v[0].isdigit(),
    "Licencia": lambda v: bool(re.match(r"^[A-ZÑ]{2,4}$", v)),
}


def _parse_bio(lineas: List[str]) -> Dict[str, str]:
    """
    La web pinta primero un esqueleto con etiquetas sin valor y después el
    bloque real. Recorremos todas las ocurrencias y nos quedamos con la ÚLTIMA
    cuyo valor pase el validador de esa etiqueta.
    """
    bio: Dict[str, str] = {}
    labels = set(_BIO_LABELS)
    for i, ln in enumerate(lineas[:-1]):
        if ln in labels:
            nxt = lineas[i + 1]
            if nxt in labels or nxt.startswith("#"):
                continue
            valida = _BIO_VALIDADORES.get(ln, lambda v: True)
            if valida(nxt):
                bio[ln] = nxt  # la última ocurrencia válida gana
    return bio


def _parse_bloque_stats(lineas: List[str], inicio: int, fin: int) -> Dict[str, str]:
    """Parsea pares etiqueta→valor dentro de [inicio, fin), ignorando rankings '# N'."""
    out: Dict[str, str] = {}
    ranks: Dict[str, str] = {}
    i = inicio
    while i < fin:
        ln = lineas[i]
        clave = _STAT_LABELS.get(ln)
        if clave and clave not in out:
            j = i + 1
            while j < fin and (lineas[j] in _STAT_LABELS):
                j += 1  # etiqueta duplicada desktop/móvil
            if j < fin and _VALUE_RE.match(lineas[j]):
                out[clave] = lineas[j]
                # ¿Ranking de liga justo después? Formato '# 7' o '# 7 en …'
                if j + 1 < fin and re.match(r"^#\s*\d+", lineas[j + 1]):
                    ranks[clave] = lineas[j + 1].strip()
                i = j
        i += 1
    if ranks:
        out["_rankings_liga"] = ranks  # type: ignore[assignment]
    return out


def _idx(lineas: List[str], pred, desde: int = 0) -> int:
    for i in range(desde, len(lineas)):
        if pred(lineas[i]):
            return i
    return -1


def _parse_records(lineas: List[str], inicio: int) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    i = inicio
    while i < len(lineas) - 1:
        ln = lineas[i]
        if ln in _RECORD_LABELS and ln not in out:
            val = lineas[i + 1]
            if _VALUE_RE.match(val):
                partido = lineas[i + 2] if i + 2 < len(lineas) else ""
                out[ln] = {"valor": val, "partido": partido}
                i += 2
        i += 1
    return out


def _extraer_foto(soup: BeautifulSoup) -> Optional[str]:
    """La foto del jugador vive en static.acb.com (a veces envuelta en /_next/image?url=…)."""
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if "_next/image" in src:
            qs = parse_qs(urlparse(src).query)
            inner = unquote(qs.get("url", [""])[0])
            if "static.acb.com/media" in inner:
                return inner
        elif "static.acb.com/media" in src:
            return src
    return None


def _parse_ficha(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    lineas = _dedupe_consecutivo(_lineas(soup))

    datos: Dict[str, Any] = {}
    datos["bio"] = _parse_bio(lineas)
    datos["foto"] = _extraer_foto(soup)

    # Dorsal + equipo: el breadcrumb/cabecera trae '7 · Facu' y el nombre del club.
    m_dorsal = _idx(lineas, lambda s: re.match(r"^\d{1,2}\s*·\s*\S", s) is not None)
    if m_dorsal >= 0:
        datos["dorsal"] = lineas[m_dorsal].split("·")[0].strip()

    for a in soup.find_all("a", href=True):
        m_eq = _TEAM_HREF_RE.search(urlparse(a["href"]).path)
        if m_eq:
            datos["equipo_url"] = f"{BASE}/es/liga/equipos/{m_eq.group(1)}"
            break

    # Temporada mostrada (la última con datos si no se pasa editionId).
    i_temp = _idx(lineas, lambda s: re.match(r"^Temporada \d{4}-\d{2}$", s) is not None)
    i_carr = _idx(lineas, lambda s: s == "Carrera", max(i_temp, 0))
    i_rec = _idx(lineas, lambda s: "Records" in s, max(i_carr, 0))

    if i_temp >= 0:
        datos["temporada_label"] = lineas[i_temp].replace("Temporada ", "")
        fin = i_carr if i_carr > i_temp else len(lineas)
        datos["temporada"] = _parse_bloque_stats(lineas, i_temp + 1, fin)
    if i_carr >= 0:
        fin = i_rec if i_rec > i_carr else len(lineas)
        datos["carrera"] = _parse_bloque_stats(lineas, i_carr + 1, fin)
    if i_rec >= 0:
        datos["records"] = _parse_records(lineas, i_rec + 1)

    return datos


# ─── Parser de estadísticas avanzadas oficiales ───────────────────────────────

# Sección → métricas EN EL ORDEN en que las publica acb.com (verificado ago-2026).
_SECCIONES_AVANZADAS: Dict[str, List[str]] = {
    "CUATRO FACTORES": ["eFG%", "ORB%", "TOV%", "FTr"],
    "MANEJO DE BALÓN": ["AST%", "STL%", "BLK%", "TOV%"],
    "LANZAMIENTO": ["TS%", "eFG%", "3PAr", "PPT"],
    "PUNTOS": ["PPFT", "PP2PS", "PP3PS"],
    "REBOTES": ["ORB%", "DRB%", "TRB"],
}

# Métricas de valor único con su etiqueta textual.
_AVANZADAS_SINGLE = {
    "Nº posesiones (40')": "Posesiones_40min",
    "Nº puntos por 100 posesiones": "PTS_100_posesiones",
}


def _split_numeros_concatenados(s: str, n: int) -> List[str]:
    """
    'TEMPORADA58,11,621,755,5' o '58,11,621,755,5' → ['58,1','1,6','21,7','55,5'].
    acb.com publica estos valores con UN decimal, lo que hace el split unívoco.
    """
    s = s.replace("TEMPORADA", "").strip()
    vals = re.findall(r"\d{1,3},\d", s)
    return vals if len(vals) == n else []


def _parse_avanzadas(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    lineas = _dedupe_consecutivo(_lineas(soup))
    out: Dict[str, Any] = {}

    for seccion, metricas in _SECCIONES_AVANZADAS.items():
        i_sec = _idx(lineas, lambda s: s.upper().strip() == seccion)
        if i_sec < 0:
            continue
        # Buscar la fila TEMPORADA dentro de la sección (ventana acotada).
        i_row = _idx(
            lineas, lambda s: s.upper().startswith("TEMPORADA"), i_sec
        )
        if i_row < 0 or i_row - i_sec > 40:
            continue
        valores: List[str] = []
        # Caso A: valores concatenados en la propia línea o en la siguiente.
        for cand in (lineas[i_row], lineas[i_row + 1] if i_row + 1 < len(lineas) else ""):
            valores = _split_numeros_concatenados(cand, len(metricas))
            if valores:
                break
        # Caso B: cada valor en su propia línea tras 'TEMPORADA'.
        if not valores:
            j = i_row + 1
            while j < len(lineas) and len(valores) < len(metricas):
                if lineas[j].upper() in ("VICTORIA", "DERROTA"):
                    break
                if _VALUE_RE.match(lineas[j]):
                    valores.append(lineas[j])
                    j += 1
                    continue
                if lineas[j] in metricas or lineas[j] in _STAT_LABELS:
                    j += 1
                    continue
                break
        if len(valores) == len(metricas):
            bloque = dict(zip(metricas, valores))
            clave = seccion.title().replace(" De ", " de ")
            out[clave] = bloque

    # Métricas de valor único.
    for etiqueta, clave in _AVANZADAS_SINGLE.items():
        i_lab = _idx(lineas, lambda s: s.startswith(etiqueta.split(" (")[0]) and etiqueta[:12] in s)
        if i_lab >= 0:
            for j in range(i_lab + 1, min(i_lab + 5, len(lineas))):
                if _VALUE_RE.match(lineas[j]):
                    out[clave] = lineas[j]
                    break

    if out:
        out["_nota"] = (
            "Estadísticas avanzadas OFICIALES publicadas por acb.com en la ficha "
            "del jugador (fila TEMPORADA). No se ha calculado ninguna de ellas."
        )
    return out

# ─── Parser de la trayectoria temporada a temporada ───────────────────────────

# Columnas de la tabla /temporada (verificadas ago-2026, 31 celdas por fila):
_COLS_TEMPORADAS = [
    "Temporada", "Club", "PJ", "Minutos", "5i",
    "Puntos", "Puntos_max",
    "T3_conv", "T3_int", "%3P",
    "T2_conv", "T2_int", "%2P",
    "TL_conv", "TL_int", "%TL",
    "Reb_of", "Reb_def", "Rebotes",
    "Asistencias", "Recuperaciones", "Pérdidas",
    "Tap_favor", "Tap_contra", "Mates",
    "Faltas_com", "Faltas_rec", "+/-", "Valoración", "V", "D",
]

_TEMP_ROW_RE = re.compile(r"^\d{2}-\d{2}$")


def _parse_temporadas(html: str) -> List[Dict[str, str]]:
    """
    Tabla 'Temporadas' de la ficha (/temporada): una fila por temporada ACB
    con el club de ese año. La página pinta un esqueleto vacío y después la
    tabla real; nos quedamos solo con filas de temporada con datos.
    """
    soup = BeautifulSoup(html, "html.parser")
    temporadas: Dict[str, Dict[str, str]] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            celdas = tr.find_all(["td", "th"])
            if not celdas:
                continue
            textos = [" ".join(c.get_text(" ").split()) for c in celdas]
            if not _TEMP_ROW_RE.match(textos[0] or ""):
                continue
            if len(textos) != len(_COLS_TEMPORADAS):
                logger.warning(
                    "Fila de temporada con %d celdas (esperadas %d): %s",
                    len(textos), len(_COLS_TEMPORADAS), textos[:3],
                )
                continue
            fila = dict(zip(_COLS_TEMPORADAS, textos))
            # El club viene como enlace; el texto del <a> es el nombre oficial.
            a = celdas[1].find("a")
            if a:
                fila["Club"] = " ".join(a.get_text(" ").split())
            # Solo filas con contenido real (el esqueleto viene vacío).
            if fila.get("PJ") and fila.get("Puntos"):
                temporadas[fila["Temporada"]] = fila
    # Más reciente primero (formato 'AA-AA' ordena bien como string).
    return sorted(temporadas.values(), key=lambda f: f["Temporada"], reverse=True)

# ─── Métrica derivada: per-40 ─────────────────────────────────────────────────

_PER40_CAMPOS = ["Puntos", "Rebotes", "Asistencias", "Tapones", "Recuperaciones", "Valoración"]


def _per40(stats: Dict[str, str]) -> Dict[str, str]:
    """
    Normalización estándar europea (partidos de 40 min): valor_per_game * 40 / minutos.
    Aritmética sobre datos reales de acb.com; se etiqueta como derivada en el informe.
    """
    minutos = _num(stats.get("Minutos", ""))
    if not minutos or minutos <= 0:
        return {}
    factor = 40.0 / minutos
    out: Dict[str, str] = {}
    for campo in _PER40_CAMPOS:
        v = _num(stats.get(campo, ""))
        if v is None:
            continue
        out[campo] = f"{v * factor:.1f}".replace(".", ",")
    return out


# ─── API pública ──────────────────────────────────────────────────────────────


def obtener_datos_jugador_acb(
    nombre: str, indice: Optional[List[Dict[str, str]]] = None
) -> Dict[str, Any]:
    """
    Pipeline completo: nombre → índice → ficha + avanzadas → dict normalizado.

    Estructura devuelta (cada bloque puede faltar de forma independiente):

        {
          "Datos personales": {Nombre, Equipo, Posición, Altura, Fecha nacimiento,
                               Lugar nacimiento, Nacionalidad, Licencia, Dorsal,
                               Foto, Ficha_acb},
          "Estadísticas": {
              "temporada_label": "2025-26",
              "temporada":  {Partidos, Minutos, Puntos, %2P, %3P, %TL, Rebotes,
                             Asistencias, Tapones, Recuperaciones, Valoración,
                             _rankings_liga, per40},
              "carrera":    {…, per40},
              "records":    {Puntos: {valor, partido}, …},
              "avanzadas":  {Cuatro Factores, Manejo de Balón, Lanzamiento,
                             Puntos, Rebotes, Posesiones_40min,
                             PTS_100_posesiones, _nota},
              "_fuente": "acb.com (ficha oficial del jugador)"
          }
        }
    """
    if indice is None:
        indice = construir_indice_jugadores()

    entrada = resolver_jugador(nombre, indice)
    logger.info("✓ Resuelto '%s' → %s (%s)", nombre, entrada["slug"], entrada["url"])

    ficha = _parse_ficha(_get_html(entrada["url"]))

    avanzadas: Dict[str, Any] = {}
    try:
        avanzadas = _parse_avanzadas(_get_html(f"{entrada['url']}/estadisticas-avanzadas"))
    except Exception as exc:
        logger.warning("Avanzadas no disponibles para %s: %s", entrada["slug"], exc)

    trayectoria: List[Dict[str, str]] = []
    try:
        trayectoria = _parse_temporadas(_get_html(f"{entrada['url']}/temporada"))
    except Exception as exc:
        logger.warning("Trayectoria no disponible para %s: %s", entrada["slug"], exc)

    bio_raw = ficha.get("bio", {})
    nombre_display = entrada.get("display") or entrada["slug"].replace("-", " ").title()

    # Equipo: derivarlo de la URL del equipo del índice ('real-madrid-9' → 'Real Madrid').
    equipo = "–"
    equipo_url = entrada.get("equipo_url") or ficha.get("equipo_url")
    if equipo_url:
        slug_eq = equipo_url.rstrip("/").split("/")[-1]
        equipo = re.sub(r"-\d+$", "", slug_eq).replace("-", " ").title()

    datos_personales: Dict[str, Any] = {
        "Nombre": nombre_display,
        "Equipo": equipo,
        "Posición": bio_raw.get("Posición", "–"),
        "Altura": bio_raw.get("Altura", "–"),
        "Fecha nacimiento": bio_raw.get("Fecha nacimiento", "–"),
        "Lugar nacimiento": bio_raw.get("Lugar nacimiento", "–"),
        "Nacionalidad": bio_raw.get("Nacionalidad", "–"),
        "Licencia": bio_raw.get("Licencia", "–"),
        "Dorsal": ficha.get("dorsal", "–"),
        "Foto": ficha.get("foto") or "",
        "Ficha_acb": entrada["url"],
    }

    estadisticas: Dict[str, Any] = {"_fuente": "acb.com (ficha oficial del jugador)"}
    if ficha.get("temporada_label"):
        estadisticas["temporada_label"] = ficha["temporada_label"]
    if ficha.get("temporada"):
        temp = dict(ficha["temporada"])
        p40 = _per40(temp)
        if p40:
            temp["per40"] = p40
        estadisticas["temporada"] = temp
    if ficha.get("carrera"):
        carr = dict(ficha["carrera"])
        p40 = _per40(carr)
        if p40:
            carr["per40"] = p40
        estadisticas["carrera"] = carr
    if ficha.get("records"):
        estadisticas["records"] = ficha["records"]
    if avanzadas:
        estadisticas["avanzadas"] = avanzadas
    if trayectoria:
        estadisticas["trayectoria"] = trayectoria

    if len(estadisticas) <= 1:
        logger.warning(
            "No se extrajeron estadísticas para %s — la estructura de acb.com "
            "puede haber cambiado. El informe saldrá solo con la bio.",
            entrada["slug"],
        )

    return {"Datos personales": datos_personales, "Estadísticas": estadisticas}
