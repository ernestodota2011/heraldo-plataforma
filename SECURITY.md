# Política de divulgación de vulnerabilidades

Heraldo es la plataforma multi-inquilino de agentes en marca blanca de
**AetherLogik**. Opera datos de varios clientes a la vez, así que una
vulnerabilidad aquí no afecta a un solo negocio.

Si encuentras un fallo de seguridad, te pedimos que nos lo cuentes **antes** de
hacerlo público. Te lo agradecemos de verdad.

## Cómo avisarnos

Escríbenos a **info@aetherlogik.com** con `[SEGURIDAD]` al principio del asunto.
Esa es hoy la única vía, y llega a un buzón que leemos.

No abras un *issue* para reportar una vulnerabilidad: los *issues* los ve
cualquiera con acceso al repositorio, y el correo no.

> El aviso privado de GitHub (*Security* → *Report a vulnerability*), que sería
> la vía preferible porque va cifrada de extremo a extremo, **no está disponible
> en este repositorio**: comprobado, la función no existe sobre un repositorio
> privado de esta cuenta. Si eso cambia, esta página lo dirá y ese pasará a ser
> el canal principal. Hasta entonces no lo busques: no está.

## Qué necesitamos de ti

- Qué falla y qué se puede conseguir explotándolo.
- Los pasos para reproducirlo (una petición, un guion, una captura).
- La versión o el commit sobre el que lo viste.

## Qué puedes esperar de nosotros

| Momento | Qué hacemos |
|---|---|
| **72 horas hábiles** | Acusamos recibo y te decimos si lo reproducimos |
| **10 días hábiles** | Te damos una evaluación con severidad y un plan |
| Al cerrarlo | Te avisamos, y te acreditamos si quieres |

Estos plazos son un compromiso de respuesta, no un plazo de arreglo: el arreglo
depende de lo que sea.

## Alcance

Entra todo lo que vive en este repositorio y el servicio desplegado a partir de
él. **No** entran los sistemas de los clientes de AetherLogik ni las cuentas de
terceros que un cliente conecta con sus propias credenciales.

## Lo que te pedimos

- No accedas a datos que no sean tuyos, ni los descargues. Si tropiezas con
  datos de un cliente, **para** y cuéntanoslo.
- Nada de denegación de servicio, ni de fuerza bruta, ni de ingeniería social.
- Danos un plazo razonable antes de publicar.

## Cómo se arregla

Cada incidente de seguridad que se cierra deja su entrada en
[`docs/heraldo-problemas.md`](docs/heraldo-problemas.md) —síntoma, causa raíz,
solución y **prevención**— y la prevención se cablea como prueba. Un arreglo sin
prueba que lo sostenga no se considera cerrado.
