"""T-014·quater (RNF-03) — ningun estado compartido vive en memoria de proceso.

RNF-03 es la promesa que sostiene tres piezas a la vez: el limite de uso, la
idempotencia y la sesion. Las tres dejan de cumplirse de la misma forma si su
estado vive dentro del proceso —y es ==el defecto 6 del referente==, medido: un
techo de 60/min con cuatro workers son 240/min, y nadie se entera porque cada
proceso cumple su cuenta.

# WHY (por que esta bateria existe, teniendo ya la de `test_limites.py` y la de
# `test_sesion_revocacion.py`): aquellas miden con dos CLIENTES de Redis dentro de
# UN interprete. Quien las construyo lo dijo sin adornos: «"dos procesos" son dos
# conexiones Redis independientes en un proceso: mide el estado compartido, **no**
# mide GIL, ni pool bajo carga, ni reconexion». Dos objetos en un proceso comparten
# memoria, asi que ==un `dict` de modulo tambien saldria verde ahi==. Aqui hay dos
# procesos del sistema operativo de verdad (`instancia_de_api.py`, arrancado dos
# veces con `subprocess`), y la primera sonda comprueba justamente eso: que los PID
# son distintos entre si y distintos del de la suite.
#
# # WHY (cada sonda con su CONTROL de una sola instancia): sin el, «pasa con dos»
# no distingue «comparten estado» de «ninguna de las dos hizo nada»
# (`feedback_prueba_sin_control`). El control corre la MISMA secuencia contra UNA
# instancia y exige el MISMO resultado.
#
# # WHY (y cada sonda con su SABOTAJE): la expectativa de cada pieza vive en una
# funcion —`_exigir_*`— que usan las dos sondas. La sonda real se la aplica a dos
# instancias sanas; la del sabotaje se la aplica a dos instancias cuya pieza se
# movio a un `dict` de modulo, y exige que la MISMA funcion levante
# `AssertionError`. No es «parece que mide»: es la sonda poniendose roja, dentro de
# la suite, cada vez que corre (`feedback_sabotaje_audita_al_test`).
#
# ==Lo que esta bateria NO mide, declarado: la pila HTTP.== Ninguna de las tres
# piezas tiene ruta todavia, asi que lo que corre en cada proceso es el modulo de
# produccion, no un `uvicorn` atendiendo. Tampoco mide carga: son dos instancias en
# una maquina, no un cluster. Lo que si mide, y no medía nadie antes, es que el
# estado esta FUERA del proceso.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from app.channels.idempotency import clave_de
from conftest import AGENCIA_A, CLIENTE_A1, RAIZ, resembrar, sesion_de_cliente

#: El programa que cada proceso hijo ejecuta.
PROGRAMA_DE_INSTANCIA = Path(__file__).with_name("instancia_de_api.py")

#: Plazo para cada respuesta. Generoso: el arranque de un hijo importa sqlalchemy,
#: psycopg y redis. Si se agota, la instancia se mata y el reproche trae su salida
#: de error — un cuelgue mudo no dice nada.
PLAZO_SEGUNDOS = 90.0

#: Cuota comoda de agotar: dos consumos y se cierra.
LIMITE = {"limite": "mensajes", "cuota": 2, "ventana": 60}
DIRECCION = "203.0.113.10"

INQUILINO = {"agencia_id": str(AGENCIA_A), "cliente_id": str(CLIENTE_A1)}

CANAL = "sonda-multiproceso"


class InstanciaCaida(RuntimeError):
    """El proceso hijo no contesto. Lleva su salida de error dentro."""


@dataclass
class Instancia:
    """El asa a un proceso hijo: se le escribe una orden, se lee su respuesta."""

    nombre: str
    proceso: subprocess.Popen
    respuestas: queue.Queue = field(default_factory=queue.Queue)
    errores: Path | None = None

    def __post_init__(self) -> None:
        # WHY (un hilo lector y no `readline()` directo): sin el, una instancia que
        # muere deja al padre bloqueado para siempre en la lectura. Con la cola, la
        # espera tiene plazo y el fallo se puede contar.
        def _leer() -> None:
            for linea in self.proceso.stdout:  # type: ignore[union-attr]
                self.respuestas.put(linea)
            self.respuestas.put(None)

        self._hilo = threading.Thread(target=_leer, daemon=True)
        self._hilo.start()

    @property
    def pid(self) -> int:
        return self.proceso.pid

    def enviar(self, **orden) -> None:
        self.proceso.stdin.write(json.dumps(orden) + "\n")  # type: ignore[union-attr]
        self.proceso.stdin.flush()  # type: ignore[union-attr]

    def recibir(self, plazo: float = PLAZO_SEGUNDOS) -> dict:
        try:
            linea = self.respuestas.get(timeout=plazo)
        except queue.Empty as vacio:
            raise InstanciaCaida(
                f"la instancia {self.nombre!r} (pid {self.pid}) no contesto en {plazo}s."
                f"{self._salida_de_error()}"
            ) from vacio
        if linea is None:
            raise InstanciaCaida(
                f"la instancia {self.nombre!r} (pid {self.pid}) cerro su salida sin "
                f"contestar: se murio.{self._salida_de_error()}"
            )
        respuesta = json.loads(linea)
        assert respuesta.get("ok") is True, (
            f"la instancia {self.nombre!r} devolvio un error: {respuesta.get('error')!r}"
        )
        return respuesta

    def pedir(self, **orden) -> dict:
        self.enviar(**orden)
        return self.recibir()

    def _salida_de_error(self) -> str:
        if self.errores is None or not self.errores.is_file():
            return ""
        texto = self.errores.read_text(encoding="utf-8", errors="replace").strip()
        return f"\n--- salida de error de {self.nombre} ---\n{texto}" if texto else ""

    def cerrar(self) -> None:
        # La limpieza intenta TODOS los pasos aunque uno falle, igual que el
        # `escenario` de Postgres: abandonar al primer error deja a medias justo lo
        # que vino a cerrar.
        try:
            if self.proceso.poll() is None:
                self.enviar(orden="fin")
                self.proceso.wait(timeout=15)
        except Exception as fallo:  # noqa: BLE001 - se reporta y se sigue
            print(f"aviso: fallo pidiendo el cierre de {self.nombre}: {fallo!r}")
        finally:
            if self.proceso.poll() is None:
                self.proceso.kill()
                self.proceso.wait(timeout=15)
            for canal in (self.proceso.stdin, self.proceso.stdout):
                try:
                    if canal is not None:
                        canal.close()
                except Exception as fallo:  # noqa: BLE001 - se reporta y se sigue
                    print(f"aviso: fallo cerrando un canal de {self.nombre}: {fallo!r}")


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Las instancias ESCRIBEN en `mensajes_entrantes`: cada sonda parte del mismo sitio."""
    resembrar(motor_de_siembra)


@pytest.fixture(scope="module", autouse=True)
def _escenario_devuelto_al_salir(motor_de_siembra):
    yield
    resembrar(motor_de_siembra)


@pytest.fixture
async def levantar(escenario, dsn_redis, banco_redis, tmp_path):
    """Fabrica de instancias. Cada llamada arranca UN proceso del sistema operativo."""
    creadas: list[Instancia] = []

    def _levantar(nombre: str, *, sabotaje: str | None = None) -> Instancia:
        entorno = dict(os.environ)
        entorno["HERALDO_DATABASE_URL"] = escenario.dsn_app
        entorno["HERALDO_REDIS_URL"] = dsn_redis
        entorno["HERALDO_PREFIJO_DE_PRUEBA"] = banco_redis.prefijo
        if sabotaje is None:
            entorno.pop("HERALDO_SABOTAJE_MEMORIA", None)
        else:
            entorno["HERALDO_SABOTAJE_MEMORIA"] = sabotaje

        errores = tmp_path / f"errores-{nombre}.txt"
        # S603: el ejecutable es el interprete de esta misma suite y el argumento es
        # una ruta derivada de `__file__`. Aqui no entra nada de fuera.
        proceso = subprocess.Popen(  # noqa: S603
            [sys.executable, str(PROGRAMA_DE_INSTANCIA)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errores.open("w", encoding="utf-8"),
            env=entorno,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        instancia = Instancia(nombre=nombre, proceso=proceso, errores=errores)
        creadas.append(instancia)
        return instancia

    try:
        yield _levantar
    finally:
        for instancia in creadas:
            instancia.cerrar()


def _dos(levantar, *, sabotaje: str | None = None) -> tuple[Instancia, Instancia]:
    return levantar("a", sabotaje=sabotaje), levantar("b", sabotaje=sabotaje)


# ==========================================================================
# Sonda 0 — ¿de verdad son dos procesos?
# ==========================================================================
def test_las_dos_instancias_son_procesos_distintos_del_sistema_operativo(levantar) -> None:
    """El control de TODA esta bateria: si fueran dos objetos, no mediria nada.

    # WHY: es la diferencia entre esta bateria y las que ya existian. Dos clientes
    # de Redis en un interprete comparten memoria, asi que un `dict` de modulo
    # tambien saldria verde. Dos PID distintos —y distintos del de la suite— es lo
    # que hace que la memoria de uno no sea la del otro.
    """
    a, b = _dos(levantar)
    identidad_a, identidad_b = a.pedir(orden="identidad"), b.pedir(orden="identidad")

    assert identidad_a["pid"] != identidad_b["pid"], (
        "las dos instancias declaran el mismo pid: no son dos procesos"
    )
    assert os.getpid() not in (identidad_a["pid"], identidad_b["pid"]), (
        "una instancia corre dentro del proceso de la suite: entonces comparte su "
        "memoria y esta bateria mediria dos objetos, no dos procesos"
    )
    assert (identidad_a["sabotaje"], identidad_b["sabotaje"]) == (None, None), (
        "una instancia arranco saboteada sin que la sonda lo pidiera"
    )


# ==========================================================================
# Pieza 1 — el limite de uso: la cuota es COMPARTIDA
# ==========================================================================
def _consumir(instancia: Instancia) -> dict:
    return instancia.pedir(orden="consumir", direccion=DIRECCION, **LIMITE, **INQUILINO)


def _exigir_cuota_compartida(veredictos: list[dict]) -> None:
    """La expectativa de RF-13, en UN sitio: la usan la sonda y su sabotaje.

    Tres consumos con cuota 2. Si el cubo es UNO, cuentan 1, 2 y 3, y el tercero se
    rechaza. Si cada proceso lleva el suyo, la cuota efectiva se multiplica por el
    numero de workers — el defecto 6.
    """
    contados = [v["consumido"] for v in veredictos]
    assert contados == [1, 2, 3], (
        f"los consumos se contaron {contados} y tenian que contarse [1, 2, 3]: no "
        "estan contando en el mismo sitio, asi que la cuota real se multiplica por el "
        "numero de instancias"
    )
    permitidos = [v["permitido"] for v in veredictos]
    assert permitidos == [True, True, False], (
        f"con cuota {LIMITE['cuota']} los veredictos fueron {permitidos}: el limite no "
        "cierra donde dice cerrar"
    )


def test_la_cuota_se_gasta_entre_las_dos_instancias(levantar) -> None:
    """Consumir en A gasta la de B (RF-13 / RNF-03), con dos procesos de verdad."""
    a, b = _dos(levantar)
    _exigir_cuota_compartida([_consumir(a), _consumir(b), _consumir(b)])


def test_control_con_una_sola_instancia_la_cuota_es_la_misma(levantar) -> None:
    """El control: el comportamiento no cambia por haber dos.

    Sin el, «pasa con dos» no distingue «comparten estado» de «ninguna hizo nada».
    """
    sola = levantar("sola")
    _exigir_cuota_compartida([_consumir(sola), _consumir(sola), _consumir(sola)])


def test_sabotaje_el_limite_en_memoria_del_proceso_pone_la_sonda_en_rojo(levantar) -> None:
    """Se mueve el cubo a un `dict` de modulo y se exige que la MISMA sonda falle.

    Es la unica forma de saber que la sonda de arriba discrimina. Si siguiera verde
    con el limite en memoria del proceso, no estaria midiendo dos procesos: estaria
    midiendo dos objetos.
    """
    a, b = _dos(levantar, sabotaje="limite")
    veredictos = [_consumir(a), _consumir(b), _consumir(b)]

    with pytest.raises(AssertionError):
        _exigir_cuota_compartida(veredictos)

    # Y ademas se nombra el dano concreto: la tercera peticion, que con el cubo
    # compartido se rechaza, aqui PASA. La cuota efectiva es el doble.
    assert veredictos[2]["permitido"] is True, (
        "el saboteador no reprodujo el defecto 6: si en memoria del proceso el limite "
        "tampoco se multiplicara, esta sonda no auditaria nada"
    )


# ==========================================================================
# Pieza 2 — la idempotencia: un identificador externo, UN resultado
# ==========================================================================
def _exigir_un_solo_procesamiento(respuestas: list[dict], filas: int) -> None:
    """RF-12 medido por sus DOS mitades: un solo `nuevo` y una sola fila."""
    veredictos = [r["veredicto"] for r in respuestas]
    nuevos = [v for v in veredictos if v == "nuevo"]
    assert len(nuevos) == 1, (
        f"el mismo identificador externo produjo {len(nuevos)} procesamientos "
        f"({veredictos}): un mensaje entro dos veces, y eso es un mensaje enviado dos "
        "veces a una persona real por cuenta de un cliente"
    )
    assert filas == 1, (
        f"hay {filas} filas para el mismo identificador externo: la restriccion unica "
        "—la defensa que no miente— no esta gobernando lo que entra por las dos "
        "instancias"
    )


def _contar(instancia: Instancia, id_externo: str) -> int:
    return instancia.pedir(
        orden="contar_mensajes", canal=CANAL, id_externo=id_externo, **INQUILINO
    )["filas"]


def test_el_mismo_identificador_a_la_vez_en_las_dos_instancias_produce_un_resultado(
    levantar,
) -> None:
    """A y B reciben el MISMO aviso a la vez. Sale uno, no dos."""
    a, b = _dos(levantar)
    a.pedir(orden="identidad")
    b.pedir(orden="identidad")  # las dos ya arrancaron: la barrera mide la carrera
    id_externo = f"a-la-vez-{uuid4().hex}"

    juntos = time.time() + 0.5
    a.enviar(
        orden="ingerir", canal=CANAL, id_externo=id_externo, no_antes_de=juntos, **INQUILINO
    )
    b.enviar(
        orden="ingerir", canal=CANAL, id_externo=id_externo, no_antes_de=juntos, **INQUILINO
    )
    respuestas = [a.recibir(), b.recibir()]

    _exigir_un_solo_procesamiento(respuestas, _contar(a, id_externo))


def test_control_con_una_sola_instancia_el_duplicado_tambien_se_atrapa(levantar) -> None:
    """El control: una instancia sola llega al mismo resultado."""
    sola = levantar("sola")
    id_externo = f"una-sola-{uuid4().hex}"
    respuestas = [
        sola.pedir(orden="ingerir", canal=CANAL, id_externo=id_externo, **INQUILINO),
        sola.pedir(orden="ingerir", canal=CANAL, id_externo=id_externo, **INQUILINO),
    ]
    _exigir_un_solo_procesamiento(respuestas, _contar(sola, id_externo))


async def test_con_la_marca_de_redis_borrada_lo_atrapa_la_base_desde_la_otra_instancia(
    levantar, redis
) -> None:
    """La defensa AUTORITATIVA, medida entre procesos.

    # WHY: si solo se comprobara el camino rapido, un Redis compartido bastaria para
    # pasar la sonda y la promesa de RF-12 seguiria colgando de un almacen que se
    # reinicia. Aqui se borra la marca —o sea, se simula ese reinicio— y se exige que
    # la SEGUNDA instancia siga viendo el duplicado. Quien lo atrapa entonces es la
    # restriccion unica, y eso se comprueba nombrandola.
    """
    a, b = _dos(levantar)
    id_externo = f"redis-vacio-{uuid4().hex}"

    primero = a.pedir(orden="ingerir", canal=CANAL, id_externo=id_externo, **INQUILINO)
    assert primero["veredicto"] == "nuevo"

    borradas = await redis.delete(
        clave_de(sesion_de_cliente(AGENCIA_A, CLIENTE_A1), canal=CANAL, id_externo=id_externo)
    )
    assert borradas == 1, (
        "no habia marca que borrar: el acelerador no se estaba usando y esta sonda "
        "mediria un escenario que no existe"
    )

    segundo = b.pedir(orden="ingerir", canal=CANAL, id_externo=id_externo, **INQUILINO)
    assert segundo["veredicto"] == "duplicado"
    assert segundo["guardian"] == "base", (
        f"el duplicado lo atrapo {segundo['guardian']!r} con Redis vacio: la defensa "
        "que sostiene el 100 % de la promesa es la restriccion unica"
    )
    _exigir_un_solo_procesamiento([primero, segundo], _contar(b, id_externo))


def test_sabotaje_la_idempotencia_en_memoria_del_proceso_pone_la_sonda_en_rojo(
    levantar,
) -> None:
    """Con el conjunto en memoria del proceso, el mismo aviso entra DOS veces."""
    a, b = _dos(levantar, sabotaje="idempotencia")
    a.pedir(orden="identidad")
    b.pedir(orden="identidad")
    id_externo = f"saboteado-{uuid4().hex}"

    juntos = time.time() + 0.5
    a.enviar(
        orden="ingerir", canal=CANAL, id_externo=id_externo, no_antes_de=juntos, **INQUILINO
    )
    b.enviar(
        orden="ingerir", canal=CANAL, id_externo=id_externo, no_antes_de=juntos, **INQUILINO
    )
    respuestas = [a.recibir(), b.recibir()]

    with pytest.raises(AssertionError):
        # Cero filas: el saboteador ni siquiera toca la base, que es justo lo que
        # hace una idempotencia «obvia» escrita en el proceso.
        _exigir_un_solo_procesamiento(respuestas, 0)

    assert [r["veredicto"] for r in respuestas] == ["nuevo", "nuevo"], (
        "el saboteador no reprodujo el defecto: si en memoria del proceso el mensaje "
        "tampoco entrara dos veces, esta sonda no auditaria nada"
    )


# ==========================================================================
# Pieza 3 — la sesion: revocada en A, el SIGUIENTE uso en B falla
# ==========================================================================
def _exigir_revocacion_compartida(antes: dict, borrada: bool, despues: dict) -> None:
    """RF-08 / D-05, con sus dos mitades — y la primera es la que importa.

    # WHY (el «antes» NO es decorativo): sin el, una instancia que no conociera la
    # sesion en absoluto —el caso exacto de la memoria de proceso— pasaria la sonda
    # con nota, porque «el siguiente uso falla» tambien es cierto cuando nunca valio.
    # Es verde POR AUSENCIA. Aqui se exige que valiera ANTES.
    """
    assert antes["valida"] is True, (
        "la otra instancia no reconocia la sesion ni ANTES de revocarla: «revocada» "
        "significaria «revocada en este worker», que es el defecto que D-05 descarta. "
        "Sin esta mitad, la sonda saldria verde por ausencia"
    )
    assert borrada is True, "la revocacion no borro nada: no habia sesion que revocar"
    assert despues["valida"] is False, (
        "la sesion revocada en una instancia sigue valiendo en la otra: RF-08 pide que "
        "la revocacion sea efectiva «en su siguiente uso», no solo en el worker que la "
        "atendio"
    )


def _abrir(instancia: Instancia) -> dict:
    return instancia.pedir(
        orden="abrir_sesion", agencia_id=str(AGENCIA_A), cliente_id=None, rol="operador_agencia"
    )


def test_la_sesion_revocada_en_una_instancia_deja_de_valer_en_la_otra(levantar) -> None:
    a, b = _dos(levantar)
    abierta = _abrir(a)

    antes = b.pedir(orden="usar_sesion", testigo=abierta["testigo"])
    borrada = a.pedir(orden="revocar_sesion", sesion_id=abierta["sesion_id"])["borrada"]
    despues = b.pedir(orden="usar_sesion", testigo=abierta["testigo"])

    _exigir_revocacion_compartida(antes, borrada, despues)


def test_control_con_una_sola_instancia_la_revocacion_se_comporta_igual(levantar) -> None:
    sola = levantar("sola")
    abierta = _abrir(sola)
    antes = sola.pedir(orden="usar_sesion", testigo=abierta["testigo"])
    borrada = sola.pedir(orden="revocar_sesion", sesion_id=abierta["sesion_id"])["borrada"]
    despues = sola.pedir(orden="usar_sesion", testigo=abierta["testigo"])

    _exigir_revocacion_compartida(antes, borrada, despues)


def test_sabotaje_la_sesion_en_memoria_del_proceso_pone_la_sonda_en_rojo(levantar) -> None:
    """En memoria del proceso, B no conoce la sesion que abrio A — ni antes ni despues."""
    a, b = _dos(levantar, sabotaje="sesion")
    abierta = _abrir(a)
    antes = b.pedir(orden="usar_sesion", testigo=abierta["testigo"])
    borrada = a.pedir(orden="revocar_sesion", sesion_id=abierta["sesion_id"])["borrada"]
    despues = b.pedir(orden="usar_sesion", testigo=abierta["testigo"])

    with pytest.raises(AssertionError):
        _exigir_revocacion_compartida(antes, borrada, despues)

    assert (antes["valida"], despues["valida"]) == (False, False), (
        "el saboteador no reprodujo el defecto: la sesion tiene que ser desconocida "
        "para la otra instancia, que es lo que hace que «el siguiente uso falla» sea "
        "verde por ausencia"
    )


# ==========================================================================
# El saboteador es andamiaje: en produccion NO existe
# ==========================================================================
#: Arboles donde vive el codigo de la APLICACION.
ARBOLES_DE_APLICACION = (Path("apps"), Path("packages"))
CARPETAS_QUE_NO_SON_APLICACION = ("tests", "__pycache__", ".venv", "node_modules")


def test_el_interruptor_del_sabotaje_no_existe_en_el_codigo_de_la_aplicacion() -> None:
    """Un interruptor que degrada una defensa NO puede viajar en el producto.

    # WHY: el saboteador de esta bateria hace, a proposito, exactamente lo que D-05
    # prohibe. Vive en `tests/` y solo ahi. Si alguien lo moviera al arbol de la
    # aplicacion —o copiara la idea— habria una variable de entorno capaz de
    # desactivar el estado compartido en produccion: la peor clase de escotilla,
    # porque el CI seguiria verde. Se comprueba con su propio control.
    """
    fuentes = [
        ruta
        for arbol in ARBOLES_DE_APLICACION
        for ruta in sorted((RAIZ / arbol).rglob("*.py"))
        if not any(parte in CARPETAS_QUE_NO_SON_APLICACION for parte in ruta.parts)
    ]
    assert len(fuentes) >= 5, (
        f"el guard solo encontro {len(fuentes)} fuentes de aplicacion: si la ruta se "
        "rompiera, saldria verde sin haber auditado nada"
    )
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in fuentes
        if "SABOTAJE" in ruta.read_text(encoding="utf-8").upper()
    ]
    assert not culpables, (
        f"el interruptor del saboteador aparece en el codigo de la aplicacion: "
        f"{culpables}. Ahi seria una forma soportada de apagar RNF-03 en produccion"
    )
    # Control: en el andamiaje SI esta, o el guard estaria buscando una palabra que
    # ya no usa nadie.
    assert "SABOTAJE" in PROGRAMA_DE_INSTANCIA.read_text(encoding="utf-8").upper(), (
        "el guard busca una palabra que el andamiaje ya no contiene: dejo de "
        "reconocer lo que dice vigilar"
    )
