"""Aislamiento entre inquilinos: el mecanismo que funda el producto (D-02)."""

from app.tenancy.inquilino import (
    CENTINELA_SIN_CLIENTE,
    Alcance,
    AlcanceInvalido,
    Inquilino,
)
from app.tenancy.rol import ROL_APLICACION
from app.tenancy.sesion import crear_motor, dsn_de_aplicacion, sesion_de_inquilino

__all__ = [
    "CENTINELA_SIN_CLIENTE",
    "ROL_APLICACION",
    "Alcance",
    "AlcanceInvalido",
    "Inquilino",
    "crear_motor",
    "dsn_de_aplicacion",
    "sesion_de_inquilino",
]
