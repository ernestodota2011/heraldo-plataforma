"""El gate documentacion <-> codigo (RF-31, CE-09, T-112), medido.

# WHY: nace de que un producto de referencia declaraba Tailwind en su propia
# documentacion sin usarlo en ningun sitio — una afirmacion sin nada que la
# sostenga. Aqui toda afirmacion de capacidad de `README.md` y `docs/*.md` cita,
# en la linea siguiente, el identificador EXACTO de la prueba que la respalda
# (`<!-- respalda: ruta::test -->`) o se declara honestamente
# `<!-- respalda: sin respaldo -->`. Este archivo prueba el MECANISMO
# (extraccion + verificacion, con un universo de nodeids FALSO e inyectado) y,
# aparte, mide la unica cosa que de verdad importa: que el README REAL de HOY
# cite pruebas que EXISTEN de verdad (el arbitro es `pytest`, no una lista
# escrita a mano — la leccion de P-44: un tercero ajeno al autor, o no hay
# testigo).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[3]
GUION = RAIZ / "scripts" / "documentacion_vs_codigo.py"


def _gate():
    """Carga `documentacion_vs_codigo.py` por ruta: no es un paquete importable.

    # WHY (se registra en `sys.modules` ANTES de ejecutar el modulo): `Cita` es
    # un dataclass `frozen, slots=True` con anotaciones diferidas
    # (`from __future__ import annotations`) — para resolverlas, `dataclasses`
    # busca `sys.modules[cls.__module__]`. Sin este registro el modulo se
    # ejecuta igual pero esa busqueda devuelve `None` y la creacion de la clase
    # revienta con un `AttributeError` que no tiene nada que ver con el gate.
    # Es el patron que la propia documentacion de `importlib` recomienda para
    # este caso.
    """
    especificacion = importlib.util.spec_from_file_location("documentacion_vs_codigo", GUION)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[especificacion.name] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


def test_el_guion_existe_donde_se_cree() -> None:
    """El control de todo lo demas: sin el archivo, cada prueba pasaria vacia."""
    assert GUION.is_file(), f"no existe {GUION}: el gate no esta donde se cree"


# --------------------------------------------------------------------------
# CONTROL sobre la REALIDAD: el README de HOY, contra lo que pytest recolecta
# --------------------------------------------------------------------------
def test_el_readme_real_no_tiene_afirmaciones_sin_respaldo() -> None:
    """La prueba que de verdad importa: cada cita del README apunta a algo real.

    # WHY (nodeids_reales() de verdad, sin monkeypatch): el arbitro es un
    # TERCERO ajeno a quien escribio el README — `pytest`, recolectando la
    # suite tal como esta HOY — y no una lista que el mismo autor repitio dos
    # veces (P-44). Es mas lento que las pruebas de abajo (levanta un
    # subproceso), y es exactamente el precio de medir contra la realidad.
    """
    gate = _gate()
    nodeids = gate.nodeids_reales()
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    faltas = gate.verificar_documento(texto, "README.md", nodeids)
    assert faltas == [], "el README cita algo que no existe:\n  " + "\n  ".join(faltas)


def test_hay_al_menos_una_cita_que_medir_en_el_readme_real() -> None:
    """Control del control: si la extraccion no viera ninguna, lo de arriba pasaria vacio."""
    gate = _gate()
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    citas = gate.citas_de(texto, "README.md")
    assert len(citas) >= 5, (
        f"el README solo tiene {len(citas)} cita(s): o se perdieron al editar, o la "
        "extraccion dejo de reconocer la forma del ancla"
    )


def test_los_documentos_derivan_del_directorio_docs(tmp_path, monkeypatch) -> None:
    """`docs/legal/` (T-030·quater) entrara solo, sin tocar este guion."""
    gate = _gate()
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    legal = tmp_path / "docs" / "legal"
    legal.mkdir()
    (legal / "promesas.md").write_text("b\n", encoding="utf-8")
    monkeypatch.setattr(gate, "RAIZ", tmp_path)
    encontrados = {r.relative_to(tmp_path).as_posix() for r in gate._documentos()}
    assert encontrados == {"README.md", "docs/a.md", "docs/legal/promesas.md"}


# --------------------------------------------------------------------------
# Extraccion pura de anclas: sin pytest real de por medio
# --------------------------------------------------------------------------
def test_una_cita_bien_formada_se_reconoce_con_su_linea() -> None:
    gate = _gate()
    texto = "El heraldo hace X.\n<!-- respalda: a/b.py::test_c -->\n"
    citas = gate.citas_de(texto, "x.md")
    assert len(citas) == 1
    assert citas[0].crudo == "a/b.py::test_c"
    assert citas[0].linea == 2
    assert citas[0].origen == "x.md"


def test_una_cita_admite_varias_pruebas_separadas_por_coma() -> None:
    gate = _gate()
    citas = gate.citas_de("<!-- respalda: a.py::t1, b.py::t2 -->\n", "x.md")
    assert citas[0].crudo == "a.py::t1, b.py::t2"


def test_una_cita_dentro_de_una_cita_en_bloque_se_reconoce() -> None:
    """El `>` de un `> [!danger]` de Obsidian/GitHub no esconde el ancla."""
    gate = _gate()
    citas = gate.citas_de("> algo que se afirma\n> <!-- respalda: a.py::t1 -->\n", "x.md")
    assert len(citas) == 1
    assert citas[0].crudo == "a.py::t1"


def test_una_cita_indentada_bajo_un_item_de_lista_se_reconoce() -> None:
    """La continuacion de un `- item` indenta con ESPACIOS, no con `>`.

    # WHY: lo encontro este mismo guard contra el README real (T-112). El
    # README cita pruebas en la linea siguiente a un sub-punto de una lista
    # (`- **texto.** ...\n  <!-- respalda: ... -->`), indentado con dos
    # espacios de continuacion — la misma indentacion que markdown exige para
    # que la linea siga perteneciendo al item. Antes de este caso, `_ANCLA`
    # solo toleraba el prefijo `>` de una cita en bloque; una indentacion de
    # ESPACIOS puros hacia que la linea NO matcheara en absoluto, y la cita
    # quedaba invisible para el gate: ni verificada, ni reportada como falta
    # — el mismo modo de fallo mudo que el propio RF-31 existe para cazar,
    # solo que aqui, adentro del cazador. Medido contra el README real: 5
    # citas de 16 (indentadas bajo un item) no se contaban antes del fix.
    """
    gate = _gate()
    texto = (
        "- **Una regla.** El texto de la regla continua\n"
        "  en la linea de abajo.\n"
        "  <!-- respalda: a.py::t1 -->\n"
    )
    citas = gate.citas_de(texto, "x.md")
    assert len(citas) == 1, (
        "una cita indentada con ESPACIOS bajo un item de lista no se reconocio: "
        "quedaria invisible para el gate, ni verificada ni reportada como falta"
    )
    assert citas[0].crudo == "a.py::t1"
    assert citas[0].linea == 3


def test_sin_ninguna_cita_la_extraccion_no_inventa_nada() -> None:
    gate = _gate()
    assert gate.citas_de("solo prosa, sin ancla ninguna.\n", "x.md") == []


def test_verificar_documento_acepta_una_cita_que_existe() -> None:
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::t1 -->\n"
    assert gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1"})) == []


def test_verificar_documento_acepta_varias_citas_si_todas_existen() -> None:
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::t1, b.py::t2 -->\n"
    assert gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1", "b.py::t2"})) == []


def test_si_una_de_varias_citas_no_existe_solo_esa_falla() -> None:
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::t1, a.py::fantasma -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1"}))
    assert len(faltas) == 1
    assert "a.py::fantasma" in faltas[0]
    assert "a.py::t1" not in faltas[0].split("cita a")[1]


def test_una_coma_doble_deja_un_identificador_vacio_entre_medio() -> None:
    """`a.py::t1,, b.py::t2` — el hueco entre las dos comas no es "ninguna cita".

    # WHY (hallazgo de Crisol, T-112): el codigo YA manejaba este caso
    # (`if not nodeid: faltas.append(...)`) desde el primer commit, pero nada
    # en esta bateria lo ejercitaba — exactamente el punto ciego que P-44 ya
    # nombro para OTRO guard: que el mecanismo haga lo correcto no vale nada
    # si ninguna prueba lo comprueba.
    """
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::t1,, b.py::t2 -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1", "b.py::t2"}))
    # CONTROL incluido en la misma aserción: si los dos nodeids REALES (a los
    # dos lados del hueco) generaran su propia falta, aqui habria 3, no 1 — la
    # unica falta real es la del hueco entre las dos comas.
    assert len(faltas) == 1 and "vacio" in faltas[0]


def test_una_cita_de_solo_espacios_entre_comas_tambien_es_un_hueco() -> None:
    """`a.py::t1,   , b.py::t2` — espacios no son un nodeid tras recortarlos."""
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::t1,   , b.py::t2 -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1", "b.py::t2"}))
    assert len(faltas) == 1 and "vacio" in faltas[0]


# --------------------------------------------------------------------------
# SABOTAJES — los tres que el troceo exige, y el que da nombre al gate
# --------------------------------------------------------------------------
def test_sabotaje_una_cita_a_una_prueba_inexistente_sale_en_rojo() -> None:
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::no_existe -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1"}))
    assert faltas and "a.py::no_existe" in faltas[0] and "no existe" in faltas[0]


def test_sabotaje_una_afirmacion_marcada_sin_cita_sale_en_rojo() -> None:
    """El ancla esta — alguien SI marco esto como afirmacion — pero no dice nada."""
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda:  -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset({"a.py::t1"}))
    assert faltas and "VACIA" in faltas[0]


def test_sabotaje_una_prueba_citada_que_se_renombra_sale_en_rojo() -> None:
    """Ayer `nodeids_viejos` la tenia; hoy, tras el rename, `nodeids_nuevos` no."""
    gate = _gate()
    texto = "afirmacion.\n<!-- respalda: a.py::test_viejo -->\n"
    nodeids_de_ayer = frozenset({"a.py::test_viejo"})
    nodeids_de_hoy = frozenset({"a.py::test_nuevo"})  # el rename ya paso
    assert gate.verificar_documento(texto, "x.md", nodeids_de_ayer) == [], (
        "el control fallo: con el nodeid viejo TODAVIA vivo, no deberia haber falta"
    )
    faltas = gate.verificar_documento(texto, "x.md", nodeids_de_hoy)
    assert faltas and "a.py::test_viejo" in faltas[0]


def test_una_afirmacion_marcada_sin_respaldo_sigue_contando_como_falta() -> None:
    """'sin respaldo' es HONESTO, no es un pase libre: la deuda sigue siendo deuda.

    # WHY: si esto no contara como falta, cualquiera podria callar un `NO-GO`
    # escribiendo `sin respaldo` en todo — el gate reporta afirmaciones sin
    # respaldo en ROJO (RF-31), venga la falta de una cita inventada o de una
    # declarada. La diferencia es solo el MENSAJE, para que quien lea el reporte
    # distinga "esto es un descuido" de "esto es una brecha ya conocida".
    """
    gate = _gate()
    texto = "afirmacion sin nada que la respalde.\n<!-- respalda: sin respaldo -->\n"
    faltas = gate.verificar_documento(texto, "x.md", frozenset())
    assert faltas and "sin respaldo" in faltas[0].lower()


def test_una_cita_dentro_de_un_bloque_de_codigo_no_cuenta() -> None:
    """El mismo cuidado que `test_cimiento.py` tiene con `ci.yml`.

    # WHY (`feedback_sabotaje_audita_al_test`): un ancla que aparece SOLO para
    # ENSEÑAR la convencion, dentro de un bloque ``` de un documento, no puede
    # hacerse pasar por una cita real — si lo hiciera, este mismo README podria
    # "demostrar" tener una cita rota sin que nadie la escribiera en serio.
    """
    gate = _gate()
    dentro_de_bloque = (
        "Asi se cita una prueba:\n\n```\n<!-- respalda: a.py::no_existe -->\n```\n"
    )
    assert gate.citas_de(dentro_de_bloque, "x.md") == [], (
        "una cita DENTRO de un bloque de codigo se conto como si fuera real"
    )
    # CONTROL: la MISMA linea, fuera del bloque, si cuenta. Sin esto, un
    # extractor que no reconociera NINGUN ancla pasaria la aserción de arriba.
    fuera_de_bloque = "Asi se cita una prueba:\n\n<!-- respalda: a.py::no_existe -->\n"
    assert len(gate.citas_de(fuera_de_bloque, "x.md")) == 1


def test_dos_bloques_de_codigo_dejan_la_cita_de_en_medio_visible() -> None:
    """Abrir y cerrar dos veces no deja el estado "dentro" pegado."""
    gate = _gate()
    texto = (
        "```\nejemplo 1\n```\n"
        "<!-- respalda: a.py::t1 -->\n"
        "```\nejemplo 2\n```\n"
    )
    citas = gate.citas_de(texto, "x.md")
    assert len(citas) == 1 and citas[0].crudo == "a.py::t1"


def test_un_bloque_delimitado_con_virgulillas_tambien_oculta_su_cita() -> None:
    """Markdown admite `~~~` ademas de tres backticks (hallazgo de Crisol, T-112).

    # WHY: el resto de esta bateria solo ejercitaba el delimitador de
    # backticks porque es el unico que este README usa hoy — pero la
    # convencion RF-31 se aplica a `docs/*.md` en general, y un documento
    # futuro que use `~~~` (CommonMark lo admite igual) no puede convertir su
    # ejemplo de la convencion en una cita real solo por elegir el otro
    # delimitador.
    """
    gate = _gate()
    dentro = "Ejemplo:\n\n~~~\n<!-- respalda: a.py::no_existe -->\n~~~\n"
    assert gate.citas_de(dentro, "x.md") == [], (
        "una cita dentro de un bloque ~~~ se conto como si fuera real"
    )
    # CONTROL: la MISMA linea, fuera del bloque, si cuenta.
    fuera = "Ejemplo:\n\n<!-- respalda: a.py::no_existe -->\n"
    assert len(gate.citas_de(fuera, "x.md")) == 1


# --------------------------------------------------------------------------
# `nodeids_reales()` — la unica funcion que abre un proceso
# --------------------------------------------------------------------------
class _Recoleccion:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_nodeids_reales_extrae_solo_las_lineas_con_nodeid(monkeypatch) -> None:
    gate = _gate()
    salida = "a.py::test_1\nb.py::test_2[caso]\n\n2 tests collected in 0.01s\n"
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: _Recoleccion(0, salida)
    )
    assert gate.nodeids_reales() == frozenset({"a.py::test_1", "b.py::test_2[caso]"})


def test_nodeids_reales_admite_espacios_dentro_del_parametrize(monkeypatch) -> None:
    """72 casos REALES de este repositorio traen espacios en su `[id]` (Crisol, T-112).

    # WHY: la sugerencia de Crisol de filtrar por caracteres permitidos
    # (`[\\w\\[\\]\\-]+`) se probo contra la coleccion REAL antes de adoptarla y
    # HABRIA excluido 72 nodeids verdaderos —`test_el_tamiz_dispara_donde_debe
    # [Policlinico Norte]`, por ejemplo— convirtiendolos en invisibles para el
    # gate (`feedback_no_propagar_sin_verificar`: el hallazgo era una hipotesis,
    # y la fix concreta que traia no sobrevivio la medicion). Esta prueba fija
    # que un espacio en el `[id]` no descarta la linea.
    """
    gate = _gate()
    salida = "a.py::test_con_espacio[Policlinico Norte]\n1 test collected in 0.01s\n"
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Recoleccion(0, salida))
    assert gate.nodeids_reales() == frozenset({"a.py::test_con_espacio[Policlinico Norte]"})


def test_nodeids_reales_descarta_una_linea_de_aviso_que_mencione_dos_puntos_dobles(
    monkeypatch,
) -> None:
    """Una linea de warning/traceback con "::" en la PROSA no es un nodeid (Crisol).

    # WHY: el filtro viejo (`"::" in linea`) aceptaria esta linea porque
    # contiene la subcadena, aunque no tenga la FORMA de un nodeid real (no
    # empieza en un `archivo.py` seguido de `::`). El filtro nuevo exige esa
    # forma desde el primer caracter de la linea.
    """
    gate = _gate()
    salida = (
        "a.py::test_1\n"
        "DeprecationWarning: usa Modulo::Clase en vez de la forma vieja\n"
        "1 test collected in 0.01s\n"
    )
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _Recoleccion(0, salida))
    assert gate.nodeids_reales() == frozenset({"a.py::test_1"})


def test_nodeids_reales_falla_si_pytest_no_pudo_recolectar(monkeypatch) -> None:
    """Un `returncode` distinto de 0 es RUIDOSO — nunca "cero pruebas encontradas"."""
    gate = _gate()
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: _Recoleccion(2, "errors", "boom")
    )
    with pytest.raises(gate.RecoleccionFallida):
        gate.nodeids_reales()


def test_nodeids_reales_falla_si_la_recoleccion_devuelve_cero_pruebas(monkeypatch) -> None:
    """Fail-closed: un conjunto vacio aprobaria CUALQUIER cita por ausencia."""
    gate = _gate()
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: _Recoleccion(0, "no hay nada aqui\n")
    )
    with pytest.raises(gate.RecoleccionFallida):
        gate.nodeids_reales()


def test_nodeids_reales_invoca_el_mismo_interprete_sin_addopts_duplicado(monkeypatch) -> None:
    """`sys.executable -m pytest`, con `addopts` anulado: ver el WHY del modulo."""
    gate = _gate()
    capturado: dict = {}

    def _falso_run(comando, **kwargs):
        capturado["comando"] = comando
        return _Recoleccion(0, "a.py::t1\n")

    monkeypatch.setattr(gate.subprocess, "run", _falso_run)
    gate.nodeids_reales()
    comando = capturado["comando"]
    assert comando[0] == gate.sys.executable
    assert "-o" in comando and comando[comando.index("-o") + 1] == "addopts="


def test_nodeids_reales_declara_un_timeout_al_subproceso(monkeypatch) -> None:
    """Sin techo de tiempo, una recoleccion colgada cuelga el CI entero (Crisol, T-112).

    # WHY: `subprocess.run` sin `timeout=` espera para SIEMPRE si `pytest
    # --collect-only` se cuelga (un conftest con un import circular, un fixture
    # de sesion que abre una conexion y nunca la suelta). El job de CI tiene su
    # propio `timeout-minutes: 15` como red de ultimo recurso, pero eso hace
    # que el paso entero salga "cancelado" sin decir POR QUE — exactamente el
    # patron que la Regla de Oro de la casa prohibe para procesos de larga
    # duracion (timeout declarado, nunca implicito).
    """
    gate = _gate()
    capturado: dict = {}

    def _falso_run(comando, **kwargs):
        capturado["kwargs"] = kwargs
        return _Recoleccion(0, "a.py::t1\n")

    monkeypatch.setattr(gate.subprocess, "run", _falso_run)
    gate.nodeids_reales()
    assert capturado["kwargs"].get("timeout"), (
        "`subprocess.run` se invoco sin `timeout=`: una recoleccion colgada "
        "colgaria este guion (y el paso de CI) sin limite"
    )


def test_nodeids_reales_falla_con_mensaje_claro_si_el_subproceso_se_cuelga(monkeypatch) -> None:
    """Un cuelgue real se traduce a `RecoleccionFallida`, no a un traceback crudo."""
    import subprocess as subprocess_real

    gate = _gate()

    def _cuelgue(comando, **kwargs):
        raise subprocess_real.TimeoutExpired(cmd=comando, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(gate.subprocess, "run", _cuelgue)
    with pytest.raises(gate.RecoleccionFallida) as capturado:
        gate.nodeids_reales()
    assert "tiempo" in str(capturado.value).lower() or "timeout" in str(capturado.value).lower()
