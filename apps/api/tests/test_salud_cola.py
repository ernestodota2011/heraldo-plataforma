"""T-025 (RF-52, R-03) — la alarma suena por ANTIGUEDAD, no por «el proceso vive».

La pregunta de control de este archivo es una sola: **¿que resultado pondria en
rojo a la alarma que de verdad importa?** Respuesta: UN trabajo, uno solo, viejo.
Por eso la sonda principal usa una cola de profundidad 1 —por debajo de cualquier
umbral de profundidad— y exige que la alarma suene igual.

Si manana alguien sustituyera la senal por la profundidad, o por un latido del
worker, esa sonda se pondria roja. Ese es el punto entero de T-025.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.tenancy import sesion_de_inquilino
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    CLIENTE_A2,
    RAIZ,
    resembrar,
    sesion_de_cliente,
)
from worker import cola, health

AHORA = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
MODULO_DE_SALUD = RAIZ / "apps" / "worker" / "health.py"


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    resembrar(motor_de_siembra)


@pytest.fixture
def inquilino():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


async def _vaciar(motor, quien) -> None:
    async with sesion_de_inquilino(motor, quien) as conexion:
        await conexion.execute(text("DELETE FROM trabajos"))


# --------------------------------------------------------------------------
# La alarma de R-03
# --------------------------------------------------------------------------
async def test_la_alarma_suena_por_antiguedad_aunque_la_cola_sea_minuscula(
    motor, inquilino
) -> None:
    """==El worker esta PARADO. La cola tiene UN trabajo. La alarma suena.==

    # WHY: es la reproduccion exacta de R-03. Nadie mata a nadie en esta sonda: hay
    # un trabajo disponible desde hace una hora y NADIE lo ha reclamado, que es lo
    # unico que se ve desde fuera cuando un worker muere. La profundidad (1) esta
    # muy por debajo del umbral de profundidad, asi que si la alarma dependiera de
    # ella no sonaria — y esta sonda seria roja.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion, inquilino, tipo="esperando", disponible_en=AHORA - timedelta(hours=1)
        )
        salud = await health.medir(conexion, ahora=AHORA)

    assert salud.profundidad == 1
    assert salud.profundidad < health.PROFUNDIDAD_TOLERADA, (
        "la sonda perdio su gracia: con esta profundidad la alarma de COLA_PROFUNDA "
        "podria estar sonando y no distinguiriamos cual de las dos salto"
    )
    assert salud.antiguedad_del_mas_viejo == timedelta(hours=1)

    encontradas = health.alarmas(salud)
    assert health.Alarma.COLA_ESTANCADA in encontradas, (
        f"con un trabajo esperando desde hace una hora la alarma no sono: {encontradas}. "
        "La senal de R-03 es la ANTIGUEDAD del trabajo mas viejo; si aqui hiciera falta "
        "profundidad o un latido, un worker muerto pasaria desapercibido"
    )
    assert health.Alarma.COLA_PROFUNDA not in encontradas


async def test_control_una_cola_recien_encolada_no_alarma(motor, inquilino) -> None:
    """Sin este control, una alarma que sonara SIEMPRE pasaria la sonda de arriba."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(conexion, inquilino, tipo="recien", disponible_en=AHORA)
        salud = await health.medir(conexion, ahora=AHORA)

    assert salud.profundidad == 1
    assert salud.antiguedad_del_mas_viejo == timedelta(0)
    assert health.alarmas(salud) == [], (
        "una cola con un trabajo recien puesto ya esta alarmando: la alarma seria "
        "ruido y nadie la miraria el dia que importe"
    )


async def test_una_cola_llena_de_trabajo_futuro_no_esta_estancada(motor, inquilino) -> None:
    """Lo que espera su turno no esta atascado: la antiguedad solo cuenta lo DISPONIBLE.

    # WHY: sin esta distincion, la espera creciente de RF-14 dispararia la alarma
    # ella sola — un trabajo reprogramado para dentro de media hora se veria como un
    # trabajo abandonado, y la alarma que existe para avisar de R-03 avisaria de que
    # los reintentos funcionan.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion, inquilino, tipo="mas-tarde", disponible_en=AHORA + timedelta(hours=2)
        )
        salud = await health.medir(conexion, ahora=AHORA)

    assert salud.profundidad == 1
    assert salud.antiguedad_del_mas_viejo is None
    assert health.Alarma.COLA_ESTANCADA not in health.alarmas(salud)


async def test_la_alarma_de_abandono_suena_cuando_se_llevan_un_trabajo_y_no_vuelven(
    motor, inquilino
) -> None:
    """El otro modo de R-03: el worker muere CON el trabajo en la mano."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion, inquilino, tipo="abandonado", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)

    tarde = AHORA + cola.PLAZO_DE_ABANDONO + timedelta(minutes=1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        salud = await health.medir(conexion, ahora=tarde)
    assert salud.en_curso == 1 and salud.abandonados == 1
    assert health.Alarma.TRABAJOS_ABANDONADOS in health.alarmas(salud)

    # Control: recien reclamado, no hay abandono.
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        pronto = await health.medir(conexion, ahora=AHORA)
    assert pronto.en_curso == 1 and pronto.abandonados == 0
    assert health.Alarma.TRABAJOS_ABANDONADOS not in health.alarmas(pronto)


async def test_la_tasa_de_fallo_se_mide_sobre_los_terminados_de_la_ventana(
    motor, inquilino
) -> None:
    """RF-52 pide tasa de fallo. Se comprueba el numero y el umbral, con su control."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        # Uno que sale bien.
        bueno = await cola.encolar(
            conexion, inquilino, tipo="bueno", disponible_en=AHORA - timedelta(hours=1)
        )
        await cola.reclamar(conexion, ahora=AHORA)
        await cola.completar(conexion, bueno, ahora=AHORA)
        # Y uno que se agota al primer intento.
        await cola.encolar(
            conexion,
            inquilino,
            tipo="malo",
            maximo_intentos=1,
            disponible_en=AHORA - timedelta(hours=1),
        )
        malo = await cola.reclamar(conexion, ahora=AHORA)
        assert malo is not None
        await cola.fallar(conexion, malo, error="no", ahora=AHORA)

        salud = await health.medir(conexion, ahora=AHORA)

    assert salud.hechos_en_la_ventana == 1
    assert salud.fallidos_en_la_ventana == 1
    assert salud.tasa_de_fallo == 0.5
    assert health.Alarma.TASA_DE_FALLO_ALTA in health.alarmas(salud)
    # Control: con el umbral por encima de la tasa, no suena.
    assert health.Alarma.TASA_DE_FALLO_ALTA not in health.alarmas(salud, tasa_tolerada=0.9)


def test_una_ventana_sin_trabajos_terminados_no_inventa_una_tasa() -> None:
    """Cero terminados no es cero por ciento de exito: es que no hay dato."""
    vacia = health.Salud(
        profundidad=0,
        antiguedad_del_mas_viejo=None,
        en_curso=0,
        abandonados=0,
        hechos_en_la_ventana=0,
        fallidos_en_la_ventana=0,
        fallidos_visibles=0,
    )
    assert vacia.tasa_de_fallo == 0.0
    assert health.alarmas(vacia) == []


async def test_la_salud_de_un_inquilino_no_cuenta_los_trabajos_de_otro(motor, inquilino) -> None:
    """La consulta de salud no lleva NI UNA clausula de inquilino: lo pone la politica.

    # WHY: un informe de salud que sumara la cola de todos los inquilinos seria una
    # fuga de VOLUMEN —cuanto trabajo tiene cada cliente— por la puerta de la
    # observabilidad, ademas de una medida inutil por inquilino.
    """
    await _vaciar(motor, inquilino)
    vecino = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)
    async with sesion_de_inquilino(motor, vecino) as conexion:
        await conexion.execute(text("DELETE FROM trabajos"))
        for _ in range(3):
            await cola.encolar(
                conexion, vecino, tipo="del-vecino", disponible_en=AHORA - timedelta(hours=1)
            )

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        mia = await health.medir(conexion, ahora=AHORA)
    assert mia.profundidad == 0, (
        f"la salud de mi cola cuenta {mia.profundidad} trabajos y no tengo ninguno: "
        "estoy viendo la cola de otro inquilino"
    )
    # Control: el vecino SI ve los suyos.
    async with sesion_de_inquilino(motor, vecino) as conexion:
        suya = await health.medir(conexion, ahora=AHORA)
    assert suya.profundidad == 3


async def test_el_informe_lleva_los_tres_numeros_y_ningun_dato_del_trabajo(
    motor, inquilino
) -> None:
    """RF-52 pide exponer profundidad, antiguedad y tasa. Y nada mas que eso."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.encolar(
            conexion,
            inquilino,
            tipo="con-carga",
            carga={"telefono": "+13055550123", "texto": "hola"},
            disponible_en=AHORA - timedelta(hours=1),
        )
        salud = await health.medir(conexion, ahora=AHORA)

    salida = health.informe(salud, health.alarmas(salud))
    assert salida["profundidad"] == 1
    assert salida["antiguedad_del_mas_viejo_s"] == 3600.0
    assert salida["tasa_de_fallo"] == 0.0
    assert "COLA_ESTANCADA" in salida["alarmas"]

    texto = repr(salida)
    assert "+13055550123" not in texto and "con-carga" not in texto, (
        "el informe de salud arrastra datos del trabajo: la observabilidad seria una "
        "via de salida de datos de cliente"
    )


# --------------------------------------------------------------------------
# El guard estructural: aqui no hay ningun concepto de «proceso vivo»
# --------------------------------------------------------------------------
#: Lo que este modulo NO puede saber. Si alguna de estas palabras aparece como
#: identificador, la senal dejo de ser la antiguedad y volvio a ser un latido.
_PALABRAS_DE_LATIDO = ("latido", "heartbeat", "pid", "proceso", "ping", "keepalive")


def test_el_modulo_de_salud_no_sabe_lo_que_es_un_proceso_vivo() -> None:
    """R-03 convertido en un guard: la alarma no puede depender de un latido.

    # WHY (`feedback_lo_que_certifica_no_es_lo_que_mide`): las sondas de arriba
    # miden el comportamiento de HOY. Esto vigila la forma: el dia que alguien
    # anada un latido «para complementar», la alarma podra salir verde con la cola
    # hundida — porque el latido llegara igual. Se mira el ARBOL, no el texto, para
    # que la palabra dentro de un comentario o de un docstring —donde justamente
    # hay que explicar por que NO se usa— no lo ponga en rojo.
    """
    arbol = ast.parse(MODULO_DE_SALUD.read_text(encoding="utf-8"))
    identificadores: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name):
            identificadores.append(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            identificadores.append(nodo.attr)
        elif isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            identificadores.append(nodo.name)
        elif isinstance(nodo, ast.arg):
            identificadores.append(nodo.arg)

    assert identificadores, "el guard no leyo ningun identificador: saldria verde por vacio"
    culpables = sorted(
        {
            nombre
            for nombre in identificadores
            for palabra in _PALABRAS_DE_LATIDO
            if palabra in nombre.lower()
        }
    )
    assert not culpables, (
        f"el modulo de salud usa {culpables}: la alarma de R-03 tiene que depender de "
        "la ANTIGUEDAD del trabajo mas viejo y de nada mas. Un worker vivo y pillado "
        "late perfectamente mientras la cola se hunde"
    )


def test_el_guard_del_latido_cazaria_uno_metido_a_mano(tmp_path) -> None:
    """Control del guard: se le da una version con latido y se comprueba que lo ve.

    # WHY (`feedback_sabotaje_audita_al_test`): un guard estructural que nunca se
    # ha probado contra el defecto que dice cazar puede estar mirando el arbol
    # equivocado. Aqui se compila a proposito una alarma que consulta un latido.
    """
    saboteado = tmp_path / "saboteado.py"
    saboteado.write_text(
        "def alarmas(salud, latido_reciente):\n"
        "    return [] if latido_reciente else ['cola estancada']\n",
        encoding="utf-8",
    )
    arbol = ast.parse(saboteado.read_text(encoding="utf-8"))
    nombres = [n.arg for n in ast.walk(arbol) if isinstance(n, ast.arg)]
    assert any(
        palabra in nombre.lower() for nombre in nombres for palabra in _PALABRAS_DE_LATIDO
    ), "el recorrido del guard no ve un latido ni cuando se lo ponemos delante"
