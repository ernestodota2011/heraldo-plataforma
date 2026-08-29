"""T-019 (RF-13) — los limites de uso viven en Redis, por INQUILINO y por DIRECCION.

# WHY (D-05, defecto 6 del referente): en memoria del proceso el limite se
# MULTIPLICA por el numero de workers. Un techo de 60/min con cuatro procesos son
# 240/min, y nadie se entera porque cada proceso cumple su cuenta. Redis es el
# unico sitio donde «una vez» significa una vez (RNF-03).
#
# WHY (los DOS ejes, que son el defecto clasico de esta pieza): un limitador que
# solo cuenta por inquilino deja que un unico atacante desde una direccion queme
# la cuota de todo un cliente; uno que solo cuenta por direccion deja que dos
# inquilinos distintos detras del mismo NAT se estorben —y se filtren informacion
# el uno del otro por el canal del limite. Aqui el cubo lo determina el PAR
# `(inquilino, direccion)`:
#   - el mismo inquilino desde dos direcciones NO comparte cubo;
#   - dos inquilinos desde la misma direccion TAMPOCO.
# Un limite global «que parece funcionar porque solo se probo un inquilino» es el
# defecto, no la prueba.
#
# WHY (la clave se DERIVA con longitudes, no se concatena): la direccion es lo
# unico de la clave que viene de fuera. Con `f"{agencia}:{cliente}:{direccion}"`,
# un valor que contenga el separador puede hacerse pasar por otro par — y una
# IPv6 lleva `:` de serie. Aqui cada parte se serializa con su longitud delante y
# el conjunto se resume; no hay forma de que una parte invada a la siguiente. La
# validacion de `Direccion` ya cierra el camino practico: esto es la segunda capa,
# porque el defecto es de FORMA y las formas sobreviven a los refactors.
#
# WHY (`Direccion` valida, no acepta texto): una direccion es una direccion de
# red, no una cadena. Si lo que llega no convierte, se RECHAZA — no se cuenta en
# un cubo llamado «texto raro», donde todos los remitentes con texto raro
# compartirian cuota y ninguno seria identificable. Y la normalizacion la hace el
# tipo: `::1` y `0:0:0:0:0:0:0:1` son LA MISMA direccion y comparten cubo; si la
# clave fuera el texto crudo, cambiar de notacion duplicaria la cuota.
#
# WHY (el conteo es ATOMICO y el vencimiento no se puede perder): `INCR` y
# `PEXPIRE` en dos viajes dejan una ventana en la que un corte deja el cubo SIN
# vencimiento — y un cubo sin vencimiento no se vacia nunca: el inquilino queda
# bloqueado para siempre. El guion de Lua hace las dos cosas en una sola
# ejecucion y ademas RE-ARMA el vencimiento si encuentra un cubo sin el, para que
# un cubo huerfano de una version anterior se cure solo.
#
# LIMITE DECLARADO, NO MAQUILLADO: la ventana es FIJA, y una ventana fija admite
# el DOBLE de la cuota a caballo del corte. Medido, no supuesto: con cuota 5 y
# ventana de 2 s se admitieron 10 consumos en 0,39 s gastando la cuota al final
# de una ventana y otra vez al principio de la siguiente. No es un defecto de
# esta implementacion —es la propiedad de toda ventana fija— pero SI es una
# diferencia real entre lo que la perilla dice («5 cada 2 s») y lo que hace en el
# peor caso, asi que se escribe aqui y se FIJA con
# `test_la_ventana_fija_admite_el_doble_a_caballo_del_corte`: nadie puede
# confundir esto con una ventana deslizante mirando el codigo ni la suite.
#
# El arreglo de raiz es una ventana DESLIZANTE ponderada (contador de la ventana
# actual + el de la anterior pesado por la fraccion transcurrida, en el mismo
# guion atomico). NO se hace aqui por dos razones que se declaran: cambia la
# semantica de `consumido` —hoy el rechazo tambien cuenta— y con ella tres
# sondas; y «cuanto vale de verdad la perilla» es una decision de la spec, no del
# constructor. Registrado como P-23 con su plan.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address

from redis.asyncio import Redis

from app.tenancy.inquilino import Inquilino

#: Prefijo de las claves de limite. Se cambia para aislar bancos de pruebas.
PREFIJO_POR_DEFECTO = "heraldo"

#: Nombres de limite admitidos. Es una ALLOWLIST por forma: lo que no case, no
#: entra en una clave (`feedback_denylist_por_allowlist`).
_NOMBRE_DE_LIMITE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class NombreDeLimiteInvalido(ValueError):
    """El nombre del limite no es interpolable con seguridad en una clave."""


class DireccionInvalida(ValueError):
    """Lo que llego no es una direccion de red. No se cuenta: se rechaza."""


class LimiteMalDeclarado(ValueError):
    """Una cuota o una ventana que no tienen sentido: se rechaza al declararlas."""


@dataclass(frozen=True, slots=True)
class Direccion:
    """La direccion de red del remitente, ya validada y NORMALIZADA.

    No se construye desde texto arbitrario: `desde_texto` es la puerta, y rechaza
    lo que no sea una direccion. Que sea un tipo y no un `str` es lo que impide
    que un encabezado inventado se convierta en un cubo.
    """

    valor: IPv4Address | IPv6Address

    @classmethod
    def desde_texto(cls, texto: str | None) -> Direccion:
        if not isinstance(texto, str) or not texto.strip():
            raise DireccionInvalida(
                "no llego ninguna direccion. Sin direccion no hay cubo por direccion, "
                "y contar todo lo anonimo junto es no contar"
            )
        try:
            return cls(ipaddress.ip_address(texto.strip()))
        except ValueError as fallo:
            raise DireccionInvalida(f"{texto!r} no es una direccion de red: {fallo}") from fallo

    @property
    def canonica(self) -> str:
        """Forma unica de esta direccion. `::1` y `0:0:...:1` dan la misma."""
        return self.valor.compressed


@dataclass(frozen=True, slots=True)
class Limite:
    """Cuanto se admite y en cuanto tiempo. Ventana fija."""

    nombre: str
    cuota: int
    ventana_segundos: int

    def __post_init__(self) -> None:
        if not _NOMBRE_DE_LIMITE.match(self.nombre):
            raise NombreDeLimiteInvalido(
                f"{self.nombre!r} no es un nombre de limite admitido "
                "(minusculas, digitos y guion bajo, hasta 40 caracteres)"
            )
        if self.cuota < 1:
            raise LimiteMalDeclarado(
                f"cuota {self.cuota}: un limite de cero no limita, cierra. Si la "
                "intencion es cerrar, se cierra explicitamente, no con un limite"
            )
        if self.ventana_segundos < 1:
            raise LimiteMalDeclarado(f"ventana {self.ventana_segundos}s: no es una ventana")


@dataclass(frozen=True, slots=True)
class Veredicto:
    """Que paso con este consumo, y cuanto falta para que la ventana se abra."""

    permitido: bool
    consumido: int
    cuota: int
    milisegundos_para_reintentar: int

    @property
    def restante(self) -> int:
        return max(0, self.cuota - self.consumido)


#: Cuenta y arma el vencimiento en UNA sola ejecucion atomica.
#: KEYS[1] = clave del cubo · ARGV[1] = ventana en milisegundos.
#: Devuelve {consumido, milisegundos_que_le_quedan_al_cubo}.
_GUION_CONSUMIR = """
local consumido = redis.call('INCR', KEYS[1])
local restante = redis.call('PTTL', KEYS[1])
if consumido == 1 or restante < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  restante = tonumber(ARGV[1])
end
return {consumido, restante}
"""


def _canonico(*partes: str) -> str:
    """Serializacion INEQUIVOCA: cada parte lleva su longitud delante.

    Con `a|b` no se distingue `("a", "b")` de `("a|b",)`. Con `1:a|1:b` si.
    """
    return "|".join(f"{len(parte)}:{parte}" for parte in partes)


def huella_del_cubo(inquilino: Inquilino, direccion: Direccion) -> str:
    """El identificador del cubo: el PAR (inquilino, direccion), y nada mas."""
    return hashlib.sha256(
        _canonico(
            str(inquilino.agencia_id),
            str(inquilino.cliente_id),
            direccion.canonica,
        ).encode("utf-8")
    ).hexdigest()


class LimitadorCompartido:
    """El limitador de Heraldo. Compartido entre procesos porque vive en Redis."""

    def __init__(self, redis: Redis, *, prefijo: str = PREFIJO_POR_DEFECTO) -> None:
        self._redis = redis
        self._prefijo = prefijo
        self._guion = redis.register_script(_GUION_CONSUMIR)

    @property
    def prefijo(self) -> str:
        return self._prefijo

    def clave(self, limite: Limite, inquilino: Inquilino, direccion: Direccion) -> str:
        return f"{self._prefijo}:limite:{limite.nombre}:{huella_del_cubo(inquilino, direccion)}"

    async def consumir(
        self, limite: Limite, inquilino: Inquilino, direccion: Direccion
    ) -> Veredicto:
        """Consume una unidad del cubo del par `(inquilino, direccion)`.

        No captura errores de Redis a proposito: si el estado compartido no
        responde, la peticion muere en vez de pasar sin contar. Un limitador que
        se abre cuando su almacen falla es un limitador que se abre justo cuando
        mas trafico hay (`feedback_fail_open_traga_al_guard`).
        """
        clave = self.clave(limite, inquilino, direccion)
        consumido, restante_ms = await self._guion(
            keys=[clave], args=[limite.ventana_segundos * 1000]
        )
        consumido = int(consumido)
        return Veredicto(
            permitido=consumido <= limite.cuota,
            consumido=consumido,
            cuota=limite.cuota,
            milisegundos_para_reintentar=max(0, int(restante_ms)),
        )
