"""Gate de DOCUMENTACION CONTRA CODIGO (RF-31, CE-09, T-112).

Nace de un defecto real de un producto de referencia: su documentacion declaraba
Tailwind como parte del stack y el codigo no lo usaba en ningun sitio. Nadie lo
media porque no habia NADA que comparara lo escrito con lo real — la
documentacion y el codigo podian divergir en silencio, para siempre. Este gate
existe para que eso sea mecanicamente imposible aqui: toda afirmacion de
capacidad de la documentacion versionada CITA la prueba que la respalda, y este
guion verifica que esa prueba EXISTE de verdad, hoy, contra lo que `pytest`
recolecta — no contra lo que alguien recuerda haber escrito.

# WHY (la convencion elegida — comentario HTML, no una tabla aparte en
`docs/afirmaciones.md`): un comentario HTML es invisible en el render (GitHub,
Outline, cualquier lector de Markdown), asi que no rompe la prosa que ya existe
ni obliga a reescribir el README como una tabla — este repositorio se escribe
para personas primero (`feedback_dialecto_espanol_neutro`, doctrina de la casa).
Y vive PEGADO a la afirmacion que respalda: quien lee "las dos sondas contestan
distinto" ve, en la linea siguiente, exactamente que prueba lo demuestra, sin
saltar a un archivo aparte a buscar la fila que le toca. Una tabla aparte tiene
el problema de T-021·quater con la lista a mano: dos ediciones que tocan la
afirmacion y la fila del respaldo por separado divergen sin que nada lo note.
Con el ancla EN la linea, mover o borrar la afirmacion mueve o borra su cita a
la vez. Y generaliza gratis a una tabla futura (T-030·quater, `docs/legal/`):
este guion no mira la ESTRUCTURA Markdown, mira el TEXTO linea por linea —un
ancla dentro de una celda de tabla se reconoce exactamente igual que una dentro
de un parrafo o una cita en bloque.

# WHY (la forma exacta): `<!-- respalda: <nodeid>[, <nodeid>...] -->`, sola en
su propia linea (puede llevar el prefijo `>` de una cita en bloque delante,
tantas veces como niveles de anidamiento, y/o la indentacion de ESPACIOS de la
continuacion de un `- item` de lista — las dos formas de anidamiento que trae
Markdown, y el README real usa ambas). Cuando de verdad no hay ninguna
prueba que respalde la afirmacion, la forma es `<!-- respalda: sin respaldo -->`
— nunca se omite el ancla y nunca se inventa una cita. La PRESENCIA del ancla es
lo que marca "esto es una afirmacion de capacidad"; no hay una lista aparte de
"que secciones cuentan", porque esa lista seria, otra vez, escrita a mano y
divergente (`feedback_mecanismo_cableado_a_uno`). Un ancla con la carga VACIA
(`<!-- respalda: -->`) es justo el caso "afirmacion marcada, sin cita": no se
ignora, se reporta.

# WHY (nodeid EXACTO, no un prefijo): un identificador de una prueba
PARAMETRIZADA (`test_x[caso]`) es un nodeid completo y distinto de `test_x` a
secas — pytest jamas recolecta `test_x` sola si esta parametrizada. Verificar
por prefijo dejaria pasar una cita a una funcion que nunca existio con ese
nombre exacto, con tal de que EMPEZARA igual que una real. La documentacion que
cita una prueba parametrizada cita el caso concreto entre corchetes.

# WHY (una cita dentro de un bloque de codigo no cuenta): el mismo cuidado que
`test_cimiento.py` tiene con `ci.yml` (`feedback_sabotaje_audita_al_test`) — si
este documento, para EXPLICAR la convencion, muestra un ejemplo de ancla dentro
de un bloque ```, esa muestra no puede colarse como una cita real. Se reconoce
por lineas, alternando dentro/fuera de un bloque delimitado por lineas que
empiezan con tres o mas backticks; no hace falta un parser de Markdown completo
para esto.

# WHY (`-o addopts=""` al recolectar): el `pyproject.toml` de este repositorio
ya declara `addopts = "-q --strict-markers"`. Pedir `--collect-only -q` por
encima de eso da DOS `-q`, y con `pytest` eso colapsa la salida a un resumen por
archivo (`archivo.py: N`) en vez de un nodeid por linea — medido al escribir
este guion. Anular `addopts` desde la propia invocacion hace que la salida sea
la misma pase lo que pase con `addopts` en el futuro, en vez de depender de que
nadie le añada una `-v` o una segunda `-q`.

# LIMITE DECLARADO (pregunta de Crisol, T-112): este guion mide que el nodeid
CITADO EXISTE en la coleccion de hoy — no que la prueba PASE, ni que su cuerpo
siga diciendo lo que la afirmacion dice. Eso es a proposito, no un descuido: no
hay forma barata de saber si una prueba pasa sin CORRERLA, y correr la suite
entera aqui duplicaria exactamente lo que el paso "Suite" del mismo trabajo de
CI ya hace, un paso despues. La garantia real es la SUMA de los dos pasos, en
el mismo `ci.yml`, en el mismo job: este descarta que una cita apunte a algo
que nunca existio o se borro/renombro; el otro exige que TODO lo que existe,
incluida la prueba citada, pase. Un cambio que rompiera la prueba citada
pondria el paso "Suite" en rojo, no este.

Uso:
    uv run --no-sync python scripts/documentacion_vs_codigo.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

#: El marcador de una deuda declarada, nunca de una cita inventada.
MARCADOR_SIN_RESPALDO = "sin respaldo"

#: El ancla, sola en su linea (con o sin el `>` de una cita en bloque delante,
#: y con o sin la indentacion de ESPACIOS de un item de lista). El `\s*` inicial
#: cubre esa segunda forma: sin el, una linea como "  <!-- respalda: ... -->"
#: (la continuacion de un `- item`) no matchea en absoluto y la cita queda
#: invisible para el gate — el bug que encontro el test de la cita indentada
#: contra el propio README de este repositorio (5 de 16 citas reales, mudas).
#: El grupo capturado es la carga CRUDA, sin recortar espacios de sobra.
_ANCLA = re.compile(r"^\s*(?:>\s*)*<!--\s*respalda:\s*(.*?)\s*-->\s*$")

#: Una linea que ABRE o CIERRA un bloque de codigo delimitado por backticks O
#: por virgulillas (Markdown/CommonMark admite las dos formas). No distingue
#: abrir de cerrar a proposito: alternar sirve igual y no exige que la marca de
#: cierre repita el lenguaje de apertura (```python ... ```), ni que las dos
#: marcas usen el MISMO delimitador (hallazgo de Crisol, T-112: un `docs/*.md`
#: futuro que use `~~~` en vez de backticks no puede convertir su ejemplo de la
#: convencion en una cita real solo por elegir el otro delimitador).
_CERCA_DE_BLOQUE = re.compile(r"^\s*(?:```|~~~)")


@dataclass(frozen=True, slots=True)
class Cita:
    """Un ancla `respalda:` encontrada en un documento, con su carga cruda."""

    origen: str
    linea: int
    crudo: str


def citas_de(texto: str, origen: str) -> list[Cita]:
    """Todas las citas del documento, ignorando las que caen dentro de un bloque.

    # WHY (por lineas, no con una expresion regular sobre el texto entero): la
    # primera version de un guion hermano (`test_cimiento.py`) cometio
    # exactamente este error con `ci.yml` y un comentario que EXPLICABA el gate
    # conto como si fuera el paso real. Aqui se recorre linea a linea y se lleva
    # un estado ("¿estoy dentro de un bloque?") para que la prosa que ENSEÑA la
    # convencion no se cuente como una cita de verdad.
    """
    citas: list[Cita] = []
    dentro_de_bloque = False
    for numero, linea in enumerate(texto.splitlines(), 1):
        if _CERCA_DE_BLOQUE.match(linea):
            dentro_de_bloque = not dentro_de_bloque
            continue
        if dentro_de_bloque:
            continue
        encontrada = _ANCLA.match(linea)
        if encontrada:
            citas.append(Cita(origen=origen, linea=numero, crudo=encontrada.group(1)))
    return citas


def verificar_documento(texto: str, origen: str, nodeids: frozenset[str]) -> list[str]:
    """Las faltas de un documento: ancla vacia, "sin respaldo" declarado, o cita rota.

    `nodeids` es el universo de la VERDAD — lo que `pytest` recolecta hoy — y se
    recibe como parametro a proposito: permite verificar la logica de comparacion
    sin levantar un `pytest --collect-only` real por cada caso (`nodeids_reales`
    es la UNICA funcion de este modulo que abre un proceso).
    """
    faltas: list[str] = []
    for cita in citas_de(texto, origen):
        if not cita.crudo:
            faltas.append(
                f"{cita.origen}:{cita.linea}: afirmacion marcada con un ancla "
                "`respalda:` VACIA — no nombra ninguna prueba ni dice "
                f"'{MARCADOR_SIN_RESPALDO}'"
            )
            continue
        if cita.crudo == MARCADOR_SIN_RESPALDO:
            faltas.append(
                f"{cita.origen}:{cita.linea}: afirmacion marcada explicitamente "
                f"'{MARCADOR_SIN_RESPALDO}' — deuda declarada, no inventada, y "
                "sigue sin tener una prueba que la respalde"
            )
            continue
        for tramo in cita.crudo.split(","):
            nodeid = tramo.strip()
            if not nodeid:
                faltas.append(
                    f"{cita.origen}:{cita.linea}: la cita trae un identificador "
                    f"vacio entre comas ({cita.crudo!r})"
                )
                continue
            if nodeid not in nodeids:
                faltas.append(
                    f"{cita.origen}:{cita.linea}: cita a {nodeid!r}, que no existe "
                    "en la suite recolectada hoy (¿se borro, se renombro, o nunca "
                    "existio?)"
                )
    return faltas


class RecoleccionFallida(RuntimeError):
    """`pytest` no pudo decir que pruebas existen: no hay verdad contra la que medir."""


#: Segundos de margen antes de dar la recoleccion por colgada (hallazgo de
#: Crisol, T-112). Sin este techo, un `conftest.py` con un import circular o un
#: fixture de sesion que abre una conexion y no la suelta cuelga este guion
#: PARA SIEMPRE — y con el, el paso de CI, que solo tiene el `timeout-minutes:
#: 15` del trabajo entero como red de ultimo recurso (cancela el paso sin decir
#: POR QUE). Es la misma Regla de Oro que ya aplica al resto de la casa: los
#: procesos de larga duracion llevan timeout declarado, nunca implicito. 120s
#: es generoso frente a lo medido en este repositorio (~6s con 890 pruebas):
#: sobra margen para una maquina de CI mas lenta sin acercarse al limite del
#: trabajo.
TIMEOUT_RECOLECCION_SEGUNDOS = 120

#: Una linea con la FORMA de un nodeid real: un archivo `.py` sin espacios,
#: seguido de `::` y de lo que sea (los `[id]` de un parametrize SI pueden
#: llevar espacios — ver el WHY de abajo). Reemplaza al filtro viejo
#: (`"::" in linea`), que aceptaria como nodeid cualquier linea de aviso o
#: traceback que mencionara "::" en su PROSA sin tener esa forma.
#:
#: WHY (por que no restringir los caracteres del `[id]`, como sugirio Crisol):
#: se midio contra la coleccion real de este repositorio antes de adoptar una
#: forma mas estricta (`feedback_no_propagar_sin_verificar`) y 72 nodeids
#: REALES traen espacios en su parametrizacion
#: (`test_el_tamiz_dispara_donde_debe[Policlinico Norte]`, por ejemplo) — una
#: forma que solo admitiera `[\w\[\]-]+` los habria vuelto invisibles para el
#: gate. Aqui solo se exige que el PREFIJO (hasta el primer `::`) no tenga
#: espacios y termine en `.py`; el resto de la linea admite lo que sea.
_LINEA_DE_NODEID = re.compile(r"^\S+\.py::.+$")


def nodeids_reales() -> frozenset[str]:
    """El universo de la verdad de HOY: lo que `pytest --collect-only` devuelve.

    # WHY (`sys.executable -m pytest`, no `shutil.which("pytest")` ni `uv run`):
    # este guion ya corre DENTRO del interprete que `uv sync` preparo — invocar
    # ese mismo interprete en `-m pytest` garantiza el pytest instalado en ESE
    # entorno, sin depender de que "pytest" resuelva a lo mismo en el PATH ni de
    # pagar una segunda resolucion de `uv`.
    """
    try:
        resultado = subprocess.run(  # noqa: S603 (argv fijo, sys.executable resuelto por Python)
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_RECOLECCION_SEGUNDOS,
        )
    except subprocess.TimeoutExpired as colgado:
        raise RecoleccionFallida(
            f"`pytest --collect-only` no termino en {TIMEOUT_RECOLECCION_SEGUNDOS}s "
            "(tiempo de espera agotado): parece colgado, no lento. No se pudo "
            "determinar que pruebas existen de verdad."
        ) from colgado
    if resultado.returncode != 0:
        raise RecoleccionFallida(
            f"`pytest --collect-only` termino con codigo {resultado.returncode}: "
            "no se pudo determinar que pruebas existen de verdad.\n"
            f"--- salida ---\n{resultado.stdout}\n--- errores ---\n{resultado.stderr}"
        )
    nodeids = frozenset(
        linea for linea in resultado.stdout.splitlines() if _LINEA_DE_NODEID.match(linea)
    )
    if not nodeids:
        raise RecoleccionFallida(
            "`pytest --collect-only` no recolecto NINGUNA prueba: el gate no "
            "puede verificar nada contra un conjunto vacio (aprobar aqui seria "
            "aprobar por ausencia)"
        )
    return nodeids


def _documentos() -> list[Path]:
    """README.md + todo `docs/*.md`. Se DERIVA del directorio, no se enumera.

    # WHY: es lo que hace que un `docs/legal/` futuro (T-030·quater) entre solo
    # con tal de vivir bajo `docs/` — nada que cablear aqui para que su tabla
    # promesa -> evidencia empiece a verificarse con este mismo mecanismo.

    # WHY (por que NO el arbol entero, pregunta de Crisol): el universo son las
    # dos raices que la propia Regla de la casa nombra como "documentacion
    # versionada" (README.md + docs/*.md, ver el bullet de "Reglas de la casa"
    # del propio README) — el texto que describe CAPACIDADES del sistema para
    # quien lo lee. `SECURITY.md`, `LICENSE` y `CHANGELOG*` son documentos
    # legales/de proceso de GitHub, no afirmaciones tecnicas sobre lo que este
    # software HACE; meterlos aqui exigiria decidir, archivo por archivo, cual
    # de sus frases es una "afirmacion de capacidad" y cual no lo es —
    # exactamente la lista escrita a mano que este mismo modulo evita en todos
    # los demas ejes (P-51 confirma la frontera: RF-31 alcanza al texto que
    # describe el sistema, no al que todavia no esta servido). Un documento
    # nuevo que SI describa capacidades entra solo con tal de vivir bajo
    # `docs/`, que es la generalizacion que este WHY ya declaraba.
    """
    rutas = [RAIZ / "README.md", *sorted(RAIZ.glob("docs/**/*.md"))]
    return [ruta for ruta in rutas if ruta.is_file()]


#: Directorios cuyo contenido nunca es documentacion versionada DE ESTE
#: repositorio: dependencias instaladas, control de version, artefactos. Un
#: `.venv` puede traer miles de `.md` de paquetes de terceros.
_DIRECTORIOS_EXENTOS_DE_COBERTURA = frozenset(
    {".venv", ".git", "node_modules", ".pytest_cache", "dist", "build", "__pycache__"}
)


def _fuera_de_cobertura() -> list[Path]:
    """Todo `.md` del arbol que este gate NO revisa — el universo del riesgo mudo.

    Hoy son reales: `SECURITY.md`, y los README de marcador de posicion bajo
    `apps/web/` y `packages/review/`. Ninguno usa la convencion todavia, y esta
    funcion es lo que se encarga de que, si algun dia alguno la usara SIN vivir
    bajo `docs/`, el gate lo note en vez de dejarlo pasar en silencio.
    """
    cubiertos = {ruta.resolve() for ruta in _documentos()}
    return [
        ruta
        for ruta in RAIZ.glob("**/*.md")
        if ruta.is_file()
        and not _DIRECTORIOS_EXENTOS_DE_COBERTURA & set(ruta.relative_to(RAIZ).parts)
        and ruta.resolve() not in cubiertos
    ]


def verificar_cobertura() -> list[str]:
    """Falta si algun `.md` FUERA de README+docs/ usa la sintaxis `respalda:`.

    # WHY (hallazgo de Crisol, T-112): un ancla escrita por error —o a
    # propósito, por alguien que no conoce el limite— en `SECURITY.md` o en el
    # README de un paquete nuevo APARENTA estar verificada: tiene la sintaxis
    # exacta de la convencion. Y el resto de este guion nunca la mira, porque
    # `_documentos()` no la enumera. Es el mismo fallo mudo que el resto de la
    # casa nombra una y otra vez —"parece protegido y no lo esta"— reproducido
    # DENTRO del propio mecanismo que existe para cazarlo. Esta funcion no
    # revisa esos documentos (siguen fuera del contrato de RF-31): solo exige
    # que ninguno use la sintaxis, para que nadie confie en una cita que nunca
    # se comprueba.
    """
    faltas: list[str] = []
    for ruta in _fuera_de_cobertura():
        origen = ruta.relative_to(RAIZ).as_posix()
        texto = ruta.read_text(encoding="utf-8")
        citas = citas_de(texto, origen)
        if citas:
            lineas = ", ".join(str(cita.linea) for cita in citas)
            faltas.append(
                f"{origen}: usa el ancla `respalda:` en la(s) linea(s) {lineas}, "
                "fuera de README.md/docs/*.md — este gate no lo revisa, asi que "
                "esa cita APARENTA estar verificada y nunca se comprueba. "
                "Muevela bajo docs/, o quita la sintaxis si no es una cita real"
            )
    return faltas


def main() -> int:
    documentos = _documentos()
    if not documentos:
        print("el gate no encontro ningun documento que revisar", file=sys.stderr)
        return 1

    try:
        nodeids = nodeids_reales()
    except RecoleccionFallida as fallo:
        print(f"GATE DE DOCUMENTACION EN ROJO — {fallo}", file=sys.stderr)
        return 1

    faltas: list[str] = []
    total_citas = 0
    for documento in documentos:
        origen = documento.relative_to(RAIZ).as_posix()
        texto = documento.read_text(encoding="utf-8")
        total_citas += len(citas_de(texto, origen))
        faltas.extend(verificar_documento(texto, origen, nodeids))

    faltas.extend(verificar_cobertura())

    if faltas:
        print(
            f"GATE DE DOCUMENTACION EN ROJO — {len(faltas)} afirmacion(es) sin "
            "respaldo real:",
            file=sys.stderr,
        )
        for falta in faltas:
            print(f"  {falta}", file=sys.stderr)
        print(
            "\nCada afirmacion de capacidad de la documentacion cita, en la linea "
            "siguiente, la prueba que la respalda: `<!-- respalda: "
            "ruta/al/archivo.py::test_nombre -->`. Si de verdad no hay ninguna, la "
            "cita dice `<!-- respalda: sin respaldo -->` — nunca se inventa.",
            file=sys.stderr,
        )
        return 1

    print(
        f"gate de documentacion: {len(documentos)} documento(s), {total_citas} "
        "cita(s), limpio"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
