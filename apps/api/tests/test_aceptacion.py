"""T-021-quinquies (RF-66) — sin aceptacion no hay alta, y una version nueva se re-acepta.

Cinco afirmaciones, y ninguna se hereda de un documento:

1. **Sin aceptacion no hay alta**, y sin alta no hay fila: el rechazo se mide por
   el efecto sobre `clientes`, no por la excepcion.
2. **La version aceptada tiene que existir en el catalogo**: una inventada es
   rojo, y tampoco deja cliente detras.
3. **`solo_desarrollo` se DERIVA** de la version aceptada, no de una columna.
4. **Una version nueva pide re-aceptacion**: aviso + apunte, y suspension solo
   pasado el plazo — nunca un corte en silencio.
5. **El catalogo de plataforma es INMUTABLE para la aplicacion**: es su unica
   defensa, porque no lo gobierna RLS.

# WHY (aqui no se escribe ni una letra de contrato): el texto de la v2 lo publica
# T-030-quater. Las versiones que estas sondas usan son las de DESARROLLO que
# siembra `conftest` —nombre, huella y dos banderas, sin texto— y las que publica
# la propia sonda con `publicar_version`. Inventar aqui un contrato de ejemplo
# habria dejado en el repositorio un texto que nadie reviso.
#
# # WHY (el control de casi todo esto es el alta que SI pasa): una comprobacion que
# rechace el alta siempre cumple «sin aceptacion no hay alta» a la perfeccion y deja
# el producto inservible. Por eso cada rechazo tiene su alta legitima al lado.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.audit.bitacora import actor_opaco, leer_apuntes
from app.tenancy import sesion_de_inquilino
from app.tenancy.aceptacion import (
    ACCION_ACEPTACION,
    ACCION_REACEPTACION_PENDIENTE,
    DOCUMENTOS_EXIGIDOS,
    Aceptacion,
    AceptacionAusente,
    AceptacionNoAutorizada,
    Documento,
    VersionInexistente,
    VersionNoVigente,
    aceptaciones_vigentes,
    catalogo,
    esta_en_solo_desarrollo,
    publicar_version,
    registrar_aceptacion,
    revisar_reaceptaciones,
    version_vigente,
)
from app.tenancy.auth import Rol, Sesion
from app.tenancy.baa_guard import Sector, alta_de_cliente
from app.tenancy.suspension import MOTIVO_REACEPTACION_PENDIENTE, esta_suspendido
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    RAIZ,
    VERSION_ANEXO_DESARROLLO,
    VERSION_CONTRATO_DESARROLLO,
    aceptacion_de_desarrollo,
    alta_de_prueba,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)

OPERADOR = Sesion(
    sesion_id="operador-de-la-sonda",
    agencia_id=AGENCIA_A,
    cliente_id=None,
    rol=Rol.OPERADOR_AGENCIA,
)

ACTOR = actor_opaco(Rol.OPERADOR_AGENCIA, "sesion-de-la-sonda-de-aceptacion")

#: El unico modulo que puede escribir estas dos tablas. Fuera de el, un `INSERT`
#: seria un segundo camino sin catalogo comprobado ni asiento.
MODULO_DE_ACEPTACION = Path("apps/api/app/tenancy/aceptacion.py")

#: El arbol de la aplicacion que el guard estructural recorre.
ARBOLES_DE_APLICACION = ("apps/api/app", "apps/worker", "packages")
CARPETAS_QUE_NO_SON_APLICACION = ("tests", "__pycache__", ".venv", "node_modules")


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Estas sondas dan de alta y suspenden: cada una arranca del mismo escenario."""
    resembrar(motor_de_siembra)


@pytest.fixture
def agencia():
    return sesion_de_agencia(AGENCIA_A)


async def _cuantos_clientes(motor, agencia) -> int:
    async with sesion_de_inquilino(motor, agencia) as conexion:
        return (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()


async def _publicar(motor, agencia, documento: Documento, version: str, *, desarrollo: bool):
    async with sesion_de_inquilino(motor, agencia) as conexion:
        return await publicar_version(
            conexion,
            documento=documento,
            version=version,
            declara_instruccion_de_derechos=not desarrollo,
            es_desarrollo=desarrollo,
            hash_del_texto=f"huella-de-{documento.value}-{version}",
        )


# ==========================================================================
# Vara 1 — sin aceptacion NO hay alta, medido sobre la tabla
# ==========================================================================
async def test_control_un_alta_con_la_aceptacion_de_desarrollo_pasa(motor, agencia) -> None:
    """El control de toda esta bateria: el camino legitimo funciona.

    Sin el, una comprobacion que rechazara TODAS las altas cumpliria RF-66 a la
    perfeccion y dejaria el producto sin forma de dar de alta a nadie.
    """
    antes = await _cuantos_clientes(motor, agencia)
    nuevo = await alta_de_prueba(
        motor, sesion=OPERADOR, nombre="Panaderia La Espiga", sector=Sector.COMERCIO
    )
    assert await _cuantos_clientes(motor, agencia) == antes + 1

    inquilino = sesion_de_cliente(AGENCIA_A, nuevo)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        vigentes = await aceptaciones_vigentes(conexion, inquilino)
    assert set(vigentes) == DOCUMENTOS_EXIGIDOS, (
        f"el alta quedo con {sorted(d.value for d in vigentes)} aceptado y RF-66 exige "
        "el contrato Y su anexo de tratamiento"
    )


def test_el_alta_no_admite_un_valor_por_defecto_para_la_aceptacion() -> None:
    """La FORMA del contrato: un defecto aqui convierte la regla en una intencion.

    # WHY: con `aceptacion=None` por defecto, «sin aceptacion no hay alta» pasaria a
    # ser «sin aceptacion hay alta y ya se registrara luego», y ninguna prueba de
    # comportamiento lo veria — porque todas las llamadas de la suite la pasan igual.
    # Es la propiedad, no el efecto, y se mide sobre la firma.
    """
    parametro = inspect.signature(alta_de_cliente).parameters["aceptacion"]
    assert parametro.default is inspect.Parameter.empty, (
        "`alta_de_cliente` declara un valor por defecto para `aceptacion`: quien se "
        "olvide de pasarla dara de alta un cliente sin contrato aceptado y nadie se "
        "enterara"
    )
    assert parametro.kind is inspect.Parameter.KEYWORD_ONLY, (
        "`aceptacion` es posicional: en una llamada larga acabaria ocupando el sitio "
        "de otro argumento sin que nada lo diga"
    )


@pytest.mark.parametrize(
    ("aceptacion", "porque"),
    [
        (None, "no se paso nada"),
        ("si", "se paso una cadena, no una aceptacion"),
        (Aceptacion(versiones=(), aceptada_por=ACTOR), "no nombra ninguna version"),
        (
            Aceptacion(versiones=(VERSION_CONTRATO_DESARROLLO,), aceptada_por="Ernesto"),
            "el «quien» no es opaco (RF-10)",
        ),
    ],
)
async def test_un_alta_sin_aceptacion_utilizable_no_deja_cliente(
    motor, agencia, aceptacion, porque
) -> None:
    """Fail-closed medido POR EFECTO: la excepcion importa menos que la fila ausente."""
    antes = await _cuantos_clientes(motor, agencia)
    with pytest.raises(AceptacionAusente):
        await alta_de_cliente(
            motor,
            sesion=OPERADOR,
            nombre="Ferreteria Lopez",
            sector=Sector.COMERCIO,
            aceptacion=aceptacion,
        )
    assert await _cuantos_clientes(motor, agencia) == antes, (
        f"el alta se rechazo ({porque}) y aun asi quedo un cliente escrito: el "
        "rechazo no es fail-closed si la fila sobrevive"
    )


async def test_una_version_inventada_es_rojo_y_no_deja_cliente(motor, agencia) -> None:
    """La version aceptada tiene que EXISTIR en el catalogo (11a C-11-04).

    # WHY (esta es la sonda que separa «aceptacion» de «cadena que alguien tecleo»):
    # sin catalogo, dos clientes podrian estar bajo textos distintos con el mismo
    # nombre de version. Aqui el identificador no existe, y el alta entera se cae.
    """
    antes = await _cuantos_clientes(motor, agencia)
    inventada = uuid4()
    with pytest.raises(VersionInexistente):
        await alta_de_cliente(
            motor,
            sesion=OPERADOR,
            nombre="Taller Mecanico Sur",
            sector=Sector.AUTOMOCION,
            aceptacion=Aceptacion(
                versiones=(VERSION_CONTRATO_DESARROLLO, inventada), aceptada_por=ACTOR
            ),
        )
    assert await _cuantos_clientes(motor, agencia) == antes, (
        "la version no existia y el cliente quedo dado de alta igual: el `INSERT` de "
        "la ficha y la comprobacion del catalogo no van en la misma transaccion"
    )


async def test_un_alta_que_solo_acepta_uno_de_los_dos_documentos_es_rojo(
    motor, agencia
) -> None:
    """RF-66 exige el contrato Y su anexo de tratamiento: media aceptacion no vale."""
    antes = await _cuantos_clientes(motor, agencia)
    with pytest.raises(AceptacionAusente):
        await alta_de_cliente(
            motor,
            sesion=OPERADOR,
            nombre="Inmobiliaria Centro",
            sector=Sector.INMOBILIARIA,
            aceptacion=Aceptacion(
                versiones=(VERSION_CONTRATO_DESARROLLO,), aceptada_por=ACTOR
            ),
        )
    assert await _cuantos_clientes(motor, agencia) == antes


async def test_un_alta_que_acepta_una_version_YA_SUPERADA_es_rojo(motor, agencia) -> None:
    """RF-66, ultima linea: nadie opera bajo una version que ya no es la publicada.

    # WHY (lo levanto la revision cruzada): comprobar solo que la version EXISTA
    # dejaba pasar un alta bajo el texto anterior. El barrido lo habria cazado al dia
    # siguiente y el cliente habria nacido ya pendiente — un alta que nace en
    # infraccion no es un alta valida, es una infraccion con fecha.
    """
    antes = await _cuantos_clientes(motor, agencia)
    for documento in sorted(DOCUMENTOS_EXIGIDOS):
        await _publicar(motor, agencia, documento, "3.0", desarrollo=False)

    with pytest.raises(VersionNoVigente):
        await alta_de_cliente(
            motor,
            sesion=OPERADOR,
            nombre="Cafeteria Tardia",
            sector=Sector.HOSTELERIA,
            # Las de desarrollo siguen PUBLICADAS; lo que ya no son es las vigentes.
            aceptacion=aceptacion_de_desarrollo(ACTOR),
        )
    assert await _cuantos_clientes(motor, agencia) == antes


async def test_control_tras_publicar_la_nueva_el_alta_con_ELLA_pasa(motor, agencia) -> None:
    """El control de la sonda de arriba: si rechazara siempre, no mediria nada."""
    antes = await _cuantos_clientes(motor, agencia)
    nuevas = [
        await _publicar(motor, agencia, documento, "3.0", desarrollo=False)
        for documento in sorted(DOCUMENTOS_EXIGIDOS)
    ]
    await alta_de_cliente(
        motor,
        sesion=OPERADOR,
        nombre="Cafeteria Puntual",
        sector=Sector.HOSTELERIA,
        aceptacion=Aceptacion(versiones=tuple(v.id for v in nuevas), aceptada_por=ACTOR),
    )
    assert await _cuantos_clientes(motor, agencia) == antes + 1


async def test_el_alta_deja_su_apunte_de_aceptacion(motor, agencia) -> None:
    """RF-10: quien acepto, cuando y QUE version. Sin asiento no hubo aceptacion."""
    nuevo = await alta_de_prueba(
        motor, sesion=OPERADOR, nombre="Libreria del Puerto", sector=Sector.COMERCIO
    )
    inquilino = sesion_de_cliente(AGENCIA_A, nuevo)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        apuntes = await leer_apuntes(conexion)

    suyos = [a for a in apuntes if a.accion == ACCION_ACEPTACION]
    assert len(suyos) == 1, "el alta no dejo apunte de la aceptacion contractual"
    assert set(suyos[0].detalle["versiones"]) == {d.value for d in DOCUMENTOS_EXIGIDOS}
    assert suyos[0].detalle["solo_desarrollo"] is True


# ==========================================================================
# Vara 2 — `solo_desarrollo` se DERIVA, y no puede tratar datos reales
# ==========================================================================
async def test_una_version_de_desarrollo_deja_al_cliente_en_solo_desarrollo(
    motor, agencia
) -> None:
    nuevo = await alta_de_prueba(
        motor, sesion=OPERADOR, nombre="Bar de Pruebas", sector=Sector.HOSTELERIA
    )
    inquilino = sesion_de_cliente(AGENCIA_A, nuevo)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_en_solo_desarrollo(conexion, inquilino) is True


async def test_control_con_las_dos_versiones_reales_el_cliente_NO_es_de_desarrollo(
    motor, agencia
) -> None:
    """El control de la sonda de arriba: si diera `True` siempre, no mediria nada."""
    reales = []
    for documento in sorted(DOCUMENTOS_EXIGIDOS):
        publicada = await _publicar(motor, agencia, documento, "1.0", desarrollo=False)
        reales.append(publicada.id)

    nuevo = await alta_de_cliente(
        motor,
        sesion=OPERADOR,
        nombre="Gestoria del Norte",
        sector=Sector.LEGAL,
        aceptacion=Aceptacion(versiones=tuple(reales), aceptada_por=ACTOR),
    )
    inquilino = sesion_de_cliente(AGENCIA_A, nuevo)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_en_solo_desarrollo(conexion, inquilino) is False


async def test_un_cliente_sin_aceptacion_completa_cuenta_como_solo_desarrollo(motor) -> None:
    """Fail-closed: de quien no consta bajo que texto opera, no se presume nada.

    El cliente sembrado tiene aceptado el contrato y no el anexo — es el minimo que
    la bateria de aislamiento necesita, no un alta completa. Justo por eso sirve de
    sonda: la derivacion tiene que tratarlo como NO apto para datos reales.
    """
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_en_solo_desarrollo(conexion, inquilino) is True


# ==========================================================================
# Vara 3 — publicar una version, y barrer, son actos de la agencia
# ==========================================================================
async def test_control_la_agencia_publica_y_el_catalogo_lo_devuelve(motor, agencia) -> None:
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)
    async with sesion_de_inquilino(motor, agencia) as conexion:
        vigente = await version_vigente(conexion, Documento.CONTRATO)
        todas = await catalogo(conexion, documento=Documento.CONTRATO)
    assert vigente is not None
    assert vigente.id == publicada.id
    assert publicada.id in {v.id for v in todas}


async def test_un_portal_de_cliente_no_publica_versiones(motor) -> None:
    """La version gobierna a TODOS los clientes: no la escribe uno de ellos."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    with pytest.raises(AceptacionNoAutorizada):
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await publicar_version(
                conexion,
                documento=Documento.CONTRATO,
                version="mia",
                declara_instruccion_de_derechos=True,
                es_desarrollo=False,
                hash_del_texto="huella",
            )


async def test_un_portal_de_cliente_no_corre_el_barrido(motor) -> None:
    """Mismo motivo: barrer la cartera es de la agencia, y su alcance lo dice."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    with pytest.raises(AceptacionNoAutorizada):
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await revisar_reaceptaciones(conexion, datetime.now(UTC), actor=ACTOR)


# ==========================================================================
# Vara 4 — la re-aceptacion: aviso siempre, suspension solo pasado el plazo
# ==========================================================================
async def _al_dia(motor) -> UUID:
    """Un cliente que acepto las DOS versiones vigentes. Es el control del barrido."""
    return await alta_de_prueba(
        motor, sesion=OPERADOR, nombre="Floristeria Al Dia", sector=Sector.COMERCIO
    )


async def _barrer(motor, agencia, hoy: datetime):
    async with sesion_de_inquilino(motor, agencia) as conexion:
        return await revisar_reaceptaciones(conexion, hoy, actor=ACTOR)


async def test_publicar_una_version_deja_pendiente_a_quien_tenia_la_anterior(
    motor, agencia
) -> None:
    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)

    avisos = await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=1))

    pendientes = {aviso.inquilino.cliente_id for aviso in avisos}
    assert al_dia in pendientes, (
        "el cliente que estaba al dia no quedo pendiente tras publicarse una version "
        "nueva: la re-aceptacion no se estaria pidiendo a nadie"
    )
    assert all(not aviso.suspendido for aviso in avisos), (
        "el barrido suspendio dentro del plazo de gracia: eso es cortar sin dar "
        "tiempo a subsanar"
    )
    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is False


async def test_cada_pendiente_deja_su_apunte(motor, agencia) -> None:
    """Un aviso que no queda escrito es un aviso que nadie puede demostrar."""
    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)
    await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=1))

    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        apuntes = await leer_apuntes(conexion)
    assert [a for a in apuntes if a.accion == ACCION_REACEPTACION_PENDIENTE], (
        "el cliente quedo pendiente y no hay ningun apunte que lo diga: RF-66 exige "
        "aviso, y un aviso sin rastro no se distingue de no haberlo mandado"
    )


async def test_control_el_que_esta_al_dia_no_recibe_aviso(motor, agencia) -> None:
    """Si el barrido avisara a todo el mundo, la sonda de arriba no mediria nada."""
    al_dia = await _al_dia(motor)
    avisos = await _barrer(motor, agencia, datetime.now(UTC))
    assert al_dia not in {aviso.inquilino.cliente_id for aviso in avisos}, (
        "el cliente que acepto las dos versiones vigentes recibio aviso de "
        "re-aceptacion: el barrido esta avisando a todo el mundo"
    )
    assert avisos, (
        "el barrido no encontro NINGUN pendiente: el escenario sembrado tiene "
        "clientes con el anexo sin aceptar, asi que un cero aqui significa que el "
        "barrido no esta mirando la cartera"
    )


@pytest.mark.parametrize("dias", [1, 29, 30])
async def test_dentro_del_plazo_se_avisa_y_NO_se_suspende(motor, agencia, dias) -> None:
    """El borde del plazo, medido por comportamiento y no por la constante.

    # WHY: comparar `GRACIA_DE_REACEPTACION` con `timedelta(days=30)` compararia una
    # constante consigo misma y pasaria aunque el barrido no la usara. Aqui lo que se
    # mide es el EFECTO a un lado y a otro del plazo de spec §9 · B12.
    """
    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)
    await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=dias))

    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is False, (
            f"a los {dias} dias de publicarse la version el cliente ya estaba "
            "suspendido: el plazo de gracia de B12 no se esta respetando"
        )


async def test_pasado_el_plazo_la_falta_de_reaceptacion_suspende_con_aviso(
    motor, agencia
) -> None:
    """RF-66: «suspension AVISADA, nunca un corte en silencio»."""
    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)

    avisos = await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=31))

    suyo = [aviso for aviso in avisos if aviso.inquilino.cliente_id == al_dia]
    assert suyo, "el cliente vencido no produjo ningun aviso"
    assert suyo[0].suspendido, (
        "el cliente se paso del plazo y su aviso no dice que se le suspende: seria un "
        "corte del que nadie avisa"
    )
    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is True
        vigente = (
            await conexion.execute(
                text(
                    "SELECT motivo FROM suspensiones "
                    "WHERE cliente_id = :c AND levantada_en IS NULL"
                ),
                {"c": al_dia},
            )
        ).scalar_one()
    assert vigente == MOTIVO_REACEPTACION_PENDIENTE


async def test_control_quien_reacepta_a_tiempo_no_se_suspende(motor, agencia) -> None:
    """El control del corte: re-aceptar dentro del plazo apaga la cuenta atras."""
    al_dia = await _al_dia(motor)
    nuevas = [
        await _publicar(motor, agencia, documento, "2.0", desarrollo=False)
        for documento in sorted(DOCUMENTOS_EXIGIDOS)
    ]

    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, agencia) as conexion:
        await registrar_aceptacion(
            conexion,
            inquilino,
            Aceptacion(versiones=tuple(v.id for v in nuevas), aceptada_por=ACTOR),
        )

    avisos = await _barrer(motor, agencia, nuevas[0].publicada_en + timedelta(days=31))

    assert al_dia not in {aviso.inquilino.cliente_id for aviso in avisos}
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is False, (
            "el cliente re-acepto a tiempo y el barrido lo suspendio igual"
        )


async def test_sin_el_paso_de_suspension_un_vencido_sigue_encendido(
    monkeypatch, motor, agencia
) -> None:
    """Se neutraliza el verbo de suspension y la sonda de arriba deja de medir.

    # WHY (`feedback_sabotaje_audita_al_test`): sin esto, el rojo/verde de
    # `test_pasado_el_plazo...` podria venir de cualquier otra cosa. Aqui se quita
    # EXACTAMENTE el mecanismo y se comprueba que, sin el, el cliente vencido se
    # queda encendido — que es el defecto que RF-66 existe para impedir.
    """
    from app.tenancy import aceptacion as modulo

    async def no_suspende(*_args, **_kwargs):
        return None

    monkeypatch.setattr(modulo, "suspender_cliente", no_suspende)

    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)
    await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=31))

    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is False, (
            "con el verbo de suspension neutralizado el cliente vencido SIGUE "
            "suspendiendose: entonces la sonda del plazo no estaba midiendo esto"
        )


async def test_si_el_apunte_falla_la_suspension_del_barrido_se_deshace(
    monkeypatch, motor, agencia
) -> None:
    """Aviso y corte van juntos o no van: es lo que impide el corte en silencio."""
    from app.tenancy import aceptacion as modulo

    async def apuntar_roto(*_args, **_kwargs):
        raise RuntimeError("la bitacora no acepta el asiento")

    al_dia = await _al_dia(motor)
    publicada = await _publicar(motor, agencia, Documento.CONTRATO, "2.0", desarrollo=False)

    monkeypatch.setattr(modulo, "apuntar", apuntar_roto)
    with pytest.raises(RuntimeError):
        await _barrer(motor, agencia, publicada.publicada_en + timedelta(days=31))

    inquilino = sesion_de_cliente(AGENCIA_A, al_dia)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        assert await esta_suspendido(conexion, inquilino) is False, (
            "el apunte fallo y el cliente quedo suspendido igual: el aviso y el corte "
            "no van en la misma transaccion, asi que hay cortes sin aviso"
        )


# ==========================================================================
# Vara 5 — el catalogo de plataforma es inmutable PARA LA APLICACION
# ==========================================================================
async def test_control_la_aplicacion_lee_el_catalogo(motor) -> None:
    """Su control: si tampoco pudiera leer, la prohibicion de abajo seria trivial."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        publicadas = await catalogo(conexion)
    assert {v.id for v in publicadas} >= {
        VERSION_CONTRATO_DESARROLLO,
        VERSION_ANEXO_DESARROLLO,
    }


@pytest.mark.parametrize(
    "sentencia",
    [
        "UPDATE versiones_publicadas SET version = 'pisada'",
        "DELETE FROM versiones_publicadas",
    ],
)
async def test_la_aplicacion_no_puede_reescribir_el_catalogo(motor, sentencia) -> None:
    """Sin RLS que lo gobierne, el PRIVILEGIO es su unica defensa.

    Una version ya aceptada que cambiara de texto convertiria en firmas sobre otro
    documento todas las aceptaciones que la nombran — y sin dejar rastro, porque no
    es una tabla de solo insercion por costumbre sino por permiso.
    """
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    with pytest.raises(DBAPIError) as fallo:
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await conexion.execute(text(sentencia))
    assert "permission denied" in str(fallo.value).lower(), (
        f"«{sentencia}» no fallo por PRIVILEGIO: {fallo.value}. Si falla por otra "
        "cosa, el dia que esa otra cosa cambie la sentencia pasara"
    )


# ==========================================================================
# Guards estructurales: que no aparezca un segundo camino
# ==========================================================================
def _fuentes_de_la_aplicacion() -> list[Path]:
    encontradas: list[Path] = []
    for arbol in ARBOLES_DE_APLICACION:
        for ruta in sorted((RAIZ / arbol).rglob("*.py")):
            if any(parte in CARPETAS_QUE_NO_SON_APLICACION for parte in ruta.parts):
                continue
            encontradas.append(ruta)
    return encontradas


def test_el_guard_estructural_encuentra_lo_que_dice_auditar() -> None:
    """Su control: un guard que no mira ningun archivo pasa siempre."""
    rutas = {r.relative_to(RAIZ).as_posix() for r in _fuentes_de_la_aplicacion()}
    assert len(rutas) >= 5
    assert MODULO_DE_ACEPTACION.as_posix() in rutas
    fuente = (RAIZ / MODULO_DE_ACEPTACION).read_text(encoding="utf-8")
    for tabla in ("aceptaciones_contractuales", "versiones_publicadas"):
        assert f"INSERT INTO {tabla}" in fuente, (
            f"el patron ya no encuentra el INSERT de {tabla} en el modulo autorizado: "
            "el guard de abajo pasaria vacio"
        )


@pytest.mark.parametrize("tabla", ["aceptaciones_contractuales", "versiones_publicadas"])
def test_solo_el_modulo_de_aceptacion_escribe_sus_tablas(tabla: str) -> None:
    """Un segundo `INSERT` seria un camino sin catalogo comprobado y sin asiento."""
    autorizado = (RAIZ / MODULO_DE_ACEPTACION).resolve()
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if ruta.resolve() != autorizado
        and f"INSERT INTO {tabla}" in ruta.read_text(encoding="utf-8")
    ]
    assert not culpables, (
        f"estos modulos escriben `{tabla}` sin pasar por "
        f"{MODULO_DE_ACEPTACION.as_posix()}: {culpables}. Por ahi entra una "
        "aceptacion sin catalogo comprobado, o una version sin quien la publique"
    )


def test_el_alta_llama_al_registro_de_la_aceptacion() -> None:
    """El paso vive en la RUTA del alta, no en esta suite.

    # WHY (`feedback_guard_solo_en_el_test`): un `registrar_aceptacion` al que solo
    # llamaran las pruebas dejaria el alta de produccion sin comprobar nada. Se mide
    # por AST —quien LLAMA— y no por texto, para que nombrarlo en un comentario no
    # cuente como cablearlo.
    """
    modulo_del_alta = RAIZ / "apps" / "api" / "app" / "tenancy" / "baa_guard.py"
    arbol = ast.parse(
        modulo_del_alta.read_text(encoding="utf-8"), filename=str(modulo_del_alta)
    )
    llamadas = {
        nodo.func.id
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    }
    faltan = {"exigir_aceptacion", "registrar_aceptacion"} - llamadas
    assert not faltan, (
        f"`baa_guard.py` no llama a {sorted(faltan)}: el paso de aceptacion de RF-66 "
        "existiria solo en esta suite y el alta de produccion no comprobaria nada"
    )


def test_el_ayudante_de_pruebas_no_vive_en_el_codigo_de_la_aplicacion() -> None:
    """El defecto vive en la SUITE: `alta_de_cliente` sigue exigiendo la aceptacion.

    # WHY: `alta_de_prueba` existe para no repetir andamiaje en una veintena de
    # llamadas. Si alguien lo importara desde el producto, el valor por defecto de la
    # aceptacion entraria por la puerta de atras.
    """
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if "alta_de_prueba" in ruta.read_text(encoding="utf-8")
    ]
    assert not culpables, (
        f"el ayudante de pruebas aparece en el codigo de la aplicacion: {culpables}. "
        "Ahi seria una forma soportada de dar de alta sin aceptacion"
    )


def test_la_aceptacion_de_desarrollo_cubre_los_documentos_exigidos() -> None:
    """El ayudante tiene que producir un alta VALIDA, o toda la suite mentiria."""
    ayudante = aceptacion_de_desarrollo()
    assert len(ayudante.versiones) == len(DOCUMENTOS_EXIGIDOS)
    assert len(set(ayudante.versiones)) == len(ayudante.versiones)
