# Documentación de Heraldo

> **RF-28** — la documentación del producto vive **en este repositorio**,
> versionada con el código que describe. Si cambias el comportamiento y no
> cambias el documento en el mismo commit, el documento ya está mintiendo.

## Qué hay aquí

| Documento | Qué contiene |
|---|---|
| [`heraldo-problemas.md`](heraldo-problemas.md) | Registro rodante de problemas: síntoma → causa raíz → solución → prevención. Es el insumo del lazo de ajuste (T-116) |
| [`plantilla-registro.md`](plantilla-registro.md) | El bloque que se copia para abrir una entrada, los tres estados de curaduría y un ejemplo |

## Qué vive fuera de aquí, y por qué

Los **documentos que gobiernan la construcción** —especificación, plan y
troceo en tareas— viven en la bóveda de AetherLogik, no en este repositorio.
No es incoherencia con RF-28: son el contrato **previo** al código, los revisa
un gate documental propio (`scripts/heraldo_doctor.py`, decisión D-18) y sus
insumos son otras notas de la bóveda que un *check* de CI no puede leer.

Lo que sí vive aquí es la documentación **del producto**: cómo se opera, cómo
se despliega, qué garantiza y qué no, y el registro de problemas.

## Convenciones

- **Español neutro con "tú".** Nada de voseo.
- **Lenguaje no técnico** en todo lo que pueda leer un cliente.
- **Cero credenciales.** Ni de la agencia, ni de un cliente, ni de ejemplo con
  aspecto de real: `[API_KEY]`.
