"""
test_scraper.py — Prueba el scraper contra acb.com EN VIVO desde tu máquina.

Uso:
    python test_scraper.py "Facu Campazzo"
    python test_scraper.py --indice          # solo construye y muestra el índice

Este script NO llama a Gemini ni genera PDF: valida únicamente la extracción.
Si el parsing falla, imprime las primeras líneas de texto de la página para
que puedas ajustar los anclajes en acb_data.py.
"""

from __future__ import annotations

import json
import logging
import sys

logging.basicConfig(level="INFO", format="%(levelname)-7s │ %(name)s │ %(message)s")

from acb_data import (  # noqa: E402
    BeautifulSoup,
    _dedupe_consecutivo,
    _get_html,
    _lineas,
    construir_indice_jugadores,
    obtener_datos_jugador_acb,
    resolver_jugador,
)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    indice = construir_indice_jugadores()
    print(f"\n✓ Índice: {len(indice)} jugadores\n")

    if args[0] == "--indice":
        for j in sorted(indice, key=lambda x: x["slug"])[:40]:
            print(f"  {j['slug']:<35} {j.get('display', ''):<25} {j['url']}")
        print("  … (primeros 40)")
        return

    nombre = " ".join(args)
    entrada = resolver_jugador(nombre, indice)
    print(f"✓ Resuelto: '{nombre}' → {entrada['url']}\n")

    datos = obtener_datos_jugador_acb(nombre, indice=indice)
    print(json.dumps(datos, ensure_ascii=False, indent=2))

    est = datos.get("Estadísticas", {})
    faltan = [k for k in ("temporada", "carrera", "avanzadas") if k not in est]
    if faltan:
        print(f"\n⚠ Bloques no extraídos: {faltan}")
        print("Primeras 120 líneas de texto de la ficha (para depurar anclajes):\n")
        html = _get_html(entrada["url"])
        for ln in _dedupe_consecutivo(_lineas(BeautifulSoup(html, "html.parser")))[:120]:
            print("   ", ln)


if __name__ == "__main__":
    main()
