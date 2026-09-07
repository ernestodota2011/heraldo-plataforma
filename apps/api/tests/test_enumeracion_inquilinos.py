"""T-032 (RF-60): la respuesta al dominio no atendido no permite enumerar clientes.

La afirmacion es de IGUALDAD, no de correccion: los **tres** casos no atendidos
—dominio desconocido, dado de alta sin verificar, e inquilino suspendido— tienen
que producir **exactamente** la misma respuesta. Se compara el codigo, el cuerpo
byte a byte y las cabeceras.

# WHY (por que una prueba de igualdad necesita SU CONTROL, y aqui dos):
# 1. **Dos peticiones del MISMO caso tambien coinciden.** Sin esto, una
#    comparacion que en realidad no compara nada —porque el ayudante normaliza de
#    mas, o devuelve siempre lo mismo por un fallo suyo— pasa en verde. El control
#    dice que el instrumento distingue peticiones de verdad.
# 2. **El dominio VERIFICADO si se atiende.** Sin esto, una plataforma que
#    devuelve 404 a todo el mundo cumple RF-60 perfectamente y no sirve para nada:
#    la igualdad se lograria destruyendo el producto.
#
# WHY (que pondria estas pruebas en ROJO): anadir a la excepcion un campo con el
# motivo y volcarlo en la respuesta; devolver 403 en el caso suspendido «porque es
# mas correcto»; o cambiar la compuerta por un `not in ESTADOS_NO_ATENDIDOS`, que
# atenderia cualquier estado futuro que nadie clasifique.
#
# WHY (lo que estas pruebas NO miden, dicho en voz alta): **el tiempo**. Dos
# respuestas identicas pueden delatar por cuanto tardan en llegar — un dominio
# desconocido se resuelve antes que uno que hay que buscar en la base. Hoy no hay
# base y no habria nada que medir; cuando el registro exista, esa es una prueba
# nueva y distinta, no un `assert` mas en esta. Tampoco se mide que las rutas de
# inquilino FUTURAS lleven la compuerta puesta: hoy no existe ninguna.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import Depends

from app.main import Entorno, crear_aplicacion
from app.superficie_publica import ExposicionDeDocumentacion
from app.tenancy import crear_motor
from app.tenancy.dominio_desconocido import (
    CODIGO_UNICO,
    CUERPO_UNICO,
    ESTADOS_NO_ATENDIDOS,
    DominioNoReconocido,
    EstadoDeDominio,
    exigir_dominio_atendible,
)

ORIGEN_DECLARADO = "https://panel.aetherlogik.example"
DSN_INALCANZABLE = "postgresql+psycopg://heraldo_app@127.0.0.1:1/no_existe"

#: La ruta que representa una superficie servida bajo el dominio de un cliente.
#: Todavia no existe ninguna en produccion; esta la monta la prueba para poder
#: ejercitar la compuerta REAL en vez de llamarla como funcion suelta.
RUTA_DEL_PORTAL = "/portal"

#: Tres dominios distintos, uno por caso. ==Que sean DISTINTOS es parte de la
#: prueba==: si los tres usaran el mismo nombre, una respuesta que filtrara el
#: dominio pedido seguiria saliendo identica y la fuga pasaria desapercibida.
DOMINIOS = {
    EstadoDeDominio.DESCONOCIDO: "nadie-lo-dio-de-alta.example",
    EstadoDeDominio.SIN_VERIFICAR: "empezado-sin-terminar.example",
    EstadoDeDominio.SUSPENDIDO: "cliente-suspendido.example",
    EstadoDeDominio.VERIFICADO: "cliente-al-dia.example",
}

#: Cabeceras que cambian entre dos peticiones cualesquiera y no dicen nada del
#: inquilino. Se excluyen **por nombre y con motivo escrito**, no con un filtro
#: vago: cualquier cabecera nueva entra sola en la comparacion.
CABECERAS_VOLATILES = frozenset({"date"})


def _aplicacion(estado: EstadoDeDominio, **extras: Any):
    """La aplicacion REAL, con un resolutor que declara ese estado."""

    async def resolutor(dominio: str) -> EstadoDeDominio:
        return estado

    parametros: dict[str, Any] = {
        "entorno": Entorno.PRODUCCION,
        "origenes": (ORIGEN_DECLARADO,),
        "motor": crear_motor(DSN_INALCANZABLE),
        "exposicion_de_documentacion": ExposicionDeDocumentacion.APAGADA,
        "resolutor_de_dominio": resolutor,
    }
    parametros.update(extras)
    aplicacion = crear_aplicacion(**parametros)

    @aplicacion.get(RUTA_DEL_PORTAL, dependencies=[Depends(exigir_dominio_atendible)])
    async def portal() -> dict:
        return {"servido": True}

    return aplicacion


def _cliente(aplicacion, dominio: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=aplicacion), base_url=f"https://{dominio}"
    )


async def _huella(
    estado: EstadoDeDominio,
) -> tuple[int, str, tuple[tuple[str, str], ...]]:
    """Todo lo que un visitante puede observar de la respuesta, en una tupla."""
    aplicacion = _aplicacion(estado)
    async with _cliente(aplicacion, DOMINIOS[estado]) as cliente:
        respuesta = await cliente.get(RUTA_DEL_PORTAL)
    cabeceras = tuple(
        sorted(
            (nombre.lower(), valor)
            for nombre, valor in respuesta.headers.items()
            if nombre.lower() not in CABECERAS_VOLATILES
        )
    )
    return respuesta.status_code, respuesta.text, cabeceras


# ==========================================================================
# EL CASO: los tres no atendidos son indistinguibles entre si
# ==========================================================================
async def test_los_tres_casos_no_atendidos_responden_EXACTAMENTE_lo_mismo() -> None:
    huellas = {estado: await _huella(estado) for estado in sorted(ESTADOS_NO_ATENDIDOS)}
    assert len(set(huellas.values())) == 1, (
        "los estados no atendidos NO responden igual, asi que la respuesta permite "
        "deducir en cual de los tres cae un dominio — y con eso se enumera la "
        "cartera de clientes desde fuera y sin credencial (RF-60, RNF-05). "
        + " | ".join(
            f"{estado.value}: {huella[0]} {huella[1]!r}"
            for estado, huella in huellas.items()
        )
    )


@pytest.mark.parametrize("estado", sorted(ESTADOS_NO_ATENDIDOS))
async def test_cada_caso_no_atendido_da_el_codigo_y_el_cuerpo_declarados(estado) -> None:
    """La igualdad de arriba se cumpliria tambien si los tres estuvieran mal.

    # WHY: tres respuestas identicas pero equivocadas —tres 500, por ejemplo— pasan
    # la prueba de igualdad perfectamente. Esta fija CUAL es la respuesta.
    """
    codigo, cuerpo, _ = await _huella(estado)
    assert codigo == CODIGO_UNICO
    assert cuerpo == json.dumps(CUERPO_UNICO, separators=(",", ":"))


# --------------------------------------------------------------------------
# CONTROL 1 — el instrumento distingue de verdad
# --------------------------------------------------------------------------
async def test_control_dos_peticiones_del_mismo_caso_coinciden() -> None:
    """Sin esto, una comparacion que no compara nada saldria verde igual."""
    assert await _huella(EstadoDeDominio.DESCONOCIDO) == await _huella(
        EstadoDeDominio.DESCONOCIDO
    )


async def test_control_la_huella_SI_distingue_una_respuesta_distinta() -> None:
    """El control del control: la huella tiene que separar lo que es distinto.

    Si `_huella` normalizara de mas, las tres del caso saldrian iguales por el
    ayudante y no por el producto. Aqui se compara contra una respuesta que
    **debe** diferir: la del dominio atendido.
    """
    assert await _huella(EstadoDeDominio.DESCONOCIDO) != await _huella(
        EstadoDeDominio.VERIFICADO
    )


# --------------------------------------------------------------------------
# CONTROL 2 — el producto sigue sirviendo a quien si se atiende
# --------------------------------------------------------------------------
async def test_control_un_dominio_verificado_SI_se_atiende() -> None:
    """Una plataforma que responde 404 a todo cumple RF-60 y no sirve para nada."""
    codigo, cuerpo, _ = await _huella(EstadoDeDominio.VERIFICADO)
    assert codigo == 200, (
        f"el dominio verificado salio {codigo}: la igualdad de los tres casos se "
        "estaria consiguiendo por no atender a nadie, que es un producto roto "
        "disfrazado de producto seguro"
    )
    assert '"servido":true' in cuerpo.replace(" ", "")


# ==========================================================================
# La respuesta no delata NI el dominio NI el producto
# ==========================================================================
@pytest.mark.parametrize("estado", sorted(ESTADOS_NO_ATENDIDOS))
async def test_la_respuesta_no_repite_el_dominio_que_se_pidio(estado) -> None:
    """Repetir lo que mando el visitante convierte una respuesta fija en variable."""
    _, cuerpo, cabeceras = await _huella(estado)
    todo = cuerpo + " " + " ".join(f"{n}:{v}" for n, v in cabeceras)
    assert DOMINIOS[estado] not in todo, (
        f"la respuesta devuelve el dominio pedido ({DOMINIOS[estado]}): con eso deja "
        "de ser una respuesta fija, y ademas mete texto de un tercero en una pagina "
        "nuestra"
    )


@pytest.mark.parametrize("estado", sorted(ESTADOS_NO_ATENDIDOS))
@pytest.mark.parametrize("marca", ["heraldo", "aetherlogik"])
async def test_la_respuesta_no_nombra_el_producto_ni_la_agencia(estado, marca) -> None:
    """CE-17: esta respuesta se sirve SOBRE el dominio de un cliente."""
    _, cuerpo, cabeceras = await _huella(estado)
    todo = (cuerpo + " " + " ".join(f"{n}:{v}" for n, v in cabeceras)).lower()
    assert marca not in todo, (
        f"la respuesta al dominio no reconocido nombra {marca!r}, y se sirve sobre el "
        "dominio de un cliente: rompe la marca blanca justo en la pagina que nadie "
        "mira (CE-17, RNF-10)"
    )


# ==========================================================================
# Guards estructurales: que la fuga NO se pueda reintroducir por descuido
# ==========================================================================
def test_la_excepcion_no_puede_llevar_un_motivo_dentro() -> None:
    """El diseno, no la disciplina: no hay donde meter el dato que filtraria.

    # WHY: la forma natural de romper RF-60 dentro de seis meses es
    # `raise DominioNoReconocido("suspendido")` «solo para el registro», y que
    # alguien lo imprima. Si la excepcion no acepta argumentos, esa linea no llega
    # ni a ejecutarse.
    """
    with pytest.raises(TypeError):
        DominioNoReconocido("suspendido")  # type: ignore[call-arg]

    vacia = DominioNoReconocido()
    assert vacia.args == ()
    assert not [
        atributo for atributo in vars(vacia) if not atributo.startswith("_")
    ], "la excepcion gano un atributo propio: ahi es donde vuelve la fuga"


def test_atendible_es_una_allowlist_de_un_solo_estado() -> None:
    """Todo estado que no sea VERIFICADO cuenta como no atendido, incluso uno nuevo.

    # WHY: si manana alguien anade `EstadoDeDominio.EN_MIGRACION` y la compuerta
    # preguntara «¿esta en la lista de prohibidos?», ese estado quedaria ATENDIDO
    # por omision. Esta prueba se deriva del enum, asi que un estado nuevo entra
    # sola en ella.
    """
    no_atendidos = {e for e in EstadoDeDominio if e is not EstadoDeDominio.VERIFICADO}
    assert set(ESTADOS_NO_ATENDIDOS) == no_atendidos
    assert EstadoDeDominio.VERIFICADO not in ESTADOS_NO_ATENDIDOS


def test_el_resolutor_por_defecto_no_atiende_a_nadie() -> None:
    """Sin registro de dominios, ningun dominio se atiende. Falla cerrado.

    # WHY: el registro todavia no existe. Un valor por defecto «permisivo mientras
    # tanto» seria un dominio atendido que nadie verifico, que es justo lo que
    # RF-60 protege.
    """
    import asyncio
    import inspect

    from app.tenancy.dominio_desconocido import sin_registro_de_dominios

    por_defecto = (
        inspect.signature(crear_aplicacion).parameters["resolutor_de_dominio"].default
    )
    assert por_defecto is sin_registro_de_dominios, (
        f"el resolutor por defecto ya no es el que falla cerrado, sino {por_defecto!r}"
    )
    assert (
        asyncio.run(sin_registro_de_dominios("cliente-al-dia.example"))
        is EstadoDeDominio.DESCONOCIDO
    )
