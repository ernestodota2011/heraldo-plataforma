"""T-106·ter / RF-07·bis — el texto libre de una herramienta es el TURNO del usuario, no el modelo.

Cuando una herramienta admite legitimamente texto libre —una consulta, un asunto, un
cuerpo—, la allowlist de forma (`schema`) no puede impedir que por ahi salga cualquier cosa.
La unica mitigacion mecanica es decidir QUIEN escribe ese argumento: no el modelo. Se
rellena unicamente con el texto del turno del usuario final que disparo la llamada.

# WHY (por que se DESCARTA lo que el modelo propone en vez de validarlo): validar texto
# libre es imposible por definicion —no tiene forma—. Si el modelo pudiera elegir el valor,
# una inyeccion que lo convenciera meteria el conocimiento del cliente en `descripcion` y el
# destino, legitimo, lo recibiria. Con el valor fijado al turno, lo peor que sale por ahi es
# lo que el propio usuario escribio: nada que no supiera ya.
#
# WHY (por que un turno es un TIPO y no una cadena): una cadena admite `"\n".join(historial)`
# sin que nada avise. `TurnoDelUsuario` obliga a quien llama a nombrar UN mensaje con su
# identificador; pasar la conversacion entera deja de ser un descuido silencioso y pasa a
# ser un `TypeError`.
#
# WHY (por que queda constancia del desvio): que el modelo haya propuesto otra cosa es la
# senal mas barata de que alguien lo esta empujando. No bloquea la llamada —el argumento ya
# va limpio— pero viaja con ella para que la bitacora (T-304) lo registre.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agents.untrusted import TOPE_POR_DEFECTO, sanear

if TYPE_CHECKING:
    from app.agents.tools.schema import DeclaracionDeHerramienta

#: Cuanto del valor propuesto por el modelo se conserva como constancia del desvio.
TOPE_DEL_DESVIO = 200

#: Tope del texto libre que sale hacia una herramienta. El mismo que el del bloque del
#: contexto: lo que no cabe en el contexto tampoco tiene por que viajar a un tercero.
#: WHY (lo levanto Crisol): el bloque tenia tope (T-105) y este argumento no — un turno
#: enorme viajaba entero a un destino externo. Se corta y queda constancia del campo.
TOPE_DEL_TEXTO_LIBRE = TOPE_POR_DEFECTO


@dataclass(frozen=True)
class TurnoDelUsuario:
    """UN mensaje del usuario final, identificado. No una cadena, no una conversacion."""

    identificador: str
    texto: str

    def __post_init__(self) -> None:
        if not isinstance(self.identificador, str) or not isinstance(self.texto, str):
            raise TypeError("un turno lleva identificador y texto, los dos de tipo str")


@dataclass(frozen=True)
class Desvio:
    """El modelo propuso para un texto libre algo distinto del turno."""

    campo: str
    propuesto: str


@dataclass(frozen=True)
class RellenoDeTextoLibre:
    argumentos: dict[str, str]
    desvios: tuple[Desvio, ...]
    #: Campos cuyo texto se corto por el tope. Constancia, no bloqueo.
    truncados: tuple[str, ...] = ()


def rellenar_texto_libre(
    declaracion: DeclaracionDeHerramienta,
    propuestos: Mapping[str, object],
    turno: TurnoDelUsuario,
    *,
    tope_de_caracteres: int = TOPE_DEL_TEXTO_LIBRE,
) -> RellenoDeTextoLibre:
    """Rellena cada argumento de texto libre con el turno saneado; anota lo que el modelo quiso."""
    if not isinstance(turno, TurnoDelUsuario):
        raise TypeError("el texto libre se rellena con UN TurnoDelUsuario, no con texto suelto")
    if not isinstance(tope_de_caracteres, int) or tope_de_caracteres < 1:
        raise ValueError("el tope tiene que ser un entero positivo")
    saneado = sanear(turno.texto).texto
    texto = saneado[:tope_de_caracteres]
    argumentos: dict[str, str] = {}
    desvios: list[Desvio] = []
    truncados: list[str] = []
    for campo, forma in declaracion.argumentos.items():
        if not forma.texto_libre:
            continue
        argumentos[campo] = texto
        if len(saneado) > tope_de_caracteres:
            truncados.append(campo)
        propuesto = propuestos.get(campo)
        if propuesto is not None and propuesto not in (turno.texto, saneado, texto):
            desvios.append(Desvio(campo, str(propuesto)[:TOPE_DEL_DESVIO]))
    return RellenoDeTextoLibre(argumentos, tuple(desvios), tuple(truncados))
