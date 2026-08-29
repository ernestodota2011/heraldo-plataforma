"""Cimiento: el punto unico de salida es un paquete de primer nivel y TODO
servicio lo declara como dependencia.

# WHY: L-07. Si `packages/egress` viviera dentro de `apps/api`, el worker —que
# corre como servicio separado en Compose— acabaria COPIANDOLO, y una copia es
# el segundo camino de salida que el diseno prohibe (D-17, T-119-bis).
# Declararlo no basta si nadie comprueba que se declaro: la lista de servicios
# se DERIVA del workspace, no se escribe a mano, para que un servicio nuevo que
# se olvide de declararlo ponga el CI en rojo en vez de nacer con su copia
# (leccion `feedback_mecanismo_cableado_a_uno`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[3]
PAQUETE_DE_SALIDA = "heraldo-egress"


def _leer(ruta: Path) -> dict:
    with ruta.open("rb") as fh:
        return tomllib.load(fh)


def _miembros_del_workspace() -> list[Path]:
    raiz = _leer(RAIZ / "pyproject.toml")
    patrones = raiz["tool"]["uv"]["workspace"]["members"]
    miembros: list[Path] = []
    for patron in patrones:
        miembros.extend(sorted(RAIZ.glob(patron)))
    return miembros


def test_el_paquete_de_salida_es_de_primer_nivel() -> None:
    """Vive en `packages/`, no dentro de ningun servicio de `apps/`."""
    assert (RAIZ / "packages" / "egress" / "pyproject.toml").is_file()
    assert not list((RAIZ / "apps").rglob("egress/pyproject.toml"))


def test_el_paquete_de_salida_se_importa() -> None:
    """Resuelto desde el workspace, no copiado en el arbol de un servicio."""
    import egress

    assert Path(egress.__file__).resolve().parent == (RAIZ / "packages" / "egress").resolve()


def test_hay_servicios_que_medir() -> None:
    """Una medida que no encuentra nada que medir es ROJA, no verde."""
    servicios = [m for m in _miembros_del_workspace() if m.parent.name == "apps"]
    assert servicios, "el workspace no declara ningun servicio bajo apps/"


def test_todo_servicio_declara_el_paquete_de_salida() -> None:
    """Derivado del workspace: un servicio nuevo que lo olvide sale en rojo."""
    for servicio in _miembros_del_workspace():
        if servicio.parent.name != "apps":
            continue
        proyecto = _leer(servicio / "pyproject.toml")["project"]
        declaradas = {d.split("[")[0].split(">")[0].split("=")[0].strip().lower()
                      for d in proyecto.get("dependencies", [])}
        assert PAQUETE_DE_SALIDA in declaradas, (
            f"{servicio.name} no declara {PAQUETE_DE_SALIDA} como dependencia: "
            "acabaria copiando el punto unico de salida (L-07)"
        )


# --------------------------------------------------------------------------
# D-10: la historia de migraciones es LINEAL, y eso se comprueba
# --------------------------------------------------------------------------
# WHY: «Alembic, historia lineal» era una decision escrita en el plan y nada la
# sostenia. Dos ramas de migracion conviviendo dan DOS cabezas: `upgrade head`
# falla en el despliegue, no aqui — y el sitio donde falla decide cuanto cuesta.
# Lo unico que el referente hace impecable son sus 17 migraciones lineales; se
# copia el rigor, pero cableado.
INI_ALEMBIC = RAIZ / "apps" / "api" / "migrations" / "alembic.ini"


def _guion_de_migraciones():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(INI_ALEMBIC)))


def test_hay_migraciones_que_medir() -> None:
    """Una historia vacia es 'lineal' por vacuidad. Eso no cuenta."""
    revisiones = list(_guion_de_migraciones().walk_revisions())
    assert revisiones, "no hay ninguna migracion: el modelo de datos no existe"


def test_la_historia_de_migraciones_es_lineal() -> None:
    """Una sola cabeza, y ninguna revision con dos padres (D-10)."""
    guion = _guion_de_migraciones()
    cabezas = guion.get_heads()
    assert len(cabezas) == 1, (
        f"la historia de migraciones tiene {len(cabezas)} cabezas ({cabezas}): "
        "`upgrade head` fallaria en el despliegue, no aqui"
    )
    for revision in guion.walk_revisions():
        padres = revision.down_revision
        if padres is None:
            continue
        assert isinstance(padres, str), (
            f"la revision {revision.revision} tiene varios padres ({padres}): "
            "eso es una fusion de ramas, y la historia deja de ser lineal"
        )


# --------------------------------------------------------------------------
# Una sola redaccion VIGENTE: el generador no puede divergir del SQL aplicado
# --------------------------------------------------------------------------
# WHY: las migraciones llevan el SQL CONGELADO literal, para que una revision ya
# aplicada no cambie de significado cuando alguien edite `app/tenancy`. Ese
# congelado compra reproducibilidad y, a cambio, abre la puerta a lo contrario:
# que `politicas.py` y el SQL que de verdad gobierna la base se separen sin que
# nadie se entere, y que el generador quede de adorno.
#
# Este guard cierra esa puerta. Para cada bloque congelado toma la revision MAS
# RECIENTE que lo declara —su redaccion VIGENTE—, le vuelve a pedir al generador
# la misma receta y exige salida IDENTICA. Tocar `politicas.py` o `rol.py` sin
# escribir una migracion nueva = ROJO. Las revisiones viejas se quedan como
# estan: eso es historia, no deuda.
#
# WHY (`feedback_mecanismo_cableado_a_uno`): el despachador de abajo LANZA ante
# un generador que no conoce, en vez de saltarselo. Un guard que ignora en
# silencio lo que no entiende cubre uno de N y no falla ni avisa.


def _texto(sentencias: tuple[str, ...]) -> str:
    return chr(10).join(f"  - {s}" for s in sentencias)


def _regenerar(generador: str, argumentos: dict) -> tuple[str, ...]:
    """Vuelve a pedirle al generador la receta que la migracion declaro."""
    from app.tenancy import politicas, rol

    if generador == "politica_de_cliente":
        expresion = politicas.expresion_de_cliente()
        return tuple(politicas.sentencias_de_politica(argumentos["tabla"], expresion))
    if generador == "politica_de_agencia":
        expresion = politicas.expresion_de_agencia(argumentos["columna_propia"])
        return tuple(politicas.sentencias_de_politica(argumentos["tabla"], expresion))
    if generador == "rol_creacion":
        return tuple(rol.sentencias_de_creacion())
    if generador == "rol_privilegios":
        # WHY: la revision 0003 cambio la FORMA de esta receta —de una lista de
        # tablas con un juego de verbos fijo a un mapa `tabla -> verbos`— porque
        # RF-10 no es expresable con la forma vieja. Aqui solo se conoce la forma
        # VIGENTE, y eso es correcto: `_redacciones_vigentes()` regenera unicamente
        # la revision MAS RECIENTE que declara cada bloque, y las viejas se quedan
        # congeladas como historia. Si alguien declarara la forma vieja en una
        # revision nueva, el `KeyError` seria un rojo ruidoso, no un salto mudo.
        return tuple(rol.sentencias_de_privilegios(argumentos["privilegios"]))
    raise AssertionError(
        f"generador {generador!r} declarado en una migracion y desconocido para este "
        "guard: anadelo a `_regenerar`. Saltarselo dejaria su redaccion sin vigilar"
    )


def _redacciones_vigentes() -> dict[str, dict]:
    """Por cada bloque congelado, la revision MAS RECIENTE que lo declara."""
    vigentes: dict[str, dict] = {}
    for revision in _guion_de_migraciones().walk_revisions():  # de head hacia atras
        modulo = revision.module
        recetas = getattr(modulo, "RECETAS_CONGELADAS", ())
        congelado = getattr(modulo, "SQL_CONGELADO", {})
        for clave, generador, argumentos in recetas:
            if clave in vigentes:
                continue  # ya la fijo una revision mas reciente
            assert clave in congelado, (
                f"la revision {revision.revision} declara la receta {clave!r} y no "
                "trae su SQL congelado: la receta no describe nada"
            )
            vigentes[clave] = {
                "revision": revision.revision,
                "generador": generador,
                "argumentos": argumentos,
                "sql": tuple(congelado[clave]),
            }
    return vigentes


def test_hay_redacciones_congeladas_que_medir() -> None:
    """Sin bloques congelados este guard seria verde por vacuidad."""
    assert _redacciones_vigentes(), (
        "ninguna migracion declara RECETAS_CONGELADAS: el generador de politicas "
        "no estaria vigilado por nadie"
    )


# --------------------------------------------------------------------------
# El gate se nombra a si mismo: la lista del CI == los archivos que existen
# --------------------------------------------------------------------------
# WHY: `ci.yml` nombra la bateria POR RUTA para que borrar o renombrar un archivo
# de prueba ponga el CI en rojo en vez de reducir en silencio lo que se mide. Esa
# defensa vale exactamente lo que valga la lista — y la lista se mantenia A MANO.
# Medido al escribir este guard: cubria 11 de los 15 archivos, y entre los cuatro
# que faltaban estaba `test_confirmacion.py`, que es el gate de RNF-06 y el que
# cazo P-31. Un mecanismo cableado a algunos de N no falla y no avisa: deja de
# cubrir (`feedback_mecanismo_cableado_a_uno`). Aqui la lista se DERIVA del
# directorio, y la divergencia es un rojo en las DOS direcciones.
FLUJO_DEL_GATE = RAIZ / ".github" / "workflows" / "ci.yml"
DIRECTORIO_DE_PRUEBAS = RAIZ / "apps" / "api" / "tests"

#: Como se reconoce el paso que de verdad corre la recoleccion. Se busca por su
#: COMANDO y no por su nombre: el nombre es prosa y se reescribe.
_MARCA_DEL_PASO = "pytest --collect-only"

#: Una linea que es UN argumento del comando, y nada mas. Un comentario no casa.
_ARGUMENTO_DE_PRUEBA = re.compile(r"^apps/api/tests/(test_[a-z0-9_]+\.py)$")


class PasoDeRecoleccionAusente(AssertionError):
    """No se encontro el paso cuyo contenido se quiere auditar."""


def _nombradas_en(flujo: str) -> set[str]:
    """Las rutas que el COMANDO nombra — no las que aparecen en el archivo.

    # WHY (lo levanto Crisol, y la sonda le dio la razon): la primera version
    # buscaba el patron en el TEXTO ENTERO del flujo. El comentario que explica el
    # guard menciona archivos de prueba, asi que una ruta escrita en un comentario
    # contaba como nombrada por el gate. ==Reproducido: sacando
    # `apps/api/tests/test_confirmacion.py` del comando y dejandola dentro de un
    # comentario, el guard salia VERDE mientras el paso real ya no la corria.== Es
    # el guard por texto castigando —o aqui, absolviendo— por la prosa (P-18 al
    # reves). Ahora se lee SOLO la lista de argumentos que cuelga del comando: un
    # comentario no tiene la forma de un argumento y corta el barrido.
    """
    lineas = flujo.splitlines()
    for indice, linea in enumerate(lineas):
        if _MARCA_DEL_PASO in linea and not linea.strip().startswith("#"):
            argumentos: set[str] = set()
            for siguiente in lineas[indice + 1 :]:
                encontrada = _ARGUMENTO_DE_PRUEBA.match(siguiente.strip())
                if encontrada is None:
                    break
                argumentos.add(encontrada.group(1))
            return argumentos
    raise PasoDeRecoleccionAusente(
        f"no hay ningun paso que ejecute {_MARCA_DEL_PASO!r} en {FLUJO_DEL_GATE}: el "
        "paso que protege la bateria desaparecio, o dejo de reconocerse"
    )


def _nombradas_por_el_gate() -> set[str]:
    return _nombradas_en(FLUJO_DEL_GATE.read_text(encoding="utf-8"))


def _archivos_de_prueba() -> set[str]:
    return {ruta.name for ruta in DIRECTORIO_DE_PRUEBAS.glob("test_*.py")}


def test_el_gate_nombra_archivos_de_prueba() -> None:
    """El control: si la extraccion no viera ninguno, el guard de abajo pasaria vacio."""
    assert FLUJO_DEL_GATE.is_file(), f"no existe {FLUJO_DEL_GATE}: el gate no esta donde se cree"
    nombradas = _nombradas_por_el_gate()
    assert len(nombradas) >= 10, (
        f"el gate solo nombra {len(nombradas)} archivos de prueba: o la lista se vacio, "
        "o este guard dejo de reconocer la forma de una ruta dentro de `ci.yml`"
    )
    assert _archivos_de_prueba(), "no hay ningun archivo de prueba que comparar"


def test_el_gate_nombra_exactamente_los_archivos_de_prueba_que_existen() -> None:
    """Un archivo nuevo entra solo en el paso que lo protege; uno muerto sale."""
    nombradas = _nombradas_por_el_gate()
    existentes = _archivos_de_prueba()

    faltan = sorted(existentes - nombradas)
    assert not faltan, (
        f"estos archivos de prueba existen y el gate no los nombra: {faltan}. Borrarlos "
        "o renombrarlos no pondria el CI en rojo, y el gate mediria menos sin decirlo"
    )
    sobran = sorted(nombradas - existentes)
    assert not sobran, (
        f"el gate nombra archivos de prueba que no existen: {sobran}. El paso fallaria "
        "con «file or directory not found» por una lista caducada, no por un defecto"
    )


def test_una_ruta_en_un_comentario_no_cuenta_como_nombrada_por_el_gate() -> None:
    """El sabotaje del propio guard: la prosa no puede sustituir al comando.

    # WHY (`feedback_sabotaje_audita_al_test`): este guard lee un archivo de texto,
    # y un guard por texto se engana con el texto que lo explica. Aqui se le da un
    # flujo fabricado donde una ruta aparece SOLO en un comentario y el comando no
    # la nombra: si el guard la contara, saldria verde mientras el paso real ya no
    # corre ese archivo. Con su control en la otra direccion, porque un extractor
    # que no viera nada tambien pasaria esta sonda.
    """
    fabricado = (
        "      - name: La bateria esta donde el gate dice\n"
        "        run: >-\n"
        "          uv run --no-sync pytest --collect-only -q\n"
        "          apps/api/tests/test_aislamiento.py\n"
        "      # nota: apps/api/tests/test_fantasma.py se saco del comando\n"
        "          apps/api/tests/test_nunca_llega.py\n"
    )
    nombradas = _nombradas_en(fabricado)
    assert "test_fantasma.py" not in nombradas, (
        "una ruta escrita en un COMENTARIO cuenta como nombrada por el gate: el paso "
        "podria dejar de correr un archivo y este guard seguiria verde"
    )
    assert "test_nunca_llega.py" not in nombradas, (
        "el barrido siguio despues del comentario: la lista de argumentos termina "
        "donde termina el comando, no donde a alguien le convenga"
    )
    # CONTROL: lo que SI esta en el comando se cuenta. Sin esto, un extractor que
    # devolviera el conjunto vacio pasaria las dos aserciones de arriba.
    assert nombradas == {"test_aislamiento.py"}


def test_el_guard_del_gate_falla_si_el_paso_desaparece() -> None:
    """Y si el paso ya no existe, esto es un rojo ruidoso y no un conjunto vacio."""
    with pytest.raises(PasoDeRecoleccionAusente):
        _nombradas_en("      - name: otro paso cualquiera\n        run: echo hola\n")


def test_la_redaccion_vigente_no_diverge_del_generador() -> None:
    """La salida de hoy del generador == el SQL que gobierna la base hoy."""
    for clave, vigente in sorted(_redacciones_vigentes().items()):
        esperado = _regenerar(vigente["generador"], vigente["argumentos"])
        assert vigente["sql"] == esperado, (
            f"la redaccion vigente de {clave!r} (revision {vigente['revision']}) ya no "
            f"coincide con lo que produce {vigente['generador']!r}."
            f"{chr(10)}En la migracion:{chr(10)}{_texto(vigente['sql'])}"
            f"{chr(10)}Genera hoy:{chr(10)}{_texto(esperado)}{chr(10)}"
            "Si el cambio del generador es intencionado, escribe una migracion NUEVA "
            "que vuelva a congelar esta receta. Editar la vieja reescribe el pasado"
        )


# --------------------------------------------------------------------------
# RF-27: lo que se le pedira a GitHub para IMPEDIR la fusion
# --------------------------------------------------------------------------
# WHY: `deploy/proteger_rama.py` no se puede correr contra GitHub hasta que el
# plan de la cuenta lo permita (hoy responde 403 por PLAN, ver P-01). Eso deja
# dos partes bien distintas: la LLAMADA de red, que no se puede medir todavia, y
# el JUICIO —que contextos exigir y que cuenta como divergencia—, que es donde
# de verdad se puede equivocar y que se mide entero aqui, sin red.
#
# Importa mucho cual de las dos falla en silencio. Si la derivacion devolviera un
# nombre que GitHub no reporta nunca, la regla quedaria esperando por siempre una
# comprobacion que no llega; si devolviera la lista vacia, la rama tendria una
# proteccion que no exige NADA y aparenta exigir. Las dos formas de ese fallo son
# MUDAS en produccion, asi que se cazan aqui.

_GUION_DE_PROTECCION = RAIZ / "deploy" / "proteger_rama.py"


def _proteccion():
    """Carga `deploy/proteger_rama.py` por ruta: no es un paquete importable."""
    import importlib.util

    especificacion = importlib.util.spec_from_file_location(
        "proteger_rama", _GUION_DE_PROTECCION
    )
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def test_el_guion_de_proteccion_existe_donde_se_cree() -> None:
    """El control de todo lo de abajo: sin el archivo, cada prueba pasaria vacia."""
    assert _GUION_DE_PROTECCION.is_file(), (
        f"no existe {_GUION_DE_PROTECCION}: el mecanismo de RF-27 no esta donde el "
        "resto de esta bateria supone que esta"
    )


def test_los_contextos_salen_del_flujo_real_y_no_estan_vacios() -> None:
    """Control: contra el `ci.yml` de este repositorio, la derivacion encuentra algo."""
    modulo = _proteccion()
    texto = FLUJO_DEL_GATE.read_text(encoding="utf-8")
    contextos = modulo.contextos_exigidos(texto)
    assert contextos, "la derivacion salio vacia contra el flujo real"
    # Y lo derivado tiene que ser un trabajo que el flujo declara de verdad.
    for contexto in contextos:
        assert contexto in texto, (
            f"se exigiria la comprobacion {contexto!r}, que no aparece en "
            f"{FLUJO_DEL_GATE.name}: GitHub esperaria para siempre una comprobacion "
            "que nadie reporta"
        )


def test_el_nombre_declarado_manda_sobre_la_clave_del_trabajo() -> None:
    """GitHub reporta el `name:` cuando existe — no la clave. Confundirlos cuelga la regla."""
    modulo = _proteccion()
    flujo = (
        "name: ci\n"
        "jobs:\n"
        "  verificacion:\n"
        "    name: Suite y aislamiento\n"
        "    runs-on: ubuntu-latest\n"
    )
    assert modulo.contextos_exigidos(flujo) == ["Suite y aislamiento"]


def test_se_derivan_todos_los_trabajos_y_solo_los_trabajos() -> None:
    """Dos trabajos dan dos contextos; el `name:` del flujo y el de un PASO no cuentan."""
    modulo = _proteccion()
    flujo = (
        "name: ci\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  verificacion:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Suite\n"
        "        run: pytest\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
    )
    assert modulo.contextos_exigidos(flujo) == ["verificacion", "lint"], (
        "o se colo algo que no es un trabajo (el nombre del flujo, el de un paso, una "
        "clave de despues del bloque), o se perdio un trabajo que si lo es"
    )


def test_un_comentario_en_linea_no_forma_parte_del_nombre_de_la_comprobacion() -> None:
    """Lo levanto Crisol y se reprodujo por efecto antes de aceptarlo.

    La version que leia el YAML con expresiones regulares devolvia
    `'Suite  # el nombre visible'`. GitHub no reporta jamas esa comprobacion, asi
    que la regla habria quedado esperando por siempre algo que no llega: el
    mecanismo cayendo en el fallo mudo que existe para evitar.
    """
    modulo = _proteccion()
    flujo = "jobs:\n  verificacion:\n    name: Suite  # el nombre visible\n"
    assert modulo.contextos_exigidos(flujo) == ["Suite"]


def test_una_almohadilla_dentro_de_comillas_no_es_un_comentario() -> None:
    """El caso que rompe a quien 'arregla' lo de arriba recortando por el simbolo."""
    modulo = _proteccion()
    flujo = 'jobs:\n  v:\n    name: "Suite # no es comentario"\n'
    assert modulo.contextos_exigidos(flujo) == ["Suite # no es comentario"]


@pytest.mark.parametrize(
    ("caso", "flujo"),
    [
        # Con matriz, GitHub publica `p (1)` y `p (2)`: el nombre concreto no
        # esta en este archivo. Exigir `p` bloquearia la rama para siempre.
        ("matriz", "jobs:\n  p:\n    strategy:\n      matrix:\n        v: [1, 2]\n"),
        # Y un nombre por expresion no existe hasta que GitHub lo evalua.
        ("expresion", "jobs:\n  p:\n    name: py-${{ matrix.v }}\n"),
    ],
)
def test_se_niega_cuando_el_nombre_real_no_esta_en_el_archivo(caso: str, flujo: str) -> None:
    """Parar y decirlo es mejor que exigir un nombre que nunca va a llegar."""
    modulo = _proteccion()
    with pytest.raises(modulo.NombreNoDerivable):
        modulo.contextos_exigidos(flujo)


def test_un_flujo_ilegible_no_produce_una_proteccion() -> None:
    """Ilegible no es vacio: se para. Un YAML roto no autoriza a escribir nada."""
    modulo = _proteccion()
    with pytest.raises(modulo.DerivacionVacia):
        modulo.contextos_exigidos("jobs:\n  - esto: [no\n   cierra\n")


def test_sin_trabajos_se_niega_a_escribir_en_vez_de_pedir_una_lista_vacia() -> None:
    """Fail-closed. Una lista vacia aplicaria una proteccion que no exige NADA."""
    modulo = _proteccion()
    with pytest.raises(modulo.DerivacionVacia):
        modulo.contextos_exigidos("name: ci\non:\n  push:\n")
    with pytest.raises(modulo.DerivacionVacia):
        modulo.contextos_exigidos("jobs:\n\npermissions:\n  contents: read\n")


def test_solo_se_abre_el_api_de_github_y_por_https() -> None:
    """El defecto confirmado del referente era un SSRF: aqui el destino se comprueba.

    Se mide sin credencial a proposito — la comprobacion del destino va DELANTE de
    la del token, asi que un fallo aqui no se puede confundir con «falta la ficha».
    """
    modulo = _proteccion()
    # Control: la ruta legitima pasa y produce la URL esperada.
    assert modulo.destino("/repos/x/y/branches/main/protection") == (
        "https://api.github.com/repos/x/y/branches/main/protection"
    )
    for ruta in (
        # El host correcto usado como NOMBRE DE USUARIO: sale hacia `evil.com`.
        "@evil.com/repos",
        ".evil.com/repos",
        "evil.com",
    ):
        with pytest.raises(SystemExit):
            modulo.destino(ruta)


def test_un_fallo_de_red_sale_como_mensaje_y_no_como_rastreo(monkeypatch) -> None:
    """Lo levanto Crisol: un fallo de TRANSPORTE no es una respuesta HTTP.

    Sin esto, quedarse sin red imprimia un rastreo crudo, que quien despliega lee
    como «el guion esta roto» en vez de «no hay red». El mensaje nombra el metodo
    y la ruta, y nunca la ficha.

    El valor de `GITHUB_TOKEN` de aqui no es una credencial ni la imita: existe
    solo para pasar de la comprobacion de presencia y no sale del proceso.
    """
    import urllib.error

    modulo = _proteccion()
    monkeypatch.setenv("GITHUB_TOKEN", "no-es-una-ficha")

    def _sin_red(*_args, **_kwargs):
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(modulo.urllib.request, "urlopen", _sin_red)
    with pytest.raises(SystemExit) as capturado:
        modulo._peticion("GET", "/repos/x/y/branches/main/protection")
    mensaje = str(capturado.value)
    assert "no se pudo hablar con GitHub" in mensaje
    assert "no-es-una-ficha" not in mensaje, "el mensaje de error no puede llevar la ficha"


def _aplicada(**cambios) -> dict:
    """El estado que GitHub devolveria si la proteccion quedara BIEN puesta."""
    vivo = {
        "required_status_checks": {"strict": True, "contexts": ["verificacion"]},
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }
    vivo.update(cambios)
    return vivo


def test_el_control_de_la_comparacion_no_ve_divergencias_cuando_no_las_hay() -> None:
    """Si esto fallara, cada sabotaje de abajo saldria rojo por el motivo equivocado."""
    modulo = _proteccion()
    assert modulo.divergencias(_aplicada(), ["verificacion"]) == []


@pytest.mark.parametrize(
    ("cambio", "senal"),
    [
        # Cada uno es una forma REAL de que la rama parezca protegida y no lo este.
        ({"enforce_admins": {"enabled": False}}, "enforce_admins"),
        ({"required_status_checks": {"strict": False, "contexts": ["verificacion"]}}, "strict"),
        ({"required_status_checks": {"strict": True, "contexts": []}}, "contextos"),
        ({"required_status_checks": {"strict": True, "contexts": ["otro"]}}, "contextos"),
        ({"allow_force_pushes": {"enabled": True}}, "allow_force_pushes"),
        ({"allow_deletions": {"enabled": True}}, "allow_deletions"),
        # El caso mudo: GitHub responde, pero sin bloque de comprobaciones.
        ({"required_status_checks": None}, "contextos"),
    ],
)
def test_cada_debilitamiento_de_la_proteccion_sale_en_rojo(cambio: dict, senal: str) -> None:
    modulo = _proteccion()
    fallos = modulo.divergencias(_aplicada(**cambio), ["verificacion"])
    assert any(senal in fallo for fallo in fallos), (
        f"debilitar {list(cambio)} no produjo ninguna divergencia que nombre {senal!r}: "
        f"la comparacion lo dejaria pasar. Divergencias vistas: {fallos}"
    )
