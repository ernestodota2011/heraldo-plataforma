"""T-019 (RF-13) — el limite se comparte entre procesos, y el cubo es el PAR.

RF-13: «CUANDO varios procesos atienden trafico, EL SISTEMA DEBE aplicar los
limites de uso de forma compartida entre todos ellos». D-05 explica por que en
Redis: en memoria del proceso el limite se MULTIPLICA por el numero de workers.

Y la trampa de esta pieza no es esa, es la otra: un limite global que parece
funcionar porque solo se probo UN inquilino. Aqui se miden los dos ejes, cada uno
con su control:

  - el mismo inquilino desde DOS direcciones no comparte cubo (y desde LA MISMA
    si lo comparte, que es el control);
  - dos inquilinos desde LA MISMA direccion no comparten cubo (y el mismo
    inquilino si, que es el control).

Y los dos niveles de la cascada, no solo uno: agencia<->agencia y, dentro de la
misma agencia, cliente<->cliente. Un limitador que separase agencias y mezclase
clientes de una misma agencia pasaria media prueba.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tenancy.limits import (
    Direccion,
    DireccionInvalida,
    LimitadorCompartido,
    Limite,
    LimiteMalDeclarado,
    NombreDeLimiteInvalido,
    _canonico,
    huella_del_cubo,
)
from conftest import (
    AGENCIA_A,
    AGENCIA_B,
    CLIENTE_A1,
    CLIENTE_A2,
    CLIENTE_B1,
    sesion_de_agencia,
    sesion_de_cliente,
)

#: Dos direcciones distintas: el eje que mas se olvida.
DIRECCION_1 = Direccion.desde_texto("203.0.113.10")
DIRECCION_2 = Direccion.desde_texto("198.51.100.20")

#: Un limite comodo de agotar: dos consumos y se cierra.
LIMITE = Limite(nombre="mensajes", cuota=2, ventana_segundos=60)


def _limitador(banco_redis, cliente=None) -> LimitadorCompartido:
    return LimitadorCompartido(cliente or banco_redis.cliente, prefijo=banco_redis.prefijo)


async def _agotar(limitador, inquilino, direccion, limite=LIMITE) -> None:
    """Consume exactamente la cuota. La ultima unidad TODAVIA es permitida."""
    for numero in range(1, limite.cuota + 1):
        veredicto = await limitador.consumir(limite, inquilino, direccion)
        assert veredicto.permitido, (
            f"el consumo {numero} de {limite.cuota} salio rechazado: el cubo venia "
            "sucio de otra prueba o la cuota no se respeta"
        )


# --------------------------------------------------------------------------
# Eje 1 — por DIRECCION
# --------------------------------------------------------------------------
async def test_el_mismo_inquilino_desde_otra_direccion_no_comparte_cubo(banco_redis) -> None:
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    await _agotar(limitador, inquilino, DIRECCION_1)

    # CONTROL del eje: desde LA MISMA direccion, el siguiente ya no pasa.
    mismo = await limitador.consumir(LIMITE, inquilino, DIRECCION_1)
    assert not mismo.permitido, (
        "el cubo de (inquilino, direccion) no se agota: el limite no cuenta, o cuenta "
        "en un sitio que no comparte"
    )

    # LA SONDA: desde otra direccion, el mismo inquilino arranca de cero.
    otra = await limitador.consumir(LIMITE, inquilino, DIRECCION_2)
    assert otra.permitido, (
        "el mismo inquilino desde OTRA direccion salio rechazado: el cubo no separa "
        "por direccion, asi que un solo atacante puede quemar la cuota del cliente entero"
    )
    assert otra.consumido == 1


async def test_dos_notaciones_de_la_misma_direccion_comparten_cubo(banco_redis) -> None:
    """`::1` y `0:0:0:0:0:0:0:1` SON la misma direccion: un cubo, no dos.

    # WHY: si la clave fuera el texto crudo, cambiar de notacion duplicaria la
    # cuota — y es un cambio que se hace escribiendo, no atacando. Que la
    # normalizacion la haga el TIPO es lo que cierra ese camino.
    """
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    corta = Direccion.desde_texto("::1")
    larga = Direccion.desde_texto("0:0:0:0:0:0:0:1")

    assert limitador.clave(LIMITE, inquilino, corta) == limitador.clave(LIMITE, inquilino, larga)

    await _agotar(limitador, inquilino, corta)
    disfrazado = await limitador.consumir(LIMITE, inquilino, larga)
    assert not disfrazado.permitido, (
        "escribiendo la misma direccion de otra forma se consiguio un cubo nuevo: la "
        "cuota se duplica con solo cambiar la notacion"
    )


# --------------------------------------------------------------------------
# Eje 2 — por INQUILINO, en los DOS niveles de la cascada
# --------------------------------------------------------------------------
async def test_dos_inquilinos_desde_la_misma_direccion_no_comparten_cubo(banco_redis) -> None:
    """Detras del mismo NAT hay dos clientes distintos. No se estorban."""
    limitador = _limitador(banco_redis)
    uno = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    otra_agencia = sesion_de_cliente(AGENCIA_B, CLIENTE_B1)

    await _agotar(limitador, uno, DIRECCION_1)

    # CONTROL: el que agoto sigue agotado desde esa misma direccion.
    assert not (await limitador.consumir(LIMITE, uno, DIRECCION_1)).permitido

    # LA SONDA: el inquilino de otra agencia, desde la MISMA direccion, arranca de cero.
    vecino = await limitador.consumir(LIMITE, otra_agencia, DIRECCION_1)
    assert vecino.permitido, (
        "un inquilino de otra agencia quedo bloqueado por el consumo de un tercero que "
        "comparte direccion: el cubo no separa por inquilino"
    )
    assert vecino.consumido == 1


async def test_dos_clientes_de_la_misma_agencia_no_comparten_cubo(banco_redis) -> None:
    """El segundo nivel de la cascada, que es el que se olvida.

    Un limitador que separase agencias y mezclase clientes de una misma agencia
    pasaria la prueba de arriba y dejaria el defecto vivo: es el defecto 3 del
    referente trasladado un piso mas arriba.
    """
    limitador = _limitador(banco_redis)
    primero = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    hermano = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)

    await _agotar(limitador, primero, DIRECCION_1)
    assert not (await limitador.consumir(LIMITE, primero, DIRECCION_1)).permitido

    otro = await limitador.consumir(LIMITE, hermano, DIRECCION_1)
    assert otro.permitido, (
        "dos clientes de la MISMA agencia comparten cubo: uno puede dejar sin cuota al "
        "otro, y notar cuando el otro trabaja"
    )


async def test_el_operador_de_agencia_tampoco_comparte_cubo_con_sus_clientes(
    banco_redis,
) -> None:
    """El alcance `agencia` lleva el centinela en `cliente_id`: es otro par."""
    limitador = _limitador(banco_redis)
    operador = sesion_de_agencia(AGENCIA_A)
    cliente = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    await _agotar(limitador, operador, DIRECCION_1)
    assert not (await limitador.consumir(LIMITE, operador, DIRECCION_1)).permitido
    assert (await limitador.consumir(LIMITE, cliente, DIRECCION_1)).permitido


# --------------------------------------------------------------------------
# RF-13: compartido entre PROCESOS
# --------------------------------------------------------------------------
async def test_dos_procesos_comparten_la_misma_cuenta(banco_redis) -> None:
    """El defecto 6 del referente, medido: en memoria esto daria el doble.

    Dos limitadores sobre conexiones independientes representan dos workers. El
    segundo tiene que encontrar el cubo que gasto el primero. Si el estado
    viviera en el proceso, `proceso_b` empezaria de cero y la cuota efectiva
    seria 2x.
    """
    proceso_a = _limitador(banco_redis, banco_redis.otro_cliente())
    proceso_b = _limitador(banco_redis, banco_redis.otro_cliente())
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    primero = await proceso_a.consumir(LIMITE, inquilino, DIRECCION_1)
    segundo = await proceso_b.consumir(LIMITE, inquilino, DIRECCION_1)
    tercero = await proceso_b.consumir(LIMITE, inquilino, DIRECCION_1)

    assert (primero.consumido, segundo.consumido, tercero.consumido) == (1, 2, 3), (
        "los dos procesos no estan contando en el mismo sitio: cada uno lleva su "
        "propia cuenta y la cuota real se multiplica por el numero de workers"
    )
    assert primero.permitido and segundo.permitido and not tercero.permitido


async def test_el_conteo_concurrente_no_pierde_unidades(banco_redis) -> None:
    """Diez consumos a la vez cuentan diez, no menos.

    Un `GET` + `SET` en dos viajes perderia incrementos bajo concurrencia y el
    limite dejaria pasar de mas justo cuando mas trafico hay. El `INCR` dentro
    del guion de Lua lo hace imposible.
    """
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    limite = Limite(nombre="rafaga", cuota=100, ventana_segundos=60)

    veredictos = await asyncio.gather(
        *(limitador.consumir(limite, inquilino, DIRECCION_1) for _ in range(10))
    )
    assert sorted(v.consumido for v in veredictos) == list(range(1, 11))


# --------------------------------------------------------------------------
# La ventana: se arma, se agota y se reabre
# --------------------------------------------------------------------------
async def test_la_ventana_se_arma_y_se_reabre(banco_redis) -> None:
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    limite = Limite(nombre="corto", cuota=1, ventana_segundos=1)

    primero = await limitador.consumir(limite, inquilino, DIRECCION_1)
    assert primero.permitido and primero.milisegundos_para_reintentar > 0
    assert not (await limitador.consumir(limite, inquilino, DIRECCION_1)).permitido

    await asyncio.sleep(1.3)
    assert (await limitador.consumir(limite, inquilino, DIRECCION_1)).permitido, (
        "la ventana no se reabrio: el cubo no tenia vencimiento y el inquilino queda "
        "bloqueado para siempre"
    )


async def test_la_ventana_fija_admite_el_doble_a_caballo_del_corte(banco_redis) -> None:
    """FIJA el limite declarado: esto NO es una ventana deslizante, y se nota.

    Gastando la cuota al final de una ventana y otra vez al principio de la
    siguiente pasan 2x consumos en una fraccion de ventana. Es la propiedad de
    toda ventana fija; esta prueba existe para que nadie la descubra en
    produccion creyendo que la perilla decia otra cosa. Si algun dia se
    implementa la ventana deslizante ponderada (P-23), esta prueba se pone en
    ROJO — y eso es exactamente lo que tiene que pasar: el cambio de semantica no
    puede ser silencioso.
    """
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    limite = Limite(nombre="borde", cuota=3, ventana_segundos=1)

    for _ in range(limite.cuota):
        assert (await limitador.consumir(limite, inquilino, DIRECCION_1)).permitido
    clave = limitador.clave(limite, inquilino, DIRECCION_1)
    restante_ms = await banco_redis.cliente.pttl(clave)
    await asyncio.sleep(restante_ms / 1000 + 0.05)

    admitidos_tras_el_corte = 0
    for _ in range(limite.cuota):
        if (await limitador.consumir(limite, inquilino, DIRECCION_1)).permitido:
            admitidos_tras_el_corte += 1

    assert admitidos_tras_el_corte == limite.cuota, (
        f"tras el corte se admitieron {admitidos_tras_el_corte} de {limite.cuota}. "
        "Si es MENOS, la ventana dejo de ser fija (¿alguien implemento la "
        "deslizante de P-19?) y hay que actualizar esta prueba y el docstring del "
        "modulo, no borrarla"
    )


async def test_un_cubo_sin_vencimiento_se_cura_solo(banco_redis) -> None:
    """Un `INCR` sin su `PEXPIRE` deja al inquilino bloqueado PARA SIEMPRE.

    Es lo que pasaria si el conteo y el vencimiento fueran dos viajes y el
    proceso muriera entre medias. El guion re-arma el vencimiento en cuanto ve un
    cubo sin el, asi que un huerfano de una version anterior se cura solo.
    """
    limitador = _limitador(banco_redis)
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    clave = limitador.clave(LIMITE, inquilino, DIRECCION_1)

    # Se fabrica el huerfano a mano: contador vivo, sin vencimiento.
    await banco_redis.cliente.set(clave, 1)
    assert await banco_redis.cliente.ttl(clave) == -1

    veredicto = await limitador.consumir(LIMITE, inquilino, DIRECCION_1)
    assert veredicto.milisegundos_para_reintentar > 0
    assert await banco_redis.cliente.pttl(clave) > 0, (
        "el cubo sigue sin vencimiento: no se vaciara nunca y el inquilino queda "
        "bloqueado hasta que alguien lo borre a mano"
    )


# --------------------------------------------------------------------------
# La direccion es una direccion, no una cadena
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto",
    [
        None,
        "",
        "   ",
        "no-soy-una-ip",
        "999.999.999.999",
        "203.0.113.10; DROP",
        "203.0.113.10:aaaaaaaa-0000-4000-8000-000000000001",
        "<script>",
    ],
)
def test_lo_que_no_es_una_direccion_se_rechaza(texto) -> None:
    """Fail-closed: sin direccion valida no hay cubo, y no se inventa uno.

    # WHY: un limitador que cayera en un cubo «texto raro» pondria a todos los
    # remitentes anonimos a compartir cuota, y ninguno seria identificable. Y el
    # penultimo caso es el que importa de verdad: una cadena que INTENTA parecer
    # dos campos separados por `:`. Aqui ni siquiera llega a ser una direccion.
    """
    with pytest.raises(DireccionInvalida):
        Direccion.desde_texto(texto)


def test_una_direccion_valida_si_se_acepta() -> None:
    """El control del anterior: si se rechazara todo, no habria limitador."""
    assert Direccion.desde_texto("203.0.113.10").canonica == "203.0.113.10"
    assert Direccion.desde_texto(" 2001:db8::1 ").canonica == "2001:db8::1"


# --------------------------------------------------------------------------
# La clave: derivada, inequivoca y sin datos en claro
# --------------------------------------------------------------------------
def test_la_serializacion_canonica_es_inequivoca() -> None:
    """`("a","b")` y `("a|b",)` no pueden producir la misma cadena.

    Es la propiedad que hace que ninguna parte de la clave pueda invadir a la
    siguiente. Sin las longitudes delante, con un separador basta.
    """
    assert _canonico("a", "b") != _canonico("a|b")
    assert _canonico("aa", "b") != _canonico("a", "ab")


def test_la_clave_no_lleva_la_direccion_ni_el_inquilino_en_claro() -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    huella = huella_del_cubo(inquilino, DIRECCION_1)
    assert DIRECCION_1.canonica not in huella
    assert str(CLIENTE_A1) not in huella
    assert str(AGENCIA_A) not in huella


def test_cada_par_tiene_su_propia_huella() -> None:
    """Cinco pares distintos, cinco huellas distintas. Sin colisiones por diseno."""
    pares = {
        huella_del_cubo(sesion_de_cliente(AGENCIA_A, CLIENTE_A1), DIRECCION_1),
        huella_del_cubo(sesion_de_cliente(AGENCIA_A, CLIENTE_A1), DIRECCION_2),
        huella_del_cubo(sesion_de_cliente(AGENCIA_A, CLIENTE_A2), DIRECCION_1),
        huella_del_cubo(sesion_de_cliente(AGENCIA_B, CLIENTE_B1), DIRECCION_1),
        huella_del_cubo(sesion_de_agencia(AGENCIA_A), DIRECCION_1),
    }
    assert len(pares) == 5


# --------------------------------------------------------------------------
# El limite se declara bien, o no se declara
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cuota", [0, -1])
def test_una_cuota_que_no_limita_se_rechaza(cuota: int) -> None:
    with pytest.raises(LimiteMalDeclarado):
        Limite(nombre="x", cuota=cuota, ventana_segundos=60)


def test_una_ventana_que_no_es_ventana_se_rechaza() -> None:
    with pytest.raises(LimiteMalDeclarado):
        Limite(nombre="x", cuota=1, ventana_segundos=0)


@pytest.mark.parametrize(
    "nombre", ["", "Mensajes", "men sajes", "men:sajes", "1mensajes", "a" * 41]
)
def test_un_nombre_de_limite_inseguro_en_una_clave_se_rechaza(nombre: str) -> None:
    """ALLOWLIST por forma: un nombre con `:` partiria la clave en otro sitio."""
    with pytest.raises(NombreDeLimiteInvalido):
        Limite(nombre=nombre, cuota=1, ventana_segundos=60)


def test_un_nombre_valido_si_se_acepta() -> None:
    assert Limite(nombre="mensajes_por_minuto", cuota=1, ventana_segundos=60).nombre


# --------------------------------------------------------------------------
# Fail-closed: si Redis no responde, no se pasa sin contar
# --------------------------------------------------------------------------
async def test_si_redis_no_responde_el_consumo_no_se_permite() -> None:
    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    muerto = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1, socket_timeout=1)
    limitador = LimitadorCompartido(muerto, prefijo="prueba:redis_muerto")
    try:
        # RedisError y no `Exception`: el fallo tiene que ser POR el almacen
        # caido. Si fallara por otra razon estariamos midiendo otra cosa.
        with pytest.raises(RedisError):
            await limitador.consumir(
                LIMITE, sesion_de_cliente(AGENCIA_A, CLIENTE_A1), DIRECCION_1
            )
    finally:
        await muerto.aclose()


# --------------------------------------------------------------------------
# Control del andamiaje
# --------------------------------------------------------------------------
async def test_el_banco_de_pruebas_usa_su_propio_prefijo(banco_redis) -> None:
    limitador = _limitador(banco_redis)
    clave = limitador.clave(LIMITE, sesion_de_cliente(AGENCIA_A, CLIENTE_A1), DIRECCION_1)
    assert clave.startswith(f"{banco_redis.prefijo}:limite:mensajes:")
