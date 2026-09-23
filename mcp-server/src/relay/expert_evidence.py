"""Evidencia mínima para preguntas que requieren decisión humana."""
from __future__ import annotations
import re


_INSTALL_HINTS = ("instal", "npm i ", "pip install", "winget", "choco",
                  "apt-get", "apt install", "dotnet tool", "descargar",
                  "bajar el binario", "falta el paquete", "no está instalado")


def _huele_a_instalacion(*textos: str) -> bool:
    """¿La pregunta es sobre instalar algo? Marca `kind='install'`.

    Solo para que la UI la pinte distinta y para poder contar cuántas
    veces el experto se frenó por una herramienta que falta — que es el
    caso que el humano pidió que SIEMPRE se pregunte.
    """
    blob = " ".join(t or "" for t in textos).lower()
    return any(h in blob for h in _INSTALL_HINTS)


# 2026-09-06. Se acumularon 11 preguntas de `ask_human` sin responder (la
# más vieja de 3 semanas): el humano no las contestaba porque verificar la
# afirmación costaba casi lo mismo que hacer el trabajo — la pregunta decía
# una conclusión ("el inventario está desactualizado") sin decir qué leyó
# para llegar ahí. `_evidencia_insuficiente` es el guard que obliga a
# `ask_human` a traer esa lectura.
#
# ponytail: heurística de texto, no un parser — detecta "hay una ruta o un
# nombre de archivo con extensión" y "el texto es puro hedge sin esa
# referencia". No entiende si la evidencia es CORRECTA, solo que no está
# vacía ni es pura especulación. Subir a algo más estricto (ej. verificar
# que el archivo citado existe de verdad en el repo) el día que el modelo
# aprenda a colar un `Foo.cs` inventado.
#: Piso de largo para la evidencia. 2026-09-06: estaba en 20 y dejaba
#: pasar "Lei Foo.cs y esta mal" —un nombre de archivo pegado a nada—,
#: que cumple la forma y no sirve: el humano igual tiene que abrir el
#: repo, que es justo el costo que esto viene a sacarle. 80 es una
#: oracion corta; la evidencia real medida en los tests da 100-132.
#: No es infalible (nada que valide lenguaje natural lo es): sube el
#: costo de inventar por encima del de mirar de verdad.
_MIN_EVIDENCIA_CHARS = 80
_FILE_REF_RE = re.compile(r'[\w][\w./\\-]*\.[A-Za-z]{2,6}\b')
_HEDGE_HINTS = ("asumo", "aparentemente", "supongo", "no leí",
                "parece que", "creo que")


def _evidencia_insuficiente(evidencia: str) -> str | None:
    """`None` si la evidencia alcanza el mínimo; si no, el motivo (para el
    `ModelRetry` que le pide al modelo completarla).

    Dos rechazos, en orden:
    1. Vacía o demasiado corta.
    2. Sin ninguna referencia a un archivo concreto (ruta con `/` o `\\`,
       o un nombre con extensión tipo `Foo.cs`).

    Un hedge ("asumo", "parece que") NO invalida por sí solo — reportar
    "el inventario dice textual 'asumo, no leí el resto'" citando el
    archivo real donde lo dice es evidencia legítima. Lo que se rechaza es
    la combinación: puro hedge y CERO archivo citado, que es la firma de
    una conclusión inventada.
    """
    ev = (evidencia or "").strip()
    if len(ev) < _MIN_EVIDENCIA_CHARS:
        return ("está vacía o es demasiado corta. Necesito qué archivos "
                "leíste concretamente y qué encontraste en ellos, no una "
                "frase de una línea")
    if not _FILE_REF_RE.search(ev):
        blob = ev.lower()
        if any(h in blob for h in _HEDGE_HINTS):
            return ("es pura especulación: solo tiene frases como 'asumo' "
                     "o 'parece que' sin nombrar ningún archivo concreto "
                     "que hayas leído")
        return ("no menciona ningún archivo concreto (una ruta o un "
                 "nombre con extensión, ej. `Service/Foo.cs`). Decime QUÉ "
                 "archivos leíste")
    return None
