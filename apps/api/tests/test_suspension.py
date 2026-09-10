"""T-021-quinquies (RF-66) — la suspension: dejar de responder, y volver.

Cuatro afirmaciones, y ninguna se hereda de un documento:

1. **El estado vigente se DERIVA** de la ultima fila sin `levantada_en`. No hay
   una segunda copia en `clientes` que pueda quedarse vieja y mentir.
2. **La transicion existe donde el producto decide**: un cliente suspendido no
   encola respuesta. El guard vive en la RUTA (`channels/idempotency.puerta`),
   no en esta suite.
3. **Los dos verbos dejan asiento** en la bitacora del inquilino, en la MISMA
   transaccion: una suspension sin rastro es un corte en silencio.
4. **El enum de dominios ya no llama «suspendido» a un inquilino retirado**
   (14a C-14-02): suspendido es un estado ATENDIDO.

# WHY (por que la sonda de la transicion mide `puerta` y no una funcion suelta):
# `feedback_guard_solo_en_el_test`. Un `exigir_cliente_activo` que solo se llame
# desde aqui autoriza en produccion. La sonda corre el mismo camino que correra
# el webhook, y el sabotaje —neutralizar el guard— demuestra que la sonda cae
# cuando el mecanismo se va.
#
# # WHY (lo que esta casilla NO mide, dicho en voz alta): el ENVIO. Hoy no existe
# `entregar()` (T-119) ni ninguna ruta de webhook: `puerta` no tiene ningun
# llamador de produccion — MEDIDO. Lo que existe hoy es la costura por la que un
# mensaje entrante abre la transaccion que encola su respuesta, y ahi esta el
# guard. Los efectos sobre el portal (T-200) y sobre el export (T-212) se miden
# donde nacen.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit.bitacora import actor_opaco, leer_apuntes
from app.channels import idempotency
from app.channels.idempotency import puerta
from app.tenancy import sesion_de_inquilino
from app.tenancy.auth import Rol
from app.tenancy.dominio_desconocido import ESTADOS_NO_ATENDIDOS, EstadoDeDominio
from app.tenancy.suspension import (
    ACCION_LEVANTAMIENTO,
    ACCION_SUSPENSION,
    ActorNoOpaco,
    AlcanceSinCliente,
    ClienteSuspendido,
    MotivoVacio,
    NoHaySuspensionVigente,
    esta_suspendido,
    exigir_cliente_activo,
    levantar_suspension,
    suspender_cliente,
    suspension_vigente,
)
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    CLIENTE_A2,
    RAIZ,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)

CANAL = "whatsapp"
EXTERNO = "mensaje-de-la-sonda-de-suspension"

#: Un actor con la FORMA que exige RF-10: rol + identificador opaco.
ACTOR = actor_opaco(Rol.OPERADOR_AGENCIA, "sesion-de-la-sonda")

#: Modulo donde tiene que vivir el guard para que no sea un guard de suite.
MODULO_DEL_PUNTO_DE_RESPUESTA = Path("apps/api/app/channels/idempotency.py")

#: Donde vive el enum que la 14a C-14-02 renombra.
MODULO_DEL_ENUM_DE_DOMINIO = Path("apps/api/app/tenancy/dominio_desconocido.py")


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Suspender ESCRIBE: cada sonda arranca del mismo escenario sembrado."""
    resembrar(motor_de_siembra)


@pytest.fixture
def inquilino_a1():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


@pytest.fixture
def inquilino_a2():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A2)


# --------------------------------------------------------------------------
# CONTROL de todo lo demas — el escenario sembrado arranca ACTIVO
# --------------------------------------------------------------------------
async def test_control_un_cliente_sembrado_no_esta_suspendido(motor, inquilino_a1) -> None:
    """Si todo el mundo saliera suspendido, cada sonda de abajo pasaria vacia."""
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        assert await esta_suspendido(conexion, inquilino_a1) is False
        await exigir_cliente_activo(conexion, inquilino_a1)


# --------------------------------------------------------------------------
# Vara 1 — suspender, y que el estado se DERIVE
# --------------------------------------------------------------------------
async def test_suspender_deja_al_cliente_suspendido(motor, inquilino_a1) -> None:
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        suspension = await suspender_cliente(
            conexion, inquilino_a1, motivo="impago", actor=ACTOR
        )
        assert suspension.vigente

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        assert await esta_suspendido(conexion, inquilino_a1) is True
        with pytest.raises(ClienteSuspendido):
            await exigir_cliente_activo(conexion, inquilino_a1)


async def test_el_estado_vigente_se_deriva_y_no_vive_en_dos_sitios(
    catalogo_de_tablas, motor, inquilino_a1
) -> None:
    """Ninguna columna de `clientes` guarda «suspendido»: se lee de la ultima fila.

    # WHY: dos sitios que dicen lo mismo divergen — y el que se queda viejo es el
    # que nadie mira. Aqui el unico sitio es la tabla de suspensiones, y la
    # consulta de estado es la derivacion.
    """
    columnas = catalogo_de_tablas["clientes"]
    sospechosas = sorted(c for c in columnas if "suspend" in c)
    assert not sospechosas, (
        f"`clientes` guarda {sospechosas}: el estado de suspension tendria DOS sitios "
        "y el que se quede viejo mandara sobre el producto sin que nadie lo mire"
    )

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        filas = (
            await conexion.execute(
                text(
                    "SELECT count(*) FROM suspensiones "
                    "WHERE cliente_id = :c AND levantada_en IS NULL"
                ),
                {"c": CLIENTE_A1},
            )
        ).scalar_one()
    assert filas == 1


async def test_la_suspension_alcanza_solo_a_su_cliente(motor, inquilino_a1, inquilino_a2) -> None:
    """El vecino de la misma agencia sigue encendido: la suspension es por cliente."""
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)

    async with sesion_de_inquilino(motor, inquilino_a2) as conexion:
        assert await esta_suspendido(conexion, inquilino_a2) is False


async def test_suspender_dos_veces_no_crea_una_segunda_vigente(motor, inquilino_a1) -> None:
    """La segunda llamada devuelve la MISMA suspension, sin duplicar ni re-apuntar."""
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        primera = await suspender_cliente(
            conexion, inquilino_a1, motivo="impago", actor=ACTOR
        )
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        segunda = await suspender_cliente(
            conexion, inquilino_a1, motivo="otra cosa", actor=ACTOR
        )
    assert segunda.id == primera.id
    assert segunda.motivo == "impago", (
        "la segunda llamada reescribio el motivo de la suspension vigente: el motivo "
        "es el de la causa que la abrio, no el de la ultima vez que alguien lo intento"
    )

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        cuantas = (
            await conexion.execute(
                text(
                    "SELECT count(*) FROM suspensiones "
                    "WHERE cliente_id = :c AND levantada_en IS NULL"
                ),
                {"c": CLIENTE_A1},
            )
        ).scalar_one()
    assert cuantas == 1


async def test_la_base_impide_dos_vigentes_aunque_el_codigo_lo_intente(
    motor, inquilino_a1
) -> None:
    """El mecanismo es el INDICE, no el `if` de arriba (que se puede saltar).

    # WHY: si la unicidad viviera solo en `suspender_cliente`, dos llamadas a la
    # vez —dos operadores, o el panel y el barrido de re-aceptacion— leerian «no
    # hay ninguna» y escribirian dos. Con dos vigentes, «levantar» deja de tener
    # respuesta unica.
    """
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)

    with pytest.raises(IntegrityError):
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            await conexion.execute(
                text(
                    "INSERT INTO suspensiones "
                    "(agencia_id, cliente_id, motivo, suspendida_por) "
                    "VALUES (:a, :c, 'a mano', :actor)"
                ),
                {"a": AGENCIA_A, "c": CLIENTE_A1, "actor": ACTOR},
            )


# --------------------------------------------------------------------------
# Vara 2 — el asiento en la bitacora, y que vaya en la MISMA transaccion
# --------------------------------------------------------------------------
async def test_suspender_deja_apunte_en_la_bitacora_del_cliente(motor, inquilino_a1) -> None:
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
        apuntes = await leer_apuntes(conexion)

    suyos = [a for a in apuntes if a.accion == ACCION_SUSPENSION]
    assert len(suyos) == 1, (
        "una suspension sin asiento es un corte en silencio: RF-66 lo prohibe "
        "explicitamente y RF-10 exige quien, que y cuando"
    )
    assert suyos[0].cliente_id == CLIENTE_A1
    assert suyos[0].actor == ACTOR
    assert suyos[0].detalle["motivo"] == "impago"


async def test_levantar_deja_apunte_en_la_bitacora_del_cliente(motor, inquilino_a1) -> None:
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)
        apuntes = await leer_apuntes(conexion)

    assert [a for a in apuntes if a.accion == ACCION_LEVANTAMIENTO]


async def test_si_el_apunte_falla_la_suspension_se_deshace(
    monkeypatch, motor, inquilino_a1
) -> None:
    """SABOTAJE del asiento: sin apunte no hay suspension. Van juntas o no van.

    # WHY: si el asiento se escribiera en otra transaccion, un fallo entre medias
    # dejaria un cliente apagado sin ninguna constancia de quien lo apago — que es
    # el corte en silencio con otro nombre.
    """
    from app.tenancy import suspension as modulo

    async def apuntar_roto(*_args, **_kwargs):
        raise RuntimeError("la bitacora no acepta el asiento")

    monkeypatch.setattr(modulo, "apuntar", apuntar_roto)

    with pytest.raises(RuntimeError):
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        assert await esta_suspendido(conexion, inquilino_a1) is False, (
            "el apunte fallo y el cliente quedo suspendido igual: el asiento y el "
            "cambio no van en la misma transaccion"
        )


# --------------------------------------------------------------------------
# Vara 3 — levantar, y lo que NO se puede levantar
# --------------------------------------------------------------------------
async def test_levantar_devuelve_al_cliente_a_activo(motor, inquilino_a1) -> None:
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        levantada = await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)
    assert not levantada.vigente
    assert levantada.levantada_por == ACTOR

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        assert await esta_suspendido(conexion, inquilino_a1) is False
        await exigir_cliente_activo(conexion, inquilino_a1)


async def test_levantar_sin_suspension_vigente_falla_ruidosamente(motor, inquilino_a1) -> None:
    """Un `no-op` silencioso aqui deja creer que se levanto algo que nadie apago."""
    with pytest.raises(NoHaySuspensionVigente):
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)


async def test_la_suspension_levantada_no_se_borra(motor, inquilino_a1) -> None:
    """El historial es persistente: levantar CIERRA la fila, no la quita.

    # WHY (se cuenta el DELTA y no un total): el escenario sembrado ya trae una
    # suspension del pasado, ya levantada — la bateria de aislamiento exige una fila
    # por inquilino en cada tabla de su clase. Un `== 1` escrito a mano seria un
    # numero caducado el dia que la siembra cambie (P-31), asi que se mide la
    # diferencia que provoca ESTA sonda.
    """

    async def cerradas() -> int:
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            return (
                await conexion.execute(
                    text(
                        "SELECT count(*) FROM suspensiones "
                        "WHERE cliente_id = :c AND levantada_en IS NOT NULL"
                    ),
                    {"c": CLIENTE_A1},
                )
            ).scalar_one()

    antes = await cerradas()
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        abierta = await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    assert await cerradas() == antes, "suspender cerro una fila: solo deberia abrir una"

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        levantada = await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)

    assert levantada.id == abierta.id
    assert await cerradas() == antes + 1, (
        "levantar no dejo la fila cerrada donde estaba: o la borro, o abrio otra"
    )
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        assert await suspension_vigente(conexion, inquilino_a1) is None


# --------------------------------------------------------------------------
# Vara 4 — fail-closed en la ENTRADA de los dos verbos
# --------------------------------------------------------------------------
@pytest.mark.parametrize("motivo", ["", "   ", None])
async def test_un_motivo_que_no_dice_nada_se_rechaza(motor, inquilino_a1, motivo) -> None:
    """Sin motivo no hay nada que subsanar, y la suspension seria inapelable."""
    with pytest.raises(MotivoVacio):
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            await suspender_cliente(conexion, inquilino_a1, motivo=motivo, actor=ACTOR)


@pytest.mark.parametrize(
    "actor",
    [
        "ernesto@ejemplo.example",
        "Ernesto Hernandez",
        "operador_agencia",
        "",
        "desconocido:0123456789abcdef",
    ],
)
async def test_un_actor_que_no_es_opaco_se_rechaza(motor, inquilino_a1, actor) -> None:
    """RF-10: el «quien» es rol + identificador opaco, nunca nombre ni correo."""
    with pytest.raises(ActorNoOpaco):
        async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
            await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=actor)


async def test_preguntar_por_la_agencia_entera_falla_en_vez_de_decir_que_no(motor) -> None:
    """Fail-closed: con el centinela la consulta saldria «no suspendido» sin medir nada.

    # WHY: `app.cliente_id` lleva el uuid nulo cuando el alcance es agencia, y ese
    # valor no coincide con ninguna fila. Un `esta_suspendido` que lo aceptara
    # devolveria `False` SIEMPRE — cero filas silenciosas leidas como «esta activo».
    """
    agencia = sesion_de_agencia(AGENCIA_A)
    with pytest.raises(AlcanceSinCliente):
        async with sesion_de_inquilino(motor, agencia) as conexion:
            await esta_suspendido(conexion, agencia)


# ==========================================================================
# LA TRANSICION — dejar de responder, y volver a responder al subsanar
# ==========================================================================
async def _encola(motor, redis, inquilino, externo: str) -> bool:
    """Corre el camino real de un mensaje entrante. Devuelve si quedo registrado."""
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=externo) as (
        reserva,
        _conexion,
    ):
        return reserva.es_nuevo


async def test_control_un_cliente_activo_SI_registra_y_encola(
    motor, redis, inquilino_a1
) -> None:
    """Sin este control, un producto que rechaza a TODO el mundo pasaria la sonda."""
    assert await _encola(motor, redis, inquilino_a1, EXTERNO) is True


async def test_un_cliente_suspendido_no_responde_ni_encola(motor, redis, inquilino_a1) -> None:
    """La transicion de RF-66, medida sobre el camino que corre el producto."""
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)

    with pytest.raises(ClienteSuspendido):
        await _encola(motor, redis, inquilino_a1, EXTERNO)

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        registrados = (
            await conexion.execute(
                text(
                    "SELECT count(*) FROM mensajes_entrantes "
                    "WHERE cliente_id = :c AND id_externo = :e"
                ),
                {"c": CLIENTE_A1, "e": EXTERNO},
            )
        ).scalar_one()
    assert registrados == 0, (
        "el mensaje del cliente suspendido quedo reservado: la transaccion que abre "
        "`puerta` es la misma en la que se encola la respuesta, asi que reservarlo "
        "deja media operacion hecha"
    )


async def test_sonda_suspender_subsanar_levantar_vuelve_a_responder(
    motor, redis, inquilino_a1
) -> None:
    """La sonda completa de la casilla, de punta a punta."""
    assert await _encola(motor, redis, inquilino_a1, f"{EXTERNO}-antes") is True

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    with pytest.raises(ClienteSuspendido):
        await _encola(motor, redis, inquilino_a1, f"{EXTERNO}-durante")

    # subsanar = levantar la suspension
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)

    assert await _encola(motor, redis, inquilino_a1, f"{EXTERNO}-despues") is True


async def test_control_el_que_no_subsano_sigue_apagado(
    motor, redis, inquilino_a1, inquilino_a2
) -> None:
    """Levantar la de uno no levanta la del otro: el control de la sonda de arriba."""
    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)
    async with sesion_de_inquilino(motor, inquilino_a2) as conexion:
        await suspender_cliente(conexion, inquilino_a2, motivo="impago", actor=ACTOR)

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await levantar_suspension(conexion, inquilino_a1, actor=ACTOR)

    assert await _encola(motor, redis, inquilino_a1, f"{EXTERNO}-a1") is True
    with pytest.raises(ClienteSuspendido):
        await _encola(motor, redis, inquilino_a2, f"{EXTERNO}-a2")


async def test_sabotaje_sin_el_guard_un_cliente_suspendido_SI_responde(
    monkeypatch, motor, redis, inquilino_a1
) -> None:
    """El sabotaje cableado: se quita el guard del punto de respuesta y se mide.

    # WHY (`feedback_sabotaje_audita_al_test`): sin esto, la sonda de arriba
    # podria estar pasando por cualquier otra razon —un fallo de la fixture, un
    # cliente que ni existe— y nadie lo sabria. Aqui se neutraliza EXACTAMENTE el
    # mecanismo y se comprueba que, sin el, el cliente suspendido vuelve a encolar.
    """

    async def sin_guard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(idempotency, "exigir_cliente_activo", sin_guard)

    async with sesion_de_inquilino(motor, inquilino_a1) as conexion:
        await suspender_cliente(conexion, inquilino_a1, motivo="impago", actor=ACTOR)

    assert await _encola(motor, redis, inquilino_a1, f"{EXTERNO}-saboteado") is True, (
        "con el guard neutralizado el cliente suspendido SIGUE sin encolar: entonces "
        "la sonda de la transicion no estaba midiendo el guard, sino otra cosa"
    )


def test_el_guard_vive_en_la_ruta_y_no_solo_en_esta_suite() -> None:
    """`feedback_guard_solo_en_el_test`: el punto de respuesta lo nombra."""
    fuente = (RAIZ / MODULO_DEL_PUNTO_DE_RESPUESTA).read_text(encoding="utf-8")
    assert "exigir_cliente_activo" in fuente, (
        f"{MODULO_DEL_PUNTO_DE_RESPUESTA} no llama a `exigir_cliente_activo`: el guard "
        "solo existiria en la suite, y en produccion un cliente suspendido responderia"
    )


def test_los_dos_verbos_reciben_la_conexion_para_compartir_transaccion() -> None:
    """La firma es parte del contrato: sin conexion, el asiento va por su cuenta."""
    for verbo in (suspender_cliente, levantar_suspension):
        parametros = list(inspect.signature(verbo).parameters)
        assert parametros[:2] == ["conexion", "inquilino"], (
            f"{verbo.__name__} no recibe la conexion como primer parametro: entonces "
            "abre la suya y el asiento deja de ir en la misma transaccion que el cambio"
        )


# ==========================================================================
# 14a C-14-02 — el tercer estado del enum de dominios pasa a RETIRADO
# ==========================================================================
def test_el_enum_de_dominios_ya_no_llama_suspendido_a_un_retirado() -> None:
    """«Suspendido» es un estado ATENDIDO del cliente, no del dominio (RF-60).

    # WHY: mientras el enum llamara `SUSPENDIDO` al inquilino que no se atiende,
    # el producto tenia dos cosas distintas con el mismo nombre — y la que RF-66
    # crea (suspendido = atendido, en solo lectura, con el export encendido) es la
    # contraria de la que RF-60 esconde (relacion terminada). `feedback_bug_dos_
    # direcciones`: lo distinto parecia lo mismo.
    """
    nombres = {estado.name for estado in EstadoDeDominio}
    assert "SUSPENDIDO" not in nombres, (
        "el enum sigue nombrando SUSPENDIDO: un inquilino suspendido por RF-66 "
        "quedaria entre los no atendidos y dejaria de recibir su portal y su export"
    )
    assert "RETIRADO" in nombres
    assert EstadoDeDominio.RETIRADO in ESTADOS_NO_ATENDIDOS
    assert EstadoDeDominio.VERIFICADO not in ESTADOS_NO_ATENDIDOS


#: El uso que el renombrado deja prohibido. ==Se compone en dos trozos a
#: proposito==: si esta linea llevara el nombre entero, el barrido se encontraria a
#: SI MISMO y se pondria en rojo sin que exista ningun uso vivo. Es P-06 en otro
#: sitio —«un documento nunca contiene el molde de sus propias entradas»—, medido:
#: la primera version de este guard fallo exactamente asi.
NOMBRE_VIEJO_DEL_ESTADO = "EstadoDeDominio." + "SUSPEN" + "DIDO"


def _usos_del_nombre_viejo(lineas_por_archivo: dict[str, list[str]]) -> list[str]:
    """Donde aparece el nombre viejo. Separado para poder ejercitarlo con un control."""
    return [
        f"{archivo}:{numero}"
        for archivo, lineas in lineas_por_archivo.items()
        for numero, linea in enumerate(lineas, 1)
        if NOMBRE_VIEJO_DEL_ESTADO in linea
    ]


def test_control_el_barrido_del_renombrado_si_ve_un_uso_vivo() -> None:
    """Su control: un barrido que no encuentra nada pasa siempre, y aqui no midio nada.

    # WHY: el guard de abajo afirma una AUSENCIA, y una ausencia sale verde tanto
    # cuando el renombrado esta completo como cuando el detector dejo de reconocer
    # lo que dice buscar. Este control le da un uso fabricado y exige que lo vea.
    """
    fabricado = {
        "inventado.py": ["from x import y", f"    if estado is {NOMBRE_VIEJO_DEL_ESTADO}:"]
    }
    assert _usos_del_nombre_viejo(fabricado) == ["inventado.py:2"]
    assert _usos_del_nombre_viejo({"limpio.py": ["EstadoDeDominio.RETIRADO"]}) == []


def test_ningun_modulo_conserva_el_nombre_viejo_del_estado() -> None:
    """El grep del renombrado, cableado: un uso olvidado pone el CI en rojo."""
    fuentes = sorted((RAIZ / "apps").rglob("*.py")) + sorted((RAIZ / "packages").rglob("*.py"))
    lineas_por_archivo = {
        ruta.relative_to(RAIZ).as_posix(): ruta.read_text(encoding="utf-8").splitlines()
        for ruta in fuentes
    }
    assert lineas_por_archivo, "el barrido no encontro ningun archivo que mirar"
    culpables = _usos_del_nombre_viejo(lineas_por_archivo)
    assert not culpables, (
        f"estos sitios siguen usando el nombre viejo del estado: {culpables}. El "
        "renombrado a medias deja dos vocabularios para el mismo enum"
    )


def test_el_enum_declara_que_un_suspendido_sigue_atendido() -> None:
    """La razon del renombrado vive en el modulo, no solo en el tracker."""
    fuente = (RAIZ / MODULO_DEL_ENUM_DE_DOMINIO).read_text(encoding="utf-8")
    assert "RF-66" in fuente, (
        f"{MODULO_DEL_ENUM_DE_DOMINIO} no dice por que un inquilino suspendido NO "
        "esta entre los no atendidos: la proxima persona que lea el enum volvera a "
        "meter la suspension aqui"
    )
