"""T-025·bis (RNF-06): nada irreversible sin confirmacion que diga QUE y CUANTO.

Tres afirmaciones, y las tres se miden por EFECTO sobre la ruta real, no sobre
el modulo aislado — un guard que solo vive en la suite autoriza en produccion
(`feedback_guard_solo_en_el_test`):

1. **La confirmacion nombra el objeto y el recuento.** «¿Seguro?» no cumple: el
   unico valor que abre la puerta se DERIVA del inventario, asi que no se puede
   escribir sin haberlo recibido antes.
2. **Fail-closed.** Si no se puede contar, no se confirma — y por tanto no se
   destruye. Se ejercita con un `REVOKE` REAL sobre la base, no con un doble.
3. **Y esta montado en la RUTA.** La compuerta es la que produce el argumento del
   manejador: no hay forma de entrar sin pasar por ella.

# WHY (que pondria esto en rojo): que la ruta destruya sin confirmacion; que
# acepte un «si» constante; que el inventario cuente de menos; que un recuento
# imposible se convierta en una destruccion en vez de en una negativa; o que
# alguien anada una segunda ruta destructiva sin la compuerta.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.main import Entorno, confirmacion_de_operacion_destructiva, crear_aplicacion
from app.tenancy import Inquilino, crear_motor, sesion_de_inquilino
from app.tenancy.confirmacion import (
    COLUMNA_IDENTIDAD_DEL_CLIENTE,
    TABLA_REGISTRO_DE_CLIENTES,
    ConfirmacionInvalida,
    ConfirmacionRequerida,
    Inventario,
    NoSePuedeContar,
    exigir_confirmacion,
    inventariar_baja_de_cliente,
)
from app.tenancy.politicas import COLUMNA_AGENCIA, COLUMNA_CLIENTE
from app.tenancy.rol import ROL_APLICACION, VERBOS
from conftest import (
    AGENCIA_A,
    AGENCIA_B,
    CLIENTE_A1,
    CLIENTE_A2,
    CLIENTE_B1,
    RAIZ,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)
from test_escalada_alcance import _fuentes_de_la_aplicacion

ORIGEN_DECLARADO = "https://panel.aetherlogik.example"
DSN_INALCANZABLE = "postgresql+psycopg://heraldo_app@127.0.0.1:1/no_existe"

#: SQL que destruye. Se busca en el arbol de la aplicacion: si aparece en un
#: modulo que no exige confirmacion, es una segunda puerta.
#:
#: # WHY (`drop column`, anadido con la revision 0004): esto es una denylist de
#: verbos, y a una denylist siempre le falta uno
#: (`feedback_denylist_por_allowlist`). El que faltaba lo destapo la primera
#: migracion que quita una columna: `ALTER TABLE ... DROP COLUMN` destruye datos de
#: un cliente y no lleva ninguna de las cuatro palabras que este guard miraba.
#: Medido al anadirlo: hoy no cae ningun modulo de la aplicacion —el unico sitio
#: donde aparece es el `downgrade()` de la 0004, que ya entra en la allowlist con su
#: motivo—, asi que cerrarlo no arregla ningun defecto vivo; se cierra igual porque
#: el arreglo vive SOLO en este guard y el riesgo de cerrarlo es cero.
VERBOS_DESTRUCTIVOS = (
    "delete from",
    "truncate",
    "drop table",
    "drop schema",
    "drop column",
)

#: Modulos que PUEDEN llevar SQL destructiva sin pasar por la confirmacion, con su
#: motivo escrito. Es una ALLOWLIST: lo que no este aqui y lleve SQL destructiva
#: pone el CI en rojo. El motivo vive en el propio artefacto, no en un comentario
#: suelto (L-20).
SQL_DESTRUCTIVA_PERMITIDA: dict[str, str] = {
    "apps/api/migrations/versions/0003_la_base_y_la_cola.py": (
        "DDL de migracion, igual que la 0001: su `DROP TABLE` esta en el "
        "`downgrade()` y lo ejecuta el rol MIGRADOR sobre el esquema, nunca la "
        "aplicacion sobre los datos de un cliente. RNF-06 gobierna las operaciones "
        "sobre datos de un cliente, y deshacer una migracion no es una de ellas"
    ),
    "apps/worker/cola.py": (
        "Su `DELETE FROM trabajos` es un ARCHIVADO, no una destruccion: vive dentro "
        "de un CTE que alimenta al `INSERT INTO trabajos_archivados` de la misma "
        "sentencia, asi que la fila cambia de tabla y no se pierde. La UNICA "
        "destruccion real del modulo es `purgar()`, y por eso `mantenimiento()` NO "
        "la ejecuta sola: hay que pedirla. La purga por retencion desatendida es "
        "T-213 y necesita antes la politica de retencion de RF-50, para que la "
        "confirmacion se de UNA vez sobre la politica y no en cada corrida — "
        "hallazgo abierto en P-31 del registro de problemas"
    ),
    "apps/api/migrations/versions/0001_cimiento_del_inquilino.py": (
        "DDL de migracion: la ejecuta el rol MIGRADOR sobre el esquema, no la "
        "aplicacion sobre los datos de un cliente. RNF-06 gobierna las operaciones "
        "sobre datos de un cliente, y una migracion no es una de ellas"
    ),
    "apps/api/migrations/versions/0004_el_sector_del_cliente.py": (
        "DDL de migracion, igual que la 0001 y la 0003: su `DROP COLUMN` esta en el "
        "`downgrade()` y lo ejecuta el rol MIGRADOR sobre el esquema. La aplicacion "
        "no hace DDL — no tiene CREATE sobre el esquema ni es duena de ninguna "
        "tabla, y las dos cosas las comprueba `test_rls_cobertura.py`"
    ),
}


# --------------------------------------------------------------------------
# El recuento ESPERADO tambien se deriva. No es un numero escrito a mano.
# --------------------------------------------------------------------------
def _lo_que_cuelga_de_a1(motor_admin) -> int:
    """Cuantas filas se lleva la baja del cliente A1, contadas POR OTRO CAMINO.

    # WHY (P-31): hasta la revision 0003 esto era un `2` escrito a mano — el
    # recuento del dia en que solo existian `clientes` y `heraldos`. En cuanto una
    # migracion anadio cinco tablas de inquilino, el inventario —que SI deriva su
    # universo del catalogo— empezo a contar 7 y estas sondas se pusieron en rojo
    # por un numero caducado, no por un defecto. Es P-10/P-11/P-12 otra vez, esta
    # vez en la EXPECTATIVA en lugar de en el universo.
    #
    # # WHY (no es una tautologia): el codigo bajo prueba cuenta con el rol de
    # APLICACION, derivando del catalogo y con RLS puesta. Esto cuenta con el rol
    # MIGRADOR y filtrando `cliente_id` a mano. Son dos caminos distintos hasta el
    # mismo numero; si se separaran, la sonda lo diria.
    """
    with motor_admin.connect() as conexion:
        columnas = conexion.execute(
            text(
                "SELECT c.relname AS tabla, a.attname AS columna "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT a.attisdropped"
            )
        ).all()
        de_cliente = sorted({f.tabla for f in columnas if f.columna == COLUMNA_CLIENTE})
        total = 0
        for tabla in de_cliente:
            total += conexion.execute(
                text(f"SELECT count(*) FROM {tabla} WHERE cliente_id = :c"),  # noqa: S608
                {"c": CLIENTE_A1},
            ).scalar_one()
        # Y su propia ficha en el registro de clientes.
        total += conexion.execute(
            text("SELECT count(*) FROM clientes WHERE id = :c"), {"c": CLIENTE_A1}
        ).scalar_one()
    assert total > 2, (
        "el escenario sembrado deja dos filas o menos colgando de A1: esta sonda "
        "estaria midiendo un caso demasiado pobre para distinguir nada"
    )
    return total


# --------------------------------------------------------------------------
# Andamiaje
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Cada sonda arranca del MISMO escenario: aqui se DESTRUYE de verdad."""
    resembrar(motor_de_siembra)


@pytest.fixture(scope="module", autouse=True)
def _escenario_devuelto_al_salir(motor_de_siembra):
    yield
    resembrar(motor_de_siembra)


class BitacoraDePrueba:
    """El sitio donde T-017 enchufara la bitacora real. Aqui solo recuerda."""

    def __init__(self) -> None:
        self.entradas: list[tuple[Inquilino, Inventario]] = []

    async def __call__(self, inquilino: Inquilino, inventario: Inventario) -> None:
        self.entradas.append((inquilino, inventario))


class BitacoraQueFalla:
    """Una bitacora que no puede escribir. Si falla, NO se destruye nada."""

    async def __call__(self, inquilino: Inquilino, inventario: Inventario) -> None:
        raise RuntimeError("la bitacora no acepta la entrada")


class BitacoraQueBorraPorDetras:
    """Simula la carrera REAL: la fila desaparece entre el inventario y el borrado.

    # WHY (y por que este es el punto de inyeccion honesto): la bitacora corre
    # DENTRO de la transaccion de inquilino, entre el inventario y el `DELETE`.
    # Es codigo real ejecutandose en esa ventana, no un parche del test. Aqui usa
    # una conexion de ADMIN aparte —o sea, otra sesion— para llevarse la fila,
    # que es exactamente lo que haria otro operador dando de baja al mismo
    # cliente a la vez.
    """

    def __init__(self, motor_admin, cliente_id) -> None:
        self.motor_admin = motor_admin
        self.cliente_id = cliente_id

    async def __call__(self, inquilino: Inquilino, inventario: Inventario) -> None:
        with self.motor_admin.connect() as conexion:
            conexion.execute(
                text(
                    f"DELETE FROM {TABLA_REGISTRO_DE_CLIENTES} "  # noqa: S608
                    f"WHERE {COLUMNA_IDENTIDAD_DEL_CLIENTE} = :cliente"
                ),
                {"cliente": self.cliente_id},
            )


def _identidad(inquilino: Inquilino):
    """El proveedor que T-015 sustituira. Deriva de la fila, nunca de la peticion."""

    async def proveedor(request) -> Inquilino:
        return inquilino

    return proveedor


def _aplicacion(motor, inquilino: Inquilino, **extras: Any):
    parametros: dict[str, Any] = {
        "entorno": Entorno.PRODUCCION,
        "origenes": (ORIGEN_DECLARADO,),
        "motor": motor,
        "proveedor_de_inquilino": _identidad(inquilino),
    }
    parametros.update(extras)
    return crear_aplicacion(**parametros)


def _cliente_http(aplicacion) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=aplicacion), base_url="http://prueba"
    )


def _censo(motor_admin) -> dict[str, int]:
    """Recuento REAL con el rol migrador: RLS no recorta lo que la sonda verifica."""
    with motor_admin.connect() as conexion:
        return {
            tabla: conexion.execute(text(f"SELECT count(*) FROM {tabla}")).scalar_one()  # noqa: S608
            for tabla in ("agencias", "clientes", "heraldos")
        }


def _clientes_vivos(motor_admin) -> set:
    with motor_admin.connect() as conexion:
        return {
            fila[0]
            for fila in conexion.execute(
                text(
                    f"SELECT {COLUMNA_IDENTIDAD_DEL_CLIENTE} "  # noqa: S608
                    f"FROM {TABLA_REGISTRO_DE_CLIENTES}"
                )
            ).all()
        }


# ==========================================================================
# El inventario nombra el objeto y el recuento
# ==========================================================================
async def test_el_inventario_nombra_el_objeto_y_el_recuento(motor, motor_admin) -> None:
    """RNF-06 en su forma exacta: QUE se destruye y CUANTO."""
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        inventario = await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)

    assert "Cliente A1" in inventario.objeto, (
        f"el inventario no nombra el objeto ({inventario.objeto!r}): «¿seguro?» no "
        "cumple RNF-06 precisamente por esto"
    )
    recuentos = {r.tabla: r.filas for r in inventario.recuentos}
    assert recuentos[TABLA_REGISTRO_DE_CLIENTES] == 1
    assert recuentos["heraldos"] == 1, (
        f"el inventario dice que la baja del cliente A1 se lleva {recuentos} y ese "
        "cliente tiene 1 heraldo colgando de la cascada"
    )
    esperado = _lo_que_cuelga_de_a1(motor_admin)
    assert inventario.total == esperado

    aviso = inventario.frase()
    assert "Cliente A1" in aviso and str(esperado) in aviso and "heraldos: 1" in aviso, (
        f"el aviso no dice que ni cuanto: {aviso!r}"
    )


async def test_el_universo_del_inventario_se_deriva_del_catalogo(motor, motor_admin) -> None:
    """Toda tabla de la clase *de cliente* entra SOLA, sin que nadie la enumere.

    # WHY: es la leccion de P-10, P-11 y P-12, y la que L-03 hizo critica. Con una
    # lista escrita a mano, la tabla que una migracion futura anada se quedaria
    # fuera del recuento y el aviso mentiria por defecto — el numero mas peligroso
    # que puede dar un inventario de destruccion.
    """
    with motor_admin.connect() as conexion:
        esperadas = {
            fila.tabla
            for fila in conexion.execute(
                text(
                    """
                    SELECT c.relname AS tabla
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid
                         AND a.attnum > 0 AND NOT a.attisdropped
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                      AND a.attname IN (:agencia, :cliente)
                    GROUP BY c.relname
                    HAVING count(DISTINCT a.attname) = 2
                    """
                ),
                {"agencia": COLUMNA_AGENCIA, "cliente": COLUMNA_CLIENTE},
            ).all()
        }
    assert esperadas, "el catalogo no tiene ninguna tabla de la clase de cliente que medir"

    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        inventario = await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)

    inventariadas = {r.tabla for r in inventario.recuentos}
    assert esperadas <= inventariadas, (
        f"el catalogo declara {sorted(esperadas)} en la clase de cliente y el "
        f"inventario solo cuenta {sorted(inventariadas)}: las que faltan caerian por "
        "la cascada sin que nadie las hubiera nombrado"
    )


# ==========================================================================
# Fail-closed: si no se puede contar, no se confirma
# ==========================================================================
async def test_no_se_inventaria_el_cliente_de_otra_agencia(motor) -> None:
    """Fail-closed y aislamiento a la vez: fuera del alcance no se cuenta."""
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        with pytest.raises(NoSePuedeContar):
            await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_B1)


async def test_no_se_inventaria_un_cliente_que_no_existe(motor) -> None:
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        with pytest.raises(NoSePuedeContar):
            await inventariar_baja_de_cliente(conexion, cliente_id=uuid4())


async def test_si_una_tabla_no_se_deja_contar_NO_se_confirma(motor, motor_admin) -> None:
    """EL fail-closed de verdad, con un privilegio revocado en la base REAL.

    # WHY (lo que esta sonda destapo y por eso existe): la primera version de
    # `confirmacion.py` derivaba el universo de `information_schema`, que esta
    # FILTRADA POR PRIVILEGIO. Medido: tras el `REVOKE`, `heraldos` desaparecia de
    # esa vista y el inventario habria salido **exitoso contando una tabla menos**
    # — «se destruye 1 fila» justo antes de que la cascada se llevara las demas.
    # Derivar de `pg_catalog` es lo que convierte esto en un fallo ruidoso.
    # Registrado como P-16.
    """
    with motor_admin.connect() as conexion:
        conexion.execute(text(f"REVOKE ALL ON heraldos FROM {ROL_APLICACION}"))  # noqa: S608
    try:
        async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
            with pytest.raises(NoSePuedeContar) as capturado:
                await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)
        assert "no pudo completarse" in str(capturado.value)
    finally:
        with motor_admin.connect() as conexion:
            conexion.execute(
                text(  # noqa: S608
                    f"GRANT {', '.join(VERBOS)} ON heraldos TO {ROL_APLICACION}"
                )
            )

    # Y el control, en la misma prueba: devuelto el privilegio, vuelve a contar.
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        inventario = await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)
    assert inventario.total == _lo_que_cuelga_de_a1(motor_admin), (
        "tras devolver el privilegio el inventario no volvio a su valor: la limpieza "
        "de esta prueba dejo la base distinta de como la encontro"
    )


# ==========================================================================
# La confirmacion no se puede escribir sin haber visto el inventario
# ==========================================================================
@pytest.fixture
async def inventario_a1(motor) -> Inventario:
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        return await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)


@pytest.mark.parametrize(
    "generica", ["si", "sí", "SI", "true", "True", "1", "yes", "ok", "confirmar", "borrar"]
)
async def test_una_confirmacion_generica_no_vale(
    inventario_a1: Inventario, generica: str
) -> None:
    """«¿Seguro?» no cumple RNF-06, y aqui eso es mecanico: no hay «si» constante."""
    with pytest.raises(ConfirmacionInvalida):
        exigir_confirmacion(inventario_a1, generica)


@pytest.mark.parametrize("vacia", [None, "", "   "])
async def test_sin_confirmacion_se_pide_el_inventario(
    inventario_a1: Inventario, vacia
) -> None:
    with pytest.raises(ConfirmacionRequerida) as capturado:
        exigir_confirmacion(inventario_a1, vacia)
    assert "Cliente A1" in str(capturado.value), (
        "al pedir la confirmacion no se dice sobre que: eso es un «¿seguro?»"
    )


async def test_control_la_confirmacion_derivada_del_inventario_si_vale(
    inventario_a1: Inventario,
) -> None:
    """El control: una compuerta que rechaza SIEMPRE no es una compuerta."""
    assert exigir_confirmacion(inventario_a1, inventario_a1.huella()) is None
    assert exigir_confirmacion(inventario_a1, f"  {inventario_a1.huella().upper()} ") is None


async def test_la_confirmacion_caduca_si_cambia_lo_que_se_va_a_destruir(
    motor, motor_admin, inventario_a1: Inventario
) -> None:
    """Confirmaste 2 filas; si ahora son 3, esa confirmacion ya no vale.

    # WHY: entre la vista previa y la confirmacion pasa tiempo real — el operador
    # lee, duda, pregunta. Si en ese hueco entran datos nuevos, la confirmacion
    # que dio ya no describe lo que se destruiria.
    """
    with motor_admin.connect() as conexion:
        conexion.execute(
            text(
                "INSERT INTO heraldos (id, agencia_id, cliente_id, nombre) "
                "VALUES (:id, :a, :c, 'Heraldo A1 nuevo')"
            ),
            {"id": uuid4(), "a": AGENCIA_A, "c": CLIENTE_A1},
        )

    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        ahora = await inventariar_baja_de_cliente(conexion, cliente_id=CLIENTE_A1)

    assert ahora.total == inventario_a1.total + 1
    assert ahora.huella() != inventario_a1.huella()
    with pytest.raises(ConfirmacionInvalida):
        exigir_confirmacion(ahora, inventario_a1.huella())


# ==========================================================================
# Y todo lo anterior, EN LA RUTA
# ==========================================================================
async def test_la_ruta_sin_confirmacion_no_destruye_y_dice_que_y_cuanto(
    motor, motor_admin
) -> None:
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}")

    assert respuesta.status_code == 409, (
        f"la ruta destructiva contesto {respuesta.status_code} sin confirmacion "
        f"ninguna: {respuesta.text[:300]}"
    )
    inventario = respuesta.json()["detail"]["que_se_destruye"]
    assert "Cliente A1" in inventario["objeto"]
    assert inventario["recuentos"]["heraldos"] == 1
    assert inventario["total"] == _lo_que_cuelga_de_a1(motor_admin)
    assert inventario["confirmacion"]

    assert _censo(motor_admin) == antes, (
        "la peticion sin confirmacion cambio la base: la vista previa no puede ser "
        "destructiva"
    )


async def test_la_ruta_con_una_confirmacion_inventada_no_destruye(motor, motor_admin) -> None:
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}?confirmacion=si")
    assert respuesta.status_code == 409
    assert _censo(motor_admin) == antes


async def test_la_ruta_con_la_confirmacion_correcta_si_destruye_y_solo_lo_suyo(
    motor, motor_admin
) -> None:
    """EL CONTROL, y el mas importante: el producto tiene que poder dar de baja.

    Ademas comprueba el aislamiento del efecto: cae el cliente A1 y su heraldo, y
    NO caen ni el cliente vecino de la misma agencia ni la otra agencia.
    """
    antes = _censo(motor_admin)
    bitacora = BitacoraDePrueba()
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=bitacora)
    esperado = _lo_que_cuelga_de_a1(motor_admin)

    async with _cliente_http(aplicacion) as cliente:
        vista = await cliente.delete(f"/clientes/{CLIENTE_A1}")
        confirmacion = vista.json()["detail"]["que_se_destruye"]["confirmacion"]
        hecho = await cliente.delete(f"/clientes/{CLIENTE_A1}?confirmacion={confirmacion}")

    assert hecho.status_code == 200, f"la baja confirmada fallo: {hecho.text[:300]}"
    assert hecho.json()["destruido"]["total"] == esperado

    despues = _censo(motor_admin)
    assert despues["clientes"] == antes["clientes"] - 1
    assert despues["heraldos"] == antes["heraldos"] - 1
    assert despues["agencias"] == antes["agencias"]

    vivos = _clientes_vivos(motor_admin)
    assert CLIENTE_A1 not in vivos
    assert {CLIENTE_A2, CLIENTE_B1} <= vivos, (
        f"la baja de A1 se llevo por delante a alguien mas: quedan "
        f"{sorted(map(str, vivos))}. El vecino de la misma agencia y la otra agencia "
        "tienen que seguir intactos"
    )

    assert len(bitacora.entradas) == 1, (
        "la operacion irreversible no dejo exactamente una entrada de bitacora (RF-10)"
    )
    assert bitacora.entradas[0][1].huella() == confirmacion


async def test_si_la_bitacora_falla_no_se_destruye_nada(motor, motor_admin) -> None:
    """RF-10 como cerrojo: sin rastro no hay destruccion.

    # WHY: la bitacora se escribe ANTES del borrado y dentro de la misma
    # transaccion de inquilino. Si registrarlo falla, la transaccion se deshace y
    # el cliente sigue ahi. Al reves —borrar y luego registrar— dejaria el caso
    # peor posible: destruido y sin rastro.
    """
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        vista = await cliente.delete(f"/clientes/{CLIENTE_A1}")
        confirmacion = vista.json()["detail"]["que_se_destruye"]["confirmacion"]

    fallona = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraQueFalla())
    async with _cliente_http(fallona) as cliente:
        with pytest.raises(RuntimeError):
            await cliente.delete(f"/clientes/{CLIENTE_A1}?confirmacion={confirmacion}")

    assert _censo(motor_admin) == antes, (
        "la bitacora fallo y aun asi se destruyo: quedaria una operacion irreversible "
        "sin quien, que ni cuando (RF-10)"
    )


async def test_si_la_fila_desaparece_entre_el_inventario_y_el_borrado_no_se_miente(
    motor, motor_admin
) -> None:
    """La carrera que levanto Crisol: el informe no puede decir «destruidas 2».

    # WHY: el inventario y el `DELETE` viven en la misma transaccion, pero en
    # READ COMMITTED otra sesion puede llevarse la fila en medio. Sin comprobar
    # las filas afectadas, la respuesta diria «destruidas 2 filas» y la bitacora
    # habria registrado una destruccion que no ocurrio. Aqui se provoca de
    # verdad, desde otra sesion, en la ventana real. Registrado como P-19.
    """
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        vista = await cliente.delete(f"/clientes/{CLIENTE_A1}")
        confirmacion = vista.json()["detail"]["que_se_destruye"]["confirmacion"]

    con_carrera = _aplicacion(
        motor,
        sesion_de_agencia(AGENCIA_A),
        registrador=BitacoraQueBorraPorDetras(motor_admin, CLIENTE_A1),
    )
    async with _cliente_http(con_carrera) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}?confirmacion={confirmacion}")

    assert respuesta.status_code == 409, (
        f"la fila desaparecio en la ventana y la ruta contesto {respuesta.status_code}: "
        f"si fuera 200, el informe y la bitacora afirmarian una destruccion que no "
        f"ocurrio — {respuesta.text[:200]}"
    )


async def test_sin_bitacora_cableada_la_ruta_se_niega(motor, motor_admin) -> None:
    """El estado REAL de hoy: T-017 no existe, asi que esta ruta no destruye nada."""
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A))  # registrador por defecto
    async with _cliente_http(aplicacion) as cliente:
        vista = await cliente.delete(f"/clientes/{CLIENTE_A1}")
        confirmacion = vista.json()["detail"]["que_se_destruye"]["confirmacion"]
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}?confirmacion={confirmacion}")
    assert respuesta.status_code == 503
    assert _censo(motor_admin) == antes


async def test_sin_identidad_cableada_la_ruta_ni_siquiera_inventaria(
    motor, motor_admin
) -> None:
    """El alcance sale de la fila del usuario, y ese camino aun no existe (T-015)."""
    antes = _censo(motor_admin)
    aplicacion = crear_aplicacion(
        entorno=Entorno.PRODUCCION, origenes=(ORIGEN_DECLARADO,), motor=motor
    )
    async with _cliente_http(aplicacion) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}")
    assert respuesta.status_code == 503
    assert _censo(motor_admin) == antes


async def test_una_sesion_de_portal_no_puede_dar_de_baja_a_nadie(motor, motor_admin) -> None:
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(
        motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1), registrador=BitacoraDePrueba()
    )
    async with _cliente_http(aplicacion) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_A1}")
    assert respuesta.status_code == 403
    assert _censo(motor_admin) == antes


async def test_no_se_puede_dar_de_baja_al_cliente_de_otra_agencia(motor, motor_admin) -> None:
    """La compuerta hereda el aislamiento: fuera de alcance no hay ni inventario."""
    antes = _censo(motor_admin)
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_A), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        respuesta = await cliente.delete(f"/clientes/{CLIENTE_B1}")
    assert respuesta.status_code == 409
    assert "no se puede contar" in str(respuesta.json()["detail"])
    assert _censo(motor_admin) == antes


async def test_control_el_operador_de_la_agencia_B_si_da_de_baja_al_suyo(
    motor, motor_admin
) -> None:
    """El par PERMITIDO del anterior: si nadie pudiera, el producto estaria roto."""
    aplicacion = _aplicacion(motor, sesion_de_agencia(AGENCIA_B), registrador=BitacoraDePrueba())
    async with _cliente_http(aplicacion) as cliente:
        vista = await cliente.delete(f"/clientes/{CLIENTE_B1}")
        confirmacion = vista.json()["detail"]["que_se_destruye"]["confirmacion"]
        hecho = await cliente.delete(f"/clientes/{CLIENTE_B1}?confirmacion={confirmacion}")
    assert hecho.status_code == 200
    vivos = _clientes_vivos(motor_admin)
    assert CLIENTE_B1 not in vivos
    assert {CLIENTE_A1, CLIENTE_A2} <= vivos


# ==========================================================================
# Guards: que olvidar la compuerta NO compile
# ==========================================================================
def _aplicacion_para_inspeccionar():
    """La aplicacion REAL, con un motor que nunca se usa: aqui no se hace ninguna
    peticion, solo se lee la tabla de rutas."""
    return crear_aplicacion(
        entorno=Entorno.PRODUCCION,
        origenes=(ORIGEN_DECLARADO,),
        motor=crear_motor(DSN_INALCANZABLE),
    )


def _rutas_con_dependencias(aplicacion):
    return [ruta for ruta in aplicacion.routes if getattr(ruta, "dependant", None) is not None]


def _dependencias(dependant):
    for sub in dependant.dependencies:
        yield sub
        yield from _dependencias(sub)


def _es_destructiva(ruta) -> bool:
    metodos = getattr(ruta, "methods", set()) or set()
    return "DELETE" in metodos or "destructiva" in (getattr(ruta, "tags", None) or [])


def test_hay_al_menos_una_ruta_destructiva_que_medir() -> None:
    """Meta-control: un guard sin sujeto pasa siempre."""
    aplicacion = _aplicacion_para_inspeccionar()
    destructivas = [r for r in _rutas_con_dependencias(aplicacion) if _es_destructiva(r)]
    assert destructivas, (
        "la aplicacion no expone ninguna ruta destructiva: el guard de abajo saldria "
        "verde sin haber comprobado nada"
    )


def test_toda_ruta_destructiva_declara_la_compuerta_de_confirmacion() -> None:
    """Anadir una ruta destructiva sin la compuerta pone el CI en ROJO.

    # WHY: es la unica forma de que RNF-06 sobreviva a T-212 (borrado de cliente)
    # y a T-213 (purga por retencion), que todavia no existen. La regla no
    # depende de que quien las escriba se acuerde.
    #
    # WHY (el limite, declarado): «destructiva» se reconoce por el metodo DELETE o
    # por la etiqueta `destructiva`. Una purga futura que sea un POST y que no se
    # etiquete NO la ve este guard — la ve el de abajo, que mira la SQL.
    """
    aplicacion = _aplicacion_para_inspeccionar()
    culpables: list[str] = []
    for ruta in _rutas_con_dependencias(aplicacion):
        if not _es_destructiva(ruta):
            continue
        llamadas = {sub.call for sub in _dependencias(ruta.dependant)}
        if confirmacion_de_operacion_destructiva not in llamadas:
            culpables.append(f"{sorted(getattr(ruta, 'methods', set()))} {ruta.path}")
    assert not culpables, (
        f"estas rutas destruyen sin declarar la compuerta de RNF-06: {culpables}. La "
        "confirmacion tiene que producir el argumento del manejador; si no, se puede "
        "entrar sin pasar por ella"
    )


#: Las funciones por las que una cadena LLEGA a la base. Solo se miran los
#: literales que viajan dentro de una de ellas.
FUNCIONES_QUE_EJECUTAN_SQL = ("text", "execute", "exec_driver_sql")


def _literales_que_van_a_la_base(arbol: ast.AST) -> list[str]:
    """Las cadenas que se le pasan a algo que ejecuta SQL. Solo esas.

    # WHY (P-31, y es P-18 otra vez): la primera version recogia TODA cadena del
    # modulo, docstrings y mensajes de error incluidos. Con eso, un modulo que
    # EXPLICA que la aplicacion «no hace TRUNCATE» quedaba senalado por decirlo — el
    # guard castigaba a quien documenta la regla que el guard hace cumplir, que es
    # literalmente la leccion de P-18. Y el precio no era el ruido: era que la
    # allowlist se llenara de excepciones que no son excepciones, hasta dejar de
    # leerse.
    #
    # # WHY (no pierde ningun caso verdadero): lo que destruye datos es una cadena
    # que se EJECUTA, y una cadena se ejecuta pasando por `text(...)`,
    # `.execute(...)` o `.exec_driver_sql(...)`. Se recorre el subarbol ENTERO de
    # esas llamadas, asi que una sentencia partida en trozos concatenados, o metida
    # dentro de un CTE, sigue cayendo. Lo comprueba
    # `test_el_guard_de_sql_ve_lo_que_se_ejecuta_y_no_la_prosa`.
    """
    encontrados: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = None
        if isinstance(nodo.func, ast.Attribute):
            nombre = nodo.func.attr
        elif isinstance(nodo.func, ast.Name):
            nombre = nodo.func.id
        if nombre not in FUNCIONES_QUE_EJECUTAN_SQL:
            continue
        for hijo in ast.walk(nodo):
            if isinstance(hijo, ast.Constant) and isinstance(hijo.value, str):
                encontrados.append(hijo.value.lower())
    return encontrados


def _lleva_sql_destructiva(fuente: str, ruta) -> bool:
    arbol = ast.parse(fuente, filename=str(ruta))
    literales = _literales_que_van_a_la_base(arbol)
    return any(verbo in literal for literal in literales for verbo in VERBOS_DESTRUCTIVOS)


def test_el_guard_de_sql_ve_lo_que_se_ejecuta_y_no_la_prosa(tmp_path) -> None:
    """El control del guard de abajo: caza lo que destruye y deja pasar lo que explica.

    # WHY (`feedback_sabotaje_audita_al_test`): al estrechar un guard hay que
    # comprobar las DOS mitades. Si solo se comprobara que dejo de dar falsos
    # positivos, un guard que ya no viera nada tambien pasaria.
    """
    destruye = tmp_path / "destruye.py"
    destruye.write_text(
        "from sqlalchemy import text\n"
        '_X = text("WITH movidos AS ( DELETE FROM trabajos RETURNING id ) SELECT 1")\n',
        encoding="utf-8",
    )
    assert _lleva_sql_destructiva(destruye.read_text(encoding="utf-8"), destruye), (
        "el guard no ve un DELETE dentro de un CTE que si se ejecuta: se estrecho "
        "demasiado y ahora hay destruccion que no mira"
    )

    explica = tmp_path / "explica.py"
    explica.write_text(
        '"""La aplicacion no hace DDL y no hace TRUNCATE."""\n'
        "def f():\n"
        '    raise ValueError("nada de TRUNCATE: no dispara politicas de fila")\n',
        encoding="utf-8",
    )
    assert not _lleva_sql_destructiva(explica.read_text(encoding="utf-8"), explica), (
        "el guard senala un modulo que solo EXPLICA la regla: vuelve a castigar a "
        "quien documenta lo que el guard hace cumplir (P-18)"
    )


def test_toda_sql_destructiva_vive_donde_se_exige_confirmacion() -> None:
    """La segunda puerta: SQL que destruye en un modulo que no conoce la compuerta.

    # WHY: el guard de rutas mira la superficie HTTP. Este mira el SQL. Entre los
    # dos cubren los dos caminos por los que se llega a destruir — y la allowlist
    # obliga a que toda excepcion venga con su motivo ESCRITO, no con un
    # comentario suelto (L-20).
    """
    culpables: list[str] = []
    for ruta in _fuentes_de_la_aplicacion():
        relativa = ruta.relative_to(RAIZ).as_posix()
        if relativa in SQL_DESTRUCTIVA_PERMITIDA:
            continue
        fuente = ruta.read_text(encoding="utf-8")
        if _lleva_sql_destructiva(fuente, ruta) and "app.tenancy.confirmacion" not in fuente:
            culpables.append(relativa)
    assert not culpables, (
        f"estos modulos llevan SQL que destruye y no pasan por la confirmacion de "
        f"RNF-06: {culpables}. O importan `app.tenancy.confirmacion`, o entran en "
        "SQL_DESTRUCTIVA_PERMITIDA con su motivo escrito"
    )


def test_la_allowlist_de_sql_destructiva_no_tiene_entradas_muertas() -> None:
    """Una excepcion a un archivo que ya no existe es una excepcion que enmascara.

    # WHY: la lista tiene que apuntar a algo real; si el archivo se renombra, la
    # excepcion se queda cubriendo un fantasma y el archivo nuevo entra sin que
    # nadie lo mire.
    """
    for relativa, motivo in SQL_DESTRUCTIVA_PERMITIDA.items():
        assert (RAIZ / relativa).is_file(), (
            f"la allowlist exime {relativa!r} y ese archivo no existe"
        )
        assert len(motivo) > 40, f"la exencion de {relativa!r} no trae un motivo escrito"


def test_el_guard_de_sql_destructiva_encuentra_de_verdad_ese_sql() -> None:
    """Meta-control del guard de arriba: si no viera nada, pasaria siempre.

    Se comprueba sobre los DOS archivos que hoy llevan SQL destructiva: la
    migracion del cimiento (exenta, con motivo) y `main.py` (la ruta con su
    compuerta). Si el detector dejara de reconocerlas, el guard quedaria ciego y
    saldria verde.
    """
    for relativa in (
        "apps/api/migrations/versions/0001_cimiento_del_inquilino.py",
        "apps/api/app/main.py",
    ):
        ruta = RAIZ / relativa
        assert _lleva_sql_destructiva(ruta.read_text(encoding="utf-8"), ruta), (
            f"el detector no reconoce como destructiva la SQL de {relativa}: el guard "
            "estaria mirando sin ver"
        )


def test_la_ruta_destructiva_no_lee_el_alcance_de_la_peticion() -> None:
    """RF-01/RF-03 sobre la superficie nueva: la compuerta no acepta un alcance.

    # WHY: el guard por AST de `test_escalada_alcance.py` impide construir un
    # `Inquilino` a mano. Este cierra la otra mitad en la capa HTTP: que no exista
    # un parametro de peticion donde escribir el alcance o la agencia.
    """
    parametros = set(inspect.signature(confirmacion_de_operacion_destructiva).parameters)
    assert parametros == {"request", "cliente_id", "confirmacion"}, (
        f"la compuerta acepta {sorted(parametros)}. En cuanto ahi aparezca un alcance "
        "o una agencia, una peticion podra pedirlos (plan §3.1 punto 5)"
    )


def test_el_identificador_del_cliente_llega_tipado_y_no_como_texto_libre() -> None:
    """`cliente_id` es un UUID: una peticion no puede colar un fragmento de SQL."""
    anotacion = (
        inspect.signature(confirmacion_de_operacion_destructiva)
        .parameters["cliente_id"]
        .annotation
    )
    assert anotacion in (UUID, "UUID"), (
        f"`cliente_id` esta anotado como {anotacion!r} y tiene que ser UUID: es el "
        "unico dato de la peticion que llega hasta una sentencia"
    )
