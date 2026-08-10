# Generador de Reportes ACB · Basketmática

Aplicación que genera scouting reports en PDF de jugadores de la Liga Endesa
(ACB), a partir de datos extraídos en tiempo real de la ficha oficial de cada
jugador en acb.com —incluidas sus estadísticas avanzadas oficiales— más un
análisis redactado por un modelo de lenguaje.

Es la herramienta que alimenta la sección de reportes de [Basketmática](https://basketmatica.com).

## Cómo funciona

1. **Resolución del jugador** — el nombre introducido se compara (con
   tolerancia a apodos y variaciones) contra un índice de las 18 plantillas
   de la liga, construido scrapeando acb.com.
2. **Extracción de datos** — se descarga y parsea la ficha oficial del
   jugador: bio, promedios de temporada y de carrera (con ranking de liga),
   récords personales y estadísticas avanzadas (Cuatro Factores, TS%, eFG%,
   AST%, ritmo…). El parser se ancla a las etiquetas de texto visibles en la
   página, no a clases CSS, para resistir mejor los rediseños del sitio.
3. **Análisis con IA** — un LLM recibe únicamente los datos ya extraídos y
   redacta el análisis (resumen de desempeño, FODA, proyección, jugadores de
   perfil similar) como JSON estructurado. El modelo nunca genera las tablas
   de cifras: esas las monta Python directamente desde los datos, de modo
   que el informe no puede contener números inventados por la IA.
4. **Render y PDF** — Python compone el HTML final combinando datos +
   análisis con los tokens de diseño de la marca, y WeasyPrint lo convierte
   a PDF.

La única métrica que no es oficial es el per-40 (normalización a 40 minutos,
estándar en baloncesto europeo): se calcula a partir de minutos y promedios
reales, y siempre aparece etiquetada como derivada.

## Stack técnico

| Capa | Tecnología |
|---|---|
| Interfaz | [Streamlit](https://streamlit.io) |
| Scraping | `httpx` + `BeautifulSoup` |
| Análisis con IA | Cliente propio compatible con la API de OpenAI, con fallback en cadena entre proveedores (Groq, OpenRouter, Cerebras, Mistral, Gemini) |
| Generación de PDF | [WeasyPrint](https://weasyprint.org) |

## Estructura del repositorio

```
streamlit_app.py   Interfaz de usuario
acb_data.py         Scraping de acb.com: índice de jugadores, ficha, avanzadas
llm_client.py        Cliente LLM multi-proveedor con fallback
report_acb.py         Prompt de análisis + render HTML determinista + PDF
test_scraper.py       Validación del scraper en vivo, sin LLM ni PDF
```

## Por qué este enfoque

Pedir al LLM que redacte solo texto (nunca cifras) reduce la respuesta a
~1K tokens, lo que la hace viable en los free tier de cualquier proveedor, y
garantiza que ningún número del PDF pase por el modelo: todos vienen
directamente del scraping de acb.com.
