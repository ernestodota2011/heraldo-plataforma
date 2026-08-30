"""Gate de PUBLICABILIDAD: este repositorio es publico, y se escribe como tal.

Nace de un defecto real (P-40): el repositorio se escribio durante meses dando por
hecho que seria privado para siempre. Cuando hubo que hacerlo publico, el arbol
nombraba a un cliente sanitario 19 veces y lo describia como cliente bajo BAA
—informacion de un tercero que no es nuestra para contarla— y el nombre estaba
ademas en cinco MENSAJES DE COMMIT, que no se pueden sanear sin reescribir la
historia y destruir la cadena de evidencia. Se descubrio el dia de publicar, que
es el peor dia posible para descubrirlo.

La leccion no es «revisar antes de publicar». Es que ==un repositorio puede
cambiar de privado a publico en cualquier momento, asi que se escribe desde el
primer commit como si ya lo fuera==. Y eso no se sostiene con disciplina: se
sostiene con un gate que se pone rojo.

WHY (por que la lista va en HASH y no en claro): un guard que lleva escritos los
nombres que vigila ES la filtracion que intenta evitar. Aqui se guarda el sha256
de cada termino normalizado, nunca el termino. Honestamente: con el hash se puede
CONFIRMAR un nombre que ya se sospecha, no DESCUBRIRLO — que es exactamente la
diferencia entre publicar una lista de clientes y no publicarla.

WHY (por que tambien se miran los mensajes de commit): es donde estaba la mitad
del problema y la unica mitad que no tiene arreglo barato. Un archivo se corrige
con un commit; un mensaje solo con una reescritura de historia.

Uso:
    python scripts/publicable.py                 # arbol versionado
    python scripts/publicable.py --desde <sha>   # + mensajes de commit del rango
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

#: sha256 de cada termino NORMALIZADO, recortado a 16. Para anadir uno:
#: `python scripts/publicable.py --huella "<termino>"` y pega el resultado aqui
#: con un comentario que diga de QUE clase es, nunca cual es.
#: ==Mantenerla es parte de dar de alta un cliente o un servidor.== Si esta lista
#: se queda vieja el gate no falla: deja de cubrir, que es peor.
TERMINOS_PROHIBIDOS: dict[str, str] = {
    "712611e49761bc1e": "cliente bajo BAA (sanidad)",
    "d70dcf518fad48b1": "cliente bajo BAA (sanidad)",
    "fe108ac067733c37": "cliente bajo BAA (sanidad)",
    "455697873d0fc67f": "cliente bajo BAA (sanidad)",
    "e1c8b877326c7dbf": "cliente bajo BAA (sanidad)",
    "76a54959e303d1c2": "cliente de la agencia",
    "9e73ec93dd05c1a4": "cliente de la agencia",
    "86d625ecfd684082": "cliente de la agencia",
    "b0d5183a056730a3": "producto interno o de cliente",
    "752115f45427e30c": "producto interno o de cliente",
    "e4049a794e20069a": "producto interno o de cliente",
    "68e0daa9a364df38": "producto interno o de cliente",
    "82c0bd3ed3670959": "marca propia de la agencia",
    "369fa7644a75ecc2": "marca propia de la agencia",
    "05fe312c7ff831ff": "marca propia de la agencia",
    "6f8d3754f72a7553": "servidor propio",
    "b2300159a94542b0": "servidor propio",
    "35441aa5b8b76563": "servidor propio",
    "788e892000eb8835": "servidor propio",
    "d48f3e8ffa529ca7": "servidor propio",
    "74460bb4cfb427c0": "servidor propio",
}

#: Formas que no necesitan una lista porque se reconocen por su ESTRUCTURA.
#: Una lista de nombres envejece; una regla no (`feedback_denylist_por_allowlist`).
ESTRUCTURALES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Direcciones de la red interna. Las de `test_superficie.py` son fixtures del
    # guard anti-SSRF y se exentan por ruta, mas abajo.
    (
        "IP privada RFC1918",
        re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("clave privada PEM", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("ficha de GitHub", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    # WHY del `(?![A-Z_]{3,}@)`: un ejemplo de uso escribe la clave como MARCADOR en
    # mayusculas (`:CLAVE@`), y sin esta salvedad el gate se pone rojo sobre la
    # documentacion — que es la forma mas rapida de que alguien lo apague. La
    # salvedad es estrecha a proposito: solo el segmento ENTERO en mayusculas.
    # Asume que nadie usa una clave real toda en mayusculas; si alguien lo hiciera,
    # el problema seria la clave, no este gate.
    (
        "DSN con credencial",
        # El `(?!\$)` deja pasar `:${VARIABLE}@` y `:$VAR@`: una referencia de
        # entorno no es una credencial — es justo lo contrario, es la forma
        # correcta de NO escribirla. `docker-compose.yml` las usa.
        re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s'\"]+:(?!\$)(?![A-Z_]{3,}@)[^@\s'\"]+@"),
    ),
)

#: Rutas donde una forma estructural es el CASO DE PRUEBA y no una fuga. Cada una
#: con su motivo: una exencion sin motivo escrito es un agujero con permiso.
EXENTAS: dict[str, str] = {
    "apps/api/tests/test_superficie.py": (
        "las IP privadas son fixtures del guard anti-SSRF: la prueba exige que se "
        "RECHACEN como origen de produccion"
    ),
    "scripts/publicable.py": (
        "este archivo lleva escritos los patrones estructurales que busca; casarian "
        "consigo mismos"
    ),
    "apps/api/tests/test_egreso_red.py": (
        "es la bateria del guard anti-SSRF (T-300): nombra los tres PREFIJOS de RFC 1918 "
        "para derivar de ellos los casos que el guard debe rechazar. Solo prefijos: las "
        "direcciones concretas se derivan y ninguna describe una red real, despues de que "
        "este gate rechazara la primera version por llevar dos de la agencia (P-41). "
        "La exencion levanta las formas ESTRUCTURALES; los terminos vigilados siguen "
        "poniendo el archivo en rojo"
    ),
    "apps/api/tests/test_publicable.py": (
        "la bateria de este gate necesita muestras que disparen cada patron"
    ),
}

_PALABRA = re.compile(r"[A-Za-zÀ-ɏ][A-Za-z0-9_À-ɏ-]{2,}")


def normalizar(termino: str) -> str:
    """Minusculas y sin acentos: «Policlínico» y «policlinico» son la misma palabra."""
    plano = unicodedata.normalize("NFKD", termino.strip().lower())
    return "".join(c for c in plano if not unicodedata.combining(c))


def huella(termino: str) -> str:
    return hashlib.sha256(normalizar(termino).encode("utf-8")).hexdigest()[:16]


class GitNoEncontrado(RuntimeError):
    """Sin `git` no hay nada que medir, y eso se dice en vez de medir cero."""


def _git() -> str:
    """`git` resuelto a ruta ABSOLUTA — el mismo patron que `deploy/restaurar_inquilino.py`.

    # WHY: no es para aplacar al linter. Un paso de CI o un cron no heredan el
    # PATH de la terminal de quien escribio esto, y con un PATH distinto se puede
    # acabar ejecutando otro binario con el mismo nombre.
    """
    ruta = shutil.which("git")
    if ruta is None:
        raise GitNoEncontrado("`git` no esta en el PATH de este proceso: el gate no puede medir")
    return ruta


def _archivos_versionados() -> list[str]:
    salida = subprocess.run(  # noqa: S603 (argv fijo + ruta absoluta resuelta arriba)
        [_git(), "ls-files"], cwd=RAIZ, capture_output=True, text=True, check=True
    ).stdout
    return [linea for linea in salida.splitlines() if linea.strip()]


def revisar_texto(texto: str, origen: str, exento: bool = False) -> list[str]:
    """Devuelve las faltas. Nombra el ORIGEN y la clase, jamas el termino.

    WHY: el informe de este gate acaba en un log de CI publico. Si dijera cual es
    la palabra encontrada, el propio aviso publicaria lo que el gate protege.
    """
    faltas: list[str] = []
    for numero, linea in enumerate(texto.splitlines(), 1):
        for palabra in _PALABRA.findall(linea):
            clase = TERMINOS_PROHIBIDOS.get(huella(palabra))
            if clase is not None:
                faltas.append(f"{origen}:{numero}: termino prohibido — clase: {clase}")
        if exento:
            continue
        for nombre, patron in ESTRUCTURALES:
            if patron.search(linea):
                faltas.append(f"{origen}:{numero}: {nombre}")
    return faltas


def revisar_arbol() -> list[str]:
    faltas: list[str] = []
    for ruta in _archivos_versionados():
        try:
            texto = (RAIZ / ruta).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, OSError):
            continue  # binario o ausente: no hay texto que revisar
        faltas.extend(revisar_texto(texto, ruta, exento=ruta in EXENTAS))
    return faltas


def revisar_mensajes(desde: str) -> list[str]:
    """Los mensajes del rango. Es la mitad del problema que no tiene arreglo barato."""
    salida = subprocess.run(  # noqa: S603 (argv fijo + ruta absoluta resuelta arriba)
        [_git(), "log", "--format=%H%x00%B%x00", f"{desde}..HEAD"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if salida.returncode != 0:
        return [f"no se pudo leer el rango {desde}..HEAD: el gate no midio NADA"]
    faltas: list[str] = []
    piezas = salida.stdout.split("\0")
    for indice in range(0, len(piezas) - 1, 2):
        sha, mensaje = piezas[indice].strip(), piezas[indice + 1]
        if sha:
            faltas.extend(revisar_texto(mensaje, f"mensaje de commit {sha[:8]}"))
    return faltas


def main() -> int:
    analizador = argparse.ArgumentParser(description="Gate de publicabilidad")
    analizador.add_argument("--desde", help="sha base: revisa tambien los mensajes de commit")
    analizador.add_argument("--huella", help="imprime la huella de un termino y sale")
    argumentos = analizador.parse_args()

    if argumentos.huella:
        print(huella(argumentos.huella))
        return 0

    # Control: con el conjunto vacio todo lo de abajo pasaria sin haber mirado
    # nada — un gate que se aprueba por ausencia.
    archivos = _archivos_versionados()
    if not archivos:
        print("el gate no encontro ningun archivo versionado que revisar", file=sys.stderr)
        return 1

    faltas = revisar_arbol()
    if argumentos.desde:
        faltas.extend(revisar_mensajes(argumentos.desde))

    if faltas:
        print(f"GATE DE PUBLICABILIDAD EN ROJO — {len(faltas)} falta(s):", file=sys.stderr)
        for falta in faltas:
            print(f"  {falta}", file=sys.stderr)
        print(
            "\nEste repositorio es PUBLICO. Nada de lo de arriba puede vivir aqui: ni el "
            "nombre de un cliente, ni el de un servidor propio, ni una direccion de la red "
            "interna, ni una credencial. Corrigelo en el archivo; si esta en un MENSAJE de "
            "commit, rehaz el commit ANTES de empujar — despues solo se arregla "
            "reescribiendo la historia.",
            file=sys.stderr,
        )
        return 1

    print(f"gate de publicabilidad: {len(archivos)} archivos revisados, limpio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
