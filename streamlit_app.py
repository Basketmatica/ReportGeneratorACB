"""
streamlit_app.py — Generador de Reportes ACB · Basketmática

Diseñado para vivir EMBEBIDO en basketmatica.com dentro de un iframe.
El tema base lo define .streamlit/config.toml; aquí va solo lo que config.toml
no puede: tipografías reales de marca, chrome oculto, espaciados y jerarquía
de botones.

Secrets (Streamlit Cloud → Settings → Secrets):
    GROQ_API_KEY = "..."               # análisis LLM (gratis, console.groq.com)
    # Fallbacks opcionales:
    # OPENROUTER_API_KEY = "..."
    # API_KEY = "..."                  # Gemini (si tu cuenta antigua conserva free tier)
    # LLM_PROVIDERS = "groq,openrouter,gemini"
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata

import streamlit as st

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("acb-report")

from acb_data import construir_indice_jugadores, obtener_datos_jugador_acb  # noqa: E402
from llm_client import cargar_proveedores  # noqa: E402
from report_acb import generar_pdf_jugador_acb  # noqa: E402

# ─── Design tokens (espejo de :root en global.css) ────────────────────────────
BG = "#F4EFE5"
SURFACE = "#FBF8F0"
LINE = "#E2D8C4"
INK = "#221A10"
INK_SOFT = "#6E5E46"
BRAND = "#583C14"
BRAND_600 = "#6F4F22"
ACCENT = "#1F8A74"
ACCENT_600 = "#176B5A"
SPOT = "#E8772E"

FONT_DISPLAY = "'Space Grotesk', ui-sans-serif, system-ui, sans-serif"
FONT_BODY = "'Source Serif 4', Georgia, 'Times New Roman', serif"
FONT_MONO = "'IBM Plex Mono', ui-monospace, Consolas, monospace"

st.set_page_config(
    page_title="Generador de Reportes ACB · Basketmática",
    page_icon="🏀",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap');

      /* ── Chrome de Streamlit fuera: dentro del iframe delata la app ajena ── */
      #MainMenu, footer, header[data-testid="stHeader"],
      [data-testid="stDecoration"], .stAppDeployButton {{ display: none !important; }}

      .block-container {{
        padding-top: 1.2rem !important;
        padding-bottom: 1.2rem !important;
        max-width: 620px;
      }}

      /* ── Los dos dialectos: EDITORIAL (serif) prosa, DATO (sans) UI ── */
      html, body, [data-testid="stAppViewContainer"], p, li {{
        font-family: {FONT_BODY};
      }}
      label, .stButton button, .stDownloadButton button,
      .bm-kicker, .bm-title, .bm-meta, [data-testid="stExpander"] summary {{
        font-family: {FONT_DISPLAY} !important;
      }}
      code, pre, .stJson {{ font-family: {FONT_MONO} !important; }}

      /* ── Cabecera del módulo ── */
      .bm-kicker {{
        color: {ACCENT_600};
        text-transform: uppercase;
        letter-spacing: 2.5px;
        font-size: .72rem;
        font-weight: 700;
        margin: 0 0 .2rem;
      }}
      .bm-title {{
        font-size: 1.35rem;
        font-weight: 700;
        color: {BRAND};
        margin: 0 0 1.1rem;
        padding-bottom: .6rem;
        border-bottom: 2px solid {ACCENT};
      }}

      /* ── Inputs ── */
      .stTextInput input {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-radius: 10px;
        color: {INK};
      }}
      .stTextInput input:focus {{
        border-color: {ACCENT};
        box-shadow: 0 0 0 2px rgba(31, 138, 116, .20);
      }}
      .stTextInput input::placeholder {{ color: {INK_SOFT}; opacity: .75; }}

      /* ── Botones: teal genera, espresso descarga ── */
      .stButton button, .stDownloadButton button {{
        border: none !important;
        border-radius: 10px;
        font-weight: 700;
        letter-spacing: .3px;
        width: 100%;
        padding: .62rem;
        transition: background .15s ease;
      }}
      .stButton button {{ background: {ACCENT} !important; color: {SURFACE} !important; }}
      .stButton button:hover {{ background: {ACCENT_600} !important; }}
      .stDownloadButton button {{ background: {BRAND} !important; color: {SURFACE} !important; }}
      .stDownloadButton button:hover {{ background: {BRAND_600} !important; }}

      /* ── Alerts: filete de marca ── */
      [data-testid="stAlert"] {{
        background: {SURFACE} !important;
        border: 1px solid {LINE} !important;
        border-left: 3px solid {ACCENT} !important;
        border-radius: 10px;
        color: {INK} !important;
      }}
      [data-testid="stAlert"]:has([data-testid="stAlertContentError"]) {{
        border-left-color: {SPOT} !important;
      }}

      .bm-meta {{ font-size: .82rem; color: {INK_SOFT}; margin: .2rem 0 .8rem; }}

      .bm-nota {{
        border: 1px solid {LINE};
        border-left: 3px solid {LINE};
        border-radius: 10px;
        padding: .6rem .9rem;
        background: {SURFACE};
        font-size: .8rem;
        color: {INK_SOFT};
        margin-top: 1.4rem;
        line-height: 1.55;
      }}

      [data-testid="stExpander"] details {{
        border: 1px solid {LINE};
        border-radius: 10px;
        background: {SURFACE};
      }}

      .stSpinner > div {{ border-top-color: {ACCENT} !important; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ─── Cachés y utilidades ──────────────────────────────────────────────────────


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def indice_jugadores():
    """Índice nombre→URL de todos los jugadores (18 plantillas). 1 vez al día."""
    return construir_indice_jugadores()


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def datos_jugador(nombre_normalizado: str):
    return obtener_datos_jugador_acb(nombre_normalizado, indice=indice_jugadores())


def _safe_filename(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 _.-]", "_", s).strip()
    return s or "Jugador"


# ─── UI ───────────────────────────────────────────────────────────────────────

st.markdown('<p class="bm-kicker">Basketmática · Herramienta</p>', unsafe_allow_html=True)
st.markdown('<p class="bm-title">Genera tu reporte</p>', unsafe_allow_html=True)

with st.form("form-reporte"):
    nombre = st.text_input(
        "Nombre del jugador",
        placeholder="Ej. Facundo Campazzo, Dario Brizuela…",
        max_chars=80,
    )
    enviar = st.form_submit_button("Generar informe PDF")

if enviar:
    nombre = (nombre or "").strip()
    if len(nombre) < 2:
        st.error("Escribe un nombre válido (mínimo 2 caracteres).")
        st.stop()

    proveedores = cargar_proveedores({**os.environ, **st.secrets})

    try:
        with st.spinner("Buscando al jugador en acb.com…"):
            data = datos_jugador(nombre.lower())

        dp = data.get("Datos personales", {})
        est = data.get("Estadísticas", {})
        temporada = est.get("temporada_label", "")
        st.markdown(
            f'<p class="bm-meta"><strong>{dp.get("Nombre", nombre)}</strong> · '
            f'{dp.get("Equipo", "—")}'
            + (f" · Temporada {temporada}" if temporada else "")
            + "</p>",
            unsafe_allow_html=True,
        )

        with st.spinner("Redactando el análisis y montando el PDF… (~10-25 s)"):
            pdf = generar_pdf_jugador_acb(nombre, proveedores, player_data=data)

        st.download_button(
            label="⬇  Descargar informe PDF",
            data=pdf,
            file_name=f"{_safe_filename(dp.get('Nombre', nombre))}_Report_ACB.pdf",
            mime="application/pdf",
        )

        with st.expander("Ver datos extraídos (JSON)"):
            st.json(data)

    except ValueError as exc:
        st.error(str(exc))
    except EnvironmentError as exc:
        st.error(str(exc))
    except RuntimeError as exc:
        st.error(f"Error transitorio: {exc}. Inténtalo de nuevo en unos segundos.")
    except Exception:
        logger.exception("Error inesperado generando informe para '%s'", nombre)
        st.error("Error interno al generar el informe. Inténtalo de nuevo más tarde.")

st.markdown(
    '<div class="bm-nota">Todos los valores proceden de la ficha oficial del '
    "jugador en acb.com, incluidas las estadísticas avanzadas; la única métrica "
    "calculada es per-40 y se etiqueta como tal. El modelo de IA solo redacta "
    "el texto analítico sobre esos mismos datos, sin intervenir en las cifras.</div>",
    unsafe_allow_html=True,
)
