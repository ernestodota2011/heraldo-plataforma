"""T-111 (RF-16) — el techo de gasto CORTA de verdad, y avisa antes de cortar.

# WHY (por que esta bateria mide contra Postgres y no contra un doble): el corte
# es una propiedad de la TRANSACCION, no del codigo. `SELECT ... FOR UPDATE`, el
# bloqueo de fila y el orden en que dos transacciones ven la suma son
# comportamiento del motor; un doble en memoria mediria el doble y saldria verde
# justo donde el dinero se escapa (RNF-03: ningun estado compartido en la memoria
# de un proceso).
#
# WHY (la prueba de concurrencia es la que funda la casilla): un techo que se
# comprueba «leo la suma, decido, escribo» sin bloqueo pasa cualquier prueba
# secuencial y deja pasar N veces el techo en cuanto hay dos procesos. Aqui se
# lanzan 24 consumos A LA VEZ, cada uno cercano al techo, y se exige que la suma
# final NO lo supere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.agents.providers import Titular
from app.tenancy import sesion_de_inquilino
from app.tenancy.budget import (
    ARCHIVO_DE_PRECIOS,
    CONCEPTO_MENSAJERIA,
    CONCEPTO_MODELO,
    DIAS_DE_VIGENCIA_DEL_CATALOGO,
    TECHO_POR_DEFECTO_AGENCIA_USD,
    TECHO_POR_DEFECTO_CLIENTE_USD,
    UMBRAL_DE_ALARMA_POR_DEFECTO,
    CatalogoDePreciosVacio,
    ConceptoNoAdmitido,
    ConsumoSinCliente,
    MontoNoAdmitido,
    SinTechoAlcanzable,
    TechoAlcanzado,
    TechoNoSeBaja,
    cargar_precios,
    costo_de_mensaje,
    exigir_margen,
    inicio_del_mes,
    leer_archivo_de_precios,
    registrar_consumo,
    subir_techo,
)
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    CLIENTE_A2,
    HERALDO_A1,
    HERALDO_A2,
    sesion_de_agencia,
    sesion_de_cliente,
)

# WHY (aqui NO hay `pytestmark = pytest.mark.asyncio`): `pyproject.toml` declara
# `asyncio_mode = "auto"`, asi que las corrutinas ya se ejecutan solas. Marcar el
# modulo entero ademas ponia la marca sobre las sondas SINCRONAS —las del
# catalogo y la de la ventana— y pytest-asyncio avisaba de ello en cada corrida.
# Un aviso recurrente en la salida de la suite es ruido que acaba tapando al
# aviso que si importa.

MOMENTO = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Andamiaje: el estado se deja limpio ANTES de cada sonda
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def limpiar(motor_admin):
    """Sin esto, la sonda anterior deja gasto y la siguiente mide su resaca.

    Se limpia con el rol MIGRADOR a proposito: la aplicacion no tiene `DELETE`
    sobre `consumos` —un registro de gasto que se puede borrar no es un
    registro— y limpiarlo con ella seria imposible por diseno.
    """
    with motor_admin.connect() as conexion:
        conexion.execute(text("DELETE FROM consumos"))
        conexion.execute(text("DELETE FROM precios_por_pais"))
        conexion.execute(
            text(
                "UPDATE clientes SET techo_usd_mes = :t, umbral_de_alarma = :u, "
                "techo_actualizado_en = NULL"
            ),
            {"t": TECHO_POR_DEFECTO_CLIENTE_USD, "u": UMBRAL_DE_ALARMA_POR_DEFECTO},
        )
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = :t, umbral_de_alarma = :u"),
            {"t": TECHO_POR_DEFECTO_AGENCIA_USD, "u": UMBRAL_DE_ALARMA_POR_DEFECTO},
        )
        conexion.execute(text("DELETE FROM bitacora"))
    yield


def _fijar_techo(motor_admin, cliente_id: UUID, techo: Decimal, umbral: Decimal) -> None:
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE clientes SET techo_usd_mes = :t, umbral_de_alarma = :u WHERE id = :c"),
            {"t": techo, "u": umbral, "c": cliente_id},
        )


async def _consumir(motor, inquilino, monto: Decimal, **extra):
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        return await registrar_consumo(
            conexion,
            inquilino,
            concepto=CONCEPTO_MODELO,
            monto_usd=monto,
            detalle={"modelo": "sonda", "tokens": 100},
            ahora=MOMENTO,
            **extra,
        )


# ==========================================================================
# CONTROL — por debajo del techo, el consumo pasa y se registra
# ==========================================================================
async def test_bajo_el_techo_el_consumo_pasa_y_queda_registrado(motor, motor_admin) -> None:
    """El control de toda la bateria: si esto no pasa, un techo que niega todo
    aprobaria cada una de las sondas de abajo y el producto no funcionaria."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    resultado = await _consumir(motor, inquilino, Decimal("1.500000"))

    assert resultado.gastado_usd == Decimal("1.500000")
    assert resultado.techo_usd == Decimal("10")
    assert resultado.avisos == ()
    with motor_admin.connect() as conexion:
        fila = conexion.execute(
            text("SELECT concepto, monto_usd, titular FROM consumos WHERE id = :i"),
            {"i": resultado.id},
        ).one()
    assert fila.concepto == CONCEPTO_MODELO
    assert fila.monto_usd == Decimal("1.500000")
    assert fila.titular == Titular.CLIENTE.value


async def test_el_gasto_se_acumula_dentro_del_mes(motor, motor_admin) -> None:
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    primero = await _consumir(motor, inquilino, Decimal("2"))
    segundo = await _consumir(motor, inquilino, Decimal("3"))

    assert primero.gastado_usd == Decimal("2")
    assert segundo.gastado_usd == Decimal("5")


# ==========================================================================
# EL CORTE — al alcanzar el techo se deja de consumir
# ==========================================================================
async def test_al_alcanzar_el_techo_el_consumo_se_rechaza(motor, motor_admin) -> None:
    """RF-16: «dejar de consumir», no «avisar y seguir» (plan §4.1 paso 5)."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    await _consumir(motor, inquilino, Decimal("9"))

    with pytest.raises(TechoAlcanzado) as caida:
        await _consumir(motor, inquilino, Decimal("2"))

    assert caida.value.gastado_usd == Decimal("9")
    assert caida.value.techo_usd == Decimal("10")
    assert any(a.motivo == "techo.alcanzado" for a in caida.value.avisos)
    assert {a.destinatario for a in caida.value.avisos} == {"operador", "cliente"}


async def test_el_consumo_rechazado_no_deja_fila(motor, motor_admin) -> None:
    """Un rechazo que registrase el gasto haria el techo IRREVERSIBLE: la suma
    subiria con cada intento fallido y el cliente no podria volver a gastar
    nunca, ni subiendo el techo."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("5"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    for _ in range(3):
        with pytest.raises(TechoAlcanzado):
            await _consumir(motor, inquilino, Decimal("9"))

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM consumos")).scalar_one()
    assert cuantas == 0


async def test_el_consumo_que_cae_justo_en_el_techo_pasa(motor, motor_admin) -> None:
    """El borde: `<=` y no `<`. Un techo de 10 admite gastar exactamente 10."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    resultado = await _consumir(motor, inquilino, Decimal("10"))

    assert resultado.gastado_usd == Decimal("10")


# ==========================================================================
# CONCURRENCIA — N consumos a la vez NUNCA superan el techo
# ==========================================================================
async def test_veinticuatro_consumos_concurrentes_no_superan_el_techo(motor, motor_admin) -> None:
    """La sonda que funda la casilla (RNF-03, y P-23 leido al reves).

    # WHY (cada consumo CERCA del techo): con montos pequenos, un fallo de
    # atomicidad se esconde detras del redondeo. Con 3 sobre un techo de 10, solo
    # caben TRES: si el mecanismo no serializa, entran cuatro o mas y la suma
    # (12+) delata el defecto sin ambiguedad.
    """
    techo = Decimal("10")
    monto = Decimal("3")
    tareas = 24
    _fijar_techo(motor_admin, CLIENTE_A1, techo, Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    resultados = await asyncio.gather(
        *(_consumir(motor, inquilino, monto) for _ in range(tareas)),
        return_exceptions=True,
    )

    aceptados = [r for r in resultados if not isinstance(r, BaseException)]
    rechazados = [r for r in resultados if isinstance(r, BaseException)]
    assert all(isinstance(r, TechoAlcanzado) for r in rechazados), (
        f"algun rechazo no fue TechoAlcanzado: {[type(r).__name__ for r in rechazados]}"
    )
    with motor_admin.connect() as conexion:
        suma = conexion.execute(
            text("SELECT COALESCE(SUM(monto_usd), 0) FROM consumos")
        ).scalar_one()
    assert suma <= techo, (
        f"la suma final {suma} supera el techo {techo}: {len(aceptados)} de {tareas} "
        "consumos concurrentes entraron. El techo no corta de verdad"
    )
    assert len(aceptados) == 3, (
        f"entraron {len(aceptados)} consumos de {monto} bajo un techo de {techo}: "
        "caben exactamente 3"
    )
    assert len(rechazados) == tareas - 3


# ==========================================================================
# AISLAMIENTO — el gasto del vecino no cuenta contra mi techo
# ==========================================================================
async def test_el_consumo_del_vecino_no_cuenta_contra_mi_techo(motor, motor_admin) -> None:
    """CE-01 aplicado al dinero: dos clientes de LA MISMA agencia."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    _fijar_techo(motor_admin, CLIENTE_A2, Decimal("10"), Decimal("0.99"))
    yo = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    vecino = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)

    await _consumir(motor, vecino, Decimal("9"))
    mio = await _consumir(motor, yo, Decimal("9"))

    assert mio.gastado_usd == Decimal("9"), (
        "mi gasto acumulado incluyo el del vecino: la suma no filtra por cliente"
    )


async def test_una_sesion_de_cliente_no_ve_los_consumos_del_vecino(motor) -> None:
    """El otro lado del mismo hecho: la POLITICA, no la consulta."""
    vecino = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)
    await _consumir(motor, vecino, Decimal("1"))

    yo = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, yo) as conexion:
        cuantas = (await conexion.execute(text("SELECT count(*) FROM consumos"))).scalar_one()
    assert cuantas == 0


# ==========================================================================
# ALARMA — antes del techo, una sola vez por mes
# ==========================================================================
async def test_al_cruzar_el_umbral_avisa_al_operador_y_al_cliente(motor, motor_admin) -> None:
    """La SONDA de la alarma: 80 % de 20 son 16."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    antes = await _consumir(motor, inquilino, Decimal("15"))
    assert antes.avisos == (), "aviso por debajo del umbral: la alarma dispara antes de tiempo"

    cruce = await _consumir(motor, inquilino, Decimal("2"))

    motivos = {a.motivo for a in cruce.avisos}
    assert motivos == {"techo.alarma"}, f"avisos inesperados: {motivos}"
    assert {a.destinatario for a in cruce.avisos} == {"operador", "cliente"}


async def test_la_alarma_se_emite_una_sola_vez_por_mes(motor, motor_admin) -> None:
    """Idempotente: el segundo consumo por encima del umbral NO vuelve a avisar."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    primero = await _consumir(motor, inquilino, Decimal("17"))
    assert [a.motivo for a in primero.avisos] == ["techo.alarma", "techo.alarma"]

    segundo = await _consumir(motor, inquilino, Decimal("1"))
    assert segundo.avisos == (), "la alarma se repitio: no es idempotente por mes"

    with motor_admin.connect() as conexion:
        apuntes = conexion.execute(
            text("SELECT count(*) FROM bitacora WHERE accion = 'techo.alarma'")
        ).scalar_one()
    assert apuntes == 1, f"{apuntes} apuntes de alarma en el mismo mes: se esperaba 1"


async def test_la_alarma_del_mes_anterior_no_silencia_la_de_este(motor, motor_admin) -> None:
    """CONTROL de la idempotencia: si la ventana no se mirara, un apunte viejo
    dejaria al cliente sin alarma para siempre."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    mes_pasado = MOMENTO - timedelta(days=40)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await registrar_consumo(
            conexion,
            inquilino,
            concepto=CONCEPTO_MODELO,
            monto_usd=Decimal("18"),
            detalle={},
            ahora=mes_pasado,
        )

    de_este_mes = await _consumir(motor, inquilino, Decimal("17"))

    assert {a.motivo for a in de_este_mes.avisos} == {"techo.alarma"}


async def test_el_gasto_del_mes_anterior_no_cuenta_contra_el_techo_de_este(
    motor, motor_admin
) -> None:
    """La ventana es el MES DE FACTURACION: al cambiar de mes el techo se renueva."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await registrar_consumo(
            conexion,
            inquilino,
            concepto=CONCEPTO_MODELO,
            monto_usd=Decimal("10"),
            detalle={},
            ahora=MOMENTO - timedelta(days=40),
        )

    ahora = await _consumir(motor, inquilino, Decimal("9"))

    assert ahora.gastado_usd == Decimal("9")


def test_el_inicio_del_mes_es_el_dia_uno_en_utc() -> None:
    assert inicio_del_mes(MOMENTO) == datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="sin zona"):
        inicio_del_mes(datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001 - es el caso que se rechaza


# ==========================================================================
# SUBIR EL TECHO — acto del operador, con su apunte
# ==========================================================================
async def test_el_operador_sube_el_techo_y_el_consumo_vuelve_a_pasar(motor, motor_admin) -> None:
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    await _consumir(motor, cliente, Decimal("9"))
    with pytest.raises(TechoAlcanzado):
        await _consumir(motor, cliente, Decimal("5"))

    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        nuevo = await subir_techo(
            conexion,
            operador,
            cliente_id=CLIENTE_A1,
            nuevo_techo_usd=Decimal("50"),
            actor="operador:sonda",
        )
    assert nuevo == Decimal("50")

    resultado = await _consumir(motor, cliente, Decimal("5"))
    assert resultado.gastado_usd == Decimal("14")
    with motor_admin.connect() as conexion:
        apuntes = conexion.execute(
            text("SELECT count(*) FROM bitacora WHERE accion = 'techo.subido'")
        ).scalar_one()
    assert apuntes == 1


async def test_un_portal_de_cliente_no_sube_el_techo_por_esta_via(motor) -> None:
    """El alcance lo decide la identidad (plan §3.1 punto 5), no el que llama."""
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, cliente) as conexion:
        with pytest.raises(PermissionError, match="alcance"):
            await subir_techo(
                conexion,
                cliente,
                cliente_id=CLIENTE_A1,
                nuevo_techo_usd=Decimal("50"),
                actor="portal:sonda",
            )


async def test_subir_el_techo_no_sirve_para_bajarlo(motor, motor_admin) -> None:
    """B4: «la plataforma nunca lo baja sola». Bajar es OTRO acto, y no existe."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        with pytest.raises(TechoNoSeBaja):
            await subir_techo(
                conexion,
                operador,
                cliente_id=CLIENTE_A1,
                nuevo_techo_usd=Decimal("5"),
                actor="operador:sonda",
            )


# ==========================================================================
# EXIGIR MARGEN — comprobar ANTES de gastar
# ==========================================================================
async def test_exigir_margen_deja_pasar_lo_que_cabe(motor, motor_admin) -> None:
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    await _consumir(motor, inquilino, Decimal("4"))

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        restante = await exigir_margen(conexion, inquilino, Decimal("3"), ahora=MOMENTO)

    assert restante == Decimal("6")


async def test_exigir_margen_corta_antes_de_gastar(motor, motor_admin) -> None:
    """Es lo que llamaran el manejador de generacion (T-109) y el paso 5 de
    `entregar()` (T-119): si no cabe, el modelo NO se invoca."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    await _consumir(motor, inquilino, Decimal("9"))

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(TechoAlcanzado):
            await exigir_margen(conexion, inquilino, Decimal("2"), ahora=MOMENTO)


async def test_exigir_margen_no_registra_gasto(motor, motor_admin) -> None:
    """Comprueba lo que NO hace: es una comprobacion previa, no una reserva."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await exigir_margen(conexion, inquilino, Decimal("5"), ahora=MOMENTO)

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM consumos")).scalar_one()
    assert cuantas == 0


# ==========================================================================
# TITULAR AGENCIA (B4) — la clave de desarrollo cuenta contra el techo de la agencia
# ==========================================================================
async def test_el_consumo_con_clave_de_agencia_cuenta_contra_el_techo_de_la_agencia(
    motor, motor_admin
) -> None:
    """B4: «Lo que consume la clave de agencia cuenta contra un techo de la
    agencia con la misma alarma de RF-16, nunca sin limite»."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("1"), Decimal("0.99"))
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = 10 WHERE agencia_id = :a"),
            {"a": AGENCIA_A},
        )
    operador = sesion_de_agencia(AGENCIA_A)

    async with sesion_de_inquilino(motor, operador) as conexion:
        resultado = await registrar_consumo(
            conexion,
            operador,
            concepto=CONCEPTO_MODELO,
            monto_usd=Decimal("5"),
            detalle={},
            cliente_id=CLIENTE_A1,
            titular=Titular.AGENCIA,
            ahora=MOMENTO,
        )

    # El techo del CLIENTE es 1 y aun asi paso: se cargo contra el de la agencia.
    assert resultado.techo_usd == Decimal("10")
    assert resultado.gastado_usd == Decimal("5")


async def test_el_techo_de_la_agencia_suma_a_todos_sus_clientes_de_desarrollo(
    motor, motor_admin
) -> None:
    """El techo de la agencia es UNO, compartido: dos altas de desarrollo no
    tienen 100 USD cada una."""
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = 10 WHERE agencia_id = :a"),
            {"a": AGENCIA_A},
        )
    operador = sesion_de_agencia(AGENCIA_A)

    async def gastar(cliente_id, monto):
        async with sesion_de_inquilino(motor, operador) as conexion:
            return await registrar_consumo(
                conexion,
                operador,
                concepto=CONCEPTO_MODELO,
                monto_usd=monto,
                detalle={},
                cliente_id=cliente_id,
                titular=Titular.AGENCIA,
                ahora=MOMENTO,
            )

    await gastar(CLIENTE_A1, Decimal("6"))
    segundo = await gastar(CLIENTE_A2, Decimal("3"))
    assert segundo.gastado_usd == Decimal("9")

    with pytest.raises(TechoAlcanzado):
        await gastar(CLIENTE_A2, Decimal("3"))


async def test_el_consumo_de_agencia_no_cuenta_contra_el_techo_del_cliente(
    motor, motor_admin
) -> None:
    """El otro lado: lo que paga la agencia no le come el techo al cliente."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = 100 WHERE agencia_id = :a"),
            {"a": AGENCIA_A},
        )
    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        await registrar_consumo(
            conexion,
            operador,
            concepto=CONCEPTO_MODELO,
            monto_usd=Decimal("9"),
            detalle={},
            cliente_id=CLIENTE_A1,
            titular=Titular.AGENCIA,
            ahora=MOMENTO,
        )

    propio = await _consumir(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1), Decimal("9"))
    assert propio.gastado_usd == Decimal("9")


async def test_una_sesion_de_cliente_no_puede_cargar_contra_la_agencia(motor) -> None:
    """Fail-closed: sin fila de techo alcanzable NO se consume «sin techo»."""
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, cliente) as conexion:
        with pytest.raises(SinTechoAlcanzable):
            await registrar_consumo(
                conexion,
                cliente,
                concepto=CONCEPTO_MODELO,
                monto_usd=Decimal("1"),
                detalle={},
                titular=Titular.AGENCIA,
                ahora=MOMENTO,
            )


async def test_en_alcance_agencia_el_cliente_del_consumo_es_obligatorio(motor) -> None:
    """El centinela no es un cliente: cargar contra el seria gasto sin dueno."""
    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        with pytest.raises(ConsumoSinCliente):
            await registrar_consumo(
                conexion,
                operador,
                concepto=CONCEPTO_MODELO,
                monto_usd=Decimal("1"),
                detalle={},
                ahora=MOMENTO,
            )


# ==========================================================================
# ENTRADAS QUE SE RECHAZAN AL BORDE
# ==========================================================================
@pytest.mark.parametrize("monto", [Decimal("0"), Decimal("-1")])
async def test_un_monto_que_no_es_gasto_se_rechaza(motor, monto) -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(MontoNoAdmitido):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto=CONCEPTO_MODELO,
                monto_usd=monto,
                detalle={},
                ahora=MOMENTO,
            )


async def test_un_concepto_fuera_de_los_dos_se_rechaza(motor) -> None:
    """Allowlist: lo que no esta declarado no entra en la cuenta del dinero."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(ConceptoNoAdmitido):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto="otros",
                monto_usd=Decimal("1"),
                detalle={},
                ahora=MOMENTO,
            )


async def test_el_heraldo_queda_atribuido_en_la_fila(motor, motor_admin) -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    resultado = await _consumir(motor, inquilino, Decimal("1"), heraldo_id=HERALDO_A1)
    with motor_admin.connect() as conexion:
        fila = conexion.execute(
            text("SELECT heraldo_id FROM consumos WHERE id = :i"), {"i": resultado.id}
        ).one()
    assert fila.heraldo_id == HERALDO_A1


# ==========================================================================
# PRECIO POR PAIS — y su RAMA DEL NO
# ==========================================================================
def _cargar(motor_admin, entradas, *, fuente="sonda", cargada_en=MOMENTO) -> int:
    with motor_admin.connect() as conexion:
        return cargar_precios(conexion, entradas=entradas, fuente=fuente, cargada_en=cargada_en)


_TARIFA = (
    {"pais": "US", "tipo": "utilidad", "precio_usd": "0.0140", "vigente_desde": "2025-07-01"},
    {"pais": "US", "tipo": "marketing", "precio_usd": "0.0250", "vigente_desde": "2025-07-01"},
    {"pais": "MX", "tipo": "utilidad", "precio_usd": "0.0050", "vigente_desde": "2025-07-01"},
)


async def test_el_precio_vigente_sale_del_catalogo(motor, motor_admin) -> None:
    """CONTROL de toda la rama del no: con el catalogo al dia, se cobra su precio
    y NO se avisa. Sin este control, una implementacion que avisara siempre
    pasaria las tres sondas de abajo."""
    _cargar(motor_admin, _TARIFA)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "US", "utilidad", MOMENTO)

    assert costo.precio.precio_usd == Decimal("0.014000")
    assert costo.precio.es_respaldo is False
    assert costo.avisos == ()


async def test_un_catalogo_caducado_cobra_el_precio_mas_alto_y_avisa(motor, motor_admin) -> None:
    """RF-16, rama del no: mas de 30 dias -> el precio MAS ALTO conocido + aviso.

    # WHY (el mas alto y no el ultimo): sobrecontar corta antes de tiempo, que
    # cuesta una conversacion; subcontar deja pasar gasto por encima del techo,
    # que cuesta dinero real y es lo que RF-16 existe para impedir.
    """
    caducado = MOMENTO - timedelta(days=DIAS_DE_VIGENCIA_DEL_CATALOGO + 1)
    _cargar(motor_admin, _TARIFA, cargada_en=caducado)

    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "US", "utilidad", MOMENTO)

    assert costo.precio.precio_usd == Decimal("0.025000"), (
        "un catalogo caducado cobro el precio viejo: la rama del no no se aplico"
    )
    assert costo.precio.es_respaldo is True
    assert [a.motivo for a in costo.avisos] == ["precios.caducados"]


async def test_el_catalogo_justo_en_el_borde_de_los_treinta_dias_sigue_vigente(
    motor, motor_admin
) -> None:
    """El borde, declarado: 30 dias EXACTOS todavia valen."""
    borde = MOMENTO - timedelta(days=DIAS_DE_VIGENCIA_DEL_CATALOGO)
    _cargar(motor_admin, _TARIFA, cargada_en=borde)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "US", "utilidad", MOMENTO)
    assert costo.avisos == ()


async def test_un_pais_sin_fila_cobra_el_precio_mas_alto_y_avisa(motor, motor_admin) -> None:
    _cargar(motor_admin, _TARIFA)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "BR", "utilidad", MOMENTO)

    assert costo.precio.precio_usd == Decimal("0.025000")
    assert costo.precio.es_respaldo is True
    assert [a.motivo for a in costo.avisos] == ["precios.sin_pais"]


async def test_un_tipo_sin_fila_para_ese_pais_cobra_el_mas_alto_y_avisa(
    motor, motor_admin
) -> None:
    """La tarificacion es por MENSAJE/PLANTILLA (T-004·pre): el tipo es parte de
    la llave, y que falte el tipo es tan «sin dato» como que falte el pais."""
    _cargar(motor_admin, _TARIFA)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "MX", "marketing", MOMENTO)

    assert costo.precio.es_respaldo is True
    assert [a.motivo for a in costo.avisos] == ["precios.sin_pais"]


async def test_una_fila_futura_no_se_cobra_todavia(motor, motor_admin) -> None:
    """`vigente_desde` manda: el precio que entra en vigor manana no es el de hoy."""
    _cargar(
        motor_admin,
        (
            {"pais": "US", "tipo": "utilidad", "precio_usd": "0.0140",
             "vigente_desde": "2025-07-01"},
            {"pais": "US", "tipo": "utilidad", "precio_usd": "0.9900",
             "vigente_desde": "2099-01-01"},
        ),
    )
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, "US", "utilidad", MOMENTO)
    assert costo.precio.precio_usd == Decimal("0.014000")


async def test_un_catalogo_vacio_falla_ruidoso(motor) -> None:
    """No hay «precio mas alto conocido» cuando no se conoce ninguno: se falla
    en voz alta en vez de contar cero, que es contar mal en la direccion cara."""
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        with pytest.raises(CatalogoDePreciosVacio):
            await costo_de_mensaje(conexion, "US", "utilidad", MOMENTO)


async def test_la_mensajeria_se_cuenta_contra_el_mismo_techo(motor, motor_admin) -> None:
    """G-07: el techo cuenta MODELO **y** MENSAJERIA, no solo tokens."""
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    _cargar(motor_admin, _TARIFA)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    await _consumir(motor, inquilino, Decimal("9.99"))

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        costo = await costo_de_mensaje(conexion, "US", "marketing", MOMENTO)
        with pytest.raises(TechoAlcanzado):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto=CONCEPTO_MENSAJERIA,
                monto_usd=costo.precio.precio_usd,
                detalle={"pais": "US", "tipo": "marketing"},
                ahora=MOMENTO,
            )


# ==========================================================================
# EL ARCHIVO VERSIONADO DEL CATALOGO
# ==========================================================================
def test_el_archivo_de_precios_existe_y_declara_fecha_y_fuente() -> None:
    """Un catalogo sin procedencia no se puede auditar ni caducar."""
    assert ARCHIVO_DE_PRECIOS.is_file(), f"no existe {ARCHIVO_DE_PRECIOS}"
    archivo = leer_archivo_de_precios(ARCHIVO_DE_PRECIOS)
    assert archivo.fecha, "el archivo de precios no declara su fecha"
    assert archivo.fuente.strip(), "el archivo de precios no declara su fuente"
    assert archivo.entradas, "el archivo de precios no trae ninguna entrada"


async def test_el_archivo_versionado_se_carga_de_extremo_a_extremo(motor, motor_admin) -> None:
    """No se prueba un archivo inventado en la prueba: se prueba EL del repositorio."""
    archivo = leer_archivo_de_precios(ARCHIVO_DE_PRECIOS)
    cuantas = _cargar(motor_admin, archivo.entradas, fuente=archivo.fuente, cargada_en=MOMENTO)
    assert cuantas == len(archivo.entradas)

    primera = archivo.entradas[0]
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        costo = await costo_de_mensaje(conexion, primera["pais"], primera["tipo"], MOMENTO)
    assert costo.precio.fuente == archivo.fuente


def test_recargar_el_catalogo_lo_reemplaza_entero(motor_admin) -> None:
    """Una carga parcial dejaria filas viejas conviviendo con las nuevas y
    «el catalogo vigente» dejaria de ser una cosa."""
    _cargar(motor_admin, _TARIFA)
    _cargar(motor_admin, _TARIFA[:1])
    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM precios_por_pais")).scalar_one()
    assert cuantas == 1


def test_un_pais_que_no_es_iso_alfa_dos_se_rechaza_al_cargar(motor_admin) -> None:
    with pytest.raises(ValueError, match="ISO"):
        _cargar(
            motor_admin,
            ({"pais": "USA", "tipo": "utilidad", "precio_usd": "0.01",
              "vigente_desde": "2025-07-01"},),
        )


def test_un_tipo_de_mensaje_fuera_de_los_cuatro_se_rechaza_al_cargar(motor_admin) -> None:
    """La tarificacion por CONVERSACION esta deprecada desde el 1-jul-2025."""
    with pytest.raises(ValueError, match="tipo"):
        _cargar(
            motor_admin,
            ({"pais": "US", "tipo": "conversacion", "precio_usd": "0.01",
              "vigente_desde": "2025-07-01"},),
        )


async def test_la_aplicacion_no_puede_escribir_el_catalogo_de_precios(motor) -> None:
    """El catalogo es de la PLATAFORMA: la aplicacion lo lee y nada mas.

    Medido POR EFECTO contra el rol real, no por lo que diga `rol.py`.
    """
    from sqlalchemy.exc import DBAPIError

    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        with pytest.raises(DBAPIError, match="permission denied"):
            await conexion.execute(
                text(
                    "INSERT INTO precios_por_pais (pais, tipo, precio_usd, vigente_desde, "
                    "fuente) VALUES ('US', 'utilidad', 1, '2025-07-01', 'sonda')"
                )
            )


async def test_la_aplicacion_si_puede_leer_el_catalogo_de_precios(motor, motor_admin) -> None:
    """CONTROL de la sonda de arriba, y ademas el otro lado del privilegio.

    # WHY: sin este control, un `REVOKE` que dejara al rol sin NINGUN privilegio
    # sobre la tabla dejaria verde la sonda anterior —el `INSERT` seguiria
    # fallando con `permission denied`— mientras el producto se cae en produccion
    # al contar el gasto de mensajeria. La excepcion a RLS de un catalogo de
    # plataforma se sostiene sobre DOS hechos, no uno: se lee y no se escribe.
    """
    _cargar(motor_admin, _TARIFA)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        cuantas = (
            await conexion.execute(text("SELECT count(*) FROM precios_por_pais"))
        ).scalar_one()
    assert cuantas == len(_TARIFA)


async def test_la_aplicacion_no_puede_corregir_ni_borrar_un_consumo(motor, motor_admin) -> None:
    """RF-10 aplicado al dinero, medido POR EFECTO contra el rol real.

    Un registro de gasto que la aplicacion pueda reescribir no es un registro: es
    un saldo editable, y entonces el techo se levanta borrando filas en vez de
    subiendolo — y sin dejar rastro de quien lo hizo. El mecanismo esta en el
    `GRANT` de la revision 0012, no en que este modulo no escriba el `UPDATE`.
    """
    from sqlalchemy.exc import DBAPIError

    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    consumo = await _consumir(motor, inquilino, Decimal("1"))

    for sentencia in (
        "UPDATE consumos SET monto_usd = 0.000001 WHERE id = :i",
        "DELETE FROM consumos WHERE id = :i",
    ):
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conexion.execute(text(sentencia), {"i": consumo.id})

    # CONTROL: la fila sigue ahi y con su importe intacto. Sin esto, una tabla
    # vacia haria pasar las dos aserciones de arriba sin haber medido nada.
    with motor_admin.connect() as conexion:
        monto = conexion.execute(
            text("SELECT monto_usd FROM consumos WHERE id = :i"), {"i": consumo.id}
        ).scalar_one()
    assert monto == Decimal("1.000000")


async def test_el_gasto_no_se_puede_atribuir_al_heraldo_de_otro_cliente(
    motor, motor_admin
) -> None:
    """La atribucion tambien cuelga de la cascada, y no de la buena fe.

    # WHY: el `WITH CHECK` de la politica gobierna `agencia_id` y `cliente_id`,
    # no `heraldo_id`. Sin la foranea COMPUESTA de la revision 0012, una sesion
    # legitima del cliente A1 podria escribir un consumo suyo atribuido a un
    # heraldo del vecino y ningun mecanismo lo veria: la fila es «suya» en las
    # dos claves que RLS mira.
    """
    from sqlalchemy.exc import DBAPIError

    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    with pytest.raises(DBAPIError, match="consumos_heraldo_fkey"):
        await _consumir(motor, inquilino, Decimal("1"), heraldo_id=HERALDO_A2)

    # CONTROL: el heraldo PROPIO si se admite. Sin el, una foranea que rechazara
    # TODA atribucion pasaria la asercion de arriba sin medir ningun aislamiento.
    resultado = await _consumir(motor, inquilino, Decimal("1"), heraldo_id=HERALDO_A1)
    assert resultado.gastado_usd == Decimal("1")


# ==========================================================================
# LA LLAVE DE LA IDEMPOTENCIA — que la alarma no se silencie a si misma
# ==========================================================================
async def test_subir_el_techo_vuelve_a_armar_la_alarma_del_mismo_mes(
    motor, motor_admin
) -> None:
    """Sin el techo en la llave, subirlo deja al cliente sin alarma el resto del mes.

    Y es justo cuando mas puede gastar, porque acaba de recibir mas margen: la
    alarma se apagaria en el unico momento en que de verdad hacia falta.
    """
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    primera = await _consumir(motor, cliente, Decimal("17"))
    assert {a.motivo for a in primera.avisos} == {"techo.alarma"}

    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        await subir_techo(
            conexion,
            operador,
            cliente_id=CLIENTE_A1,
            nuevo_techo_usd=Decimal("100"),
            actor="operador:sonda",
        )

    # 17 + 64 = 81, o sea el 81 % de 100: cruza el umbral del techo NUEVO.
    segunda = await _consumir(motor, cliente, Decimal("64"))
    assert {a.motivo for a in segunda.avisos} == {"techo.alarma"}, (
        "el techo subio y la alarma del techo nuevo no salto: la idempotencia "
        "mensual esta silenciando un limite que ya no es el que se aviso"
    )


async def test_la_alarma_de_la_agencia_no_silencia_la_del_cliente(motor, motor_admin) -> None:
    """Dos techos distintos, dos alarmas: la llave lleva el RECURSO.

    Las dos son `techo.alarma` sobre la misma agencia, asi que sin el recurso en
    la llave la primera que salte apagaria a la otra durante todo el mes.
    """
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("20"), UMBRAL_DE_ALARMA_POR_DEFECTO)
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = 20 WHERE agencia_id = :a"),
            {"a": AGENCIA_A},
        )
    operador = sesion_de_agencia(AGENCIA_A)
    async with sesion_de_inquilino(motor, operador) as conexion:
        de_agencia = await registrar_consumo(
            conexion,
            operador,
            concepto=CONCEPTO_MODELO,
            monto_usd=Decimal("17"),
            detalle={},
            cliente_id=CLIENTE_A1,
            titular=Titular.AGENCIA,
            ahora=MOMENTO,
        )
    assert {a.motivo for a in de_agencia.avisos} == {"techo.alarma"}

    del_cliente = await _consumir(
        motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1), Decimal("17")
    )

    assert {a.motivo for a in del_cliente.avisos} == {"techo.alarma"}, (
        "la alarma del techo de la AGENCIA silencio la del CLIENTE: la llave de "
        "idempotencia no distingue los dos techos"
    )
    with motor_admin.connect() as conexion:
        apuntes = conexion.execute(
            text("SELECT count(*) FROM bitacora WHERE accion = 'techo.alarma'")
        ).scalar_one()
    assert apuntes == 2


# ==========================================================================
# EL CARGADOR — un error de ENTRADA no puede matar el DESTINO
# ==========================================================================
def test_un_cargue_invalido_no_destruye_el_catalogo_vigente(motor_admin) -> None:
    """Si se borrara antes de validar, un archivo mal escrito apagaria el conteo.

    # WHY: `costo_de_mensaje` LEVANTA con el catalogo vacio —a proposito, porque
    # contar cero es contar mal en la direccion cara—, asi que dejar la tabla
    # vacia a mitad de una carga no degrada el producto: lo para. La entrada
    # invalida va la ULTIMA a proposito: con la validacion mezclada con la
    # escritura, las anteriores ya se habrian escrito sobre una tabla vaciada.
    """
    _cargar(motor_admin, _TARIFA)

    with pytest.raises(ValueError, match="ISO"):
        _cargar(
            motor_admin,
            (
                {"pais": "MX", "tipo": "utilidad", "precio_usd": "0.0050",
                 "vigente_desde": "2025-07-01"},
                {"pais": "USA", "tipo": "utilidad", "precio_usd": "0.0140",
                 "vigente_desde": "2025-07-01"},
            ),
        )

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM precios_por_pais")).scalar_one()
    assert cuantas == len(_TARIFA), (
        f"quedan {cuantas} filas de {len(_TARIFA)}: un cargue rechazado toco el "
        "catalogo vigente. Un error de ENTRADA no puede matar el DESTINO"
    )


def test_un_catalogo_vacio_no_se_carga(motor_admin) -> None:
    """Vaciar la tabla «cargando nada» dejaria la rama del no sin respaldo."""
    _cargar(motor_admin, _TARIFA)
    with pytest.raises(ValueError, match="VACIO"):
        _cargar(motor_admin, ())
    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM precios_por_pais")).scalar_one()
    assert cuantas == len(_TARIFA)


def test_el_archivo_de_precios_dice_si_esta_verificado() -> None:
    """La pregunta «¿estas cifras estan contrastadas?» la contesta quien escribe.

    # WHY (se exige la DECLARACION, no un valor concreto): hoy el catalogo del
    # repositorio son cotas superiores sin contrastar y declara `false`; el dia
    # que alguien cargue la tarifa real declarara `true`. Lo que esta prueba
    # impide es la tercera opcion —no decirlo— porque una cifra sin procedencia
    # repetida acaba pareciendo verificada (P-03).
    """
    archivo = leer_archivo_de_precios(ARCHIVO_DE_PRECIOS)
    assert isinstance(archivo.verificado, bool)
    if not archivo.verificado:
        assert "VERIFICAR" in archivo.fuente.upper(), (
            "el catalogo se declara NO verificado y su `fuente` no lo dice: la "
            "procedencia tiene que viajar con cada fila hasta la base, no quedarse "
            "en un campo que nadie consulta"
        )


def test_un_cargue_con_una_llave_repetida_no_destruye_el_catalogo(motor_admin) -> None:
    """El `UNIQUE` de la tabla tambien lo rechaza — pero DESPUES del borrado.

    # WHY: el cargador corre en AUTOCOMMIT, asi que cuando la base levantara por
    # la clave repetida el `DELETE` ya estaria confirmado y el catalogo vigente
    # perdido. Es la misma leccion que la sonda de arriba en la variante que el
    # guard no cubria: uno que solo ve algunas formas de su propio error deja de
    # proteger el dia que llega la otra.
    """
    _cargar(motor_admin, _TARIFA)
    repetida = {
        "pais": "US", "tipo": "utilidad",
        "precio_usd": "0.0140", "vigente_desde": "2025-07-01",
    }

    with pytest.raises(ValueError, match="dos veces"):
        _cargar(motor_admin, (repetida, dict(repetida)))

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM precios_por_pais")).scalar_one()
    assert cuantas == len(_TARIFA), (
        f"quedan {cuantas} filas de {len(_TARIFA)}: una llave repetida en el archivo "
        "se llevo por delante el catalogo vigente"
    )


@pytest.mark.parametrize("monto", [0.5, 1.0])
async def test_un_monto_en_coma_flotante_se_rechaza(motor, motor_admin, monto) -> None:
    """Ruta de dinero: un tipo que no representa exacto no entra «con cuidado».

    `Decimal(0.1)` es 0.1000000000000000055511151231257827: aceptar el `float` y
    convertirlo aqui esconderia el error de redondeo dentro de la suma del mes,
    donde ya no lo ve nadie. El caso `1.0` esta a proposito: un float que parece
    exacto es el que se cuela.
    """
    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(MontoNoAdmitido, match="float"):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto=CONCEPTO_MODELO,
                monto_usd=monto,
                detalle={},
                ahora=MOMENTO,
            )

    # CONTROL: el mismo importe como `Decimal` SI pasa. Sin esto, un rechazo
    # indiscriminado de todo monto pasaria la asercion de arriba.
    resultado = await _consumir(motor, inquilino, Decimal(str(monto)))
    assert resultado.gastado_usd == Decimal(str(monto))


async def test_un_secreto_dentro_del_detalle_aborta_el_registro(motor, motor_admin) -> None:
    """RF-09 no se puede romper por la puerta de RF-16.

    `consumos` es de SOLO INSERCION, asi que un secreto escrito ahi no se puede
    corregir despues: es la misma razon por la que la bitacora barre su detalle.
    Se mide con material CIFRADO —lo que el barrido rechaza por tipo— porque es
    lo que de verdad acabaria dentro de un «guardo la respuesta entera por si
    acaso».
    """
    from app.tenancy.secrets import SecretoEnLaRespuesta

    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(SecretoEnLaRespuesta):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto=CONCEPTO_MODELO,
                monto_usd=Decimal("1"),
                detalle={"respuesta": {"credencial": b"\x00material-cifrado"}},
                ahora=MOMENTO,
            )

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM consumos")).scalar_one()
    assert cuantas == 0, "el consumo se registro con el secreto dentro del detalle"

    # CONTROL: un detalle normal SI se registra. Sin esto, un barrido que
    # rechazara todo pasaria la asercion de arriba sin medir nada.
    resultado = await _consumir(motor, inquilino, Decimal("1"))
    assert resultado.gastado_usd == Decimal("1")


# ==========================================================================
# LO QUE LEVANTO LA REVISION CRUZADA (Crisol)
# ==========================================================================
def test_un_fallo_de_la_base_a_mitad_de_carga_no_destruye_el_catalogo(motor_admin) -> None:
    """La validacion previa cubre los errores de ENTRADA. Esto cubre los de la BASE.

    # WHY (el disparador es un DESBORDAMIENTO numerico y no una entrada
    # invalida): tiene que ser un error que la validacion NO pueda ver, o esta
    # sonda mediria otra vez el guard de arriba. `numeric(12,6)` no admite 10^12,
    # y eso solo lo sabe Postgres. Sin la transaccion explicita, el `DELETE` ya
    # estaria confirmado cuando el `INSERT` revienta, y el catalogo vigente se
    # habria perdido por un fallo del servidor.
    """
    from sqlalchemy.exc import DBAPIError

    _cargar(motor_admin, _TARIFA)

    with pytest.raises(DBAPIError):
        _cargar(
            motor_admin,
            (
                {"pais": "MX", "tipo": "utilidad", "precio_usd": "0.0050",
                 "vigente_desde": "2025-07-01"},
                {"pais": "US", "tipo": "utilidad", "precio_usd": "1000000000000",
                 "vigente_desde": "2025-07-01"},
            ),
        )

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM precios_por_pais")).scalar_one()
    assert cuantas == len(_TARIFA), (
        f"quedan {cuantas} filas de {len(_TARIFA)}: un fallo de la base a mitad de "
        "carga se llevo el catalogo vigente. La carga tiene que ser atomica"
    )


async def test_una_conexion_asincrona_al_cargador_se_rechaza_ruidosa(motor) -> None:
    """El error que NO daria error: cada `execute` seria una corrutina sin esperar.

    # WHY: la carga no ocurriria, nadie levantaria, y `cargar_precios` devolveria
    # el recuento de filas «cargadas» igual — un exito falso sobre la ruta del
    # dinero. Es el defecto de contrato mezclado que levanto la revision cruzada.
    """
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        with pytest.raises(TypeError, match="SINCRONA"):
            cargar_precios(conexion, entradas=_TARIFA, fuente="sonda", cargada_en=MOMENTO)


def test_un_precio_en_coma_flotante_se_rechaza_al_cargar(motor_admin) -> None:
    """La misma politica que el monto: en dinero, el float no entra."""
    with pytest.raises(ValueError, match="float"):
        _cargar(
            motor_admin,
            ({"pais": "US", "tipo": "utilidad", "precio_usd": 0.014,
              "vigente_desde": "2025-07-01"},),
        )


def test_un_precio_que_no_es_numero_se_rechaza_como_valueerror(motor_admin) -> None:
    """`InvalidOperation` no es `ValueError`: sin traducirlo se sale del contrato.

    Quien llama al cargador captura `ValueError` —es lo que levanta el resto de
    la validacion— y un precio mal escrito se le escaparia por debajo.
    """
    with pytest.raises(ValueError, match="no es"):
        _cargar(
            motor_admin,
            ({"pais": "US", "tipo": "utilidad", "precio_usd": "carisimo",
              "vigente_desde": "2025-07-01"},),
        )


def test_el_catalogo_no_se_carga_sin_fuente(motor_admin) -> None:
    """P-03: una cifra que gobierna dinero y no dice de donde salio no se audita."""
    with pytest.raises(ValueError, match="fuente"):
        _cargar(motor_admin, _TARIFA, fuente="   ")


async def test_el_costo_de_un_mensaje_exige_un_instante_con_zona(motor, motor_admin) -> None:
    """Sin zona no se puede decir si el catalogo caduco: es error de dominio."""
    _cargar(motor_admin, _TARIFA)
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        with pytest.raises(ValueError, match="sin zona"):
            await costo_de_mensaje(
                conexion, "US", "utilidad", datetime(2026, 9, 15, 12, 0)  # noqa: DTZ001
            )


async def test_un_detalle_desmedido_no_entra_en_el_registro_de_gasto(
    motor, motor_admin
) -> None:
    """`consumos` es de SOLO INSERCION: lo que entre ahi no se recorta despues.

    # WHY (el caso es «guardo la respuesta entera por si acaso»): es el atajo
    # natural, y mete kilobytes de texto ajeno por fila en la tabla que mas crece
    # del producto. El `barrer` de al lado cubre el otro eje del mismo atajo —que
    # lo volcado lleve un secreto—; este cubre el tamano.
    """
    from app.tenancy.budget import TOPE_DEL_DETALLE_BYTES, DetalleDemasiadoGrande

    _fijar_techo(motor_admin, CLIENTE_A1, Decimal("10"), Decimal("0.99"))
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(DetalleDemasiadoGrande):
            await registrar_consumo(
                conexion,
                inquilino,
                concepto=CONCEPTO_MODELO,
                monto_usd=Decimal("1"),
                detalle={"respuesta": "x" * (TOPE_DEL_DETALLE_BYTES + 1)},
                ahora=MOMENTO,
            )

    with motor_admin.connect() as conexion:
        cuantas = conexion.execute(text("SELECT count(*) FROM consumos")).scalar_one()
    assert cuantas == 0

    # CONTROL: un detalle del tamano que el requisito describe SI entra. Sin esto,
    # un tope de cero pasaria la asercion de arriba sin medir nada.
    resultado = await _consumir(motor, inquilino, Decimal("1"))
    assert resultado.gastado_usd == Decimal("1")


def test_el_catalogo_no_se_carga_con_una_fecha_sin_zona(motor_admin) -> None:
    """`cargada_en` es `timestamptz`: sin zona, la caducidad a 30 dias se desplaza.

    Postgres interpretaria el instante en el huso del servidor, asi que el mismo
    catalogo caducaria en momentos distintos segun donde corra la base — y la
    rama del no de RF-16 se dispararia antes o despues sin que nadie lo vea.
    """
    with pytest.raises(ValueError, match="sin zona"):
        _cargar(motor_admin, _TARIFA, cargada_en=datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001


async def test_veinte_consumos_concurrentes_no_superan_el_techo_de_la_agencia(
    motor, motor_admin
) -> None:
    """El techo de la AGENCIA se serializa sobre SU fila, y es un cerrojo DISTINTO.

    # WHY (no basta con la sonda del techo del cliente): son dos caminos de codigo
    # distintos —uno bloquea la fila de `clientes`, el otro la de `agencias`— y el
    # de la agencia es el que MAS lo necesita, porque su techo lo comparten todas
    # las altas de desarrollo a la vez. Una sonda que solo mide el del cliente
    # firmaria el otro sin haberlo tocado. Lo levanto la revision cruzada.
    #
    # Los consumos se reparten entre DOS clientes a proposito: si el cerrojo se
    # tomara por cliente en vez de por agencia, cabrian seis en vez de tres y la
    # suma se pasaria del techo. Aqui se veria.
    """
    techo = Decimal("10")
    monto = Decimal("3")
    tareas = 20
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("UPDATE agencias SET techo_usd_mes = :t WHERE agencia_id = :a"),
            {"t": techo, "a": AGENCIA_A},
        )
    operador = sesion_de_agencia(AGENCIA_A)

    async def gastar(cliente_id):
        async with sesion_de_inquilino(motor, operador) as conexion:
            return await registrar_consumo(
                conexion,
                operador,
                concepto=CONCEPTO_MODELO,
                monto_usd=monto,
                detalle={},
                cliente_id=cliente_id,
                titular=Titular.AGENCIA,
                ahora=MOMENTO,
            )

    resultados = await asyncio.gather(
        *(
            gastar(CLIENTE_A1 if indice % 2 == 0 else CLIENTE_A2)
            for indice in range(tareas)
        ),
        return_exceptions=True,
    )

    aceptados = [r for r in resultados if not isinstance(r, BaseException)]
    rechazados = [r for r in resultados if isinstance(r, BaseException)]
    assert all(isinstance(r, TechoAlcanzado) for r in rechazados), (
        f"algun rechazo no fue TechoAlcanzado: {[type(r).__name__ for r in rechazados]}"
    )
    with motor_admin.connect() as conexion:
        suma = conexion.execute(
            text("SELECT COALESCE(SUM(monto_usd), 0) FROM consumos WHERE titular = 'agencia'")
        ).scalar_one()
    assert suma <= techo, (
        f"la suma final {suma} supera el techo de la agencia {techo}: entraron "
        f"{len(aceptados)} de {tareas} consumos concurrentes. El techo de la agencia "
        "no corta de verdad, y es el unico gasto que no paga un cliente"
    )
    assert len(aceptados) == 3, (
        f"entraron {len(aceptados)} consumos de {monto} bajo un techo de {techo}: "
        "caben exactamente 3, y los consumos se reparten entre DOS clientes — si el "
        "cerrojo fuera por cliente, cabrian 6"
    )


async def test_exigir_margen_tampoco_carga_contra_la_agencia_desde_un_cliente(
    motor,
) -> None:
    """La comprobacion previa falla-cerrado por la MISMA razon que el registro.

    # WHY: `exigir_margen` lee el techo SIN cerrojo, y ahi cabia la duda de si el
    # camino sin `FOR UPDATE` alcanzaba una fila que el camino con cerrojo no. No
    # la alcanza, y por la POLITICA y no por un `if`: `agencias` es invisible al
    # alcance cliente (L-02), asi que la lectura devuelve cero filas en los dos
    # casos. Lo pregunto la revision cruzada; aqui esta medido.
    """
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, cliente) as conexion:
        with pytest.raises(SinTechoAlcanzable):
            await exigir_margen(
                conexion, cliente, Decimal("1"), titular=Titular.AGENCIA, ahora=MOMENTO
            )
