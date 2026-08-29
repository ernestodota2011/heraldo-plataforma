"""T-010/T-014 (RF-27) — la proteccion que IMPIDE la fusion con el CI en rojo.

RF-27 no dice «avisar» ni «marcar»: dice **impedir la fusion**. Un CI que se
pone rojo y deja fusionar igual no cumple el requisito — cumple su apariencia.
Este guion aplica, sobre la rama principal, la unica forma que GitHub tiene de
impedirla de verdad: comprobaciones de estado OBLIGATORIAS, y la administracion
INCLUIDA en la regla.

WHY (por que `enforce_admins` no es un detalle): el dueno del repositorio es
administrador. Una proteccion que exime a la administracion deja el mecanismo en
manos de que esa persona se acuerde de no pulsar «Merge» — es decir, lo devuelve
a la disciplina, que es exactamente lo que RF-27 existe para sacar del medio. Con
`enforce_admins` la regla vale para todos o no vale.

WHY (por que las comprobaciones se DERIVAN y no se escriben): una lista escrita a
mano envejece en silencio. Si alguien renombra el trabajo del flujo, la regla
sigue nombrando un contexto que ya no existe y GitHub espera para siempre una
comprobacion que nunca llegara — o, peor, la regla queda sin ninguna comprobacion
viva, protegiendo NADA mientras aparenta proteger. Aqui los contextos salen de
`.github/workflows/ci.yml`, y si la derivacion sale vacia el guion se niega a
escribir (`feedback_denylist_por_allowlist`, `feedback_mecanismo_cableado_a_uno`).

WHY (por que se RELEE lo aplicado): una respuesta 200 dice que la peticion se
acepto, no que el estado quedara como se pidio. Se relee y se compara campo a
campo; si algo no coincide, esto sale en rojo. Un guion de proteccion que se cree
su propia escritura es un guard que se aprueba a si mismo.

Uso:
    GITHUB_TOKEN=... uv run python deploy/proteger_rama.py             # aplica
    GITHUB_TOKEN=... uv run python deploy/proteger_rama.py --verificar # solo lee

El token se toma del ENTORNO y jamas de `argv`: la linea de comandos es visible
para cualquier proceso de la maquina y queda en el historial
(`feedback_secreto_por_argv`). Nunca se imprime, ni siquiera truncado.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
FLUJO = RAIZ / ".github" / "workflows" / "ci.yml"

REPOSITORIO = "ernestodota2011/heraldo"
RAMA = "main"

_API = "https://api.github.com"

class DerivacionVacia(RuntimeError):
    """No se pudo derivar ni un contexto: escribir aqui protegeria nada."""


class NombreNoDerivable(RuntimeError):
    """El nombre real de la comprobacion no se sabe hasta que GitHub la ejecuta."""


def contextos_exigidos(texto: str) -> list[str]:
    """Los nombres de comprobacion que GitHub reportara para este flujo.

    GitHub nombra la comprobacion con el `name:` del trabajo si esta declarado, y
    con la CLAVE del trabajo si no lo esta. Se lee solo el mapa `jobs`: cualquier
    otra cosa del archivo (el nombre del flujo, los pasos) no es una comprobacion
    y no puede acabar en la regla.

    WHY (por que un lector de YAML de verdad y no expresiones regulares): la
    primera version leia el archivo por lineas, y lo levanto Crisol. Verificado
    por efecto: `name: Suite  # el nombre visible` daba el contexto
    `'Suite  # el nombre visible'`. Un contexto que GitHub no reporta jamas deja
    la regla esperando por siempre una comprobacion que no llega — es decir, el
    mecanismo cayendo exactamente en el fallo mudo que existe para evitar. YAML
    tiene comentarios, comillas, escalares de bloque y mapas en linea; leerlo con
    patrones es acertar hasta que alguien escribe YAML valido.

    WHY (por que se NIEGA ante una matriz o un nombre dinamico): con
    `strategy.matrix` GitHub publica una comprobacion por combinacion
    (`prueba (3.12)`), y con `name: py-${{ matrix.version }}` el nombre no existe
    hasta que se evalua. En los dos casos el nombre concreto no se puede derivar
    de este archivo. Emitir el nombre generico seria peor que no escribir:
    bloquearia la rama para siempre esperando algo que nunca llega. Se prefiere
    parar y decirlo.
    """
    try:
        documento = yaml.safe_load(texto)
    except yaml.YAMLError as error:
        raise DerivacionVacia(
            f"{FLUJO} no se pudo leer como YAML ({error.__class__.__name__}): no se "
            "escribe una proteccion a partir de un flujo que no se entiende"
        ) from None

    trabajos = documento.get("jobs") if isinstance(documento, dict) else None
    if not isinstance(trabajos, dict) or not trabajos:
        raise DerivacionVacia(
            f"no se derivo ni un contexto de {FLUJO}. Aplicar la proteccion con la "
            "lista vacia dejaria la rama con una regla que no exige NADA y aparenta "
            "exigir: se prefiere no escribir"
        )

    contextos: list[str] = []
    for clave, cuerpo in trabajos.items():
        detalle = cuerpo if isinstance(cuerpo, dict) else {}
        estrategia = detalle.get("strategy")
        if isinstance(estrategia, dict) and estrategia.get("matrix"):
            raise NombreNoDerivable(
                f"el trabajo {clave!r} usa `strategy.matrix`: GitHub publicara una "
                "comprobacion por combinacion y su nombre concreto no esta en este "
                "archivo. Exigir el nombre generico bloquearia la rama para siempre"
            )
        nombre = str(detalle.get("name") or clave)
        if "${{" in nombre:
            raise NombreNoDerivable(
                f"el nombre del trabajo {clave!r} es una expresion ({nombre!r}): no "
                "existe hasta que GitHub la evalua, y exigirlo literal dejaria la "
                "rama esperando una comprobacion que nunca llega"
            )
        contextos.append(nombre)
    return contextos


def destino(ruta: str) -> str:
    """La URL a abrir, con el esquema y el host comprobados ANTES de abrir nada.

    WHY: este producto nace de auditar un referente cuyo defecto confirmado por
    efecto era un SSRF (`Heraldo-00-Auditoria-OpenLivery`). Que hoy `ruta` la
    componga este mismo archivo no es una defensa: es una propiedad de la version
    de hoy. La comprobacion va delante de la del token a proposito, para que se
    pueda medir sin credencial ninguna.

    Ojo con la forma que parece bien y no lo esta: `https://api.github.com@otro`
    tiene el host correcto como NOMBRE DE USUARIO y sale hacia `otro`. Por eso se
    exige la barra que cierra el host, y no solo el prefijo del dominio.
    """
    url = f"{_API}{ruta}"
    if not url.startswith(f"{_API}/"):
        raise SystemExit(
            f"destino no permitido: {url!r}. Solo se abre el API de GitHub por https"
        )
    return url


def _peticion(metodo: str, ruta: str, cuerpo: dict | None = None) -> dict:
    url = destino(ruta)
    ficha = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not ficha:
        raise SystemExit(
            "falta GITHUB_TOKEN (o GH_TOKEN) en el entorno. No se acepta por "
            "argumento: la linea de comandos la ve cualquier proceso de la maquina "
            "y queda en el historial"
        )
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    # noqa justificado: el esquema y el host los verifica `destino()` justo arriba.
    pedido = urllib.request.Request(url, data=datos, method=metodo)  # noqa: S310
    pedido.add_header("Authorization", f"Bearer {ficha}")
    pedido.add_header("Accept", "application/vnd.github+json")
    pedido.add_header("X-GitHub-Api-Version", "2022-11-28")
    if datos is not None:
        pedido.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(pedido, timeout=30) as respuesta:  # noqa: S310
            crudo = respuesta.read().decode("utf-8")
            return json.loads(crudo) if crudo else {}
    except json.JSONDecodeError:
        # WHY: GitHub contesto algo que no es JSON — un portal cautivo, un proxy,
        # una pagina de error. Sin esto salia un rastreo crudo que parece un fallo
        # del guion cuando el problema esta en la red.
        raise SystemExit(
            f"GitHub respondio algo que no es JSON en {metodo} {ruta}: probablemente "
            "hay un proxy o un portal por el medio"
        ) from None
    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", "replace")[:400]
        if error.code == 403 and "Upgrade to GitHub Pro" in detalle:
            raise SystemExit(
                "GitHub RECHAZA la proteccion de rama por PLAN, no por permisos:\n"
                f"  {detalle.strip()}\n\n"
                "Un repositorio PRIVADO de una cuenta gratuita no admite proteccion "
                "de rama ni rulesets. Las dos unicas puertas las nombra el propio "
                "mensaje: contratar GitHub Pro, o hacer publico el repositorio.\n"
                "Mientras tanto RF-27 NO se cumple: el CI se pone rojo y la fusion "
                "sigue permitida. La casilla se queda abierta — no hay atajo que la "
                "cierre sin mentir."
            ) from None
        raise SystemExit(
            f"GitHub respondio {error.code} a {metodo} {ruta}: {detalle}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # WHY: un fallo de TRANSPORTE (sin red, DNS caido, TLS, tiempo agotado) no
        # es una respuesta HTTP y no lo veia el bloque de arriba. Salia un rastreo
        # crudo, que se lee como «el guion esta roto» cuando lo que falta es la
        # red. Se nombra el metodo y la ruta; jamas la ficha.
        raise SystemExit(
            f"no se pudo hablar con GitHub en {metodo} {ruta} "
            f"({error.__class__.__name__}): {error}"
        ) from None


def aplicar(contextos: list[str]) -> dict:
    cuerpo = {
        "required_status_checks": {"strict": True, "contexts": contextos},
        # WHY: la regla vale tambien para quien administra el repositorio.
        "enforce_admins": True,
        # WHY: en un repositorio de una CUENTA (no de una organizacion) GitHub
        # solo acepta `null` aqui. Exigir ademas revision humana dejaria al unico
        # desarrollador sin poder fusionar nada: RF-27 pide que decida el CI.
        "required_pull_request_reviews": None,
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    return _peticion("PUT", f"/repos/{REPOSITORIO}/branches/{RAMA}/protection", cuerpo)


def divergencias(vivo: dict, contextos: list[str]) -> list[str]:
    """Compara lo APLICADO contra lo que se pidio. Vacio = coinciden.

    Se separa de la llamada de red a proposito: asi la comparacion —que es donde
    vive el juicio— se puede medir sin tocar GitHub.
    """
    fallos: list[str] = []

    comprobaciones = vivo.get("required_status_checks") or {}
    aplicados = sorted(comprobaciones.get("contexts") or [])
    if aplicados != sorted(contextos):
        fallos.append(f"contextos exigidos: se pidio {sorted(contextos)} y hay {aplicados}")
    if not comprobaciones.get("strict"):
        fallos.append("`strict` esta apagado: una rama desactualizada podria fusionarse")
    if not (vivo.get("enforce_admins") or {}).get("enabled"):
        fallos.append(
            "`enforce_admins` esta apagado: la administracion puede fusionar en rojo, "
            "que es justo lo que RF-27 prohibe"
        )
    if (vivo.get("allow_force_pushes") or {}).get("enabled"):
        fallos.append("`allow_force_pushes` esta encendido: se puede reescribir la rama")
    if (vivo.get("allow_deletions") or {}).get("enabled"):
        fallos.append("`allow_deletions` esta encendido: se puede borrar la rama")
    return fallos


def verificar(contextos: list[str]) -> list[str]:
    vivo = _peticion("GET", f"/repos/{REPOSITORIO}/branches/{RAMA}/protection")
    return divergencias(vivo, contextos)


def main() -> int:
    analizador = argparse.ArgumentParser(description="Proteccion de rama (RF-27)")
    analizador.add_argument(
        "--verificar",
        action="store_true",
        help="solo relee y compara; no escribe nada",
    )
    argumentos = analizador.parse_args()

    contextos = contextos_exigidos(FLUJO.read_text(encoding="utf-8"))
    print(f"contextos derivados de {FLUJO.name}: {contextos}")

    if not argumentos.verificar:
        aplicar(contextos)
        print(f"proteccion aplicada sobre {REPOSITORIO}@{RAMA}")

    fallos = verificar(contextos)
    if fallos:
        print("\nLo aplicado NO coincide con lo pedido:", file=sys.stderr)
        for fallo in fallos:
            print(f"  - {fallo}", file=sys.stderr)
        return 1
    print("releido y verificado: la rama impide la fusion con las comprobaciones en rojo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
