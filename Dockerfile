# syntax=docker/dockerfile:1
#
# T-023 (RNF-07) — imagen unica del workspace de Heraldo.
#
# WHY (una imagen, no dos): `heraldo-api`, `heraldo-worker` y `heraldo-egress`
# son miembros del MISMO workspace de uv (raiz `pyproject.toml`), y el worker
# declara `heraldo-api` como dependencia (P-29, deuda declarada: la direccion
# esta al reves, pero es la que existe hoy). Separar en dos imagenes obligaria a
# instalar heraldo-api DENTRO de la imagen del worker de todos modos. Una sola
# imagen con el workspace completo instalado, y el SERVICIO lo decide el CMD que
# compose le pasa — no dos Dockerfiles casi identicos que puedan divergir.
#
# WHY (sin build-essential): tanto `psycopg[binary]` como `cryptography` traen
# ruedas (wheels) precompiladas para linux/amd64 en PyPI. No hace falta compilar
# nada, asi que no hace falta el compilador — imagen mas chica y build mas
# rapido (relevante porque la regla de la casa es construir FUERA del servidor y
# transferir ya construida, nunca un build pesado compartiendo el NVMe de
# produccion).
FROM python:3.12-slim-trixie

# uv, copiado del binario oficial (no se instala via pip: mas rapido y sin
# arrastrar su propia cadena de dependencias a la imagen final).
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/

WORKDIR /srv/heraldo

# Metadata primero (cache de capas): un cambio en el codigo de la app no invalida
# la capa de `uv sync` mientras el lockfile no cambie.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/egress/pyproject.toml packages/egress/pyproject.toml

# El codigo real. Solo lo que el runtime necesita — ni tests, ni docs, ni el
# frontend (`apps/web`, Next.js, no forma parte de esta imagen).
COPY apps/api/app apps/api/app
COPY apps/api/migrations apps/api/migrations
COPY apps/worker apps/worker
COPY packages/egress packages/egress
COPY deploy deploy

# --frozen: el lockfile manda, nunca se re-resuelve en el build.
# --no-dev: pytest/ruff/httpx-de-pruebas no viajan a produccion.
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/srv/heraldo/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# WHY (no declara un CMD de producto): esta imagen sirve a DOS roles distintos
# (la migracion de un solo tiro y la API de larga duracion) mas el worker el dia
# que P-28 se resuelva. El CMD lo declara cada servicio en docker-compose.yml,
# para que la imagen sea neutral y compose sea el UNICO sitio que decide que rol
# corre cada contenedor.
