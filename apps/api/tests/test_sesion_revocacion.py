"""T-015 (RF-08) — «revocar» se mide por EFECTO: el SIGUIENTE USO falla.

La vara de RF-08 no es «borre la clave». Es que la sesion revocada deje de servir
la proxima vez que alguien la presente. Un almacen que borra la clave y una capa
que se guarda la sesion en memoria cumplirian la primera frase y no la segunda —
y es la segunda la que impide que un testigo robado siga entrando.

Cada sonda de aqui viene con su CONTROL: una defensa que rechaza a todo el mundo
no es una defensa, es un producto roto. Si la sesion NO revocada tambien fallara,
el verde de la revocacion no diria nada (`feedback_prueba_sin_control`).

# WHY (que pondria cada prueba en rojo, dicho de una vez):
#   - `usar` con cache en memoria        -> lo caza el segundo uso previo a la
#                                           revocacion, que dejaria la cache caliente;
#   - revocar sin borrar de verdad       -> falla la sonda de revocacion;
#   - RBAC leido del rol y no de la fila -> la sonda de escalada ve 2/2 en vez de 1/1;
#   - fail-open ante Redis caido         -> la sonda del almacen muerto devuelve
#                                           una sesion en vez de levantar.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from app.tenancy.auth import (
    AlmacenDeSesiones,
    PermisoDenegado,
    Rol,
    Sesion,
    SesionInvalida,
    cliente_es_exigido_por,
)
from app.tenancy.inquilino import Alcance
from app.tenancy.sesion import crear_motor, sesion_de_inquilino
from conftest import AGENCIA_A, CLIENTE_A1


def _almacen(banco_redis, cliente=None) -> AlmacenDeSesiones:
    return AlmacenDeSesiones(cliente or banco_redis.cliente, prefijo=banco_redis.prefijo)


# --------------------------------------------------------------------------
# La vara: revocada -> el SIGUIENTE uso falla. Con su control.
# --------------------------------------------------------------------------
async def test_la_sesion_revocada_falla_en_su_siguiente_uso(banco_redis) -> None:
    almacen = _almacen(banco_redis)
    testigo_revocado, _ = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    testigo_control, _ = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )

    # Se usa DOS veces antes de revocar: si hubiera cache, aqui quedaria poblada,
    # y el uso posterior a la revocacion la encontraria caliente.
    assert (await almacen.usar(testigo_revocado)).rol is Rol.OPERADOR_AGENCIA
    assert (await almacen.usar(testigo_revocado)).rol is Rol.OPERADOR_AGENCIA

    assert await almacen.revocar_testigo(testigo_revocado) is True

    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo_revocado)

    # CONTROL: la que no se revoco sigue viva. Sin esto, un almacen que rechazara
    # todo pasaria la sonda de arriba con nota.
    viva = await almacen.usar(testigo_control)
    assert viva.rol is Rol.USUARIO_CLIENTE
    assert viva.cliente_id == CLIENTE_A1


async def test_la_revocacion_en_un_proceso_se_ve_en_otro(banco_redis) -> None:
    """D-05: en memoria del proceso, «revocada» seria «revocada aqui».

    Dos clientes de Redis independientes representan dos procesos de la API. El
    que revoca no es el que valida. Si la sesion viviera en memoria, el segundo
    seguiria sirviendola.
    """
    proceso_a = _almacen(banco_redis, banco_redis.otro_cliente())
    proceso_b = _almacen(banco_redis, banco_redis.otro_cliente())

    testigo, sesion = await proceso_a.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    assert (await proceso_b.usar(testigo)).sesion_id == sesion.sesion_id

    await proceso_a.revocar(sesion.sesion_id)

    with pytest.raises(SesionInvalida):
        await proceso_b.usar(testigo)


async def test_se_puede_revocar_sin_tener_el_testigo(banco_redis) -> None:
    """Revocacion ADMINISTRATIVA: el operador que la corta no conoce el secreto.

    Si la clave de Redis fuera la huella del testigo entero, revocar exigiria el
    testigo — y RF-08 quedaria cumplido solo para quien ya lo tiene, es decir,
    para el ladron.
    """
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    assert await almacen.usar(testigo)

    assert await almacen.revocar(sesion.sesion_id) is True
    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)

    # Revocar dos veces no es un error; simplemente ya no habia nada.
    assert await almacen.revocar(sesion.sesion_id) is False


async def test_la_sesion_caduca_sola(banco_redis) -> None:
    """El techo absoluto existe: pasada la vida declarada, el testigo no sirve.

    RF-08 pide invalidar «sin esperar a que caduque»; la caducidad sigue siendo la
    red de abajo, y una red que no esta armada no es una red.
    """
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA, ttl_segundos=1
    )
    assert await banco_redis.cliente.pttl(almacen.clave(sesion.sesion_id)) > 0
    assert await almacen.usar(testigo)

    await asyncio.sleep(1.3)
    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)


# --------------------------------------------------------------------------
# RBAC: el alcance sigue saliendo de la FILA, y el rol tiene que coincidir
# --------------------------------------------------------------------------
async def test_un_usuario_de_cliente_no_obtiene_alcance_de_agencia(banco_redis, motor) -> None:
    almacen = _almacen(banco_redis)
    testigo, _ = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    sesion = await almacen.usar(testigo)

    assert sesion.inquilino.alcance is Alcance.CLIENTE
    with pytest.raises(PermisoDenegado):
        sesion.exige(Rol.OPERADOR_AGENCIA)

    # Y POR EFECTO contra la base: su alcance real se nota en las filas.
    async with sesion_de_inquilino(motor, sesion.inquilino) as conexion:
        heraldos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
        clientes = (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()
    assert (heraldos, clientes) == (1, 1), (
        f"el usuario de portal vio {heraldos} heraldos y {clientes} clientes; con su "
        "alcance tiene que ver 1 y 1. Mas seria escalada por la via de la sesion"
    )


async def test_control_el_operador_de_agencia_si_obtiene_alcance_de_agencia(
    banco_redis, motor
) -> None:
    """El control del anterior: si nadie pudiera operar, el panel no existiria."""
    almacen = _almacen(banco_redis)
    testigo, _ = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    sesion = await almacen.usar(testigo)

    assert sesion.inquilino.alcance is Alcance.AGENCIA
    sesion.exige(Rol.OPERADOR_AGENCIA)  # no levanta

    async with sesion_de_inquilino(motor, sesion.inquilino) as conexion:
        heraldos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
        clientes = (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()
    assert (heraldos, clientes) == (2, 2)


def test_cada_rol_declara_si_exige_cliente() -> None:
    """Un rol nuevo sin clasificar pone el CI en rojo, no cae en una rama permisiva.

    # WHY: `cliente_es_exigido_por` termina en `assert_never`. Este recorrido del
    # enum entero es lo que hace que ese `assert_never` se EJECUTE en el CI, en vez
    # de esperar a que una peticion lo encuentre en produccion.
    """
    for rol in Rol:
        assert isinstance(cliente_es_exigido_por(rol), bool)
    assert cliente_es_exigido_por(Rol.USUARIO_CLIENTE) is True
    assert cliente_es_exigido_por(Rol.OPERADOR_AGENCIA) is False


def test_una_sesion_cuyo_rol_contradice_su_fila_no_se_puede_construir() -> None:
    """Contradiccion = falla cerrada (`feedback_protocolo_proveedor_seam`)."""
    with pytest.raises(SesionInvalida):
        Sesion(
            sesion_id="x",
            agencia_id=AGENCIA_A,
            cliente_id=CLIENTE_A1,
            rol=Rol.OPERADOR_AGENCIA,
        )
    with pytest.raises(SesionInvalida):
        Sesion(sesion_id="x", agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.USUARIO_CLIENTE)


async def test_un_registro_manipulado_hacia_operador_no_pasa(banco_redis) -> None:
    """Alguien con escritura en Redis reescribe el rol: la sesion se invalida.

    Es la escalada por el unico camino que queda una vez cerrada la peticion
    (T-014·ter) y el codigo (guard por AST): manipular el estado guardado. El
    alcance saldria bien igual —lo da `cliente_id`—, pero los PERMISOS habrian
    escalado. Por eso la incoherencia se rechaza en vez de repararse.
    """
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    clave = almacen.clave(sesion.sesion_id)
    registro = json.loads(await banco_redis.cliente.get(clave))
    registro["rol"] = Rol.OPERADOR_AGENCIA.value  # la fila sigue trayendo cliente
    await banco_redis.cliente.set(clave, json.dumps(registro))

    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)


async def test_un_rol_desconocido_no_se_degrada_al_mas_debil(banco_redis) -> None:
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    clave = almacen.clave(sesion.sesion_id)
    registro = json.loads(await banco_redis.cliente.get(clave))
    registro["rol"] = "superadministrador_del_futuro"
    await banco_redis.cliente.set(clave, json.dumps(registro))

    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)


async def test_un_registro_ilegible_no_es_un_registro_valido(banco_redis) -> None:
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    await banco_redis.cliente.set(almacen.clave(sesion.sesion_id), "esto no es json")
    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)


# --------------------------------------------------------------------------
# El testigo: ni se adivina, ni se puede reconstruir desde Redis
# --------------------------------------------------------------------------
async def test_el_secreto_no_se_guarda_en_claro(banco_redis) -> None:
    """Un volcado de Redis no entrega testigos utilizables, solo huellas."""
    almacen = _almacen(banco_redis)
    testigo, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    _, _, secreto = testigo.partition(".")
    guardado = (await banco_redis.cliente.get(almacen.clave(sesion.sesion_id))).decode()
    assert secreto not in guardado, (
        "el secreto del testigo aparece tal cual en Redis: quien lea el almacen se "
        "lleva sesiones utilizables, no huellas"
    )
    assert "huella" in guardado


async def test_el_identificador_correcto_con_secreto_falso_no_entra(banco_redis) -> None:
    almacen = _almacen(banco_redis)
    _, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    with pytest.raises(SesionInvalida):
        await almacen.usar(f"{sesion.sesion_id}.no-es-el-secreto")


@pytest.mark.parametrize(
    "testigo", ["", "sin-separador", ".solo-secreto", "solo-id.", "  ", "otra.cosa"]
)
async def test_un_testigo_mal_formado_no_entra(banco_redis, testigo: str) -> None:
    almacen = _almacen(banco_redis)
    with pytest.raises(SesionInvalida):
        await almacen.usar(testigo)


# --------------------------------------------------------------------------
# Fail-closed: si el almacen no responde, la peticion muere
# --------------------------------------------------------------------------
async def test_si_redis_no_responde_no_se_devuelve_ninguna_sesion() -> None:
    """Tragarse el error de Redis convertiria una caida en un bypass de auth.

    # WHY: esta prueba se pondria en rojo si alguien anadiera un
    # `except RedisError: return sesion_por_defecto` — que es la forma de
    # `feedback_fail_open_traga_al_guard` que mas caro sale.
    """
    muerto = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1, socket_timeout=1)
    almacen = AlmacenDeSesiones(muerto, prefijo="prueba:redis_muerto")
    try:
        # `RedisError` y no `SesionInvalida`: la peticion muere por el almacen
        # caido, y muere RUIDOSAMENTE. Un `SesionInvalida` aqui seria correcto
        # tambien, pero `Exception` a secas no discrimina nada.
        with pytest.raises(RedisError):
            await almacen.usar("cualquiera.cosa")
    finally:
        await muerto.aclose()


# --------------------------------------------------------------------------
# Control del andamiaje: el banco de pruebas de verdad aisla
# --------------------------------------------------------------------------
async def test_el_banco_de_pruebas_usa_su_propio_prefijo(banco_redis) -> None:
    """Si el prefijo no acotara, dos pruebas se pisarian y el orden decidiria.

    Es el control del andamiaje: sin el, todo lo de arriba podria estar midiendo
    las claves de otra prueba (`feedback_no_paralelizar_compartido`).
    """
    almacen = _almacen(banco_redis)
    _, sesion = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=None, rol=Rol.OPERADOR_AGENCIA
    )
    assert almacen.clave(sesion.sesion_id).startswith(f"{banco_redis.prefijo}:")
    assert await banco_redis.cliente.exists(almacen.clave(sesion.sesion_id)) == 1


async def test_el_prefijo_es_propio_de_esta_prueba_y_de_esta_corrida(
    banco_redis, token_de_corrida
) -> None:
    """El prefijo tiene que DISTINGUIR, no solo existir.

    # WHY (P-16, medido): la version anterior de este control solo comprobaba que
    # la clave colgara del prefijo — y eso sigue siendo cierto con un prefijo FIJO
    # para todas las pruebas. El sabotaje «pon el mismo prefijo a todo» salia
    # VERDE en una corrida secuencial, porque el andamiaje limpia antes y despues
    # de cada prueba; solo se caia con DOS corridas simultaneas, que es
    # exactamente el escenario que hay hoy (varios agentes contra el mismo Redis)
    # y que una suite secuencial no ejerce. El verde no decia QUE media
    # (`feedback_verde_no_dice_que_midio`). Ahora el prefijo tiene que nombrar la
    # CORRIDA y la PRUEBA, y el sabotaje se cae sin necesidad de concurrencia.
    """
    assert banco_redis.prefijo.startswith(f"prueba:{token_de_corrida}:"), (
        f"el prefijo {banco_redis.prefijo!r} no nombra esta corrida: dos corridas "
        "simultaneas contra el mismo Redis se pisarian"
    )
    assert "test_el_prefijo_es_propio_de_esta_prueba" in banco_redis.prefijo, (
        f"el prefijo {banco_redis.prefijo!r} no nombra esta prueba: dos pruebas de la "
        "misma corrida compartirian claves y el ORDEN decidiria el resultado"
    )


async def test_la_sesion_alimenta_el_mecanismo_de_aislamiento(banco_redis, motor) -> None:
    """La capa de sesion ALIMENTA el aislamiento; no lo puentea.

    El inquilino que sale de la sesion se usa con `sesion_de_inquilino` tal cual,
    sin que nadie le pase un alcance por parametro. Si algun dia alguien lo
    pasara, el guard por AST de `test_escalada_alcance.py` lo caza.
    """
    almacen = _almacen(banco_redis)
    testigo, _ = await almacen.abrir(
        agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, rol=Rol.USUARIO_CLIENTE
    )
    sesion = await almacen.usar(testigo)
    motor_propio = crear_motor(motor.url.render_as_string(hide_password=False), tamano_pool=1)
    try:
        async with sesion_de_inquilino(motor_propio, sesion.inquilino) as conexion:
            filas = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
    finally:
        await motor_propio.dispose()
    assert filas == 1

# --------------------------------------------------------------------------
# La frontera de este carril: construir T-015 NO abre el cerrojo de la ruta
# destructiva. Cablearlo es una decision, no un efecto secundario.
# --------------------------------------------------------------------------
def test_construir_la_sesion_no_cablea_la_identidad_de_la_superficie() -> None:
    """`auth.py` existe y la ruta destructiva SIGUE respondiendo 503.

    # WHY: la capa HTTP dejo una costura que falla cerrado —`identidad_no_cableada`
    # levanta 503— apuntando a esta misma tarea. Que T-015 exista no es motivo
    # para que esa costura se abra: abrirla es cablear el proveedor en
    # `crear_aplicacion`, y eso es una decision con su propia evidencia, no un
    # efecto secundario de que el modulo aparezca en el arbol.
    #
    # Sin esta prueba nada lo sujeta: la suite de la superficie no fija cual es el
    # proveedor por defecto, asi que un cableado futuro abriria una ruta que
    # DESTRUYE datos de un cliente sin que ningun rojo lo anunciara. Si algun dia
    # se cablea de verdad, esta prueba se pone en rojo — y ese rojo es el momento
    # de comprobar que el otro cerrojo (la bitacora, T-017) sigue puesto.
    """
    import inspect

    from app.main import crear_aplicacion, identidad_no_cableada

    por_defecto = inspect.signature(crear_aplicacion).parameters["proveedor_de_inquilino"].default
    assert por_defecto is identidad_no_cableada, (
        "el proveedor de identidad por defecto ya no es la costura que falla "
        f"cerrado, sino {por_defecto!r}. La ruta que borra un cliente entero pudo "
        "haberse abierto: comprueba a mano que sigue habiendo autenticacion real y "
        "que la bitacora de T-017 esta puesta antes de cambiar esta prueba"
    )
