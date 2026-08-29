# Plantilla del registro de problemas

Lo que se copia para abrir una entrada nueva en
[`heraldo-problemas.md`](heraldo-problemas.md).

> Este archivo **no** se llama `*-problemas.md` a propósito: así el barrido de
> curaduría no lo confunde con el registro y no cuenta esta plantilla como una
> entrada pendiente. Es la misma razón por la que la plantilla de la bóveda se
> llama `Plantilla-Registro-Problemas-AetherLogik.md` y no encaja en el patrón.

## Formato de entrada (copia este bloque)

```markdown
### P-NN · <título corto del problema> · AAAA-MM-DD
- **Síntoma:** qué se observó — el error o el comportamiento, citando `archivo:línea` o el registro.
- **Causa raíz:** la de verdad, no el síntoma. Si aún no se confirmó: `pendiente`.
- **Solución:** qué se hizo + evidencia (commit corto · prueba que lo fija · medición con su control).
- **Prevención propuesta:** qué regla, prueba o guard habría evitado esto (o "ninguna clara — que lo juzgue el curador").
- **Caso nuevo en el banco:** `apps/api/tests/banco/<archivo>` (T-116) — o `no aplica`, con el motivo.
- **Clase:** codigo | proceso | harness | infra | doc
- estado-curaduria: PENDIENTE
```

Cuando el curador procesa la entrada, sustituye la última línea por:

`estado-curaduria: CURADA (AAAA-MM-DD · <acción: prueba añadida, skill editada y cableada a X, memoria>)`

Tercer estado, cuando la prevención necesita aprobación de Ernesto (gobierno,
harness protegido, borrar algo):

`estado-curaduria: EN-PROPUESTA (AAAA-MM-DD · ver Cola-de-Ernesto)` — sale del
inventario pendiente sin fingirse curada; al aprobarse y aplicarse pasa a
`CURADA`.

## Ejemplo de referencia (no es una entrada real de Heraldo)

```markdown
### P-00 · El retry caía fuera de la ventana de envío · 2026-07-31
- **Síntoma:** el comprador de madrugada nunca recibía respuesta; los 3 reintentos se agotaban antes de abrir la ventana.
- **Causa raíz:** el backoff del reintento y la ventana horaria estaban anti-correlados: si el primer intento cae fuera, TODOS caen fuera. No es "tarde", es NUNCA.
- **Solución:** diferir hasta la próxima apertura de ventana en vez de reintentar — commit `abc1234`, prueba `test_ventana_madrugada` en rojo→verde.
- **Prevención propuesta:** todo reintento que interactúa con una ventana temporal difiere, no reintenta.
- **Caso nuevo en el banco:** `apps/api/tests/banco/ventana_madrugada.py`
- **Clase:** codigo
- estado-curaduria: CURADA (2026-07-31 · memoria `feedback_relojes_anticorrelados`)
```
