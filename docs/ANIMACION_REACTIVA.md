# Animación reactiva de personajes

RigM incorpora `BehaviorRuntime`, una capa de ejecución que mezcla un clip base
de reposo con movimiento procedural acotado, transiciones entre estados,
parpadeo, mirada, gestos guardados y formas de boca. Cada actualización recibe
tiempo transcurrido en segundos y devuelve una pose temporal; no escribe el
proyecto. El editor la expone como **Simular agente** y `rigmctl` permite
renderizar una evidencia estática.

Las conductas incluyen escucha con inclinación y asentimientos, procesamiento
con miradas cambiantes y movimientos pequeños de cabeza, habla con gestos
alternos de hombro, e inquietud con pasos alternados, balanceo de brazos,
rebote y recorrido horizontal de ida y vuelta. La intensidad modula amplitud y
ritmo sin alterar el rig guardado.

## Modelo para Meido

El anfitrión comunica señales observables del ciclo de vida del agente: reposo,
escucha, procesamiento, respuesta hablada, espera, inquietud o error. También
puede enviar mirada, un nombre de gesto, nivel de voz o visemas normalizados.
No hace falta ni se debe transmitir razonamiento privado. La animación expresa
actividad visible (por ejemplo, `thinking`), no el contenido de pensamientos.

```python
from rigm.behavior import BehaviorRuntime, motion_event_from_agent
from rigm.project import load_project
from rigm.rig import render_project_pose

runtime = BehaviorRuntime(load_project("meido.rigm"))
runtime.dispatch(motion_event_from_agent({"type": "agent.phase", "phase": "processing"}))
pose = runtime.update(1 / 60)
frame = render_project_pose(
    "meido.rigm", pose["rotations"],
    layer_visibility=pose["layer_visibility"],
    facial_parameters=pose["facial_parameters"],
    translation=pose["translation"],
)
```

Los eventos disponibles son `state`, `parameter`, `gaze`, `speech`, `viseme`,
`gesture` y `gesture_end`. El adaptador entiende `agent.status`, `agent.phase`,
`agent.gaze`, `agent.gesture`, `speech.level` y `tts.viseme`. Para visemas, el
proveedor de voz debe mapear sus identificadores al vocabulario normalizado
(`silence`, `closed`, `open`, `wide`, `round`, `teeth`) y enviar su duración.
La documentación de Azure Speech muestra que los visemas llevan tiempos de
desplazamiento; Spine recomienda actualizar la animación con delta de tiempo y
mezclar pistas con transiciones; Live2D documenta parámetros separados para
mirada, parpadeo, boca y respiración. RigM adopta esos patrones sin acoplarse a
esos proveedores. [Azure visemas](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-speech-synthesis-viseme)
· [Spine mezcla y actualización](https://us.esotericsoftware.com/spine-applying-animations)
· [Parámetros estándar Live2D](https://docs.live2d.com/en/cubism-editor-manual/standard-parameter-list/).

## Simulación

En el editor, abre un proyecto, activa **Simular agente** y cambia el estado.
La intensidad regula la amplitud; la pose no se guarda. Para producir un PNG:

```bash
rigmctl behavior render meido.rigm /tmp/meido-thinking.png \
  --state thinking --seconds 2.5 --intensity 0.45
```

Se pueden programar señales públicas en tiempos relativos:

```bash
rigmctl behavior render meido.rigm /tmp/meido-speaking.png \
  --state idle --seconds 3 --event '{"at":0,"event":{"type":"agent.phase","phase":"responding"}}' \
  --event '{"at":1.1,"event":{"type":"tts.viseme","shape":"wide","duration":0.16}}'
```

## Límites que requieren trabajo de arte

La simulación es un cimiento, no una actuación humana completa. El render facial
actual deforma la capa de boca con una forma sencilla; no sustituye una hoja de
visemas dibujados. Los ojos se cierran por opacidad y la mirada desplaza iris si
la capa está separada y nombrada. Las piezas que no estén divididas (por ejemplo,
dedos) no pueden cambiar de pose proceduralmente. Para mejorar a Meido hacen
falta variantes dibujadas de boca/mano, jerarquías y mallas corregidas, clips de
gesto de autor y revisión visual a tamaño final. Un personaje sólo debe marcarse
listo tras validar el rig y revisar sus evidencias.

El archivo de Meido aún conserva restos de la silueta en su placa de fondo. En
la previsualización de paseo se desplaza la composición rasterizada completa
para evitar que quede una silueta duplicada sobre su fondo oscuro casi plano.
Para mantener un fondo texturizado fijo mientras el personaje camina se necesita
una placa limpia y máscaras completas. La marcha actual es una aproximación 2D:
no gira al personaje de perfil ni cambia sus sprites.
