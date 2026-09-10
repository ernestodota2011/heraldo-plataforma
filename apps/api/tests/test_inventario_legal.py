"""T-030-bis (RNF-09, RF-31) — el inventario legal se DERIVA del codigo, o esto se pone rojo.

# WHY (P-51): la v1 del piso legal se redacto desde el DISENO —lo que el sistema
# iba a hacer— y afirmo como hechos ocho cosas que nada sostenia. La revision
# legal externa las conto una a una. La leccion no es «revisar mejor el texto»:
# es que un texto publico sobre el sistema NO se escribe, se GENERA desde lo que
# el codigo declara, y algo tiene que ponerse rojo el dia que los dos dejen de
# coincidir. Eso es RF-31 —documentacion contra codigo— aplicado al piso legal.
#
# # WHY (por que la prueba vive en la bateria y no solo en el CI): el paso de CI
# corre el guion en modo `--verificar`. Esta bateria mide ademas los MECANISMOS
# que hacen que ese verde signifique algo: que una tabla nueva sin categoria se
# caiga, que un destinatario nuevo en el codigo se caiga, que una superficie que
# aparezca se caiga, y que la medida de cookies coincida con la de un cliente
# HTTP ajeno al guion. Un `--verificar` en verde sin esto seria coherencia, no
# suficiencia (P-44).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

RAIZ = Path(__file__).resolve().parents[3]
GUION = RAIZ / "scripts" / "inventario_legal.py"


def _inventario():
    """Carga el guion como modulo, igual que `test_publicable` con su gate.

    # WHY (el registro en `sys.modules` NO es ceremonia): sin el, un `@dataclass`
    # definido en el modulo cargado revienta al construirse —`dataclasses` busca
    # el modulo por su nombre para resolver anotaciones y encuentra `None`—. El
    # gate de publicabilidad no tiene dataclasses y por eso su cargador se libra;
    # este si.
    """
    especificacion = importlib.util.spec_from_file_location("inventario_legal", GUION)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[especificacion.name] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def guion():
    return _inventario()


@pytest.fixture
def conexion_admin(motor_admin):
    with motor_admin.connect() as conexion:
        yield conexion


# ==========================================================================
# Controles: sin esto, todo lo de abajo pasaria vacio
# ==========================================================================
def test_el_guion_existe_donde_se_cree() -> None:
    assert GUION.is_file(), f"no existe {GUION}: el generador del inventario no esta"


def test_hay_algo_que_inventariar(guion, conexion_admin) -> None:
    """Un inventario que no encuentra nada que inventariar es ROJO, no verde."""
    inventario = guion.derivar(conexion_admin)
    assert inventario.datos, "el inventario de datos salio vacio: no midio el catalogo"
    assert inventario.destinatarios, "el inventario de destinatarios salio vacio"
    assert inventario.superficies, "no se midio ninguna superficie"
    assert inventario.licencias, "el inventario de licencias salio vacio"
    assert inventario.retencion, "el inventario de retencion salio vacio"
    assert inventario.identidades, "el inventario de identidades salio vacio"


# ==========================================================================
# EL gate: lo comprometido contra lo derivado
# ==========================================================================
def test_lo_comprometido_no_diverge_de_lo_derivado(guion, conexion_admin) -> None:
    """RF-31: si el codigo cambio y el inventario no, esto se pone rojo.

    Es la prueba que da sentido a las demas. Se arregla regenerando:
    `uv run --no-sync python scripts/inventario_legal.py`.
    """
    inventario = guion.derivar(conexion_admin)
    faltas = guion.divergencias(inventario)
    assert faltas == [], (
        "lo comprometido en docs/legal/ ya no es lo que el codigo deriva:\n  "
        + "\n  ".join(faltas)
        + "\n\nRegenera con: uv run --no-sync python scripts/inventario_legal.py"
    )


def test_control_lo_recien_escrito_no_diverge(guion, conexion_admin, tmp_path) -> None:
    """El control del gate: escribir y comparar contra lo escrito da VERDE.

    Sin este control, un comparador roto —o uno que no lee nada— dejaria la
    prueba de arriba pasando por ausencia.
    """
    inventario = guion.derivar(conexion_admin)
    guion.escribir(inventario, salida=tmp_path)
    assert guion.divergencias(inventario, salida=tmp_path) == []


def test_el_verificador_distingue_lo_comprometido_de_lo_derivado(
    guion, conexion_admin, tmp_path
) -> None:
    """Por EFECTO, sobre el modo que corre el CI: 0 cuando coincide, 1 cuando no."""
    inventario = guion.derivar(conexion_admin)
    guion.escribir(inventario, salida=tmp_path)
    assert guion.main(["--verificar", "--salida", str(tmp_path)]) == 0

    comprometido = tmp_path / guion.NOMBRE_MD
    comprometido.write_text(
        comprometido.read_text(encoding="utf-8") + "\nuna linea que nadie derivo\n",
        encoding="utf-8",
    )
    assert guion.main(["--verificar", "--salida", str(tmp_path)]) == 1


# ==========================================================================
# Sabotaje 1 — una tabla nueva del catalogo sin categoria declarada
# ==========================================================================
TABLA_DE_SONDA = "sonda_de_inventario_sin_categoria"


@pytest.fixture
def tabla_nueva_en_el_catalogo(motor_admin):
    """Crea una tabla REAL en el esquema y la retira pase lo que pase.

    # WHY (una tabla de verdad y no un catalogo fabricado): lo que se mide es que
    # el universo del inventario sea el catalogo VIVO. Con un diccionario de
    # mentira se mediria el diccionario.
    """
    with motor_admin.connect() as conexion:
        conexion.execute(text(f"CREATE TABLE {TABLA_DE_SONDA} (id uuid PRIMARY KEY)"))
    try:
        yield TABLA_DE_SONDA
    finally:
        with motor_admin.connect() as conexion:
            conexion.execute(text(f"DROP TABLE IF EXISTS {TABLA_DE_SONDA}"))


def test_una_tabla_nueva_sin_categoria_declarada_pone_el_inventario_en_rojo(
    guion, conexion_admin, tabla_nueva_en_el_catalogo
) -> None:
    with pytest.raises(guion.TablaSinCategoria, match=tabla_nueva_en_el_catalogo):
        guion.derivar(conexion_admin)


def test_control_con_la_categoria_declarada_la_misma_tabla_entra_en_el_inventario(
    guion, conexion_admin, tabla_nueva_en_el_catalogo, monkeypatch
) -> None:
    """El control del sabotaje: no se cae por existir, se cae por no estar declarada."""
    monkeypatch.setitem(
        guion.CATEGORIA_POR_TABLA,
        tabla_nueva_en_el_catalogo,
        ("plataforma", "tabla de sonda de la bateria; no la crea ninguna migracion"),
    )
    inventario = guion.derivar(conexion_admin)
    tablas = {fila["nombre"] for fila in inventario.datos}
    assert tabla_nueva_en_el_catalogo in tablas


def test_una_categoria_fuera_de_la_lista_no_se_admite(
    guion, conexion_admin, tabla_nueva_en_el_catalogo, monkeypatch
) -> None:
    """La lista de categorias tambien es una allowlist: no vale inventarse una."""
    monkeypatch.setitem(
        guion.CATEGORIA_POR_TABLA,
        tabla_nueva_en_el_catalogo,
        ("categoria que nadie declaro", "un motivo suficientemente largo para pasar"),
    )
    with pytest.raises(guion.CategoriaDesconocida):
        guion.derivar(conexion_admin)


def test_un_catalogo_de_otra_revision_no_se_inventaria(
    guion, conexion_admin, monkeypatch
) -> None:
    """El banco de pruebas es UNO y lo comparten varias ramas.

    # WHY (pasó de verdad el 2026-09-09): al generar el inventario contra un
    # banco que otra rama había migrado, el catálogo traía una tabla que en esta
    # rama no existe. Sin esta comprobación el fallo aparecía como «tabla sin
    # categoría», que manda a declarar una tabla ajena en un documento público
    # en vez de a mirar de qué esquema se estaba hablando.
    """
    monkeypatch.setattr(guion, "revision_de_este_repositorio", lambda raiz=None: "9999")
    with pytest.raises(guion.CatalogoDeOtraRevision, match="9999"):
        guion.exigir_catalogo_de_esta_revision(conexion_admin)


def test_control_el_catalogo_del_banco_si_es_el_de_esta_rama(guion, conexion_admin) -> None:
    """El control: con la revisión de verdad, la comprobación no salta."""
    guion.exigir_catalogo_de_esta_revision(conexion_admin)
    assert guion.revision_de_este_repositorio(), "no se pudo leer la revisión `head`"


# ==========================================================================
# Sabotaje 2 — un destinatario en codigo sin fila en el inventario
# ==========================================================================
def test_un_destinatario_nuevo_en_el_codigo_pone_el_inventario_en_rojo(
    guion, conexion_admin
) -> None:
    """Un proveedor mas en la lista declarada = un destinatario mas de datos."""
    from app.agents.providers import LISTA_DECLARADA, Proveedor

    inventado = Proveedor(
        nombre="proveedor-de-sonda",
        url_de_validacion="https://ejemplo.invalid/v1/models",
        cabeceras=lambda credencial: {},
        condiciones=None,
    )
    ampliada = {**LISTA_DECLARADA, "proveedor-de-sonda": inventado}

    con_el_nuevo = guion.derivar(conexion_admin, lista_de_proveedores=ampliada)
    faltas = guion.divergencias(con_el_nuevo)
    assert faltas, (
        "aparecio un destinatario nuevo en el codigo y el inventario comprometido "
        "siguio pareciendo correcto: el gate no cubre el eje de destinatarios"
    )


def test_una_salida_nueva_al_punto_unico_pone_el_inventario_en_rojo(guion, tmp_path) -> None:
    """El otro eje: quien LLAMA al punto unico de salida se deriva del arbol.

    Se le da un arbol fabricado con una llamada mas. Con su control en la otra
    direccion: un barrido que no viera nada tambien pasaria esta sonda.
    """
    arbol = tmp_path / "arbol"
    (arbol / "apps" / "api" / "app" / "agents").mkdir(parents=True)
    (arbol / "packages" / "egress").mkdir(parents=True)
    (arbol / "packages" / "egress" / "red.py").write_text(
        "async def pedir(url):\n    return url\n", encoding="utf-8"
    )
    modulo = arbol / "apps" / "api" / "app" / "agents" / "otro.py"
    modulo.write_text(
        "from egress.red import pedir\n\n\nasync def salir():\n    return await pedir('x')\n",
        encoding="utf-8",
    )

    encontradas = guion.llamantes_del_punto_de_salida(arbol)
    assert "apps/api/app/agents/otro.py" in encontradas, (
        "el barrido no vio una llamada al punto unico de salida que si esta: "
        "el eje de destinatarios se mediria vacio"
    )
    modulo.write_text("def nada():\n    return 1\n", encoding="utf-8")
    assert guion.llamantes_del_punto_de_salida(arbol) == [], (
        "el control fallo: el barrido encuentra llamadas donde no las hay"
    )


# ==========================================================================
# Sabotaje 3 — una superficie que existe y no esta inventariada
# ==========================================================================
def test_una_superficie_nueva_sin_medir_pone_el_inventario_en_rojo(
    guion, conexion_admin
) -> None:
    """Una ruta mas es superficie mas: el inventario comprometido deja de valer."""

    def fabrica_con_una_ruta_de_mas():
        aplicacion = guion.aplicacion_de_medida()

        @aplicacion.get("/sonda-de-inventario")
        async def sonda() -> dict:
            return {"ok": True}

        return aplicacion

    ampliado = guion.derivar(conexion_admin, fabrica_de_aplicacion=fabrica_con_una_ruta_de_mas)
    assert guion.divergencias(ampliado), (
        "aparecio una ruta servida y el inventario comprometido siguio pareciendo "
        "correcto: el gate no cubre el eje de superficies"
    )


def test_un_artefacto_de_navegador_nuevo_obliga_a_medirlo(guion, tmp_path) -> None:
    """`apps/web/` solo tiene su marcador; lo que aparezca ahi hay que medirlo."""
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "README.md").write_text("marcador\n", encoding="utf-8")
    assert guion.artefactos_de_navegador(tmp_path) == []

    (web / "package.json").write_text("{}\n", encoding="utf-8")
    assert guion.artefactos_de_navegador(tmp_path) == ["package.json"], (
        "un artefacto nuevo en apps/web no se vio: una superficie de navegador "
        "podria construirse sin que su inventario de cookies existiera"
    )


# ==========================================================================
# El universo de la medida de superficie: lo que se sirve, TODO
# ==========================================================================
def test_la_medida_alcanza_las_rutas_de_un_router_incluido(guion) -> None:
    """El control del recorrido: un router INCLUIDO es superficie servida igual.

    # WHY: `aplicacion.routes` no es una lista plana — un `include_router` deja
    # un envoltorio con `path = None`. La primera version del recorrido miraba
    # solo el primer nivel y perdio las DOS sondas de salud, que son justo las
    # rutas que cualquiera puede pedir SIN credencial. La medida salia verde
    # diciendo «no emite cookies» sin haberlas mirado. Nombrarlas aqui es lo que
    # impide que la superficie se encoja otra vez en silencio.
    """
    from app.health import RUTA_DISPONIBILIDAD, RUTA_VIVACIDAD

    medidas = {fila["ruta"] for fila in guion.medir_rutas(guion.aplicacion_de_medida())}
    assert {RUTA_VIVACIDAD, RUTA_DISPONIBILIDAD} <= medidas, (
        f"la medida de superficie no alcanza las sondas de salud; midio {sorted(medidas)}"
    )


def test_el_recorrido_de_rutas_atraviesa_los_envoltorios(guion) -> None:
    """El sabotaje del propio recorrido, sobre un árbol fabricado.

    Un objeto que no tiene `path` pero envuelve a otro que sí lo tiene: si el
    recorrido no lo atraviesa, devuelve una lista vacía y todo lo de arriba
    pasaría por ausencia.
    """

    class Hoja:
        path = "/adentro"
        methods = {"GET"}

    class Envoltorio:
        path = None
        methods = None

        def __init__(self) -> None:
            self.original_router = type("R", (), {"routes": [Hoja()]})()
            self.include_context = type("C", (), {"prefix": "/prefijo"})()

    planas = guion.rutas_planas(type("App", (), {"routes": [Envoltorio()]})())
    assert planas == [("/prefijo/adentro", {"GET"})], planas


# ==========================================================================
# El segundo testigo de la medida de cookies (P-44)
# ==========================================================================
async def test_la_medida_de_cookies_coincide_con_un_cliente_http_ajeno(guion) -> None:
    """El arbitro no es una constante del guion: es otro cliente, ajeno a el.

    # WHY (P-44): el guion mide la superficie con un llamador ASGI minimo escrito
    # dentro de el. Comparar esa medida con una constante suya seria un testigo
    # repetido. Aqui la misma aplicacion se recorre con `httpx` —codigo de un
    # tercero— y las dos lecturas tienen que decir lo mismo.
    """
    aplicacion = guion.aplicacion_de_medida()
    medido_por_el_guion = {
        (fila["metodo"], fila["ruta"]): (fila["codigo"], tuple(fila["cookies"]))
        for fila in await guion.medir_rutas_async(aplicacion)
    }
    assert medido_por_el_guion, "no se midio ninguna ruta"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=aplicacion), base_url="http://heraldo.invalid"
    ) as cliente:
        for (metodo, ruta), esperado in medido_por_el_guion.items():
            respuesta = await cliente.request(metodo, guion.rellenar_parametros(ruta))
            cookies = tuple(
                valor.split("=", 1)[0].strip()
                for clave, valor in respuesta.headers.multi_items()
                if clave.lower() == "set-cookie"
            )
            assert (respuesta.status_code, cookies) == esperado, (
                f"las dos lecturas de {metodo} {ruta} no coinciden: el llamador ASGI "
                f"del guion dice {esperado} y httpx dice "
                f"{(respuesta.status_code, cookies)}"
            )


# ==========================================================================
# Las allowlists del guion: ni entradas muertas ni categorias inventadas
# ==========================================================================
def test_ninguna_categoria_declarada_esta_muerta(guion, conexion_admin) -> None:
    """Una categoria escrita para una tabla que ya no existe es un motivo que miente."""
    tablas = set(guion.tablas_y_columnas(conexion_admin))
    sobrantes = sorted(set(guion.CATEGORIA_POR_TABLA) - tablas)
    assert not sobrantes, (
        f"CATEGORIA_POR_TABLA declara tablas que no existen: {sobrantes}. Una "
        "declaracion caducada tapa la siguiente"
    )


def test_toda_categoria_declarada_es_de_la_lista(guion) -> None:
    for tabla, (categoria, motivo) in guion.CATEGORIA_POR_TABLA.items():
        assert categoria in guion.CATEGORIAS, f"{tabla} declara {categoria!r}, que no existe"
        assert len(motivo.strip()) >= 30, f"{tabla} no explica que contiene: {motivo!r}"


def test_el_alcance_del_guion_coincide_con_el_del_gate_de_rls(guion, conexion_admin) -> None:
    """Dos redacciones de «de que clase es esta tabla» que discrepan es drift.

    # WHY: la clase la define `test_rls_cobertura.clase_de` y el inventario la
    # vuelve a derivar de las mismas dos columnas. Que exista una segunda lectura
    # solo es admisible si algo se pone rojo cuando dejan de coincidir.
    """
    from test_rls_cobertura import clase_de

    for tabla, columnas in guion.tablas_y_columnas(conexion_admin).items():
        assert guion.alcance_de(columnas) == clase_de(columnas), (
            f"el inventario y el gate de RLS clasifican {tabla} de forma distinta"
        )


# ==========================================================================
# El inventario es PUBLICABLE, y lo dice el mismo gate del repositorio
# ==========================================================================
def test_el_inventario_pasa_el_gate_de_publicabilidad(guion, conexion_admin) -> None:
    """RNF-08: lo que se publica pasa por `publicable.py` ANTES de escribirse."""
    inventario = guion.derivar(conexion_admin)
    gate = guion.gate_de_publicabilidad()
    for nombre, texto in (
        (guion.NOMBRE_MD, guion.render_markdown(inventario)),
        (guion.NOMBRE_JSON, guion.render_json(inventario)),
    ):
        assert gate.revisar_texto(texto, f"docs/legal/{nombre}") == []


def test_un_inventario_impublicable_no_se_escribe(guion, conexion_admin, tmp_path) -> None:
    """El sabotaje del propio gate: si la salida no pasa, no se escribe NADA.

    # WHY (P-49): el caracter invisible se construye con `chr()`, nunca como
    # literal. Un literal aqui seria justo lo que el gate del repositorio
    # prohibe, y el archivo de la prueba se pondria rojo a si mismo.
    """
    inventario = guion.derivar(conexion_admin)
    inventario.limites.append("un limite con" + chr(0x202E) + "algo que nadie ve")
    with pytest.raises(guion.InventarioNoPublicable):
        guion.escribir(inventario, salida=tmp_path)
    assert not (tmp_path / guion.NOMBRE_MD).exists(), (
        "el inventario se escribio a pesar de no pasar el gate de publicabilidad"
    )


# ==========================================================================
# Lo pendiente es una AUSENCIA MEDIDA, no una frase
# ==========================================================================
def test_lo_pendiente_se_comprueba_contra_el_arbol(guion, tmp_path) -> None:
    """El dia que el artefacto exista, el guion EXIGE derivarlo de verdad."""
    for clave, (ruta, _casilla) in guion.ARTEFACTOS_PENDIENTES.items():
        assert not (RAIZ / ruta).exists(), (
            f"{ruta} ya existe: {clave} dejo de estar pendiente y el inventario "
            "tiene que derivarlo, no declararlo"
        )

    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "retencion_respaldos.toml").write_text("", encoding="utf-8")
    with pytest.raises(guion.ArtefactoQueYaExiste, match="retencion_respaldos"):
        guion.exigir_que_sigan_pendientes(tmp_path)


def test_control_con_el_arbol_de_verdad_lo_pendiente_sigue_pendiente(guion) -> None:
    """El control: sobre este repositorio, la comprobacion de arriba no salta."""
    guion.exigir_que_sigan_pendientes(RAIZ)


# ==========================================================================
# Licencias: el entorno tiene que corresponder al bloqueo
# ==========================================================================
def test_una_declaracion_de_licencia_que_ya_no_coincide_se_cae(guion, monkeypatch) -> None:
    """La declaracion existe para lo que este entorno no puede leer; se audita donde si.

    # WHY: hay paquetes que solo se instalan en un sistema operativo (`uv.lock` lo
    # dice con un marcador). Su licencia se declara aqui para que el inventario no
    # cambie segun donde se genere — y esa declaracion se CRUZA contra los
    # metadatos instalados en el entorno que si los tiene. Una declaracion que
    # miente se cae ahi, en vez de vivir para siempre.
    """
    instalado = next(
        nombre
        for nombre in guion.LICENCIAS_DE_PAQUETES_CONDICIONADOS
        if guion.licencia_instalada(nombre) is not None
    )
    monkeypatch.setitem(
        guion.LICENCIAS_DE_PAQUETES_CONDICIONADOS,
        instalado,
        ("License", "Licencia Que Nadie Declara", "un motivo cualquiera"),
    )
    with pytest.raises(guion.DeclaracionDeLicenciaCaduca, match=instalado):
        guion.derivar_licencias(RAIZ)


def test_un_paquete_del_bloqueo_que_falta_en_el_entorno_se_cae(guion, monkeypatch) -> None:
    """Sin el entorno completo, el inventario de licencias saldria corto y verde."""
    original = guion.licencia_instalada

    def ciega(nombre: str):
        return None if nombre == "fastapi" else original(nombre)

    monkeypatch.setattr(guion, "licencia_instalada", ciega)
    with pytest.raises(guion.EntornoNoCorrespondeAlBloqueo, match="fastapi"):
        guion.derivar_licencias(RAIZ)


def test_el_bloqueo_y_el_inventario_cuentan_los_mismos_paquetes(guion) -> None:
    """Control: la lectura de `uv.lock` encuentra paquetes de verdad."""
    filas = guion.derivar_licencias(RAIZ)
    assert len(filas) >= 20, f"solo se leyeron {len(filas)} paquetes de uv.lock"
    assert all(fila["licencia"] for fila in filas), "hay filas sin columna de licencia"


# ==========================================================================
# La salida es estable: dos derivaciones seguidas dicen lo mismo
# ==========================================================================
def test_dos_derivaciones_seguidas_producen_el_mismo_documento(guion, conexion_admin) -> None:
    """Sin esto, el gate se pondria rojo solo, y se acabaria apagando.

    Es la razon por la que en el documento no hay fecha de generacion: una marca
    de tiempo haria divergir el archivo en cada corrida.
    """
    primero = guion.render_markdown(guion.derivar(conexion_admin))
    segundo = guion.render_markdown(guion.derivar(conexion_admin))
    assert primero == segundo


def test_el_json_es_legible_y_lleva_procedencia_en_cada_fila(guion, conexion_admin) -> None:
    """T-030-quater lee este JSON: cada fila dice de donde salio."""
    documento: dict[str, Any] = json.loads(guion.render_json(guion.derivar(conexion_admin)))
    bloques = documento["bloques"]
    assert set(bloques) == {
        "datos",
        "destinatarios",
        "superficies",
        "licencias",
        "retencion",
        "identidades",
    }
    for nombre, filas in bloques.items():
        assert filas, f"el bloque {nombre} salio vacio en el JSON"
        for fila in filas:
            assert fila.get("procedencia"), f"una fila de {nombre} no dice de donde salio"


# ==========================================================================
# La ficha de un proveedor: la region NO se publica sin su evidencia
# ==========================================================================
def _proveedor_de_sonda(guion, ficha):
    from app.agents.providers import Proveedor

    return {
        "proveedor-de-sonda": Proveedor(
            nombre="proveedor-de-sonda",
            url_de_validacion="https://ejemplo.invalid/v1/models",
            cabeceras=lambda credencial: {},
            condiciones=ficha,
        )
    }


def _fila_del_proveedor(filas):
    return next(fila for fila in filas if "proveedor-de-sonda" in fila["destinatario"])


def test_la_region_de_un_proveedor_se_publica_con_quien_la_verifico_y_cuando(guion) -> None:
    """Una región publicada a secas es una afirmación sin respaldo: eso es P-51.

    # WHY: `FichaDeCondiciones` trae `ubicacion`, `verificada_en` y `fuente`. La
    # primera versión de este inventario leía solo la primera, así que el día que
    # T-100·bis rellene las fichas el documento diría «se trata en tal región» sin
    # decir quién lo comprobó ni cuándo — que es exactamente la clase de frase que
    # la revisión legal externa contó ocho veces. Hoy ningún proveedor tiene ficha,
    # así que esto se mide con una inyectada: la rama existe antes que su dato.
    """
    from datetime import date

    from app.agents.providers import FichaDeCondiciones

    ficha = FichaDeCondiciones(
        no_entrenamiento_por_defecto=True,
        retencion="30 dias",
        acceso_humano="solo por abuso",
        ubicacion="region de sonda",
        verificada_en=date(2026, 9, 9),
        fuente="condiciones publicadas del proveedor",
    )
    fila = _fila_del_proveedor(
        guion.derivar_destinatarios(lista_de_proveedores=_proveedor_de_sonda(guion, ficha))
    )
    assert fila["ubicacion"] == "region de sonda", (
        "la region publicada no es la de la ficha: " + fila["ubicacion"]
    )
    assert "2026-09-09" in fila["procedencia"], (
        "la region se publico sin la FECHA en que se verifico: " + fila["procedencia"]
    )
    assert "condiciones publicadas del proveedor" in fila["procedencia"], (
        "la region se publico sin la FUENTE que la respalda: " + fila["procedencia"]
    )


def test_control_un_proveedor_sin_ficha_no_afirma_ninguna_region(guion) -> None:
    """El control en la otra dirección: sin ficha no se inventa ni región ni evidencia."""
    fila = _fila_del_proveedor(
        guion.derivar_destinatarios(lista_de_proveedores=_proveedor_de_sonda(guion, None))
    )
    assert "pendiente (T-100·bis)" in fila["ubicacion"]
    assert "2026" not in fila["procedencia"], (
        "sin ficha no puede haber fecha de verificacion: " + fila["procedencia"]
    )


# ==========================================================================
# El universo de los DERIVADOS se deriva: ningun prefijo se queda fuera
# ==========================================================================
def test_toda_constante_de_prefijo_de_produccion_esta_inventariada(guion) -> None:
    """El control sobre el árbol REAL: hoy no hay ninguna familia de claves sin fila.

    # WHY (P-52, otra vez): el bloque de datos enumeraba a mano las tres familias
    # de claves de Redis que la casilla nombra, y con eso afirmaba cubrir «los
    # derivados» habiendo mirado solo donde ya sabía que mirar. Al derivar el
    # universo apareció una CUARTA constante de prefijo en producción que ninguna
    # fila mencionaba. No fallaba: medía menos, y salía verde.
    """
    guion.exigir_que_todo_prefijo_este_inventariado(RAIZ)


def test_un_prefijo_de_produccion_sin_fila_pone_el_inventario_en_rojo(
    guion, monkeypatch
) -> None:
    """El sabotaje sobre el universo real: se quita una declaración y tiene que caerse."""
    declaradas = dict(guion.PREFIJOS_INVENTARIADOS)
    quitada = sorted(declaradas)[0]
    del declaradas[quitada]
    monkeypatch.setattr(guion, "PREFIJOS_INVENTARIADOS", declaradas)
    with pytest.raises(guion.PrefijoSinInventariar, match=quitada.split(":")[-1]):
        guion.exigir_que_todo_prefijo_este_inventariado(RAIZ)


def test_el_barrido_de_prefijos_ve_una_constante_nueva(guion, tmp_path) -> None:
    """El sabotaje del propio barrido: si no ve nada, todo lo de arriba pasa por ausencia."""
    modulo = tmp_path / "apps" / "api" / "app" / "channels" / "otro.py"
    modulo.parent.mkdir(parents=True)
    modulo.write_text('PREFIJO_NUEVO = "heraldo:otro"\n', encoding="utf-8")
    assert guion.constantes_de_prefijo(tmp_path) == [
        "apps/api/app/channels/otro.py:PREFIJO_NUEVO"
    ]

    modulo.write_text('OTRA_COSA = "heraldo:otro"\n', encoding="utf-8")
    assert guion.constantes_de_prefijo(tmp_path) == [], (
        "el barrido encuentra prefijos donde no los hay"
    )


def test_una_declaracion_de_prefijo_muerta_se_cae(guion, monkeypatch) -> None:
    """Una declaración que apunta a una constante que ya no existe tapa a la siguiente."""
    declaradas = {
        **guion.PREFIJOS_INVENTARIADOS,
        "apps/api/app/ya_no_existe.py:PREFIJO": "una familia que se fue",
    }
    monkeypatch.setattr(guion, "PREFIJOS_INVENTARIADOS", declaradas)
    with pytest.raises(guion.DeclaracionDePrefijoMuerta, match="ya_no_existe"):
        guion.exigir_que_todo_prefijo_este_inventariado(RAIZ)


def test_el_generador_se_niega_si_hay_un_prefijo_sin_inventariar(
    guion, conexion_admin, monkeypatch
) -> None:
    """El guard tiene que estar CABLEADO a `derivar`, no solo existir.

    # WHY (`feedback_mecanismo_cableado_a_uno`): una comprobación que solo llama
    # su propia prueba no protege el documento. Si alguien quita la llamada de
    # `derivar`, esto se cae.
    """
    declaradas = dict(guion.PREFIJOS_INVENTARIADOS)
    del declaradas[sorted(declaradas)[0]]
    monkeypatch.setattr(guion, "PREFIJOS_INVENTARIADOS", declaradas)
    with pytest.raises(guion.PrefijoSinInventariar):
        guion.derivar(conexion_admin)
