"""T-021·bis (RNF-04) — el alta sanitaria se rechaza EN LA RUTA, y hay centinela.

RNF-04 era una promesa sin mecanismo (I-03): «el sistema no aloja datos de salud
protegidos ni opera bajo BAA en v1; el cliente sanitario de la agencia no es
inquilino». Una frase en un
documento. Este archivo mide el mecanismo que la sostiene, y lo mide POR EFECTO:
lo que cuenta no es que `evaluar_alta` devuelva un veredicto, es que la FILA NO
SE ESCRIBA.

Tres varas, cada una con su control:

  1. un alta marcada como sanitaria se RECHAZA;
  2. control: un alta normal PASA (una defensa que rechaza a todos es un producto
     roto, no una defensa);
  3. fail-closed: un dato indeterminado —sector ausente, sector desconocido, o un
     nombre que CONTRADICE al sector— se RECHAZA.

Y la cuarta, que es la que decide si esto es un guard o una decoracion: el
centinela es LOAD-BEARING. Si se borra, se corrompe o declara algo que este
codigo no sabe interpretar, TODAS las altas se rechazan — incluida la normal. Un
centinela que se puede borrar sin consecuencias no es un centinela.

# WHY (`feedback_guard_solo_en_el_test`): un guard que solo vive en la suite
# autoriza en produccion. Por eso ademas de las sondas por efecto hay un guard
# ESTRUCTURAL: ningun otro modulo de la aplicacion puede contener un
# `INSERT INTO clientes`. Si alguien escribe un segundo camino de alta, este
# archivo lo caza — y el guard trae su propio control, porque un guard que no
# mira ningun archivo pasa siempre.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.audit.bitacora import leer_apuntes
from app.tenancy.auth import PermisoDenegado, Rol, Sesion
from app.tenancy.baa_guard import (
    ACCION_REVERIFICACION,
    NOMBRE_DEL_CENTINELA,
    RAIZ_DEL_REPOSITORIO,
    REGIMEN_SIN_BAA,
    VARIABLE_DE_ENTORNO_CENTINELA,
    AltaRechazada,
    Clasificacion,
    GuardDeBaaRechaza,
    RegimenIndeterminado,
    ReverificacionRechazada,
    Sector,
    VeredictoDeAlta,
    alta_de_cliente,
    evaluar_alta,
    marcadores_sanitarios,
    regimen_declarado,
    reverificar_sector,
    sector_persistido,
)
from app.tenancy.sesion import sesion_de_inquilino
from conftest import (
    AGENCIA_A,
    AGENCIA_B,
    CLIENTE_A1,
    RAIZ,
    resembrar,
    sesion_de_agencia,
)

OPERADOR = Sesion(
    sesion_id="operador", agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
)
USUARIO_DE_PORTAL = Sesion(
    sesion_id="portal", agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
)


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """El alta ESCRIBE: cada prueba arranca del mismo escenario sembrado.

    # WHY: sin esto el ORDEN de ejecucion pasaria a formar parte del resultado, y
    # las sondas de aislamiento de los otros modulos —que cuentan filas— verian
    # los clientes que este archivo dio de alta. La limpieza reusa
    # `sembrar_escenario`, no una lista escrita a mano (P-10/P-11/P-12).
    """
    resembrar(motor_de_siembra)


@pytest.fixture(scope="module", autouse=True)
def _escenario_devuelto_al_salir(motor_de_siembra):
    yield
    resembrar(motor_de_siembra)


async def _cuantos_clientes(motor) -> int:
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        return (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()


# --------------------------------------------------------------------------
# Vara 1 — el alta sanitaria se rechaza. Por EFECTO.
# --------------------------------------------------------------------------
async def test_un_alta_declarada_sanitaria_es_rechazada(motor) -> None:
    antes = await _cuantos_clientes(motor)

    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(
            motor, sesion=OPERADOR, nombre="Consultorio del Doctor Ruiz", sector=Sector.SALUD
        )

    assert capturado.value.veredicto.clasificacion is Clasificacion.SANITARIA
    assert await _cuantos_clientes(motor) == antes, (
        "el alta sanitaria fue rechazada y AUN ASI escribio la fila: el guard esta "
        "despues del INSERT, o al lado y no dentro"
    )


async def test_un_nombre_compuesto_con_el_marcador_pegado_no_cuela(motor) -> None:
    """RNF-04 nombra a un cliente sanitario concreto. Lo que se comprueba aqui es
    la FORMA de ese nombre: el marcador pegado DENTRO de una palabra compuesta.

    # WHY: es el caso limite exacto del requisito, y el que casi se escapa. Con el
    # patron anclado (`\\bclinic`) un nombre compuesto asi NO disparaba: no hay frontera de
    # palabra antes de «clinic». Medido antes de escribir esta prueba.
    """
    antes = await _cuantos_clientes(motor)
    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(
            motor, sesion=OPERADOR, nombre="Policlinico", sector=Sector.COMERCIO
        )
    assert "clinica" in capturado.value.veredicto.marcadores
    assert await _cuantos_clientes(motor) == antes


# --------------------------------------------------------------------------
# Vara 2 — el CONTROL: un alta normal pasa
# --------------------------------------------------------------------------
async def test_control_un_alta_normal_si_pasa(motor) -> None:
    """Si nada pudiera darse de alta, el guard seria un producto roto."""
    antes = await _cuantos_clientes(motor)

    nuevo = await alta_de_cliente(
        motor,
        sesion=OPERADOR,
        nombre="Ferreteria Lopez",
        sector=Sector.COMERCIO,
        descripcion="Venta de herramienta y material de construccion",
    )

    assert await _cuantos_clientes(motor) == antes + 1
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        nombre = (
            await conexion.execute(
                text("SELECT nombre FROM clientes WHERE id = :id"), {"id": nuevo}
            )
        ).scalar_one()
    assert nombre == "Ferreteria Lopez"


async def test_el_alta_cuelga_de_la_agencia_de_la_sesion_y_no_de_un_parametro(motor) -> None:
    """La agencia sale de la SESION. `alta_de_cliente` ni siquiera acepta otra.

    # WHY: es RF-01 aplicado al alta. Si la agencia viniera por parametro, un
    # operador podria dar de alta clientes en la agencia del vecino, y la politica
    # de RLS no lo veria raro: comprueba coherencia con la SESION.
    """
    parametros = set(inspect.signature(alta_de_cliente).parameters)
    assert "agencia_id" not in parametros, (
        "`alta_de_cliente` acepta una agencia por parametro: por ahi entra el alta "
        "cruzada entre agencias"
    )

    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Panaderia La Espiga", sector=Sector.COMERCIO
    )
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        agencia = (
            await conexion.execute(
                text("SELECT agencia_id FROM clientes WHERE id = :id"), {"id": nuevo}
            )
        ).scalar_one()
    assert agencia == AGENCIA_A


# --------------------------------------------------------------------------
# Vara 3 — fail-closed: lo indeterminado se rechaza
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sector",
    [None, "", "   ", "sanidad", "salud_mental", "SALUD", 42, "otro sector"],
)
async def test_un_sector_que_no_se_puede_determinar_se_rechaza(motor, sector) -> None:
    """«No pude preguntar» no es «adelante» (RNF-04, fail-closed)."""
    antes = await _cuantos_clientes(motor)
    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(motor, sesion=OPERADOR, nombre="Negocio Cualquiera", sector=sector)
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert await _cuantos_clientes(motor) == antes


async def test_un_nombre_que_contradice_al_sector_se_rechaza(motor) -> None:
    """Dos fuentes que se contradicen no dan una respuesta: dan un indeterminado.

    Declarar «comercio» y llamarse «Clinica Dental Sur» es exactamente el camino
    por el que un cliente sanitario entraria sin declararse.
    """
    antes = await _cuantos_clientes(motor)
    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(
            motor, sesion=OPERADOR, nombre="Clinica Dental Sur", sector=Sector.COMERCIO
        )
    veredicto = capturado.value.veredicto
    assert veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert set(veredicto.marcadores) >= {"clinica", "dental"}
    assert await _cuantos_clientes(motor) == antes


async def test_la_contradiccion_tambien_se_busca_en_la_descripcion(motor) -> None:
    """El nombre se puede lavar; la descripcion es la segunda fuente."""
    with pytest.raises(AltaRechazada):
        await alta_de_cliente(
            motor,
            sesion=OPERADOR,
            nombre="Grupo Aurora",
            sector=Sector.COMERCIO,
            descripcion="Gestion de historias clinicas para pacientes de la zona",
        )


async def test_un_alta_sin_nombre_se_rechaza(motor) -> None:
    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(motor, sesion=OPERADOR, nombre="   ", sector=Sector.COMERCIO)
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA


# --------------------------------------------------------------------------
# Vara 4 — el centinela es LOAD-BEARING
# --------------------------------------------------------------------------
def test_el_repositorio_lleva_su_centinela_y_declara_sin_baa() -> None:
    ruta = RAIZ / NOMBRE_DEL_CENTINELA
    assert ruta.is_file(), (
        f"falta {NOMBRE_DEL_CENTINELA} en la raiz del repositorio: RNF-04 volveria a "
        "ser una promesa sin artefacto, igual que antes de T-021·bis"
    )
    assert regimen_declarado() == REGIMEN_SIN_BAA
    assert RAIZ_DEL_REPOSITORIO.resolve() == RAIZ.resolve(), (
        "el modulo cree que la raiz del repositorio esta en otro sitio que la suite: "
        "en produccion leeria un centinela distinto del que se versiona"
    )


@pytest.mark.parametrize(
    "contenido",
    [
        None,  # el archivo no existe
        "",  # vacio
        "# solo comentarios\n\n",  # sin declaracion
        "con-baa\n",  # un regimen que este codigo NO implementa
        "sin_baa\n",  # casi, pero no
        "SIN-BAA\n",  # casi, pero no
    ],
)
async def test_sin_regimen_interpretable_no_se_admite_ninguna_alta(
    motor, tmp_path: Path, monkeypatch, contenido
) -> None:
    """El sabotaje del centinela, hecho prueba: si no se puede leer, se cierra.

    Nota deliberada: el alta que aqui se rechaza es LA MISMA que en
    `test_control_un_alta_normal_si_pasa` se admite. Lo unico que cambia es el
    centinela. Si el centinela fuera decoracion, esta prueba saldria verde por
    alta admitida y nadie se enteraria.
    """
    falso = tmp_path / NOMBRE_DEL_CENTINELA
    if contenido is not None:
        falso.write_text(contenido, encoding="utf-8")
    monkeypatch.setenv(VARIABLE_DE_ENTORNO_CENTINELA, str(falso))

    antes = await _cuantos_clientes(motor)
    with pytest.raises(AltaRechazada) as capturado:
        await alta_de_cliente(
            motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
        )
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert await _cuantos_clientes(motor) == antes


def test_un_regimen_no_implementado_levanta_al_leerlo(tmp_path: Path, monkeypatch) -> None:
    """`regimen_declarado` no devuelve un valor por defecto: levanta.

    Si devolviera uno, borrar el centinela seria la forma mas comoda de desactivar
    RNF-04 y el CI seguiria verde.
    """
    falso = tmp_path / NOMBRE_DEL_CENTINELA
    falso.write_text("con-baa\n", encoding="utf-8")
    monkeypatch.setenv(VARIABLE_DE_ENTORNO_CENTINELA, str(falso))
    with pytest.raises(RegimenIndeterminado):
        regimen_declarado()


def test_el_rechazo_por_centinela_ausente_dice_como_arreglarlo(
    tmp_path: Path, monkeypatch
) -> None:
    """Fallar cerrado tiene que ser ACCIONABLE, no solo cerrado.

    # WHY (hallazgo del gate): la raiz del repositorio se deriva de la posicion de
    # este modulo. Un despliegue que no lleve el arbol —una rueda instalada, una
    # imagen que solo copie el paquete— deja el centinela fuera de alcance y, como
    # el guard falla cerrado, TODAS las altas se rechazan. Eso es correcto. Lo que
    # no era correcto es que el mensaje no nombrara la salida: quien opera veria
    # «no se pudo leer» sin saber que existe una variable para declararlo.
    """
    monkeypatch.setenv(VARIABLE_DE_ENTORNO_CENTINELA, str(tmp_path / "no-existe"))
    with pytest.raises(RegimenIndeterminado) as capturado:
        regimen_declarado()
    mensaje = str(capturado.value)
    assert VARIABLE_DE_ENTORNO_CENTINELA in mensaje, (
        "el rechazo no nombra la variable con la que se declara el centinela: el "
        f"operador se queda encerrado sin saber por donde salir. Mensaje: {mensaje}"
    )
    assert NOMBRE_DEL_CENTINELA in mensaje


# --------------------------------------------------------------------------
# RBAC (T-015) en la misma ruta
# --------------------------------------------------------------------------
async def test_un_usuario_de_portal_no_da_de_alta_clientes(motor) -> None:
    antes = await _cuantos_clientes(motor)
    with pytest.raises(PermisoDenegado):
        await alta_de_cliente(
            motor, sesion=USUARIO_DE_PORTAL, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
        )
    assert await _cuantos_clientes(motor) == antes


# --------------------------------------------------------------------------
# El guard ESTRUCTURAL: no puede haber un segundo camino de alta
# --------------------------------------------------------------------------
#: Arboles donde vive el codigo de la APLICACION. Los tests quedan fuera: ellos
#: siembran a proposito con el rol migrador. Las migraciones tambien: crean el
#: esquema, no dan de alta clientes.
ARBOLES_DE_APLICACION = (Path("apps"), Path("packages"))
CARPETAS_QUE_NO_SON_APLICACION = ("tests", "__pycache__", ".venv", "node_modules", "migrations")

#: El unico modulo autorizado a escribir la tabla `clientes`.
MODULO_DE_ALTA = Path("apps") / "api" / "app" / "tenancy" / "baa_guard.py"

#: El unico modulo autorizado a escribir la bitacora (RF-10).
MODULO_DE_BITACORA = Path("apps") / "api" / "app" / "audit" / "bitacora.py"

_INSERTA_CLIENTES = re.compile(r"insert\s+into\s+clientes", re.IGNORECASE)

#: T-021·ter: el sector vive EN la fila del cliente, asi que cambiarlo es un
#: `UPDATE clientes`. Un segundo sitio que lo hiciera cambiaria la clasificacion
#: sin pasar por el guard — exactamente el agujero que `_INSERTA_CLIENTES` cierra
#: para el alta, pero un dia despues del alta.
_ACTUALIZA_CLIENTES = re.compile(r"update\s+clientes", re.IGNORECASE)

_INSERTA_BITACORA = re.compile(r"insert\s+into\s+bitacora", re.IGNORECASE)


def _fuentes_de_la_aplicacion() -> list[Path]:
    encontradas: list[Path] = []
    for arbol in ARBOLES_DE_APLICACION:
        for ruta in sorted((RAIZ / arbol).rglob("*.py")):
            if any(parte in CARPETAS_QUE_NO_SON_APLICACION for parte in ruta.parts):
                continue
            encontradas.append(ruta)
    return encontradas


def test_el_guard_estructural_encuentra_lo_que_dice_auditar() -> None:
    """Su propio control: un guard que no mira ningun archivo pasa siempre."""
    rutas = {r.relative_to(RAIZ).as_posix() for r in _fuentes_de_la_aplicacion()}
    assert len(rutas) >= 5, f"el guard solo encontro {len(rutas)} fuentes de aplicacion"
    assert MODULO_DE_ALTA.as_posix() in rutas
    assert MODULO_DE_BITACORA.as_posix() in rutas
    autorizado = (RAIZ / MODULO_DE_ALTA).read_text(encoding="utf-8")
    assert _INSERTA_CLIENTES.search(autorizado), (
        "el patron ya no encuentra el INSERT del modulo autorizado: dejo de reconocer "
        "la forma que dice vigilar, y el guard de abajo pasaria vacio"
    )
    assert _ACTUALIZA_CLIENTES.search(autorizado), (
        "el patron ya no encuentra el UPDATE del modulo autorizado: la reverificacion "
        "de sector (T-021·ter) dejaria de estar vigilada y el guard pasaria vacio"
    )
    assert _INSERTA_BITACORA.search((RAIZ / MODULO_DE_BITACORA).read_text(encoding="utf-8")), (
        "el patron ya no encuentra el INSERT de la bitacora: el guard del camino "
        "unico de registro (RF-10) quedaria mirando sin ver"
    )


def test_ningun_otro_modulo_de_la_aplicacion_da_de_alta_clientes() -> None:
    """Un segundo camino de alta seria un camino SIN guard de BAA.

    # WHY (`feedback_guard_solo_en_el_test` + `feedback_mecanismo_cableado_a_uno`):
    # el guard vive dentro de `alta_de_cliente`, asi que cubre ese camino y solo
    # ese. Que no aparezca un segundo es lo que hace que la cobertura sea total en
    # vez de «uno de N». Olvidarlo no compila.
    """
    autorizado = (RAIZ / MODULO_DE_ALTA).resolve()
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if ruta.resolve() != autorizado
        and _INSERTA_CLIENTES.search(ruta.read_text(encoding="utf-8"))
    ]
    assert not culpables, (
        f"estos modulos escriben la tabla `clientes` sin pasar por el guard de "
        f"RNF-04: {culpables}. El unico camino de alta es {MODULO_DE_ALTA.as_posix()}"
    )


def test_ningun_otro_modulo_cambia_la_ficha_del_cliente() -> None:
    """T-021·ter: el sector vive en la fila, asi que un `UPDATE clientes` lo mueve.

    # WHY: persistir el sector cierra un hueco y abre otro. Antes, el guard solo
    # podia esquivarse dando de alta por un camino nuevo; ahora tambien
    # ACTUALIZANDO la fila, que es una operacion mucho mas anodina de escribir. Un
    # `UPDATE clientes SET sector = ...` en cualquier otro modulo reclasificaria a
    # un cliente sin que `evaluar_alta` lo mirara — y sin dejar asiento.
    """
    autorizado = (RAIZ / MODULO_DE_ALTA).resolve()
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if ruta.resolve() != autorizado
        and _ACTUALIZA_CLIENTES.search(ruta.read_text(encoding="utf-8"))
    ]
    assert not culpables, (
        f"estos modulos actualizan la tabla `clientes` fuera del guard de RNF-04: "
        f"{culpables}. La clasificacion de un cliente solo la cambia "
        f"{MODULO_DE_ALTA.as_posix()}, y siempre con su asiento en la bitacora"
    )


def test_la_bitacora_tiene_un_unico_camino_de_escritura() -> None:
    """RF-10: «no abras un segundo camino de registro».

    # WHY: la reverificacion podria haberse guardado en una tabla de historia
    # propia. Se guarda en la bitacora que ya existe justamente para que haya UN
    # registro de lo que pasa sobre un cliente. Ese «uno» deja de ser cierto en
    # cuanto alguien escribe un segundo `INSERT INTO bitacora` fuera del modulo que
    # barre secretos del detalle (RF-09 por la puerta de RF-10).
    """
    autorizado = (RAIZ / MODULO_DE_BITACORA).resolve()
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if ruta.resolve() != autorizado
        and _INSERTA_BITACORA.search(ruta.read_text(encoding="utf-8"))
    ]
    assert not culpables, (
        f"estos modulos escriben la bitacora sin pasar por `apuntar`: {culpables}. "
        "Un segundo camino de registro no barre el detalle y no se puede corregir "
        f"despues. El unico es {MODULO_DE_BITACORA.as_posix()}"
    )


# --------------------------------------------------------------------------
# El tamiz, con sus dos listas: lo que DEBE disparar y lo que NO
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto",
    [
        "Betaclinic",
        "Policlinico Norte",
        "Clinica Dental Sur",
        "Clínica Dermatológica",  # con acentos: se normaliza antes de mirar
        "Hospital General",
        "Consultorio Medico Ruiz",
        "Farmacia del Centro",
        "Centro de Fisioterapia",
        "Laboratorio Clinico Vega",
        "Telemedicina Express",
        "Servicios HIPAA compliant",
        "Gestion de expedientes medicos",
    ],
)
def test_el_tamiz_dispara_donde_debe(texto: str) -> None:
    assert marcadores_sanitarios(texto), f"{texto!r} no disparo ningun marcador sanitario"


@pytest.mark.parametrize(
    "texto",
    [
        "Ferreteria Lopez",
        "Medicion Ambiental SA",  # contiene «medic» y NO es sanitario
        "Taller Mecanico Nunez",
        "Panaderia La Espiga",
        "Bufete Legal Torres",
        "Inmobiliaria Costa",
        "Escuela de Idiomas Aurora",
        "Logistica del Sur",
    ],
)
def test_el_tamiz_no_dispara_donde_no_debe(texto: str) -> None:
    """El control del tamiz: uno que dispara con todo no discrimina nada.

    `Medicion Ambiental` es el caso que un `"medic" in texto` habria rechazado.
    """
    assert not marcadores_sanitarios(texto), (
        f"{texto!r} disparo {marcadores_sanitarios(texto)} y no es sanitario: el tamiz "
        "rechaza de mas y el alta legitima se queda fuera"
    )


def test_declarar_salud_con_honestidad_da_el_motivo_correcto() -> None:
    """SANITARIA e INDETERMINADA no son lo mismo, y el motivo que se escribe importa.

    Confundirlos convertiria «este cliente es una clinica» en «no supe
    clasificar», que es lo que un humano leera cuando pregunte por que no entra.
    """
    veredicto = evaluar_alta(nombre="Ferreteria Lopez", sector=Sector.SALUD)
    assert veredicto.clasificacion is Clasificacion.SANITARIA
    assert not veredicto.admitida
    assert "RNF-04" in veredicto.motivo


# ==========================================================================
# T-021·ter — el sector se PERSISTE, y cambiarlo vuelve a pasar por el guard
# ==========================================================================
# El hueco que cierra esta seccion, con las palabras del modulo cuando todavia
# estaba abierto: «el guard mide el ALTA, no la vida del cliente — si manana un
# cliente de comercio se convierte en clinica, nada lo vuelve a mirar». Un
# requisito que solo se comprueba una vez deja de cumplirse al dia siguiente y
# NADA se pone rojo. Aqui se mide lo contrario, y siempre POR EFECTO: lo que
# cuenta no es el veredicto que devuelve una funcion, es lo que queda escrito en
# la fila y en la bitacora — o lo que NO queda.

OTRO_OPERADOR = Sesion(
    sesion_id="operador-b", agencia_id=AGENCIA_B, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
)

#: Un operador cuyo identificador de sesion no se parece a nada del resto del
#: modulo. Es a proposito: la sonda que comprueba que ese identificador NO acaba
#: escrito en la bitacora busca su texto dentro del actor, y con el `"operador"` de
#: `OPERADOR` la sonda se enganaba sola — `"operador"` es subcadena de
#: `"operador_agencia:..."`, asi que fallaba con el codigo correcto delante.
#: Un centinela solo mide si no se puede confundir con lo que lo rodea.
OPERADOR_CON_SESION_RECONOCIBLE = Sesion(
    sesion_id="zz-centinela-de-sesion-9f18",
    agencia_id=AGENCIA_A,
    cliente_id=None,
    rol=Rol.OPERADOR_AGENCIA,
)


async def _ficha_de(motor, cliente_id) -> tuple[str | None, object]:
    """El sector y su fecha, leidos por el camino del producto (con RLS puesta)."""
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        fila = (
            await conexion.execute(
                text("SELECT sector, sector_verificado_en FROM clientes WHERE id = :id"),
                {"id": cliente_id},
            )
        ).one()
    return fila.sector, fila.sector_verificado_en


def _sector_crudo(motor_admin, cliente_id) -> str | None:
    """Lo que la columna guarda DE VERDAD, leido con el rol migrador.

    # WHY: para comprobar que un rechazo no escribio nada en la fila de OTRA
    # agencia hace falta un camino que la vea. El del producto, por diseno, no la ve.
    """
    with motor_admin.connect() as conexion:
        return conexion.execute(
            text("SELECT sector FROM clientes WHERE id = :id"), {"id": cliente_id}
        ).scalar_one()


def _plantar_cliente(motor_admin, *, cliente_id, agencia_id, nombre, sector) -> None:
    """Escribe una ficha SALTANDOSE el guard, con el rol migrador.

    # WHY: es la unica forma de representar lo que esta seccion existe para medir —
    # un cliente cuya fila dice hoy algo distinto de lo que dijo el dia del alta.
    # El guard, por construccion, no deja crearlo; y si lo dejara, no habria nada
    # que reverificar. Va con el rol MIGRADOR y solo desde la suite: en la
    # aplicacion, `test_ningun_otro_modulo_de_la_aplicacion_da_de_alta_clientes` lo
    # prohibe.
    #
    # # WHY (se confirma y ademas se RELEE por otra conexion) — lo levanto la
    # revision cruzada, y tenia razon en el fondo aunque no en el sintoma: hoy
    # `motor_admin` corre en AUTOCOMMIT, asi que el `INSERT` si se confirmaba. Pero
    # la robustez no puede colgar de una perilla que se declara en otro archivo: si
    # alguien la quitara, la siembra se desharia en silencio y varias sondas de esta
    # seccion —las que EXIGEN un rechazo— seguirian VERDES, porque un cliente que no
    # existe tambien se rechaza. Verde por la razon equivocada, que es peor que
    # rojo. Con la relectura, el andamiaje comprueba su propio efecto.
    """
    with motor_admin.begin() as conexion:
        conexion.execute(
            text(
                "INSERT INTO clientes (id, agencia_id, nombre, sector, sector_verificado_en) "
                "VALUES (:id, :a, :nombre, :sector, now())"
            ),
            {"id": cliente_id, "a": agencia_id, "nombre": nombre, "sector": sector},
        )
    with motor_admin.connect() as otra:
        plantado = otra.execute(
            text("SELECT nombre, sector FROM clientes WHERE id = :id"), {"id": cliente_id}
        ).one_or_none()
    assert plantado is not None, (
        f"la siembra de {nombre!r} no quedo visible desde otra conexion: la sonda que "
        "la usa mediria un cliente que no existe, y las que esperan un rechazo saldrian "
        "VERDES por la razon equivocada"
    )
    assert (plantado.nombre, plantado.sector) == (nombre, sector)


async def _apuntes_de_reverificacion(motor) -> list:
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        apuntes = await leer_apuntes(conexion)
    return [a for a in apuntes if a.accion == ACCION_REVERIFICACION]


# --------------------------------------------------------------------------
# El alta deja el sector escrito
# --------------------------------------------------------------------------
async def test_el_alta_persiste_el_sector_y_la_fecha_en_que_se_verifico(motor) -> None:
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    sector, verificado_en = await _ficha_de(motor, nuevo)
    assert sector == Sector.COMERCIO.value, (
        "el alta admitio al cliente y no dejo escrito con que sector: el guard "
        "vuelve a medir solo el instante del alta"
    )
    assert verificado_en is not None, (
        "sin la fecha de verificacion nadie puede contestar «a quien hay que volver "
        "a mirar», que es la mitad del sentido de persistir el sector"
    )


async def test_un_alta_rechazada_no_deja_ninguna_clasificacion(motor) -> None:
    """El control por el otro lado: lo rechazado no escribe ni fila ni sector."""
    with pytest.raises(AltaRechazada):
        await alta_de_cliente(
            motor, sesion=OPERADOR, nombre="Clinica Dental Sur", sector=Sector.COMERCIO
        )
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        cuantos = (
            await conexion.execute(
                text("SELECT count(*) FROM clientes WHERE nombre = :n"),
                {"n": "Clinica Dental Sur"},
            )
        ).scalar_one()
    assert cuantos == 0


async def test_un_cliente_anterior_a_la_migracion_queda_indeterminado(motor) -> None:
    """`NULL` no es «da igual»: es un sector que nadie declaro.

    Los clientes sembrados existen desde antes de la revision 0004 y no traen
    sector. Leerlos tiene que devolver `None`, no un valor por defecto — un relleno
    seria una clasificacion que nadie hizo, indistinguible de una verificada.
    """
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        actual = await sector_persistido(conexion, cliente_id=CLIENTE_A1)
    assert actual is not None and actual.sector is None
    assert actual.nombre == "Cliente A1"


# --------------------------------------------------------------------------
# El cambio de sector: se reverifica y queda escrito
# --------------------------------------------------------------------------
async def test_el_cambio_de_sector_se_reverifica_y_deja_asiento_en_la_bitacora(motor) -> None:
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )

    resultado = await reverificar_sector(
        motor,
        sesion=OPERADOR_CON_SESION_RECONOCIBLE,
        cliente_id=nuevo,
        sector=Sector.HOSTELERIA,
    )

    assert resultado.cambio is True
    assert (resultado.sector_anterior, resultado.sector) == (Sector.COMERCIO, Sector.HOSTELERIA)
    sector, _ = await _ficha_de(motor, nuevo)
    assert sector == Sector.HOSTELERIA.value, "la reverificacion decidio y no escribio"

    apuntes = await _apuntes_de_reverificacion(motor)
    suyos = [a for a in apuntes if a.id == resultado.apunte_id]
    assert len(suyos) == 1, (
        "el cambio de clasificacion de un cliente no dejo su asiento: RF-10 exige "
        "quien, que y cuando sobre los datos de un cliente"
    )
    apunte = suyos[0]
    assert apunte.cliente_id == nuevo, (
        "el asiento cuelga del inquilino equivocado: la bitacora del cliente al que "
        "le cambiaron la clasificacion no tendria rastro de ello"
    )
    assert apunte.detalle["sector_anterior"] == Sector.COMERCIO.value
    assert apunte.detalle["sector"] == Sector.HOSTELERIA.value
    assert apunte.detalle["cambio"] is True
    assert apunte.actor.startswith(f"{Rol.OPERADOR_AGENCIA.value}:"), (
        f"el asiento no dice quien lo pidio: actor={apunte.actor!r}"
    )
    assert OPERADOR_CON_SESION_RECONOCIBLE.sesion_id not in apunte.actor, (
        "el asiento guarda el identificador de sesion EN CLARO: es el mango con el "
        "que se revoca, y la bitacora no se puede corregir despues"
    )


async def test_reverificar_sin_cambiar_el_sector_tambien_deja_asiento(motor) -> None:
    """Refrescar la fecha tambien es una escritura, y ninguna es silenciosa."""
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    _, antes = await _ficha_de(motor, nuevo)

    resultado = await reverificar_sector(
        motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.COMERCIO
    )

    assert resultado.cambio is False
    _, despues = await _ficha_de(motor, nuevo)
    assert despues >= antes
    apunte = next(a for a in await _apuntes_de_reverificacion(motor) if a.id == resultado.apunte_id)
    assert apunte.detalle["cambio"] is False


async def test_la_reverificacion_saca_a_un_cliente_de_indeterminado(motor) -> None:
    """El camino de vuelta desde `NULL` es el guard, no un `UPDATE` a mano."""
    resultado = await reverificar_sector(
        motor, sesion=OPERADOR, cliente_id=CLIENTE_A1, sector=Sector.COMERCIO
    )
    assert resultado.sector_anterior is None and resultado.cambio is True
    sector, _ = await _ficha_de(motor, CLIENTE_A1)
    assert sector == Sector.COMERCIO.value
    apunte = next(a for a in await _apuntes_de_reverificacion(motor) if a.id == resultado.apunte_id)
    assert apunte.detalle["sector_anterior"] is None


# --------------------------------------------------------------------------
# Fail-closed: lo mismo que en el alta, y nada se escribe
# --------------------------------------------------------------------------
async def test_reverificar_hacia_salud_se_rechaza_y_no_toca_la_fila(motor) -> None:
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    apuntes_antes = len(await _apuntes_de_reverificacion(motor))

    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(
            motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.SALUD
        )

    assert capturado.value.veredicto.clasificacion is Clasificacion.SANITARIA
    sector, _ = await _ficha_de(motor, nuevo)
    assert sector == Sector.COMERCIO.value, "el rechazo escribio igual"
    assert len(await _apuntes_de_reverificacion(motor)) == apuntes_antes, (
        "un rechazo dejo asiento: la bitacora contaria como reverificado un cliente "
        "que no se reverifico"
    )


@pytest.mark.parametrize(
    "sector",
    [None, "", "   ", "sanidad", "salud_mental", "SALUD", 42, "otro sector"],
)
async def test_un_sector_indeterminado_en_la_reverificacion_se_rechaza(motor, sector) -> None:
    """La misma tabla de casos que el alta: la regla es UNA, en dos momentos."""
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(motor, sesion=OPERADOR, cliente_id=nuevo, sector=sector)
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert (await _ficha_de(motor, nuevo))[0] == Sector.COMERCIO.value


async def test_el_nombre_que_se_tamiza_es_el_persistido_y_no_el_que_traiga_quien_llama(
    motor, motor_admin
) -> None:
    """==El caso que RNF-04 nombra, un dia despues del alta.==

    Un cliente cuya ficha dice hoy «Policlinico» y sigue declarado `comercio`: es
    literalmente «el comercio que manana se vuelve clinica». La reverificacion tiene
    que rechazarlo aunque quien llama no diga nada raro, porque la segunda fuente
    sale de la FILA. Y un compuesto asi es ademas el caso que casi se escapa (P-22):
    con el patron anclado (`\\bclinic`) no disparaba, porque no hay frontera de
    palabra antes de «clinic».
    """
    disfrazado = UUID("aaaaaaaa-0000-4000-8000-00000000dc01")
    _plantar_cliente(
        motor_admin,
        cliente_id=disfrazado,
        agencia_id=AGENCIA_A,
        nombre="Policlinico",
        sector=Sector.COMERCIO.value,
    )

    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(
            motor, sesion=OPERADOR, cliente_id=disfrazado, sector=Sector.COMERCIO
        )

    veredicto = capturado.value.veredicto
    assert veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert "clinica" in veredicto.marcadores, (
        f"la reverificacion no vio el nombre persistido: marcadores={veredicto.marcadores}"
    )


async def test_la_descripcion_solo_puede_endurecer_el_veredicto(motor) -> None:
    """Se admite por parametro porque los marcadores solo se SUMAN."""
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Grupo Aurora", sector=Sector.COMERCIO
    )
    with pytest.raises(ReverificacionRechazada):
        await reverificar_sector(
            motor,
            sesion=OPERADOR,
            cliente_id=nuevo,
            sector=Sector.COMERCIO,
            descripcion="Gestion de historias clinicas para pacientes de la zona",
        )


@pytest.mark.parametrize("guardado", ["sanidad", "", "salud"])
async def test_un_sector_guardado_que_este_codigo_no_sabe_leer_falla_cerrado(
    motor, motor_admin, guardado
) -> None:
    """No poder leer lo que hay escrito no es «adelante»: es indeterminado.

    `salud` esta en la lista a proposito: ningun camino de este modulo lo admite,
    asi que encontrarlo escrito significa que la fila la puso alguien sin guard.
    Darlo por verificado seria firmar justo lo que RNF-04 no admite.
    """
    raro = UUID("aaaaaaaa-0000-4000-8000-00000000dc02")
    _plantar_cliente(
        motor_admin,
        cliente_id=raro,
        agencia_id=AGENCIA_A,
        nombre="Negocio Cualquiera",
        sector=guardado,
    )
    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(motor, sesion=OPERADOR, cliente_id=raro, sector=Sector.COMERCIO)
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert _sector_crudo(motor_admin, raro) == guardado, "el rechazo escribio igual"


async def test_sin_regimen_interpretable_tampoco_se_reverifica(
    motor, tmp_path: Path, monkeypatch
) -> None:
    """El centinela es LOAD-BEARING tambien aqui, no solo en el alta."""
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    monkeypatch.setenv(VARIABLE_DE_ENTORNO_CENTINELA, str(tmp_path / "no-existe"))
    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(
            motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.HOSTELERIA
        )
    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert (await _ficha_de(motor, nuevo))[0] == Sector.COMERCIO.value


# --------------------------------------------------------------------------
# Aislamiento y RBAC sobre el camino nuevo
# --------------------------------------------------------------------------
async def test_un_operador_no_reverifica_al_cliente_de_otra_agencia(motor, motor_admin) -> None:
    """RLS deja la fila fuera de alcance, y fuera de alcance es indeterminado."""
    ajeno = UUID("bbbbbbbb-0000-4000-8000-00000000dc03")
    _plantar_cliente(
        motor_admin,
        cliente_id=ajeno,
        agencia_id=AGENCIA_B,
        nombre="Cliente B ajeno",
        sector=Sector.COMERCIO.value,
    )

    with pytest.raises(ReverificacionRechazada) as capturado:
        await reverificar_sector(motor, sesion=OPERADOR, cliente_id=ajeno, sector=Sector.LEGAL)

    assert capturado.value.veredicto.clasificacion is Clasificacion.INDETERMINADA
    assert _sector_crudo(motor_admin, ajeno) == Sector.COMERCIO.value, (
        "el operador de la agencia A cambio la clasificacion de un cliente de la B"
    )

    # CONTROL: el operador de SU agencia si puede. Sin esto, un guard que lo negara
    # todo pasaria esta sonda con nota.
    resultado = await reverificar_sector(
        motor, sesion=OTRO_OPERADOR, cliente_id=ajeno, sector=Sector.LEGAL
    )
    assert resultado.cambio is True
    assert _sector_crudo(motor_admin, ajeno) == Sector.LEGAL.value


async def test_un_usuario_de_portal_no_reverifica_el_sector(motor) -> None:
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    with pytest.raises(PermisoDenegado):
        await reverificar_sector(
            motor, sesion=USUARIO_DE_PORTAL, cliente_id=nuevo, sector=Sector.LEGAL
        )
    assert (await _ficha_de(motor, nuevo))[0] == Sector.COMERCIO.value


async def test_si_el_asiento_de_la_bitacora_falla_el_cambio_se_deshace(motor, monkeypatch) -> None:
    """La atomicidad que el docstring promete, MEDIDA — la señaló la revisión cruzada.

    # WHY: `reverificar_sector` afirma que el asiento va «en la MISMA transaccion
    # que el cambio» y que «si el asiento fallara, el cambio se deshace». Eso era
    # prosa: ninguna sonda lo comprobaba, y una promesa sin medida es justo lo que
    # este repositorio existe para no repetir. Si se rompiera —por ejemplo abriendo
    # una transaccion propia para el apunte— la reclasificacion quedaria hecha SIN
    # rastro, que es lo que RF-10 prohibe.
    """
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )
    antes = await _ficha_de(motor, nuevo)
    apuntes_antes = len(await _apuntes_de_reverificacion(motor))

    async def _la_bitacora_no_acepta(*_argumentos, **_nombrados):
        raise RuntimeError("la bitacora no acepto el asiento")

    monkeypatch.setattr("app.tenancy.baa_guard.apuntar", _la_bitacora_no_acepta)

    with pytest.raises(RuntimeError):
        await reverificar_sector(motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.LEGAL)

    # Releido por otra conexion: la de la operacion ya se deshizo.
    assert await _ficha_de(motor, nuevo) == antes, (
        "el sector cambio aunque el asiento no se pudo escribir: la reclasificacion "
        "quedaria hecha sin rastro, que es exactamente lo que RF-10 prohibe"
    )
    assert len(await _apuntes_de_reverificacion(motor)) == apuntes_antes


async def test_dos_reverificaciones_a_la_vez_no_inventan_una_transicion(motor) -> None:
    """==Hallazgo de Crisol, verificado con esta sonda antes de aceptarlo.==

    Dos operadores reverifican el mismo cliente a la vez. El estado final da igual
    —manda el ultimo—, pero los DOS asientos tienen que encadenar: el `sector_anterior`
    de cada uno es el `sector` del que va delante.

    # WHY (por que importa, y por que no es un problema de «ultimo gana»): sin
    # cerrojo, las dos transacciones LEEN el mismo sector viejo y despues escriben
    # una detras de otra. El estado final es correcto y la bitacora —que nadie puede
    # corregir— registra dos transiciones desde `comercio`, cuando la segunda salio
    # de otro sitio. Es exactamente el defecto que la ruta destructiva ya cerro
    # mirando `rowcount`: el informe mintiendo en la direccion mas cara. Aqui
    # `rowcount` no lo ve, porque la fila SI existe: lo que caduco es lo leido.
    """
    nuevo = await alta_de_cliente(
        motor, sesion=OPERADOR, nombre="Ferreteria Lopez", sector=Sector.COMERCIO
    )

    resultados = await asyncio.gather(
        reverificar_sector(motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.HOSTELERIA),
        reverificar_sector(motor, sesion=OPERADOR, cliente_id=nuevo, sector=Sector.LEGAL),
    )

    asientos = {
        a.id: a.detalle
        for a in await _apuntes_de_reverificacion(motor)
        if a.id in {r.apunte_id for r in resultados}
    }
    assert len(asientos) == 2, f"se esperaban dos asientos y hay {len(asientos)}"

    desde_el_alta = [d for d in asientos.values() if d["sector_anterior"] == Sector.COMERCIO.value]
    assert len(desde_el_alta) == 1, (
        f"{len(desde_el_alta)} asientos dicen venir de {Sector.COMERCIO.value!r}, y solo "
        "uno puede: los dos leyeron el sector viejo y la bitacora registra una "
        "transicion que no ocurrio"
    )
    primero = desde_el_alta[0]
    segundo = next(d for d in asientos.values() if d is not primero)
    assert segundo["sector_anterior"] == primero["sector"], (
        f"la cadena no encadena: el segundo asiento dice venir de "
        f"{segundo['sector_anterior']!r} y el primero dejo {primero['sector']!r}"
    )
    assert (await _ficha_de(motor, nuevo))[0] == segundo["sector"], (
        "el estado final no es el del ultimo asiento: la bitacora y la fila cuentan "
        "historias distintas"
    )


def test_los_dos_rechazos_del_guard_comparten_base() -> None:
    """Capturar uno y dejarse el otro fuera es como se pierde el nuevo camino."""
    assert issubclass(AltaRechazada, GuardDeBaaRechaza)
    assert issubclass(ReverificacionRechazada, GuardDeBaaRechaza)


def test_solo_una_de_las_tres_clasificaciones_admite_el_alta() -> None:
    """ALLOWLIST de veredictos: una clasificacion nueva no nace admitida.

    Recorre el enum entero, asi que anadir un cuarto veredicto sin decidir si
    admite o no pone el CI en rojo.
    """
    admitidas = [c for c in Clasificacion if VeredictoDeAlta(c, "").admitida]
    assert admitidas == [Clasificacion.NO_SANITARIA]
