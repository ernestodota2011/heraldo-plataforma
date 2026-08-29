"""El inquilino es el par `(agencia, cliente)` — y su ALCANCE no se pide.

# WHY: plan §3.1 punto 5. «El alcance lo fija la identidad autenticada, jamas la
# peticion». Aqui eso no es una advertencia en un comentario: `Inquilino` no
# tiene ningun constructor publico que acepte un alcance. El unico camino es
# `Inquilino.desde_usuario(...)`, que lo DERIVA de la fila del usuario. Una
# cabecera o un cuerpo de peticion no pueden pedir `alcance=agencia` porque no
# existe el parametro donde escribirlo.
#
# WHY: plan §3.1 punto 7 (L-19). `cliente_id` se declara SIEMPRE — las tres
# variables, sin excepcion — y cuando el alcance es `agencia` lleva el uuid nulo,
# no cadena vacia: una cadena vacia no convierte a uuid y la expresion fallaria
# por una razon distinta de la que queremos.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Valor centinela de `app.cliente_id` cuando el alcance es `agencia` (L-19).
#: No coincide con ninguna fila real y SI convierte a uuid.
CENTINELA_SIN_CLIENTE = UUID("00000000-0000-0000-0000-000000000000")


class Alcance(StrEnum):
    """Los dos alcances de la cascada. No hay un tercero, ni un comodin."""

    AGENCIA = "agencia"
    CLIENTE = "cliente"


class AlcanceInvalido(ValueError):
    """El par (alcance, cliente_id) es incoherente: se rechaza antes de la base."""


@dataclass(frozen=True, slots=True)
class Inquilino:
    """Las tres variables de la cascada, ya coherentes entre si.

    Es inmutable a proposito: una vez abierta la sesion, nadie le cambia el
    alcance a mitad de transaccion.
    """

    agencia_id: UUID
    cliente_id: UUID
    alcance: Alcance

    def __post_init__(self) -> None:
        if self.alcance is Alcance.AGENCIA and self.cliente_id != CENTINELA_SIN_CLIENTE:
            raise AlcanceInvalido(
                "alcance 'agencia' exige el centinela en cliente_id (plan §3.1 punto 7)"
            )
        if self.alcance is Alcance.CLIENTE and self.cliente_id == CENTINELA_SIN_CLIENTE:
            raise AlcanceInvalido("alcance 'cliente' exige un cliente_id real, no el centinela")

    @classmethod
    def desde_usuario(cls, *, agencia_id: UUID, cliente_id: UUID | None) -> Inquilino:
        """UNICA forma de construir un inquilino: a partir de la fila del usuario.

        `cliente_id is None` en la fila significa «operador de la agencia» — y de
        ahi, y solo de ahi, sale el alcance `agencia`. No se recibe: se deriva.
        """
        if cliente_id is None:
            return cls(
                agencia_id=agencia_id,
                cliente_id=CENTINELA_SIN_CLIENTE,
                alcance=Alcance.AGENCIA,
            )
        return cls(agencia_id=agencia_id, cliente_id=cliente_id, alcance=Alcance.CLIENTE)
