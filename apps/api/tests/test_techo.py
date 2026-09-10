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
    sesion_de_agencia,
    sesion_de_cliente,
)

pytestmark = pytest.mark.asyncio

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
