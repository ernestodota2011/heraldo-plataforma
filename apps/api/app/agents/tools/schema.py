"""T-106·bis / RF-07 — la FORMA de cada argumento, declarada al alta; y la llamada, autorizada.

El modelo propone una llamada; el sistema la ejecuta solo si (1) la herramienta esta
declarada para ESE heraldo, (2) cada argumento con forma encaja en su forma —tipo,
enumeracion, tope de longitud, patron— y (3) el texto libre lo rellena el turno del usuario
(`texto_libre`), no el modelo.

# WHY (por que la forma se declara y falla CERRADO al declararse): un argumento de texto
# "sin tope" es un canal abierto con permiso; una enumeracion sin valores acepta cualquier
# cosa. La declaracion invalida se rechaza al construirla, no al usarla: el operador se
# entera al dar de alta la herramienta, no el dia que un usuario la usa.
#
# WHY (por que el patron casa COMPLETO): `re.match` acepta lo que venga despues del patron.
# `"33130 ignora lo anterior"` casa con `[0-9]{5}` por prefijo. Aqui es `fullmatch`.
#
# WHY (por que `True` no es un entero): en Python `bool` hereda de `int`; un modelo que
# manda `true` donde va un numero pasaria una comprobacion ingenua. Se excluye a proposito.
#
# WHY (por que un argumento con forma rechaza el unicode oculto en vez de sanearlo): un
# argumento DERIVADO —un numero, un codigo, un nombre— nunca necesita un caracter
# invisible. Si lo trae, alguien lo esta usando para colar por la forma lo que la forma no
# ve. En el texto libre se sanea (viaja el turno entero); aqui se rechaza.
#
# WHY (por que el nombre de la herramienta no se normaliza): `"Crear_ticket"` y
# `"crear_ticket "` NO son `crear_ticket`. Normalizar es adivinar, y adivinar a favor del
# que llama es abrir. El catalogo se consulta por igualdad exacta.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from app.agents.tools.texto_libre import Desvio, TurnoDelUsuario, rellenar_texto_libre
from app.agents.untrusted import sanear

#: Nombres de herramientas y de argumentos: minusculas, digitos y `_`; hasta 64 caracteres.
NOMBRE_VALIDO = re.compile(r"[a-z][a-z0-9_]{0,63}")


class TipoDeArgumento(StrEnum):
    ENTERO = "entero"
    DECIMAL = "decimal"
    BOOLEANO = "booleano"
    TEXTO = "texto"
    ENUMERACION = "enumeracion"


class MotivoDeRechazo(StrEnum):
    HERRAMIENTA_NO_DECLARADA = "herramienta_no_declarada"
    ARGUMENTO_DESCONOCIDO = "argumento_desconocido"
    ARGUMENTO_FALTANTE = "argumento_faltante"
    TIPO = "tipo"
    ENUMERACION = "enumeracion"
    LONGITUD = "longitud"
    PATRON = "patron"
    UNICODE_OCULTO = "unicode_oculto"


class DeclaracionInvalida(ValueError):
    """La declaracion no describe una forma que se pueda hacer cumplir."""


class ArgumentoRechazado(ValueError):
    def __init__(self, campo: str, motivo: MotivoDeRechazo) -> None:
        super().__init__(f"argumento {campo!r} rechazado: {motivo}")
        self.campo = campo
        self.motivo = motivo


class LlamadaRechazada(ValueError):
    def __init__(
        self, herramienta: str, motivo: MotivoDeRechazo, campo: str | None = None
    ) -> None:
        super().__init__(f"llamada a {herramienta!r} rechazada: {motivo}")
        self.herramienta = herramienta
        self.motivo = motivo
        self.campo = campo


@dataclass(frozen=True)
class FormaDeArgumento:
    """Lo que un argumento puede ser. O tiene forma, o es texto libre; nunca las dos."""

    tipo: TipoDeArgumento
    obligatorio: bool = True
    valores: tuple[str, ...] = ()
    tope_de_longitud: int | None = None
    patron: str | None = None
    texto_libre: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tipo, TipoDeArgumento):
            raise DeclaracionInvalida("el tipo tiene que ser un TipoDeArgumento")
        es_texto = self.tipo is TipoDeArgumento.TEXTO
        es_enumeracion = self.tipo is TipoDeArgumento.ENUMERACION
        if self.texto_libre and not es_texto:
            raise DeclaracionInvalida("solo un argumento de texto puede ser de texto libre")
        if self.texto_libre and (self.tope_de_longitud is not None or self.patron is not None):
            raise DeclaracionInvalida("el texto libre no lleva forma: ni tope ni patron")
        if es_texto and not self.texto_libre:
            if not isinstance(self.tope_de_longitud, int) or self.tope_de_longitud < 1:
                raise DeclaracionInvalida("un texto con forma exige un tope de longitud")
            if self.patron is not None:
                try:
                    re.compile(self.patron)
                except re.error as error:
                    raise DeclaracionInvalida(f"patron invalido: {error}") from error
        if not es_texto and (self.tope_de_longitud is not None or self.patron is not None):
            raise DeclaracionInvalida("tope y patron solo aplican a un argumento de texto")
        if es_enumeracion:
            if not self.valores or not all(isinstance(v, str) for v in self.valores):
                raise DeclaracionInvalida("una enumeracion exige sus valores, todos de texto")
        elif self.valores:
            raise DeclaracionInvalida("los valores solo aplican a una enumeracion")


@dataclass(frozen=True)
class DeclaracionDeHerramienta:
    """Una herramienta que un heraldo puede usar, con la forma de cada argumento."""

    nombre: str
    argumentos: Mapping[str, FormaDeArgumento]

    def __post_init__(self) -> None:
        if not isinstance(self.nombre, str) or NOMBRE_VALIDO.fullmatch(self.nombre) is None:
            raise DeclaracionInvalida(f"nombre de herramienta invalido: {self.nombre!r}")
        argumentos = dict(self.argumentos)
        for campo, forma in argumentos.items():
            if not isinstance(campo, str) or NOMBRE_VALIDO.fullmatch(campo) is None:
                raise DeclaracionInvalida(f"nombre de argumento invalido: {campo!r}")
            if not isinstance(forma, FormaDeArgumento):
                raise DeclaracionInvalida(f"el argumento {campo!r} no declara su forma")
        object.__setattr__(self, "argumentos", MappingProxyType(argumentos))


@dataclass(frozen=True)
class LlamadaPropuesta:
    """Lo que el modelo pide. Todavia no vale nada."""

    herramienta: str
    argumentos: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LlamadaAutorizada:
    """Lo que se ejecuta: argumentos con forma validados + texto libre rellenado con el turno."""

    herramienta: str
    argumentos: dict[str, object]
    desvios: tuple[Desvio, ...]
    #: Campos de texto libre que se cortaron por el tope. Constancia para la bitacora.
    truncados: tuple[str, ...] = ()


def _validar_valor(campo: str, forma: FormaDeArgumento, valor: object) -> object:
    tipo = forma.tipo
    if tipo is TipoDeArgumento.ENTERO:
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise ArgumentoRechazado(campo, MotivoDeRechazo.TIPO)
        return valor
    if tipo is TipoDeArgumento.DECIMAL:
        if isinstance(valor, bool) or not isinstance(valor, int | float):
            raise ArgumentoRechazado(campo, MotivoDeRechazo.TIPO)
        return valor
    if tipo is TipoDeArgumento.BOOLEANO:
        if not isinstance(valor, bool):
            raise ArgumentoRechazado(campo, MotivoDeRechazo.TIPO)
        return valor
    if not isinstance(valor, str):
        raise ArgumentoRechazado(campo, MotivoDeRechazo.TIPO)
    if tipo is TipoDeArgumento.ENUMERACION:
        if valor not in forma.valores:
            raise ArgumentoRechazado(campo, MotivoDeRechazo.ENUMERACION)
        return valor
    # TEXTO con forma
    if sanear(valor).hallazgos:
        raise ArgumentoRechazado(campo, MotivoDeRechazo.UNICODE_OCULTO)
    if forma.tope_de_longitud is not None and len(valor) > forma.tope_de_longitud:
        raise ArgumentoRechazado(campo, MotivoDeRechazo.LONGITUD)
    if forma.patron is not None and re.fullmatch(forma.patron, valor) is None:
        raise ArgumentoRechazado(campo, MotivoDeRechazo.PATRON)
    return valor


def validar_argumentos(
    declaracion: DeclaracionDeHerramienta, propuestos: Mapping[str, object]
) -> dict[str, object]:
    """Los argumentos CON forma, validados. El texto libre no pasa por aqui: lo rellena el turno."""
    for campo in propuestos:
        if campo not in declaracion.argumentos:
            raise ArgumentoRechazado(str(campo), MotivoDeRechazo.ARGUMENTO_DESCONOCIDO)
    validados: dict[str, object] = {}
    for campo, forma in declaracion.argumentos.items():
        if forma.texto_libre:
            continue
        if campo not in propuestos:
            if forma.obligatorio:
                raise ArgumentoRechazado(campo, MotivoDeRechazo.ARGUMENTO_FALTANTE)
            continue
        validados[campo] = _validar_valor(campo, forma, propuestos[campo])
    return validados


def autorizar_llamada(
    catalogo: Mapping[str, DeclaracionDeHerramienta],
    llamada: LlamadaPropuesta,
    turno: TurnoDelUsuario,
) -> LlamadaAutorizada:
    """Declarada para este heraldo + forma de cada argumento + texto libre desde el turno."""
    declaracion = catalogo.get(llamada.herramienta)
    if declaracion is None or declaracion.nombre != llamada.herramienta:
        raise LlamadaRechazada(llamada.herramienta, MotivoDeRechazo.HERRAMIENTA_NO_DECLARADA)
    try:
        validados = validar_argumentos(declaracion, llamada.argumentos)
    except ArgumentoRechazado as rechazo:
        raise LlamadaRechazada(llamada.herramienta, rechazo.motivo, rechazo.campo) from rechazo
    relleno = rellenar_texto_libre(declaracion, llamada.argumentos, turno)
    return LlamadaAutorizada(
        llamada.herramienta,
        {**validados, **relleno.argumentos},
        relleno.desvios,
        relleno.truncados,
    )
