"""Plantillas de directiva + el veredicto que el nocturno tiraba (2026-08-27).

Dos cosas que llegan juntas porque las dos atacan lo mismo: que el modo
nocturno pierda información que ya tenía en la mano.

1. `night_templates` — escribir una directiva buena no es escribir un
   pedido. Hay que numerar los puntos (el planificador los extrae y
   después chequea cobertura) y nombrar SOLO paths que resuelvan contra
   el índice cbm: una ref inválida descarta la tarea entera y en
   silencio. Eso se aprende perdiendo un run; sin dónde guardarlo, se
   vuelve a perder.

2. El veredicto — el nocturno ya corría el runner por etapas, o sea que
   PAGABA el verificador, y su respuesta moría en un `logger.info`. No
   llegaba al TaskResult ni al reporte de la mañana: un `needs_human`
   commiteaba igual y nadie se enteraba.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import night  # noqa: E402
from relay.db import Database  # noqa: E402
from relay.night import BranchWorker, MorningReporter, NightConfig  # noqa: E402
from relay.night import NightTask, TaskResult  # noqa: E402


# --- 1. plantillas ----------------------------------------------------

class PlantillasTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        import tempfile
        # Mismo patrón que test_night_questions: el schema se aplica en el
        # constructor y el tempfile se limpia al GC. No hay close().
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.init_schema()

    async def test_alta_y_lectura(self) -> None:
        await self.db.save_night_template("release", "P1. limpiar.")
        t = await self.db.get_night_template("release")
        self.assertEqual(t["directiva"], "P1. limpiar.")
        self.assertEqual(t["project_slug"], "")     # global

    async def test_la_del_proyecto_tapa_a_la_global(self) -> None:
        """Es la razón de ser del scope: una 'release' genérica y una
        'release' propia de sample-app pueden convivir."""
        await self.db.save_night_template("release", "generica")
        await self.db.save_night_template(
            "release", "de sample-app", project_slug="sample-app")
        self.assertEqual(
            (await self.db.get_night_template("release", "sample-app"))["directiva"],
            "de sample-app")
        # Otro proyecto sigue viendo la global.
        self.assertEqual(
            (await self.db.get_night_template("release", "inventorydemo"))["directiva"],
            "generica")

    async def test_guardar_dos_veces_actualiza_no_duplica(self) -> None:
        await self.db.save_night_template("x", "v1")
        await self.db.save_night_template("x", "v2")
        filas = await self.db.list_night_templates()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["directiva"], "v2")

    async def test_dos_globales_con_el_mismo_nombre_no_se_duplican(self) -> None:
        """El motivo de usar '' y no NULL para 'global': en SQLite dos
        NULL son DISTINTOS para un UNIQUE, así que con NULL esto creaba
        dos filas y `get` devolvía cualquiera de las dos."""
        await self.db.save_night_template("dup", "a", project_slug="")
        await self.db.save_night_template("dup", "b", project_slug="")
        self.assertEqual(len(await self.db.list_night_templates()), 1)

    async def test_borrar_la_del_proyecto_no_toca_la_global(self) -> None:
        await self.db.save_night_template("r", "global")
        await self.db.save_night_template("r", "propia", project_slug="sample-app")
        self.assertTrue(await self.db.delete_night_template("r", "sample-app"))
        self.assertEqual(
            (await self.db.get_night_template("r", "sample-app"))["directiva"],
            "global")

    async def test_borrar_lo_que_no_existe_devuelve_false(self) -> None:
        self.assertFalse(await self.db.delete_night_template("nada"))

    async def test_listado_por_proyecto_pone_primero_las_especificas(self) -> None:
        await self.db.save_night_template("zzz-global", "g")
        await self.db.save_night_template("aaa-propia", "p",
                                          project_slug="sample-app")
        filas = await self.db.list_night_templates("sample-app")
        self.assertEqual([f["nombre"] for f in filas],
                         ["aaa-propia", "zzz-global"])


# --- 2. el veredicto llega al reporte ---------------------------------

def _worker() -> BranchWorker:
    return BranchWorker({"slug": "demo", "repo_path": ".", "defaults_json": {}},
                        NightConfig(), run_id="run_t")


class VeredictoTests(unittest.TestCase):

    def test_taskresult_arranca_sin_veredicto(self) -> None:
        """Vacío ≠ 'complete'. Una tarea que murió antes del experto no
        tiene veredicto, y el reporte tiene que poder decir eso."""
        r = TaskResult(task_id="T-001", status="done")
        self.assertEqual(r.verdict, "")
        self.assertIn("sin veredicto", night._verdict_icon(r.verdict))

    def test_el_worker_arranca_limpio(self) -> None:
        self.assertEqual(_worker()._last_verdict, "")

    def test_iconos_de_los_cuatro_veredictos(self) -> None:
        for v in ("complete", "needs_more", "off_plan", "needs_human"):
            self.assertIn(v, night._verdict_icon(v))

    def _md(self, results: list[TaskResult]) -> str:
        rep = MorningReporter(Path("."))
        return rep.build_md(
            run_id="run_t", project_slug="demo", started_at="x",
            deadline_at="y", end_reason="completed",
            tasks=[NightTask(r.task_id, f"tarea {r.task_id}", ["a.cs"])
                   for r in results],
            results=results, branch="night/x", pr_url="http://pr/1")

    def test_el_reporte_muestra_el_veredicto(self) -> None:
        md = self._md([TaskResult(task_id="T-001", status="done",
                                  verdict="complete")])
        self.assertIn("Veredicto", md)
        self.assertIn("✅ complete", md)

    def test_el_reporte_separa_los_que_pasaron_pero_dudan(self) -> None:
        """Lo que hace útil al veredicto: la lista corta de PRs a mirar
        primero. Commitearon (los gates pasaron) pero el juicio dudó."""
        md = self._md([
            TaskResult(task_id="T-001", status="done", verdict="complete"),
            TaskResult(task_id="T-002", status="done", verdict="needs_human",
                       verdict_feedback="falta decidir el naming"),
        ])
        self.assertIn("el verificador dudó", md)
        self.assertIn("T-002", md.split("el verificador dudó")[1])
        self.assertIn("falta decidir el naming", md)
        # El que salió limpio NO aparece en esa sección.
        self.assertNotIn("T-001", md.split("el verificador dudó")[1])

    def test_sin_dudosos_no_aparece_la_seccion(self) -> None:
        md = self._md([TaskResult(task_id="T-001", status="done",
                                  verdict="complete")])
        self.assertNotIn("el verificador dudó", md)

    def test_sin_veredicto_tampoco_cuenta_como_dudoso(self) -> None:
        """Vacío es 'no se juzgó', no 'se juzgó mal': no debe inflar la
        lista de revisión con tareas sobre las que no hay nada que decir."""
        md = self._md([TaskResult(task_id="T-001", status="done")])
        self.assertNotIn("el verificador dudó", md)


if __name__ == "__main__":
    unittest.main()
