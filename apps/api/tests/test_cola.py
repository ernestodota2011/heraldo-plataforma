"""T-020 (RF-11, RF-14, D-04, R-07) — la cola, medida por efecto.

Lo que este archivo tiene que demostrar, y con que control:

- **Encolar va en la MISMA transaccion**: se deshace la transaccion y el trabajo
  no queda · control: otra que si se confirma y el trabajo esta.
- **`SKIP LOCKED` reparte, no duplica**: dos reclamos traen dos trabajos
  distintos · control: el tercero devuelve `None`.
- **La espera CRECE**: el 2.º reintento espera mas que el 1.º · control: en el
  tope deja de crecer, y no diverge.
- **Un fallo acaba en `fallido` VISIBLE**: la cadena entera · control: sale en la
  lista que ve una persona.
- **La purga BORRA**: se cuentan filas antes y despues · control: respeta la
  retencion y no borra lo reciente.

# WHY (cada sonda vacia su propia cola antes de medir): el escenario sembrado deja
# un trabajo por inquilino, a proposito, para que la bateria de aislamiento tenga
# filas contra las que medir. Aqui estorba, y se borra con el rol de la aplicacion
# —o sea, solo el del inquilino de la sonda—. `resembrar` lo devuelve despues.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.tenancy import sesion_de_inquilino
from conftest import AGENCIA_A, CLIENTE_A1, CLIENTE_A2, resembrar, sesion_de_cliente
from worker import cola

AHORA = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    resembrar(motor_de_siembra)


@pytest.fixture
def inquilino():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


async def _vaciar(motor, inquilino) -> None:
    """Deja la cola del inquilino de la sonda vacia. Solo la suya: lo hace RLS."""
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await conexion.execute(text("DELETE FROM trabajos"))
        await conexion.execute(text("DELETE FROM trabajos_archivados"))


def _contar(motor_admin, tabla: str, cliente_id) -> int:
    with motor_admin.connect() as conexion:
        # S608: `tabla` es una constante de este archivo, nunca entrada de usuario.
        return conexion.execute(
            text(f"SELECT count(*) FROM {tabla} WHERE cliente_id = :c"),  # noqa: S608
            {"c": cliente_id},
        ).scalar_one()


# --------------------------------------------------------------------------
# D-04 — encolar en la misma transaccion que guarda el mensaje
# --------------------------------------------------------------------------
async def test_si_la_transaccion_se_deshace_el_trabajo_no_queda_encolado(
    motor, motor_admin, inquilino
) -> None:
    """==El punto entero de D-04.== No hay instante con mensaje y sin trabajo.

    # WHY: con un broker aparte, «guardar el mensaje» y «encolar» son dos actos que
    # pueden discrepar. Aqui la sonda provoca la discrepancia a proposito: encola y
    # revienta la transaccion. Si el trabajo sobreviviera, existiria un camino por
    # el que la cola y los datos no cuentan la misma historia.
    """
    await _vaciar(motor, inquilino)
    with pytest.raises(RuntimeError):
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await cola.encolar(conexion, inquilino, tipo="generar", carga={"x": 1})
            raise RuntimeError("algo revienta despues de encolar")

    assert _contar(motor_admin, "trabajos", CLIENTE_A1) == 0, (
        "el trabajo sobrevivio a una transaccion que se deshizo"
    )


async def test_control_si_la_transaccion_se_confirma_el_trabajo_esta(
    motor, motor_admin, inquilino
) -> None:
    """Control de la anterior: un `encolar` que no encolara nunca tambien la pasaria."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(conexion, inquilino, tipo="generar", carga={"x": 1})
    assert _contar(motor_admin, "trabajos", CLIENTE_A1) == 1


# --------------------------------------------------------------------------
# Reclamar
# --------------------------------------------------------------------------
async def test_se_reclama_el_pendiente_disponible_mas_antiguo(motor, inquilino) -> None:
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        viejo = await cola.encolar(
            conexion, inquilino, tipo="viejo", disponible_en=AHORA - timedelta(minutes=10)
        )
        await cola.encolar(
            conexion, inquilino, tipo="nuevo", disponible_en=AHORA - timedelta(minutes=1)
        )

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        primero = await cola.reclamar(conexion, ahora=AHORA)
    assert primero is not None and primero.id == viejo
    assert primero.intentos == 1, "reclamar tiene que gastar el intento, no fallar"


async def test_un_trabajo_reclamado_no_lo_reclama_nadie_mas(motor, inquilino) -> None:
    """`SKIP LOCKED` reparte, no duplica: dos reclamos traen dos trabajos distintos."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(conexion, inquilino, tipo="a", disponible_en=AHORA - timedelta(hours=1))
        await cola.encolar(conexion, inquilino, tipo="b", disponible_en=AHORA - timedelta(hours=1))

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        uno = await cola.reclamar(conexion, ahora=AHORA)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        dos = await cola.reclamar(conexion, ahora=AHORA)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        tres = await cola.reclamar(conexion, ahora=AHORA)

    assert uno is not None and dos is not None
    assert uno.id != dos.id, "el mismo trabajo se entrego dos veces"
    assert tres is None, "aparecio un tercer trabajo donde solo habia dos"


async def test_un_trabajo_programado_para_luego_no_se_reclama(motor, inquilino) -> None:
    """La espera creciente no sirve de nada si el reclamo la ignora."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion, inquilino, tipo="luego", disponible_en=AHORA + timedelta(minutes=5)
        )
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await cola.reclamar(conexion, ahora=AHORA) is None
    # Control: cuando llega su hora, SI se reclama.
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await cola.reclamar(conexion, ahora=AHORA + timedelta(minutes=6)) is not None


async def test_una_sesion_no_reclama_trabajos_de_otro_inquilino(motor, inquilino) -> None:
    """El `reclamar` no lleva NI UNA clausula de inquilino: lo pone la politica."""
    await _vaciar(motor, inquilino)
    vecino = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)
    async with sesion_de_inquilino(motor, vecino) as conexion:
        await conexion.execute(text("DELETE FROM trabajos"))
        await cola.encolar(
            conexion, vecino, tipo="del-vecino", disponible_en=AHORA - timedelta(hours=1)
        )

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await cola.reclamar(conexion, ahora=AHORA) is None, (
            "un inquilino reclamo el trabajo de otro: la cola seria un canal entre "
            "inquilinos, que es la peor fuga posible de este producto"
        )
    # Control: el dueño SI lo reclama.
    async with sesion_de_inquilino(motor, vecino) as conexion:
        suyo = await cola.reclamar(conexion, ahora=AHORA)
    assert suyo is not None and suyo.tipo == "del-vecino"


# --------------------------------------------------------------------------
# RF-14 — espera creciente y `fallido` VISIBLE
# --------------------------------------------------------------------------
def test_la_espera_crece_y_se_recorta_en_el_tope() -> None:
    esperas = [cola.espera_del_reintento(n) for n in range(1, 6)]
    assert esperas == sorted(esperas), f"la espera no crece: {esperas}"
    assert len(set(esperas)) == len(esperas), "dos intentos con la misma espera: no crece"
    assert cola.espera_del_reintento(50) == cola.ESPERA_MAXIMA, "sin tope, la espera diverge"
    with pytest.raises(ValueError):
        cola.espera_del_reintento(0)


async def test_un_trabajo_que_falla_reintenta_con_espera_creciente_y_acaba_visible(
    motor, motor_admin, inquilino
) -> None:
    """==La cadena entera de RF-14, medida paso a paso.==

    Se agota un trabajo de tres intentos. Tras cada fallo se comprueba (a) que
    volvio a `pendiente`, (b) que su `disponible_en` esta MAS LEJOS que la vez
    anterior, y al final (c) que quedo `fallido` y (d) que sale en la lista que ve
    una persona.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion,
            inquilino,
            tipo="siempre-falla",
            maximo_intentos=3,
            disponible_en=AHORA - timedelta(hours=1),
        )

    # WHY (el reloj avanza entre intentos): tras el primer fallo el trabajo queda
    # programado para el FUTURO — que es justo lo que la espera creciente hace—, asi
    # que un segundo reclamo con el mismo instante no encontraria nada. Que la sonda
    # tenga que mover el reloj es, en si mismo, la primera evidencia de que la espera
    # existe: sin ella, el segundo `reclamar` habria funcionado con el reloj parado.
    esperas_observadas: list[timedelta] = []
    momento = AHORA
    for intento in (1, 2):
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            trabajo = await cola.reclamar(conexion, ahora=momento)
            assert trabajo is not None and trabajo.intentos == intento, (
                f"al intento {intento} no habia nada que reclamar en {momento}: el "
                "trabajo no volvio a la cola, o volvio con otro contador"
            )
            estado = await cola.fallar(
                conexion, trabajo, error="el modelo dijo que no", ahora=momento
            )
        assert estado is cola.Estado.PENDIENTE, (
            f"al intento {intento} de 3 el trabajo se rindio antes de tiempo"
        )
        with motor_admin.connect() as conexion:
            fila = conexion.execute(
                text("SELECT estado, disponible_en FROM trabajos WHERE id = :id"),
                {"id": identificador},
            ).one()
        assert fila.estado == "pendiente"
        esperas_observadas.append(fila.disponible_en - momento)
        momento = fila.disponible_en

    assert esperas_observadas[1] > esperas_observadas[0], (
        f"la espera del 2.º reintento ({esperas_observadas[1]}) no es mayor que la del "
        f"1.º ({esperas_observadas[0]}): no hay espera CRECIENTE, hay espera fija"
    )

    # Tercer intento: se agota y se rinde.
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        trabajo = await cola.reclamar(conexion, ahora=momento)
        assert trabajo is not None and trabajo.intentos == 3
        estado = await cola.fallar(
            conexion, trabajo, error="el modelo dijo que no", ahora=momento
        )
    assert estado is cola.Estado.FALLIDO

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        visibles = await cola.fallidos(conexion)
    assert [f["id"] for f in visibles] == [identificador], (
        "el trabajo agotado no aparece en la lista de fallidos: RF-14 pide que quede "
        "VISIBLE para un humano, no solo que deje de reintentarse"
    )
    assert visibles[0]["ultimo_error"] == "el modelo dijo que no"


async def test_un_trabajo_reclamado_y_abandonado_vuelve_a_la_cola(
    motor, motor_admin, inquilino
) -> None:
    """El otro modo de fallo de R-03: el worker muere A MEDIAS, con el trabajo.

    # WHY: la eleccion de confirmar el `en_curso` antes de ejecutar —la que hace que
    # `SKIP LOCKED` funcione— tiene este precio. No se ignora: se paga con el
    # rescate, y el rescate se mide.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="abandonado", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)

    mas_tarde = AHORA + cola.PLAZO_DE_ABANDONO + timedelta(minutes=1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        rescatados = await cola.rescatar_abandonados(conexion, ahora=mas_tarde)
    assert rescatados == 1

    with motor_admin.connect() as conexion:
        estado = conexion.execute(
            text("SELECT estado FROM trabajos WHERE id = :id"), {"id": identificador}
        ).scalar_one()
    assert estado == "pendiente"

    # Control: ANTES del plazo no se rescata nada. Sin esto, un rescate que
    # devolviera todo siempre pasaria la mitad de arriba y romperia la cola.
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        trabajo = await cola.reclamar(conexion, ahora=mas_tarde)
        assert trabajo is not None
        pronto = await cola.rescatar_abandonados(conexion, ahora=mas_tarde + timedelta(minutes=1))
    assert pronto == 0


# --------------------------------------------------------------------------
# R-07 — archivado y purga, desde el dia 1
# --------------------------------------------------------------------------
async def test_el_archivado_saca_lo_hecho_de_la_cola_y_lo_deja_en_el_archivo(
    motor, motor_admin, inquilino
) -> None:
    """Se cuentan las filas de las DOS tablas, antes y despues. Es un movimiento."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="hecho", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)
        await cola.completar(conexion, identificador, ahora=AHORA)

    assert _contar(motor_admin, "trabajos", CLIENTE_A1) == 1
    assert _contar(motor_admin, "trabajos_archivados", CLIENTE_A1) == 0

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        movidos = await cola.archivar(
            conexion, ahora=AHORA + timedelta(days=2), retencion=timedelta(days=1)
        )

    assert movidos == 1
    assert _contar(motor_admin, "trabajos", CLIENTE_A1) == 0, (
        "el trabajo archivado sigue en la tabla caliente: no se movio, se copio, y la "
        "tabla de cola sigue creciendo — que es exactamente R-07"
    )
    assert _contar(motor_admin, "trabajos_archivados", CLIENTE_A1) == 1


async def test_un_fallido_no_se_archiva_solo(motor, motor_admin, inquilino) -> None:
    """RF-14 gana al archivado: un fallido tiene que seguir VISIBLE.

    # WHY: archivar un fallido por antiguedad es hacerlo desaparecer de la vista
    # con otro nombre. Se archiva solo si alguien lo PIDE, y quien lo pide esta
    # diciendo que ya lo miro.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion,
            inquilino,
            tipo="fallido",
            maximo_intentos=1,
            disponible_en=AHORA - timedelta(hours=1),
        )
        trabajo = await cola.reclamar(conexion, ahora=AHORA)
        assert trabajo is not None
        assert await cola.fallar(conexion, trabajo, error="no", ahora=AHORA) is cola.Estado.FALLIDO

    despues = AHORA + timedelta(days=30)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        movidos = await cola.archivar(conexion, ahora=despues, retencion=timedelta(days=1))
    assert movidos == 0, "un fallido se archivo solo y dejo de estar a la vista"
    assert _contar(motor_admin, "trabajos", CLIENTE_A1) == 1

    # Control: cuando se pide EXPLICITAMENTE, si se archiva.
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        movidos = await cola.archivar(
            conexion, ahora=despues, retencion=timedelta(days=1), incluir_fallidos=True
        )
    assert movidos == 1


async def test_la_purga_borra_de_verdad(motor, motor_admin, inquilino) -> None:
    """==Se mide con filas antes y despues, no con lo que devuelve la funcion.=="""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="para-purgar", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)
        await cola.completar(conexion, identificador, ahora=AHORA)
        await cola.archivar(conexion, ahora=AHORA + timedelta(days=2), retencion=timedelta(days=1))

    antes = _contar(motor_admin, "trabajos_archivados", CLIENTE_A1)
    assert antes == 1, "no hay nada archivado: la purga no tendria nada que borrar"

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        purgados = await cola.purgar(
            conexion, ahora=AHORA + timedelta(days=90), retencion=timedelta(days=30)
        )

    despues = _contar(motor_admin, "trabajos_archivados", CLIENTE_A1)
    assert purgados == 1
    assert despues == 0, (
        f"la purga dijo que borro {purgados} y quedan {despues} filas: o borro otra cosa, "
        "o el borrado es logico y la tabla sigue creciendo"
    )


async def test_la_purga_respeta_la_retencion(motor, motor_admin, inquilino) -> None:
    """Control de la anterior: una purga que borrara todo siempre la pasaria."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="reciente", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)
        await cola.completar(conexion, identificador, ahora=AHORA)
        await cola.archivar(conexion, ahora=AHORA + timedelta(days=2), retencion=timedelta(days=1))
        purgados = await cola.purgar(
            conexion, ahora=AHORA + timedelta(days=2), retencion=timedelta(days=30)
        )
    assert purgados == 0
    assert _contar(motor_admin, "trabajos_archivados", CLIENTE_A1) == 1


async def test_el_archivo_no_se_puede_reescribir(motor, inquilino) -> None:
    """Un archivo que se puede editar no es un archivo: sin `UPDATE`, por permiso."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="hecho", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)
        await cola.completar(conexion, identificador, ahora=AHORA)
        await cola.archivar(conexion, ahora=AHORA + timedelta(days=2), retencion=timedelta(days=1))

    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await conexion.execute(
                text("UPDATE trabajos_archivados SET tipo = 'otro' WHERE id = :id"),
                {"id": identificador},
            )
    assert "permission denied" in str(capturado.value).lower()
