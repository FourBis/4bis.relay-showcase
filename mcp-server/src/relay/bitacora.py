"""Evidencia y pasos verificados que sobreviven entre turnos del experto."""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("relay.experts")

# Constantes exportadas para que tests/imports no tengan que entrar a la clase.
# Las marcas reales viven en `Bitacora.MAX_*`.
BITACORA_MAX_PASOS = 12  # pasos marcados del plan (Etapa B, P2)


class Bitacora:
    """Los hechos verificados de un run, inmunes a la elisión.

    Contracara de la capa 2 (`_elide_old_tool_returns`): esa recorta el
    historial para cortar el N², y al hacerlo le borra al experto lo que
    ya comprobó. Acá se acumula lo que él decide que vale guardar, y
    `render()` se engancha como instructions dinámicas —fuera del
    historial elidible, re-armadas en cada request.

    Los topes no son decorativos: esto se re-manda en CADA turno. Sin
    techo, arreglar la amnesia costaría el mismo blowup que la elisión
    vino a evitar. Peor caso ~4KB por request, contra los ~48KB que puede
    pesar UN solo result de shell.
    """

    MAX = 60            # hechos; pasado el tope se cae el más viejo
    MAX_CHARS = 4000    # techo duro de lo que se re-manda por request
    MAX_COMANDOS = 20   # comandos; ring aparte, ver `anotar_comando`
    MAX_PASOS = 12      # pasos marcados del plan (Etapa B, P2)

    def __init__(self) -> None:
        self.hechos: list[str] = []
        self.comandos: list[str] = []
        self.pasos: dict[int, str] = {}   # paso -> nota corta

    def anotar_comando(self, cmd: str, code: Optional[int]) -> None:
        """Registra un comando ejecutado. Lo llama el harness, no el modelo.

        La bitácora de `anotar` depende de que el experto elija usarla, y
        lo que se midió es justamente que no se auto-reporta bien: llegó a
        pedirle al humano que confirmara a mano un `docker --version` que
        él mismo había podido correr. Esto no le pide permiso a nadie —
        cada comando deja su rastro con el exit code.

        Ring propio y no la lista de hechos: un run de 172 tool calls
        vaciaría los 60 hechos del modelo con puro log de comandos.
        """
        estado = f"exit={code}" if code is not None else "activo; disponibilidad sin verificar"
        linea = f"$ {' '.join((cmd or '').split())[:120]} → {estado}"
        self.comandos.append(linea)
        del self.comandos[:-self.MAX_COMANDOS]

    def anotar(self, hecho: str) -> str:
        """Agrega un hecho. Devuelve el ack que ve el LLM."""
        h = " ".join((hecho or "").split())[:300]
        if not h:
            return "Bitácora sin cambios: el hecho venía vacío."
        if h in self.hechos:
            return f"Ya estaba anotado ({len(self.hechos)} hechos)."
        if len(self.hechos) >= self.MAX:
            # En un run largo lo reciente es lo que necesita para
            # cerrar, y un techo que cede deja de ser un techo.
            self.hechos.pop(0)
        self.hechos.append(h)
        return f"Anotado ({len(self.hechos)} hechos en la bitácora)."

    def evidencia(self, max_chars: int = 1500) -> str:
        """Lo comprobable del run, para el verificador. `""` si no hay nada.

        El verificador venía recibiendo el resultado del ejecutor y una
        lista de nombres de tools con sus argumentos. Con eso puede ver
        que se LLAMÓ a `shell` con `pytest`, y no puede distinguir
        "corrió las pruebas" de "las pruebas pasaron" — que es la
        diferencia entre aprobar trabajo terminado y aprobar una
        intención.

        Los comandos van PRIMERO y separados del resto a propósito: un
        `exit=0` lo escribió el harness y es un hecho; un "verifiqué que
        compila" lo escribió el modelo y es una afirmación suya. El
        verificador tiene que poder pesarlas distinto, así que se le
        entregan etiquetadas distinto.

        El tope es más chico que `MAX_CHARS` porque esto viaja en el
        prompt de una etapa auxiliar que corre en el modelo barato: la
        evidencia tiene que caber sin desplazar al plan ni al resultado.
        """
        partes = []
        if self.comandos:
            partes.append("### Comandos que corrió el harness (exit code real)\n"
                          + "\n".join(self.comandos))
        if self.pasos:
            partes.append(
                "### Pasos del plan que el ejecutor marcó como hechos\n"
                + "\n".join(f"- paso {k}: {v}"
                            for k, v in sorted(self.pasos.items())))
        if self.hechos:
            partes.append("### Lo que el ejecutor dice haber comprobado\n"
                          + "\n".join(f"- {h}" for h in self.hechos))
        texto = "\n\n".join(partes)
        aviso = "\n[evidencia recortada; las omisiones no prueban éxito ni fallo]"
        return (texto if len(texto) <= max_chars
                else texto[:max(0, max_chars - len(aviso))] + aviso[:max_chars])

    def volcar(self) -> str:
        """La bitácora como JSON, para guardarla entre turnos. `""` si está vacía.

        2026-08-26. Hasta hoy esto vivía solo en memoria del run: se
        engancha como *instructions*, y el choke point de persistencia
        (`_dump_messages`) hace `_strip_instructions` antes de guardar.
        Resultado: lo único que registraba qué había verificado el
        experto se perdía al terminar el turno — y en un corte por
        `off_plan`, donde el historial además pierde las tool calls
        enteras, el siguiente **continuá** arrancaba sin nada.

        Etapa B (2026-09): `pasos` es lo que el ejecutor va marcando con
        `plan_step_done`. Se serializa con claves string porque json no
        acepta int como key, y el destino es `chats.stages_json` que es
        texto SQLite.
        """
        if not self.hechos and not self.comandos and not self.pasos:
            return ""
        return json.dumps(
            {"hechos": self.hechos, "comandos": self.comandos,
             "pasos": {str(k): v for k, v in self.pasos.items()}},
            ensure_ascii=False)

    @classmethod
    def cargar(cls, raw: str) -> "Bitacora":
        """Reconstruye desde `volcar()`. Best-effort: JSON roto → vacía.

        Nunca tira: una bitácora ilegible es peor contexto, no un run
        muerto. Y se re-aplican los topes al cargar, porque el JSON pudo
        haberse guardado con una versión que tenía otros límites.
        """
        def _lista(v, tope):
            # `isinstance(list)` y no un `or []`: un JSON con
            # `"hechos": "texto"` es iterable, y sin este chequeo la
            # comprensión recorre CARACTERES y le mete al modelo una
            # bitácora de letras sueltas. Lo encontró el test, no yo.
            if not isinstance(v, list):
                return []
            return [str(x) for x in v][-tope:]

        b = cls()
        try:
            d = json.loads(raw or "")
            if not isinstance(d, dict):
                raise ValueError("la bitácora no es un objeto")
            b.hechos = _lista(d.get("hechos"), cls.MAX)
            b.comandos = _lista(d.get("comandos"), cls.MAX_COMANDOS)
            # Etapa B: pasos marcados por `plan_step_done`.
            ps = d.get("pasos")
            if isinstance(ps, dict):
                # Los dicts no soportan `[-N:]` — el último run
                # tiraba KeyError. Hay que ir por `items()` y volver
                # a armar el dict.
                pares = [(int(k), str(v)[:200])
                         for k, v in ps.items()
                         if str(k).isdigit()][-cls.MAX_PASOS:]
                b.pasos = dict(pares)
        except (ValueError, TypeError, AttributeError):
            logger.warning("bitácora ilegible al cargar; sigo con una vacía")
        return b

    def fusionar_pasos_verificados(self, pasos) -> None:
        """Fusión desde el verificador (Etapa B P5).

        El verificador vio qué pasos quedaron cubiertos aunque el
        ejecutor no los haya marcado con `plan_step_done`. Los sumamos
        a `self.pasos` con una nota estándar, sin pisar los que ya
        marcó el ejecutor. Tope igual que `marcar_paso`.
        """
        for raw in (pasos or []):
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if idx < 1:
                continue
            if idx in self.pasos:
                continue
            self.pasos[idx] = "verificado por el verificador"
        if len(self.pasos) > self.MAX_PASOS:
            for viejo in sorted(self.pasos)[:-self.MAX_PASOS]:
                del self.pasos[viejo]

    def marcar_paso(self, n: int, nota: str = "") -> None:
        """Marca el paso `n` (1-based) como completado (Etapa B, P2).

        Acepta cualquier entero (incluso fuera de rango): el cap a
        `MAX_PASOS` mantiene el techo del 4KB por request, y la
        validación contra el plan la hace el orquestador al armar el
        dict de salida (no la tool — un número mal puesto no puede
        cortar el run).
        """
        try:
            n = int(n)
        except (TypeError, ValueError):
            return
        if n < 1:
            return
        # Notas largas se cortan acá: la UI las muestra al hover y 200
        # chars es más que suficiente.
        self.pasos[n] = " ".join((nota or "").split())[:200]
        # Techo: si el modelo decide marcar 50 pasos, dejamos los
        # últimos MAX_PASOS (los recientes son los que importan).
        if len(self.pasos) > self.MAX_PASOS:
            for viejo in sorted(self.pasos)[:-self.MAX_PASOS]:
                del self.pasos[viejo]

    def render(self) -> str:
        """El bloque para las instructions. "" si no hay nada anotado."""
        bloques: list[str] = []
        if self.hechos:
            lineas: list[str] = []
            total = 0
            # De atrás para adelante: si hay que recortar, se recorta lo
            # viejo, no lo último que verificó.
            for h in reversed(self.hechos):
                total += len(h) + 3
                if total > self.MAX_CHARS:
                    lineas.append(
                        f"- […{len(self.hechos) - len(lineas)} hechos más "
                        "viejos omitidos por espacio]")
                    break
                lineas.append(f"- {h}")
            bloques.append(
                "## Bitácora de este run (lo que YA verificaste)\n"
                + "\n".join(reversed(lineas)))
        if self.comandos:
            bloques.append(
                f"## Últimos {len(self.comandos)} comandos que ejecutaste\n"
                + "\n".join(self.comandos)
                + "\n\nEsta lista la escribe el harness, no el modelo: si "
                "un comando aparece aquí, se ejecutó de verdad. No pidas "
                "confirmación humana de algo que ya ejecutaste.")
        if not bloques:
            return ""
        return "\n\n".join(bloques) + (
            "\n\nArma tu respuesta final desde esto. Lo que no aparezca "
            "aquí, no lo afirmes como hecho.")
