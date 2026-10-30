# Poses por capa y animación de manos

## Criterio de rigging

Una mano dibujada como una sola pieza permite cambiar la orientación de la muñeca, pero no modificar de forma independiente los dedos. Para conseguir esos gestos hacen falta dibujos alternativos de la mano o capas separadas por dedo y articulación. El rig debe conservar la ilustración original y mostrar las piezas inferidas como arte derivado que requiere revisión.

Live2D documenta una preparación basada en separar materiales, completar las zonas que quedarán visibles y después modelar y animar. Spine documenta tanto piezas rígidas bajo un hueso como mallas con pesos para deformación. Para una ilustración que no contiene el dorso o los dedos extendidos, una pose alternativa dibujada es una inferencia artística; ni una máscara ni un relleno de píxeles pueden recuperar esos datos observados. Fuentes: [flujo de Live2D](https://docs.live2d.com/en/cubism-editor-manual/workflow/), [separación de materiales](https://docs.live2d.com/4.2/en/cubism-editor-manual/divide-the-material/), [huesos de Spine](https://us.esotericsoftware.com/spine-bones), [mallas y pesos de Spine](https://us.esotericsoftware.com/spine-weights).

## Soporte incorporado en RigM

- Las animaciones admiten pistas de visibilidad por capa en `layer_tracks`. Son claves discretas con interpolación escalonada: conservan la pieza anterior hasta que entra la siguiente pose.
- `rigmctl clip visibility-key` registra estas claves y `rigmctl pose render` las aplica al renderizar.
- El editor muestra una casilla para previsualizar la visibilidad de la capa seleccionada y un botón para guardar la clave en el fotograma actual.
- `rigmctl layer import-sprite` importa un PNG RGBA de lienzo completo como una capa editable. Las capas pueden registrar procedencia para identificar dibujos generados o reconstruidos.
- `rigmctl mesh generate --joint ID` vincula una pieza rígida a una articulación sin repartirla entre huesos vecinos.
- IK de dos huesos permite escoger el lado de flexión (`--bend-direction positive|negative` en CLI; **Flexionar al lado opuesto** en el editor).
- El reproductor de la línea de tiempo permite pausa, reproducción inversa, búsqueda y avance/retroceso de un fotograma; la velocidad se ajusta entre 0.5× y 8×.
- Las claves de rotación pueden usar interpolación lineal, suave por tramo o cúbica monótona. La cúbica conserva continuidad de velocidad entre claves y limita sobreoscilaciones en extremos.

## Movimiento de brazo y prevención de giros

La documentación de Spine distingue FK —rotar hombro y luego codo— de IK —fijar la mano y resolver los ángulos— y expone explícitamente la dirección de flexión del codo. Su flujo de animación recomienda plantear primero las poses principales y después afinar el movimiento entre ellas. RigM aplica ese criterio a un saludo: define una trayectoria de muñeca, resuelve hombro/codo con una sola rama de IK durante todo el clip y conserva una orientación estable de palma compensando la rotación acumulada en la muñeca. Fuentes: [IK de Spine](https://us.esotericsoftware.com/spine-ik-constraints) y [flujo de animación de Spine](https://us.esotericsoftware.com/spine-animating).

El cálculo del codo debe expresarse en el espacio local de su padre. Si `φ1` es el ángulo mundial del brazo superior y `b1` su ángulo en la pose de enlace, entonces `θ1 = φ1 - b1`. Para el antebrazo, `θ2 = φ2 - b2 - θ1`, donde `φ2` es el ángulo mundial del antebrazo y `b2` el de enlace. Restar `φ1` en lugar de `θ1` produce un error igual al ángulo de enlace; la mano deja de alcanzar el objetivo y la cadena parece invertirse. RigM ahora usa la conversión local correcta y ofrece las dos ramas para que el codo no cambie de lado por accidente.

En la revisión visual del clip `Saludo` apareció una segunda causa de rigidez: la rama de flexión inicial dejaba el codo detrás del cabello, aunque la muñeca llegaba al objetivo. La variante revisada conserva cada objetivo y la orientación mundial de la mano, pero elige la rama que saca el codo por fuera del cabello. La mano abierta entra cuando la muñeca ya llegó a la zona de saludo (fotograma 20), en lugar de aparecer mientras el brazo aún está abajo. La superposición del editor también calcula las posiciones de huesos con la misma cinemática que usa el render de sprites, para que la guía no se quede en la pose neutra durante la previsualización.

## Meido

El PSD original sólo contiene el guante cerrado de la mano derecha; las puntas de los dedos no están separadas. Se conserva esa pieza para reposo y se añade una mano abierta de cinco dedos como variante generada a partir del PSD. El clip `Saludo` cambia a esa capa en la fase de saludo, mueve el brazo derecho y rota la muñeca. La capa incluye procedencia y `review_required`; no se presenta como píxeles originales.

Esta solución anima el cambio entre dos poses completas de mano y el movimiento de muñeca. No ofrece articulación independiente de cada dedo. El saludo usa objetivos de muñeca que suben en arco, un intervalo corto de anticipación, varias oscilaciones pequeñas de muñeca y una bajada con retorno al guante original. Para animar falanges, el arte debe incluir dedos separados y el rig debe añadir una cadena jerárquica por dedo, límites de rotación y correcciones de forma en extremos. La pose neutra y las uniones del brazo siguen requiriendo revisión visual antes de marcar el proyecto como listo.

## Validación requerida

La revisión posterior cambia las pistas del brazo de `Saludo` a cúbica monótona para evitar el frenado individual en cada clave. Añade una inclinación de cabeza muy pequeña durante la subida y la espera, y vuelve a cero antes de cerrar el clip. Es un gesto secundario deliberadamente sutil: la cara y el cabello siguen siendo una sola composición y una inclinación grande haría evidentes las limitaciones de las capas.

1. Comprobar que la pose neutra conserva el PSD original.
2. Revisar la transición a mano abierta, la muñeca en ambos extremos y el regreso al guante cerrado.
3. Inspeccionar las zonas que el brazo deja al descubierto; el relleno clásico local no sustituye una reconstrucción artística.
4. Confirmar hashes, rutas portables y procedencia de cada sprite derivado.
5. Mantener `needs_review` mientras haya costuras, reconstrucciones o piezas generadas sin aprobación visual.
