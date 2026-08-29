"""T-017 (RF-10) — «solo insercion» es un PERMISO, y aqui se mide por EFECTO.

La afirmacion que hay que probar no es «el codigo no hace UPDATE». Es «el codigo
NO PUEDE hacer UPDATE». La diferencia entre las dos es todo el requisito: la
primera la sostiene la disciplina de quien programa —que es lo que RF-10 existe
para no necesitar— y la segunda la sostiene el motor.

Por eso las sondas de aqui abajo NO llaman a `app.audit`: escriben el `UPDATE` y
el `DELETE` a mano, con el rol real de la aplicacion, y exigen que fallen. Y su
control es el `INSERT`, que tiene que pasar: una tabla sobre la que la aplicacion
no pudiera hacer NADA tambien haria fallar el `UPDATE`, y no seria una bitacora.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.audit import apuntar, leer_apuntes
from app.tenancy import sesion_de_inquilino
from app.tenancy.rol import PRIVILEGIOS_DE_APLICACION, VERBOS
from app.tenancy.secrets import SecretoEnClaro, SecretoEnLaRespuesta
from conftest import (
    AGENCIA_A,
    APUNTE_A1,
    CLIENTE_A1,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    resembrar(motor_de_siembra)


_ACTUALIZAR = text("UPDATE bitacora SET accion = 'reescrita' WHERE id = :id")
_BORRAR = text("DELETE FROM bitacora WHERE id = :id")
_INSERTAR = text(
    "INSERT INTO bitacora (agencia_id, cliente_id, actor, accion, recurso) "
    "VALUES (:a, :c, 'control', 'alta', 'prueba') RETURNING id"
)


async def test_la_bitacora_rechaza_el_update_con_el_rol_de_la_aplicacion(motor) -> None:
    """El `UPDATE` de una fila PROPIA y alcanzable falla por PRIVILEGIO.

    # WHY (la fila es suya a proposito): si se intentara sobre una fila ajena, el
    # fallo podria venir de la politica de RLS y la sonda no distinguiria «no puedes
    # tocar ESA fila» de «no puedes tocar NINGUNA». Aqui la fila es del propio
    # inquilino y perfectamente visible: lo unico que puede pararlo es el permiso.
    """
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(
            motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
        ) as conexion:
            await conexion.execute(_ACTUALIZAR, {"id": APUNTE_A1})
    mensaje = str(capturado.value).lower()
    assert "permission denied" in mensaje, (
        "el UPDATE sobre la bitacora fallo, pero NO por falta de privilegio: "
        f"{mensaje}. Si falla por otra razon, el dia que esa razon cambie la "
        "bitacora sera reescribible y nadie se enterara"
    )


async def test_la_bitacora_rechaza_el_delete_con_el_rol_de_la_aplicacion(motor) -> None:
    """Y borrar tampoco: esconder el apunte vale lo mismo que corregirlo."""
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(
            motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
        ) as conexion:
            await conexion.execute(_BORRAR, {"id": APUNTE_A1})
    assert "permission denied" in str(capturado.value).lower()


async def test_ni_el_operador_de_la_agencia_puede_reescribir_la_bitacora(motor) -> None:
    """El alcance mas ancho tampoco: el limite es del ROL, no del alcance.

    # WHY: sin esta sonda, una implementacion que solo cerrara el portal de cliente
    # pasaria las dos de arriba. El operador de la agencia ve TODAS las filas de su
    # agencia; si el limite viviera en la politica y no en el permiso, el podria.
    """
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
            await conexion.execute(_ACTUALIZAR, {"id": APUNTE_A1})
    assert "permission denied" in str(capturado.value).lower()


async def test_control_la_bitacora_si_acepta_insertar_y_leer(motor, motor_admin) -> None:
    """==El control.== Sin el, una tabla inalcanzable pasaria las tres sondas."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        nuevo = (await conexion.execute(_INSERTAR, {"a": AGENCIA_A, "c": CLIENTE_A1})).scalar_one()
    assert nuevo is not None

    with motor_admin.connect() as conexion:
        existe = conexion.execute(
            text("SELECT count(*) FROM bitacora WHERE id = :id"), {"id": nuevo}
        ).scalar_one()
    assert existe == 1, "el INSERT dijo que si y la fila no esta: se confirmo la nada"

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        apuntes = await leer_apuntes(conexion)
    assert any(a.id == nuevo for a in apuntes), "la aplicacion no puede leer lo que escribio"


async def test_el_apunte_se_escribe_con_quien_que_y_cuando(motor) -> None:
    """RF-10 pide las tres cosas. Se comprueba que las tres llegan a la fila."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await apuntar(
            conexion,
            inquilino,
            actor="operador@agencia",
            accion="publicar",
            recurso="heraldo:principal",
            detalle={"version": 3},
        )
        apuntes = await leer_apuntes(conexion)

    apunte = next(a for a in apuntes if a.id == identificador)
    assert apunte.actor == "operador@agencia"
    assert apunte.accion == "publicar"
    assert apunte.recurso == "heraldo:principal"
    assert apunte.detalle == {"version": 3}
    assert apunte.ocurrido_en is not None


async def test_un_apunte_no_puede_llevar_un_secreto_dentro(motor) -> None:
    """RF-09 por la puerta de RF-10: el detalle pasa por el barrido ANTES de escribirse.

    # WHY: la bitacora es la tabla mas peligrosa donde escribir un secreto, porque
    # es la unica que despues NADIE puede corregir. Si `apuntar` no barriera, un
    # «guardo el cuerpo de la peticion por si acaso» dejaria la credencial escrita
    # para siempre — y el arreglo tendria que hacerlo el rol migrador a mano.
    """
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(SecretoEnLaRespuesta):
            await apuntar(
                conexion,
                inquilino,
                actor="operador@agencia",
                accion="configurar",
                recurso="canal",
                detalle={"credencial": SecretoEnClaro("tk_vivo")},
            )


# --------------------------------------------------------------------------
# La otra mitad: que lo declarado y lo EFECTIVO sean lo mismo
# --------------------------------------------------------------------------
def test_los_privilegios_efectivos_son_exactamente_los_declarados(
    catalogo_de_tablas, motor_admin
) -> None:
    """El mapa de `rol.py` frente a lo que la base concede DE VERDAD.

    # WHY (`feedback_guard_solo_en_el_test`): que una migracion contenga el `GRANT`
    # correcto no dice que el estado de la base sea ese — una revision posterior
    # puede haberlo ampliado, o un `GRANT` a mano en un despliegue. Esto lee
    # `has_table_privilege` para CADA tabla del catalogo y CADA verbo, y lo compara
    # con la declaracion. Es la unica lectura que no depende de creerse la historia.
    #
    # # WHY (recorre el catalogo entero, no el mapa): asi una tabla que existe y NO
    # esta declarada tambien se mide — y tiene que salir con cero privilegios.
    """
    with motor_admin.connect() as conexion:
        efectivos = {
            tabla: {
                verbo
                for verbo in VERBOS
                if conexion.execute(
                    text("SELECT has_table_privilege('heraldo_app', :tabla, :verbo)"),
                    {"tabla": tabla, "verbo": verbo},
                ).scalar_one()
            }
            for tabla in sorted(catalogo_de_tablas)
        }

    esperados = {
        tabla: set(PRIVILEGIOS_DE_APLICACION.get(tabla, ()))
        for tabla in sorted(catalogo_de_tablas)
    }
    assert efectivos == esperados, (
        "los privilegios EFECTIVOS del rol de aplicacion no son los declarados en "
        f"PRIVILEGIOS_DE_APLICACION.\n  efectivos: {efectivos}\n  declarados: {esperados}\n"
        "Un verbo de mas es una puerta que nadie decidio abrir; uno de menos es una "
        "funcion del producto que va a fallar en produccion, no aqui"
    )


def test_la_bitacora_se_declara_sin_update_ni_delete() -> None:
    """La declaracion, leida en voz alta: si esto cambia, cambia RF-10.

    # WHY: la sonda de arriba compara declarado contra efectivo, asi que los dos
    # podrian moverse a la vez y seguir coincidiendo. Esta fija el valor concreto
    # que el requisito exige, y obliga a que ampliarlo sea un cambio VISIBLE en un
    # diff con esta prueba al lado.
    """
    assert set(PRIVILEGIOS_DE_APLICACION["bitacora"]) == {"SELECT", "INSERT"}, (
        "la bitacora dejo de ser de solo insercion. RF-10 pide una bitacora que la "
        "propia aplicacion no pueda reescribir: con UPDATE o DELETE concedidos, ya no lo es"
    )
