# RigM

RigM es un prototipo de escritorio para preparar personajes 2D animables desde una imagen.

La meta del producto incluye una biblioteca gráfica que verifique qué rigs existen y muestre los trabajos en curso. La IA deberá poder llevar un proyecto de la imagen al rig, animaciones, validación y exportación, mientras la interfaz expone progreso, evidencia, errores y versiones.

## Prototipo actual

- Importa JPG, PNG, BMP y WebP.
- Presenta una biblioteca de rigs registrados con estado, etapa, progreso y última actualización; señala proyectos o imágenes faltantes.
- Permite registrar, abrir, validar y quitar de la biblioteca proyectos existentes sin borrar sus archivos.
- Permite colocar y mover pivotes encima de la imagen.
- Permite seleccionar dos pivotes y conectarlos con un hueso.
- Crea máscaras por pieza y permite pintarlas en el lienzo; clic izquierdo añade y clic derecho borra. La biblioteca muestra el conteo de capas.
- Extrae cada máscara como una pieza PNG transparente, rellena debajo de las piezas con inpainting clásico y presenta una vista compuesta revisable.
- Genera una malla triangular por pieza, asigna pesos normalizados a las articulaciones y deforma los sprites con skinning lineal en la vista previa. Al renderizar, limpia en memoria los píxeles originales de las piezas para evitar que quede una copia estática debajo.
- Incluye previsualización de rotación, ajuste de pesos por vértice, IK planar de dos huesos, clips con interpolación suave, expresiones faciales basadas en pivotes y opacidad de capas, validación de mallas y exportación de un bundle ZIP portable.
- Puede separar ilustraciones anime en capas semánticas con See-through (servicio de Hugging Face), importar el PSD y mantener el JPG original. La app pide consentimiento antes de enviar una copia.
- Guarda y vuelve a abrir proyectos `.rigm` versionados con una copia portable de la imagen fuente.
- Expone las operaciones disponibles con `rigmctl`, para que un agente pueda inspeccionar y modificar el mismo proyecto que la interfaz, incluidas la creación de capas y la importación de máscaras PNG.
- El lienzo admite zoom y desplazamiento.

Las máscaras se pintan manualmente, se importan como PNG monocromos o se proponen con SAM 2 usando puntos positivos/negativos o una caja. SAM 2 devuelve tres candidatos clasificados; se muestran como previsualizaciones no destructivas y sólo se sustituyen las máscaras al aceptar una. La inferencia es local y el primer uso puede descargar el modelo `facebook/sam2.1-hiera-tiny` de Hugging Face. De las máscaras aprobadas se extraen sprites RGBA y OpenCV rellena la zona que dejan en la imagen base. El inpainting clásico prolonga contexto local; no inventa de forma fiable anatomía o detalles semánticos, así que la vista compuesta requiere revisión.

## Investigación y criterios de calidad

La [investigación sobre rigging 2D](docs/INVESTIGACION_RIG_2D.md) recoge el flujo de preparación, capas y oclusión, deformaciones, cara, principios de animación, uso de IA, validación y las etapas que aún faltan.

## Ejecutar

Requiere Python 3.10+, PyQt6 y Pillow:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
rigm
```

Para activar la reconstrucción local de zonas ocultas, instala el extra opcional: `python -m pip install -e '.[inpaint]'`.
Para activar la segmentación SAM 2, instala: `python -m pip install -e '.[segmentation]'`.
Para la separación completa anime con See-through, instala: `python -m pip install -e '.[decomposition]'` y autentícate con `.venv/bin/hf auth login`. El servicio recibe la imagen sólo después de aceptar el aviso de privacidad dentro de la app.
Para activar mallas, deformación, IK y render de poses, instala: `python -m pip install -e '.[rigging]'`.

El comando `rigmctl --help` muestra la interfaz estructurada para listar, crear, inspeccionar y validar proyectos; modificar pivotes, huesos, capas y progreso. Es la base para que un agente opere RigM sin simular clics de ratón.

Ejemplo para un agente:

```bash
rigmctl list --json
rigmctl inspect ~/RigM/Projects/Meido.rigm
rigmctl validate ~/RigM/Projects/Meido.rigm
rigmctl status ~/RigM/Projects/Meido.rigm in_progress skeleton --progress 35 --task-id rig-001
rigmctl layer add ~/RigM/Projects/Meido.rigm brazo_derecho
rigmctl layer import-mask ~/RigM/Projects/Meido.rigm cabello cabello.png
rigmctl layer extract ~/RigM/Projects/Meido.rigm layer-abc123
rigmctl layer inpaint ~/RigM/Projects/Meido.rigm --method telea --radius 3
rigmctl layer render ~/RigM/Projects/Meido.rigm /tmp/meido-preview.png
rigmctl layer propose ~/RigM/Projects/Meido.rigm layer-abc123 --positive 400 350 --negative 450 400 --box 280 200 550 600
rigmctl layer accept ~/RigM/Projects/Meido.rigm layer-abc123 proposal-0123456789ab
rigmctl mesh generate ~/RigM/Projects/Meido.rigm layer-abc123 --spacing 20
rigmctl clip create ~/RigM/Projects/Meido.rigm caminar --duration 60 --fps 30
rigmctl clip key ~/RigM/Projects/Meido.rigm clip-abc123 joint-brazo 0 0
rigmctl clip key ~/RigM/Projects/Meido.rigm clip-abc123 joint-brazo 30 35
rigmctl pose render ~/RigM/Projects/Meido.rigm /tmp/pose.png --clip clip-abc123 --frame 15
rigmctl export ~/RigM/Projects/Meido.rigm ~/RigM/Exports/Meido.zip
rigmctl decompose ~/RigM/Projects/Meido.rigm --allow-upload
```

Extrae cada pieza después de finalizar su máscara. Cuando todas las máscaras activas tienen una pieza actual, ejecuta el inpainting y abre **Ver composición reconstruida** para revisar el resultado. Esto rellena con píxeles vecinos; no sustituye una reconstrucción semántica. La validación detecta piezas ausentes o desactualizadas y no permite marcar `ready` mientras haya errores o advertencias.

Para proponer máscaras desde `rigmctl`, instala el extra `segmentation`. `--positive x y` y `--negative x y` se pueden repetir; `--box` recibe `x1 y1 x2 y2`. Las propuestas se guardan en el proyecto y deben aprobarse con `rigmctl layer accept`.

La separación completa anime requiere el extra `decomposition` y una sesión de Hugging Face. El comando `rigmctl decompose` exige `--allow-upload`; la app solicita confirmación antes de transmitir la imagen. See-through devuelve un PSD con partes semánticas y reconstrucciones; RigM conserva ese PSD como evidencia e importa las piezas como capas editables. En la app, **Agrupar capas generadas en una carpeta** viene activado: las piezas aparecen bajo un grupo desplegable en el panel de capas. La opción se puede desactivar antes de iniciar la separación. La calidad se debe revisar antes de construir el esqueleto.

## Uso

1. En **Biblioteca**, crea un rig desde una imagen o registra un proyecto `.rigm` existente.
2. Abre un proyecto para pasar al **Editor**.
3. Pulsa **Añadir pivote** y haz clic en cada articulación.
4. Selecciona dos pivotes con Ctrl y pulsa **Conectar selección**.
5. En **Capas / máscaras**, crea una capa, selecciónala y pulsa **Pintar máscara**. Pinta con clic izquierdo y borra con clic derecho; se guarda al soltar.
6. Elige **+ Punto** y marca la pieza que quieres conservar; añade **− Puntos** para excluir zonas o dibuja una **Caja**. Pulsa **Proponer máscara con SAM 2**, revisa los candidatos y acepta el adecuado.
7. Pulsa **Extraer pieza activa** para cortar la zona en un sprite transparente. Repite con las piezas que quieras animar.
8. Pulsa **Reconstruir zonas ocultas** y revisa la composición. El relleno es local y clásico, no una inferencia semántica.
9. Pulsa **Generar malla** por cada pieza; prueba la rotación y corrige pesos si hay pliegues o deriva.
10. Crea un clip, selecciona un pivote, define una rotación y guarda claves en distintos fotogramas. Reproduce para revisar la interpolación.
11. Para IK selecciona raíz, codo/rodilla y extremo en ese orden, marca un objetivo en el lienzo y resuelve. Guarda las rotaciones resultantes como claves para usarlas en el clip.
12. Para expresiones coloca pivotes faciales y crea capas alternativas (por ejemplo, ojos abiertos/cerrados o boca abierta/cerrada); ajusta la pose/opacidad y guarda una expresión.
13. Valida el proyecto y exporta el bundle ZIP portable cuando la composición y los movimientos se hayan revisado.

La validación comprueba referencias, máscaras, sprites y reconstrucciones. Marca el rig para revisión mientras falten deformaciones y animaciones; no declara terminado un rig incompleto.

## Limitaciones actuales

1. El generador propone una malla uniforme y pesos por distancia; no identifica anatomía ni crea automáticamente máscaras correctas. El usuario debe revisar y corregir la topología/pesos.
2. La IK resuelve cadenas planas de dos huesos. Las expresiones combinan rotaciones de pivotes y opacidad de capas; todavía no generan automáticamente formas de ojos o boca.
3. La exportación actual es un bundle portable propio de RigM; no produce directamente archivos Rive, Live2D o Spine.
4. SAM 2 propone máscaras, pero no decide por sí solo la segmentación final. La opción See-through sí propone capas semánticas y reconstrucciones para ilustración anime, aunque sus resultados requieren revisión y el servicio se ejecuta en la nube.
5. El flujo asistido por IA puede operar mediante `rigmctl`, pero aún falta el coordinador autónomo que planifica, ejecuta, compara poses y solicita revisión dentro de la biblioteca.
6. Faltan deshacer/rehacer, relleno semántico de zonas ocultas, cambios animados de orden de dibujo y validación visual automática con evidencia.

RigM ya permite construir y revisar manualmente un rig deformable y animarlo; la automatización integral desde una sola imagen sigue siendo una etapa futura. La [investigación de rigging 2D](docs/INVESTIGACION_RIG_2D.md) recoge los criterios de calidad.
