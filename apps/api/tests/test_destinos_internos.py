"""T-118 (RF-46-bis, CE-07, I-4A-06) — la lista de destinos declarados, medida por efecto.

El contrato de entrega (plan S4.1) fija que un aviso INTERNO comprueba, como
PRIMER paso, si el destino esta en la lista que el propio inquilino declaro.
Estas sondas miden exactamente eso: un destino nunca declarado se rechaza, uno
declarado y activo pasa, uno retirado vuelve a rechazarse (y deja su rastro en
la bitacora, RF-10), el vecino ni ve ni usa el destino de otro cliente, un
webhook con direccion interna se rechaza AL DECLARARLO (guard de FORMA, distinto
del guard de RED de T-300 — CE-07), y el guard, saboteado, deja de proteger.

# WHY (`.invalid` en todo destino de correo/webhook de estas pruebas): RFC 2606
# reserva ese TLD para que nunca resuelva a nada real; es la misma convencion
# que ya usan `test_egreso_red.py` y `test_proveedores.py`. Un dato de prueba
# inventado a mano tiende a describir lo que el autor tiene delante todos los
# dias — la leccion medida en P-41 — asi que aqui no se inventa nada: ni un
# dominio con pinta de host propio, ni una direccion privada escrita a mano
# (la de la sonda de red se DERIVA del prefijo RFC 1918, igual que alli).
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.audit import leer_apuntes
from app.channels import destinos_internos
from app.channels.destinos_internos import (
    Canal,
    CanalNoAdmitido,
    DestinoDeAviso,
    DestinoNoDeclarado,
    declarar_destino,
    exigir_destino_declarado,
    listar_destinos,
    retirar_destino,
)
from app.tenancy import sesion_de_inquilino
from conftest import AGENCIA_A, CLIENTE_A1, CLIENTE_A2, resembrar, sesion_de_cliente
from egress.red import DestinoRechazado


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Este modulo ESCRIBE destinos. Cada sonda arranca del mismo escenario sembrado."""
    resembrar(motor_de_siembra)


def _primera_direccion_privada() -> str:
    """La primera direccion utilizable de 10.0.0.0/8. DERIVADA, nunca escrita (P-41)."""
    return str(ipaddress.ip_network("10.0.0.0/8")[1])


#: Una direccion PUBLICA de verdad, la misma que usa `test_egreso_red.py` como
#: control: es lo que debe seguir pasando cuando el destino SI tiene forma valida.
_PUBLICA = "93.184.216.34"


# ==========================================================================
# 1 — el guard rechaza lo no declarado, con su control
# ==========================================================================
async def test_exigir_destino_declarado_rechaza_un_destino_no_declarado(motor) -> None:
    """I-4A-06: sin declaracion, el guard dice que NO."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(DestinoNoDeclarado) as capturado:
            await exigir_destino_declarado(
                conexion, inquilino, Canal.CORREO, "nadie-declaro-esto@negocio-a1.invalid"
            )

    # RF-46-bis / la regla de la casa de nunca ecoar un dato ajeno: el mensaje
    # nombra el canal (publico, uno de tres) pero NUNCA el destino que se pidio.
    mensaje = str(capturado.value)
    assert "nadie-declaro-esto@negocio-a1.invalid" not in mensaje
    assert "correo" in mensaje


async def test_control_un_destino_declarado_y_activo_pasa(motor) -> None:
    """Sin este control, un guard que rechazara TODO pasaria la sonda de arriba."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        declarado = await declarar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="alertas@negocio-a1.invalid",
            etiqueta="Gerente de turno",
            declarado_por="admin@negocio-a1.invalid",
        )
        encontrado = await exigir_destino_declarado(
            conexion, inquilino, Canal.CORREO, "alertas@negocio-a1.invalid"
        )

    assert encontrado.id == declarado.id
    assert encontrado.activo is True
    assert encontrado.etiqueta == "Gerente de turno"
    assert encontrado.declarado_por == "admin@negocio-a1.invalid"


# ==========================================================================
# 2 — retirar marca inactivo, no borra, y lo apunta la bitacora
# ==========================================================================
async def test_un_destino_retirado_es_rechazado_y_queda_en_la_bitacora(motor) -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        declarado = await declarar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="retirame@negocio-a1.invalid",
            etiqueta="Soporte",
            declarado_por="admin@negocio-a1.invalid",
        )
        await retirar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="retirame@negocio-a1.invalid",
            retirado_por="admin@negocio-a1.invalid",
        )

        with pytest.raises(DestinoNoDeclarado):
            await exigir_destino_declarado(
                conexion, inquilino, Canal.CORREO, "retirame@negocio-a1.invalid"
            )

        apuntes = await leer_apuntes(conexion)

    # RF-10: retirar NO borra la fila (no hay `DELETE` concedido, ver rol.py) —
    # lo que deja constancia de QUIEN y CUANDO es la bitacora.
    coincidencias = [
        a
        for a in apuntes
        if a.accion == "retirar_destino_de_aviso" and a.detalle.get("id") == str(declarado.id)
    ]
    assert coincidencias, "retirar_destino no dejo su rastro en la bitacora (RF-10)"
    assert coincidencias[0].actor == "admin@negocio-a1.invalid"


async def test_retirar_un_destino_nunca_declarado_es_rechazado(motor) -> None:
    """El propio `retirar_destino` es fail-closed: no hay nada que marcar inactivo."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(DestinoNoDeclarado):
            await retirar_destino(
                conexion,
                inquilino,
                canal=Canal.MENSAJERIA,
                destino="jamas-existio",
                retirado_por="admin@negocio-a1.invalid",
            )


async def test_declarar_de_nuevo_un_destino_retirado_lo_reactiva(motor) -> None:
    """`declarar_destino` reactiva (UPSERT), no choca con la fila retirada."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        primero = await declarar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="vaivien@negocio-a1.invalid",
            etiqueta="Version 1",
            declarado_por="admin@negocio-a1.invalid",
        )
        await retirar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="vaivien@negocio-a1.invalid",
            retirado_por="admin@negocio-a1.invalid",
        )
        segundo = await declarar_destino(
            conexion,
            inquilino,
            canal=Canal.CORREO,
            destino="vaivien@negocio-a1.invalid",
            etiqueta="Version 2",
            declarado_por="otro-admin@negocio-a1.invalid",
        )
        vigente = await exigir_destino_declarado(
            conexion, inquilino, Canal.CORREO, "vaivien@negocio-a1.invalid"
        )

    assert segundo.id == primero.id, "es la MISMA fila reactivada, no una segunda"
    assert vigente.activo is True
    assert vigente.etiqueta == "Version 2"
    assert vigente.declarado_por == "otro-admin@negocio-a1.invalid"


# ==========================================================================
# 3 — el vecino no ve ni usa los destinos de otro cliente
# ==========================================================================
async def test_el_vecino_no_ve_ni_usa_los_destinos_de_otro_cliente(motor) -> None:
    """Dos clientes de la MISMA agencia declaran, cada uno, el MISMO texto de destino.

    Si el aislamiento fallara por el lado que RF-46-bis existe para cerrar, la
    sesion de A1 podria ver o usar el destino que en realidad declaro A2 (o al
    reves). Que los dos hayan elegido el mismo texto es a proposito: prueba que
    lo que separa a los dos es RLS (agencia+cliente), nunca el contenido.
    """
    a1 = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    a2 = sesion_de_cliente(AGENCIA_A, CLIENTE_A2)
    destino = "compartido@dos-negocios.invalid"

    async with sesion_de_inquilino(motor, a1) as conexion:
        await declarar_destino(
            conexion,
            a1,
            canal=Canal.CORREO,
            destino=destino,
            etiqueta="Equipo A1",
            declarado_por="admin-a1@negocio.invalid",
        )
    async with sesion_de_inquilino(motor, a2) as conexion:
        await declarar_destino(
            conexion,
            a2,
            canal=Canal.CORREO,
            destino=destino,
            etiqueta="Equipo A2",
            declarado_por="admin-a2@negocio.invalid",
        )

    async with sesion_de_inquilino(motor, a1) as conexion:
        propio_de_a1 = await exigir_destino_declarado(conexion, a1, Canal.CORREO, destino)
        listado_de_a1 = await listar_destinos(conexion, a1)
    async with sesion_de_inquilino(motor, a2) as conexion:
        propio_de_a2 = await exigir_destino_declarado(conexion, a2, Canal.CORREO, destino)
        listado_de_a2 = await listar_destinos(conexion, a2)

    # NO VE: el listado de cada cliente solo trae SU propia etiqueta para ese texto.
    assert propio_de_a1.etiqueta == "Equipo A1"
    assert propio_de_a2.etiqueta == "Equipo A2"
    assert all(d.etiqueta != "Equipo A2" for d in listado_de_a1)
    assert all(d.etiqueta != "Equipo A1" for d in listado_de_a2)

    # NO USA: son DOS filas distintas, no una compartida con dos nombres.
    assert propio_de_a1.id != propio_de_a2.id


# ==========================================================================
# 4 — sabotaje: sin la busqueda real, el guard deja pasar lo no declarado
# ==========================================================================
async def test_sabotaje_sin_la_busqueda_activa_el_guard_no_rechaza_nada(
    motor, monkeypatch
) -> None:
    """Se reemplaza `_buscar_destino_activo` por una que dice que SIEMPRE existe.

    Es la unica forma de confirmar que la sonda 1 mide el mecanismo real: con la
    busqueda neutralizada, un destino que jamas se declaro deja de rechazarse —
    exactamente el defecto que RF-46-bis (y L-05) existen para impedir.
    """
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)

    async def _dice_que_siempre_existe(
        conexion, *, canal, destino
    ) -> DestinoDeAviso:  # noqa: ARG001 - firma exigida por el punto que sustituye
        return DestinoDeAviso(
            id=uuid4(),
            agencia_id=AGENCIA_A,
            cliente_id=CLIENTE_A1,
            canal=canal,
            destino=destino,
            etiqueta="fabricado por el sabotaje",
            activo=True,
            declarado_en=datetime.now(UTC),
            declarado_por="sabotaje",
        )

    monkeypatch.setattr(destinos_internos, "_buscar_destino_activo", _dice_que_siempre_existe)

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        # Sin el sabotaje, esta misma llamada levanta DestinoNoDeclarado (sonda 1).
        colado = await exigir_destino_declarado(
            conexion, inquilino, Canal.CORREO, "esto-jamas-se-declaro@negocio-a1.invalid"
        )

    assert colado.etiqueta == "fabricado por el sabotaje", (
        "el saboteador no llego a sustituir la busqueda: si esto fallara por otra "
        "razon, la sonda no estaria midiendo lo que dice medir"
    )


# ==========================================================================
# 5 — un webhook se valida por FORMA al declararlo (guard de DESTINO, no de RED)
# ==========================================================================
async def test_declarar_un_webhook_con_direccion_interna_es_rechazado(motor) -> None:
    """CE-07: para el tipo `webhook_interno`, una direccion interna se rechaza AL DECLARAR."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    interna = f"https://{_primera_direccion_privada()}/aviso"

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(DestinoRechazado):
            await declarar_destino(
                conexion,
                inquilino,
                canal=Canal.WEBHOOK_INTERNO,
                destino=interna,
                etiqueta="Webhook interno del panel",
                declarado_por="admin@negocio-a1.invalid",
            )
        # Control en la MISMA transaccion: el rechazo no dejo nada declarado.
        with pytest.raises(DestinoNoDeclarado):
            await exigir_destino_declarado(conexion, inquilino, Canal.WEBHOOK_INTERNO, interna)


async def test_control_un_webhook_con_direccion_publica_si_se_declara(motor) -> None:
    """Sin este control, un `declarar_destino` que rechazara TODO webhook pasaria
    la sonda de arriba."""
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    publica = f"https://{_PUBLICA}/aviso"

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        declarado = await declarar_destino(
            conexion,
            inquilino,
            canal=Canal.WEBHOOK_INTERNO,
            destino=publica,
            etiqueta="Webhook interno del panel",
            declarado_por="admin@negocio-a1.invalid",
        )
        encontrado = await exigir_destino_declarado(
            conexion, inquilino, Canal.WEBHOOK_INTERNO, publica
        )

    assert declarado.destino == publica
    assert encontrado.id == declarado.id


# ==========================================================================
# 6 — validaciones de entrada: canal fuera de los tres declarables, campos vacios
# ==========================================================================
async def test_un_canal_no_admitido_se_rechaza_al_declarar_y_al_exigir(motor) -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(CanalNoAdmitido):
            await declarar_destino(
                conexion,
                inquilino,
                canal="sms",
                destino="+10000000000",
                etiqueta="Numero de guardia",
                declarado_por="admin@negocio-a1.invalid",
            )
        with pytest.raises(CanalNoAdmitido):
            await exigir_destino_declarado(conexion, inquilino, "sms", "+10000000000")


@pytest.mark.parametrize("campo", ["destino", "etiqueta", "declarado_por"])
async def test_declarar_exige_los_tres_campos_no_vacios(motor, campo: str) -> None:
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    argumentos = {
        "canal": Canal.CORREO,
        "destino": "valido@negocio-a1.invalid",
        "etiqueta": "Equipo",
        "declarado_por": "admin@negocio-a1.invalid",
    }
    argumentos[campo] = "   "  # vacio tras strip()
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(ValueError):
            await declarar_destino(conexion, inquilino, **argumentos)


async def test_exigir_destino_declarado_rechaza_un_destino_vacio_como_entrada_invalida(
    motor,
) -> None:
    """Un destino vacio es un error de ENTRADA, no una pregunta legitima sobre lo

    declarado (hallazgo de la revision cruzada): `declarar_destino` nunca guarda
    un destino vacio, asi que sin esta comprobacion la busqueda simplemente no
    encontraria nada y el llamador veria `DestinoNoDeclarado` — un `LookupError`
    que confundiria "no escribiste nada" con "esto de verdad no esta declarado".
    """
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        with pytest.raises(ValueError) as capturado:
            await exigir_destino_declarado(conexion, inquilino, Canal.CORREO, "   ")
    assert not issubclass(capturado.type, DestinoNoDeclarado), (
        "un destino vacio tiene que fallar como entrada invalida, no como "
        "'no declarado': son dos causas distintas"
    )
