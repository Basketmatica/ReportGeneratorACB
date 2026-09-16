# Generador de Reportes ACB · Basketmática

Aplicación que genera scouting reports en PDF de jugadores de la Liga Endesa
(ACB), a partir de datos extraídos en tiempo real de la ficha oficial de cada
jugador en acb.com —incluidas sus estadísticas avanzadas oficiales— más un
análisis redactado por un modelo de lenguaje.

Es la herramienta que alimenta la sección de reportes de [Basketmática](https://basketmatica.com).
Tiene un generador hermano para la NBA (`ReportGeneratorNBA`) que comparte el
mismo núcleo: mismo informe, mismas reglas y mismo tratamiento del texto de la IA.

## Cómo funciona

1. **Resolución del jugador** — el nombre introducido se compara (con
   tolerancia a apodos y variaciones) contra un índice de las 18 plantillas
   de la liga, construido scrapeando acb.com.
2. **Extracción de datos** — se descarga y parsea la ficha oficial del
   jugador: bio, promedios de temporada y de carrera (con ranking de liga),
   récords personales, trayectoria y estadísticas avanzadas (Cuatro Factores,
   TS%, eFG%, AST%, ritmo…). Si la temporada nueva aún no tiene partidos, se
   usa la última temporada con partidos. El parser se ancla a las etiquetas de
   texto visibles en la página, no a clases CSS, para resistir mejor los
   rediseños del sitio.
3. **Vista del informe** — los datos se reducen exactamente a lo que muestran
   las tablas del PDF, en formato español (coma decimal). Esa misma vista es
   la que recibe el LLM: no puede citar ninguna cifra que el lector no
   encuentre en una tabla.
4. **Análisis con IA** — el LLM redacta el análisis (resumen de desempeño,
   FODA, proyección, jugadores de perfil similar) como JSON estructurado. El
   modelo nunca genera las tablas de cifras. Después, Python normaliza el
   texto (coma decimal, terminología de baloncesto en España) y descarta al
   propio jugador si aparece entre los similares.
5. **Render y PDF** — Python compone el HTML final con los tokens de diseño
   de la marca y WeasyPrint lo convierte a PDF.

Las métricas calculadas por Basketmática (per-40, estándar en baloncesto
europeo, y ratios como AST/BP) salen de minutos y promedios oficiales y
siempre aparecen etiquetadas como calculadas, tanto en las tablas como en el
texto de la IA.

## Stack técnico

| Capa | Tecnología |
|---|---|
| Interfaz | [Streamlit](https://streamlit.io) |
| Datos | Scraping de acb.com con `httpx` + `BeautifulSoup` |
| Análisis con IA | Cliente propio compatible con la API de OpenAI, con fallback en cadena entre proveedores (Groq, OpenRouter, Cerebras, Mistral, Gemini) |
| Generación de PDF | [WeasyPrint](https://weasyprint.org) |

## Estructura del repositorio

```
streamlit_app.py   Interfaz de usuario
acb_data.py        Datos de la fuente: índice de jugadores, ficha, avanzadas, trayectoria
report_acb.py      Lo propio de la competición: configuración, vista y ratios calculados
informe_comun.py   Núcleo común: prompt, normalización del texto de la IA, render y PDF
llm_client.py      Cliente LLM multi-proveedor con fallback
test_scraper.py    Validación del scraper en vivo, sin LLM ni PDF
```

`informe_comun.py` y `llm_client.py` son **idénticos** en los generadores ACB y
NBA. Tras cambiar uno, cópialo al otro repositorio y comprueba:

```bash
diff informe_comun.py ../ReportGeneratorNBA/informe_comun.py
diff llm_client.py ../ReportGeneratorNBA/llm_client.py
```

## Probarlo en local

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Necesita al menos una clave de LLM en `.streamlit/secrets.toml`:

```toml
GROQ_API_KEY = "..."                  # console.groq.com
GROQ_MODEL = "openai/gpt-oss-120b"
# Fallback recomendado (mismo modelo, límites más altos):
# CEREBRAS_API_KEY = "..."
# LLM_PROVIDERS = "groq,cerebras,gemini"
```

## Por qué este enfoque

Pedir al LLM que redacte solo texto (nunca cifras) mantiene la respuesta
pequeña, viable en los free tier de cualquier proveedor, y garantiza que
ningún número de las tablas del PDF pase por el modelo: todos vienen
directamente de acb.com o de un cálculo etiquetado sobre esos datos.
