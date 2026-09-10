"""T-100 (RF-18, B4) — la credencial del proveedor, medida por efecto.

Cinco preguntas, cada una con su control:

1. ¿Se puede registrar un proveedor fuera de la lista, o uno sin ficha? · control: uno
   declarado y con ficha SI se registra.
2. ¿Se guarda algo cuando el proveedor RECHAZA o cuando no contesta? · control: cuando
   acepta, la fila existe, esta cifrada y se vuelve a leer.
3. ¿La validacion sale por el guard de red unico? · estructural, con su sabotaje.
4. ¿Sale la credencial en claro por alguna via? · `repr`, barrido y serializador.
5. **B4**: ¿un cliente REAL sin credencial propia llega a la clave de agencia? · control:
   un alta de desarrollo si la usa · sabotaje: quitarle la marca hace aparecer el rojo.

# WHY (el proveedor de la bateria es uno de mentira, con URL en `.invalid`): la
# regla se mide sin tocar la red — como en `test_egreso_red.py` con el resolutor
# inyectado. Y la lista real se mide aparte, por su FORMA: que nadie declare una
# URL que el guard no dejaria salir.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from app.agents import providers
from app.agents.providers import (
    LISTA_DECLARADA,
    CredencialRechazadaPorElProveedor,
    CredencialResuelta,
    CredencialVacia,
    FichaDeCondiciones,
    Proveedor,
    ProveedorNoDeclarado,
    ProveedorNoVerificable,
    ProveedorSinCondicionesVerificadas,
    SesionSinCliente,
    SinCredencial,
    Titular,
    UrlDeValidacionInvalida,
    nombre_del_secreto,
    registrar_credencial,
    resolver_credencial,
    variable_de_agencia,
)
from app.tenancy import sesion_de_inquilino
from app.tenancy.auth import Rol, Sesion
from app.tenancy.baa_guard import Sector
from app.tenancy.secrets import (
    SecretoEnClaro,
    SecretoEnLaRespuesta,
    barrer,
    clave_de_cifrado,
    leer,
    serializar,
)
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    RAIZ,
    alta_de_prueba,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)
from egress import red

MODULO = RAIZ / "apps" / "api" / "app" / "agents" / "providers.py"

#: La credencial de la bateria. No es una credencial: es el testigo que las sondas
#: buscan —y no deben encontrar— en filas y respuestas. Nace y muere con la corrida.
TESTIGO = "credencial-de-prueba-que-no-debe-verse"
NOMBRE = "prueba"

OPERADOR = Sesion(
    sesion_id="operador", agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
)


def _cabeceras(credencial: SecretoEnClaro) -> dict[str, str]:
    return {"Authorization": "Bearer " + credencial.revelar()}


FICHA = FichaDeCondiciones(
    no_entrenamiento_por_defecto=True,
    retencion="de prueba",
    acceso_humano="de prueba",
    ubicacion="de prueba",
    verificada_en=date(2026, 9, 9),
    fuente="https://proveedor.invalid/condiciones",
)
CON_FICHA = Proveedor(
    nombre=NOMBRE,
    url_de_validacion="https://proveedor.invalid/v1/modelos",
    cabeceras=_cabeceras,
    condiciones=FICHA,
)
SIN_FICHA = Proveedor(
    nombre="sin_ficha",
    url_de_validacion="https://otro.invalid/v1/modelos",
    cabeceras=_cabeceras,
    condiciones=None,
)
LISTA = MappingProxyType({NOMBRE: CON_FICHA, "sin_ficha": SIN_FICHA})


def _proveedor_que_contesta(codigo: int, llamadas: list):
    """Un `pedir` que contesta lo que se le diga y anota con que lo llamaron."""

    async def pedir(url, *, metodo="POST", contenido=None, cabeceras=None, **_):
        llamadas.append((url, metodo, dict(cabeceras or {})))
        return httpx.Response(codigo, request=httpx.Request(metodo, url))

    return pedir


def _proveedor_que_no_contesta(fallo: Exception, llamadas: list):
    async def pedir(url, *, metodo="POST", contenido=None, cabeceras=None, **_):
        llamadas.append((url, metodo, dict(cabeceras or {})))
        raise fallo

    return pedir


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Este modulo ESCRIBE secretos y clientes. Cada sonda arranca del mismo escenario."""
    resembrar(motor_de_siembra)


def _filas_del_secreto(motor_admin, cliente_id: UUID, proveedor: str = NOMBRE) -> list:
    with motor_admin.connect() as conexion:
        return conexion.execute(
            text("SELECT cifrado FROM secretos WHERE cliente_id = :c AND nombre = :n"),
            {"c": cliente_id, "n": nombre_del_secreto(proveedor)},
        ).all()


async def _registrar(motor, *, proveedor: str, pedir, credencial: str = TESTIGO):
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        return await registrar_credencial(
            conexion,
            inquilino,
            proveedor=proveedor,
            credencial=SecretoEnClaro(credencial),
            clave=clave_de_cifrado(),
            pedir=pedir,
            lista=LISTA,
        )


# ==========================================================================
# 1 — la lista declarada, y la ficha
# ==========================================================================
async def test_un_proveedor_fuera_de_la_lista_no_se_registra(motor, motor_admin) -> None:
    llamadas: list = []
    with pytest.raises(ProveedorNoDeclarado):
        await _registrar(
            motor, proveedor="inventado", pedir=_proveedor_que_contesta(200, llamadas)
        )
    assert llamadas == [], "se llamo al proveedor ANTES de comprobar que estaba en la lista"
    assert _filas_del_secreto(motor_admin, CLIENTE_A1, "inventado") == []


async def test_un_proveedor_sin_ficha_de_condiciones_no_se_registra(motor, motor_admin) -> None:
    """RF-18 ampliado: sin condiciones verificadas y fechadas, la lista no basta."""
    llamadas: list = []
    with pytest.raises(ProveedorSinCondicionesVerificadas):
        await _registrar(
            motor, proveedor="sin_ficha", pedir=_proveedor_que_contesta(200, llamadas)
        )
    assert llamadas == []
    assert _filas_del_secreto(motor_admin, CLIENTE_A1, "sin_ficha") == []


def test_hoy_ningun_proveedor_real_tiene_ficha_y_por_eso_ninguno_registra() -> None:
    """Lo que la lista real dice HOY, dicho en voz alta: cerrada hasta T-100·bis.

    # WHY: es la prueba que se pondra roja el dia que T-100·bis fecha una ficha, y
    # entonces se reescribe a proposito — no un `skip` que deja la lista sin mirar.
    """
    assert LISTA_DECLARADA, "una lista vacia pasaria todo lo de abajo sin medir nada"
    for nombre in LISTA_DECLARADA:
        with pytest.raises(ProveedorSinCondicionesVerificadas):
            providers.proveedor_declarado(nombre)


def test_toda_url_de_validacion_declarada_es_lo_que_el_guard_deja_salir() -> None:
    """Por FORMA: https, sin puerto, con nombre. Sin tocar el DNS."""
    for proveedor in LISTA_DECLARADA.values():
        partes = httpx.URL(proveedor.url_de_validacion)
        assert partes.scheme == "https", proveedor.nombre
        assert partes.port is None, proveedor.nombre
        assert partes.host, proveedor.nombre


@pytest.mark.parametrize(
    "url",
    [
        "http://proveedor.invalid/v1",
        "https://proveedor.invalid:8443/v1",
        "https:///v1",
        "ftp://x/v1",
    ],
)
def test_una_url_que_el_guard_no_dejaria_salir_se_cae_al_declararla(url: str) -> None:
    with pytest.raises(UrlDeValidacionInvalida):
        Proveedor(nombre="mal", url_de_validacion=url, cabeceras=_cabeceras, condiciones=FICHA)


# ==========================================================================
# 2 — validar contra el proveedor, y solo entonces guardar
# ==========================================================================
async def test_control_una_credencial_que_el_proveedor_acepta_se_guarda_cifrada(
    motor, motor_admin
) -> None:
    llamadas: list = []
    await _registrar(motor, proveedor=NOMBRE, pedir=_proveedor_que_contesta(200, llamadas))

    # Se valido CON la credencial, contra la URL declarada, por GET.
    assert len(llamadas) == 1
    url, metodo, cabeceras = llamadas[0]
    assert url == CON_FICHA.url_de_validacion
    assert metodo == "GET"
    assert cabeceras["Authorization"] == "Bearer " + TESTIGO

    # En la base hay texto cifrado, no el valor.
    filas = _filas_del_secreto(motor_admin, CLIENTE_A1)
    assert len(filas) == 1
    assert TESTIGO.encode("utf-8") not in bytes(filas[0].cifrado)

    # Y se vuelve a leer por el camino de produccion — el control del cifrado.
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        leido = await leer(
            conexion, inquilino, nombre=nombre_del_secreto(NOMBRE), clave=clave_de_cifrado()
        )
    assert leido == SecretoEnClaro(TESTIGO)


@pytest.mark.parametrize("codigo", [401, 403])
async def test_una_credencial_que_el_proveedor_rechaza_no_se_guarda(
    motor, motor_admin, codigo: int
) -> None:
    llamadas: list = []
    with pytest.raises(CredencialRechazadaPorElProveedor) as capturado:
        await _registrar(
            motor, proveedor=NOMBRE, pedir=_proveedor_que_contesta(codigo, llamadas)
        )
    assert len(llamadas) == 1, "no se llego a preguntar al proveedor"
    assert _filas_del_secreto(motor_admin, CLIENTE_A1) == []
    # RF-09 «ni siquiera al rechazarlo»: el error no cita la credencial.
    assert TESTIGO not in str(capturado.value)


@pytest.mark.parametrize(
    "proveedor_roto",
    [
        lambda llamadas: _proveedor_que_contesta(429, llamadas),
        lambda llamadas: _proveedor_que_contesta(500, llamadas),
        lambda llamadas: _proveedor_que_contesta(302, llamadas),
        lambda llamadas: _proveedor_que_no_contesta(red.SalidaFallida("no contesta"), llamadas),
        lambda llamadas: _proveedor_que_no_contesta(red.DestinoRechazado("interno"), llamadas),
    ],
    ids=["429", "500", "302", "red-caida", "destino-rechazado"],
)
async def test_si_no_se_puede_saber_no_se_acepta(motor, motor_admin, proveedor_roto) -> None:
    """Fail-closed: «no se pudo verificar» no es «vale». Nada se guarda."""
    llamadas: list = []
    with pytest.raises(ProveedorNoVerificable) as capturado:
        await _registrar(motor, proveedor=NOMBRE, pedir=proveedor_roto(llamadas))
    assert len(llamadas) == 1
    assert _filas_del_secreto(motor_admin, CLIENTE_A1) == []
    assert TESTIGO not in str(capturado.value)


async def test_una_credencial_vacia_no_llega_al_proveedor(motor, motor_admin) -> None:
    llamadas: list = []
    for vacia in ("", "   "):
        with pytest.raises(CredencialVacia):
            await _registrar(
                motor,
                proveedor=NOMBRE,
                pedir=_proveedor_que_contesta(200, llamadas),
                credencial=vacia,
            )
    assert llamadas == []
    assert _filas_del_secreto(motor_admin, CLIENTE_A1) == []


async def test_registrar_dos_veces_reemplaza_y_no_duplica(motor, motor_admin) -> None:
    llamadas: list = []
    await _registrar(motor, proveedor=NOMBRE, pedir=_proveedor_que_contesta(200, llamadas))
    await _registrar(
        motor,
        proveedor=NOMBRE,
        pedir=_proveedor_que_contesta(200, llamadas),
        credencial="otra-nueva",
    )
    assert len(_filas_del_secreto(motor_admin, CLIENTE_A1)) == 1
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        leido = await leer(
            conexion, inquilino, nombre=nombre_del_secreto(NOMBRE), clave=clave_de_cifrado()
        )
    assert leido == SecretoEnClaro("otra-nueva")


# ==========================================================================
# 3 — la validacion sale por el guard de red unico (estructural)
# ==========================================================================
def test_la_validacion_sale_por_el_guard_de_red() -> None:
    """El valor por defecto de `pedir` ES el guard. Cambiarlo por un cliente suelto es rojo."""
    defecto = inspect.signature(registrar_credencial).parameters["pedir"].default
    assert defecto is red.pedir


_ABRE_RED = re.compile(
    r"^\s*(?:import\s+(httpx|socket|aiohttp)|from\s+(httpx|socket|aiohttp|urllib\.request)\b)",
    re.M,
)


def test_el_modulo_no_abre_ninguna_conexion_por_su_cuenta() -> None:
    fuente = MODULO.read_text(encoding="utf-8")
    assert not _ABRE_RED.search(fuente), "providers.py importa un cliente de red fuera del guard"
    # El control del control: el patron SI caza lo que dice cazar.
    assert _ABRE_RED.search("import httpx\n")
    assert _ABRE_RED.search("from urllib.request import urlopen\n")
    assert not _ABRE_RED.search("from urllib.parse import urlsplit\n")


# ==========================================================================
# 4 — la credencial no sale en claro por ninguna via
# ==========================================================================
def test_la_credencial_resuelta_no_se_ensena_ni_pasa_el_barrido() -> None:
    resuelta = CredencialResuelta(TESTIGO, proveedor=NOMBRE, titular=Titular.CLIENTE)
    assert repr(resuelta) == "[REDACTADO]"
    assert TESTIGO not in f"{resuelta!r} {resuelta} {[resuelta]}"
    assert resuelta.titular is Titular.CLIENTE
    with pytest.raises(SecretoEnLaRespuesta):
        barrer({"credencial": resuelta})
    # Control: es un SecretoEnClaro de verdad, comparable en tiempo constante.
    assert resuelta == SecretoEnClaro(TESTIGO)


def test_la_fila_del_secreto_serializada_no_lleva_el_material() -> None:
    fila = {
        "id": "11111111-1111-4111-8111-111111111111",
        "agencia_id": str(AGENCIA_A),
        "cliente_id": str(CLIENTE_A1),
        "nombre": nombre_del_secreto(NOMBRE),
        "creado_en": "2026-09-09T00:00:00Z",
        "actualizado_en": "2026-09-09T00:00:00Z",
        "cifrado": b"\x00material",
    }
    salida = serializar("secretos", fila)
    assert "cifrado" not in salida
    assert salida["nombre"] == nombre_del_secreto(NOMBRE)


# ==========================================================================
# 5 — B4: quien paga. Sonda, control y sabotaje.
# ==========================================================================
CLAVE_DE_AGENCIA = "clave-de-agencia-de-prueba"


async def _alta(motor, nombre: str, *, desarrollo: bool) -> UUID:
    return await alta_de_prueba(
        motor, sesion=OPERADOR, nombre=nombre, sector=Sector.COMERCIO, desarrollo=desarrollo
    )


async def _resolver(motor, cliente_id: UUID, entorno: dict[str, str]) -> CredencialResuelta:
    inquilino = sesion_de_cliente(AGENCIA_A, cliente_id)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        return await resolver_credencial(
            conexion, inquilino, proveedor=NOMBRE, clave=clave_de_cifrado(), entorno=entorno
        )


def _entorno_con_clave_de_agencia() -> dict[str, str]:
    return {variable_de_agencia(NOMBRE): CLAVE_DE_AGENCIA}


async def test_el_alta_persiste_la_marca_de_desarrollo_y_por_defecto_es_real(motor) -> None:
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    real = await alta_de_prueba(
        motor, sesion=OPERADOR, nombre="Negocio Real", sector=Sector.COMERCIO
    )
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        marcas = dict(
            (
                await conexion.execute(
                    text("SELECT id, desarrollo FROM clientes WHERE id IN (:t, :r)"),
                    {"t": taller, "r": real},
                )
            ).all()
        )
    assert marcas[taller] is True
    assert marcas[real] is False


async def test_sonda_un_cliente_real_sin_credencial_propia_nunca_usa_la_de_agencia(
    motor,
) -> None:
    """==B4 como mecanismo==: la clave de agencia esta en el entorno y aun asi no se usa."""
    real = await _alta(motor, "Negocio Real", desarrollo=False)
    with pytest.raises(SinCredencial) as capturado:
        await _resolver(motor, real, _entorno_con_clave_de_agencia())
    assert CLAVE_DE_AGENCIA not in str(capturado.value)


async def test_control_un_alta_de_desarrollo_si_usa_la_clave_de_agencia(motor) -> None:
    """Sin este control, un `resolver` que negara todo pasaria la sonda de arriba."""
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    resuelta = await _resolver(motor, taller, _entorno_con_clave_de_agencia())
    assert resuelta.titular is Titular.AGENCIA, "el consumo se carga al techo de la agencia"
    assert resuelta == SecretoEnClaro(CLAVE_DE_AGENCIA)
    assert resuelta.proveedor == NOMBRE


async def test_sabotaje_quitar_la_marca_de_desarrollo_hace_aparecer_el_rojo(
    motor, motor_admin
) -> None:
    """El mismo cliente, la misma clave en el entorno: solo cambia la fila. Y cambia todo."""
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    antes = await _resolver(motor, taller, _entorno_con_clave_de_agencia())
    assert antes.titular is Titular.AGENCIA

    with motor_admin.begin() as conexion:
        conexion.execute(
            text("UPDATE clientes SET desarrollo = false WHERE id = :id"), {"id": taller}
        )

    with pytest.raises(SinCredencial):
        await _resolver(motor, taller, _entorno_con_clave_de_agencia())


async def test_la_credencial_propia_manda_sobre_la_de_agencia_aunque_sea_desarrollo(
    motor,
) -> None:
    """BYOK: si el cliente registro la suya, paga el cliente — desarrollo o no."""
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    inquilino = sesion_de_cliente(AGENCIA_A, taller)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await registrar_credencial(
            conexion,
            inquilino,
            proveedor=NOMBRE,
            credencial=SecretoEnClaro(TESTIGO),
            clave=clave_de_cifrado(),
            pedir=_proveedor_que_contesta(200, []),
            lista=LISTA,
        )
    resuelta = await _resolver(motor, taller, _entorno_con_clave_de_agencia())
    assert resuelta.titular is Titular.CLIENTE
    assert resuelta == SecretoEnClaro(TESTIGO)


async def test_desarrollo_sin_clave_de_agencia_en_el_entorno_tampoco_resuelve(motor) -> None:
    """Fail-closed en la otra direccion: la clave de agencia no se inventa."""
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    with pytest.raises(SinCredencial):
        await _resolver(motor, taller, {})
    with pytest.raises(SinCredencial):
        await _resolver(motor, taller, {variable_de_agencia(NOMBRE): "   "})


async def test_una_sesion_de_agencia_no_resuelve_credencial(motor) -> None:
    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        with pytest.raises(SesionSinCliente):
            await resolver_credencial(
                conexion,
                operador,
                proveedor=NOMBRE,
                clave=clave_de_cifrado(),
                entorno=_entorno_con_clave_de_agencia(),
            )


async def test_la_marca_de_un_cliente_no_se_lee_desde_otro_cliente(motor) -> None:
    """RLS: el vecino no ve la fila del taller, asi que para el vecino no es desarrollo."""
    taller = await _alta(motor, "Taller de desarrollo", desarrollo=True)
    vecino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, vecino) as conexion:
        visible = (
            await conexion.execute(
                text("SELECT desarrollo FROM clientes WHERE id = :id"), {"id": taller}
            )
        ).scalar_one_or_none()
    assert visible is None, "la marca del taller es visible desde la sesion de otro cliente"


def test_el_nombre_del_secreto_es_el_declarado() -> None:
    assert nombre_del_secreto("anthropic") == "credencial_modelo:anthropic"
    assert Path(MODULO).exists()
