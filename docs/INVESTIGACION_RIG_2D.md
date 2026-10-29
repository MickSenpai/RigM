# Investigación: qué necesita un buen rig 2D desde una ilustración

**Fecha de revisión:** 2026-10-29  
**Aplicación:** RigM  
**Objetivo:** convertir una imagen estática en un personaje 2D articulado, expresivo y revisable, sin ocultar los límites de reconstruir información desde una sola imagen.

## Contexto persistente para continuar el proyecto

La persona usuaria quiere que RigM tenga una interfaz gráfica para comprobar qué rigs existen y cuáles se están trabajando, y que la IA pueda manejar el proceso completo. La meta es importar una imagen, preparar arte y capas, construir y animar el rig, validarlo, corregir defectos y exportarlo. El catálogo debe mostrar estados y evidencia reales del disco; la automatización debe usar operaciones estructuradas del proyecto, registrar historial y poder pausarse, deshacerse y retomarse. No describir la IA como infalible: debe distinguir resultados confirmados de reconstrucciones inferidas y enseñar claramente los casos inciertos.

Estado actual: RigM incluye biblioteca PyQt6, máscaras y propuestas locales SAM 2, extracción de sprites, mallas, pesos, deformación, IK, expresiones, clips, validación y bundle portable. Para automatizar la separación de ilustraciones anime se integró See-through como servicio de nube opcional: la app pide permiso antes de enviar la imagen, recupera su PSD y crea capas semánticas editables. La integración conserva el original y el PSD de evidencia. No se ha ejecutado aún See-through sobre Meido desde RigM; la revisión con el parser de ropa de fotografías mostró que no separa bien las mangas pixel art, motivo para usar un modelo entrenado para anime. El hardware local no expone CUDA, así que la generación completa local no está disponible en esta instalación. Siguen pendientes la revisión visual automática, el coordinador autónomo, generación automática de rigs/animaciones, y exportación nativa Rive/Live2D/Spine.

## Conclusión de diseño

Un rig convincente no sale de poner huesos encima de un JPG. Antes hay que convertir la imagen en materiales independientes, reconstruir lo que la pose original ocultaba, definir qué partes son rígidas y cuáles se deforman, y comprobar el personaje en sus poses límite. La documentación de Live2D describe ese mismo orden: separar las partes que se moverán, añadir las zonas que quedarán visibles al moverlas y después modelar y animar. [Flujo de producción de Live2D](https://docs.live2d.com/en/cubism-editor-manual/workflow/)

RigM debe automatizar propuestas, conservar cada paso editable y poner la revisión visual en el centro. Desde una imagen no se puede saber con certeza cómo se ve la espalda de un brazo tapado o el cabello detrás del torso. El sistema puede inferir una continuación plausible, pero debe marcarla como reconstrucción y permitir corregirla.

## 1. Qué significa “correcto”

Un rig es correcto cuando:

- Conserva la silueta, identidad, estilo, colores y pose neutral de la ilustración.
- Las piezas se conectan al rotar y deformarse; no aparecen cortes, agujeros, bordes dobles ni partes que atraviesan otras sin intención.
- Los pivotes se ubican en articulaciones visuales y las jerarquías hacen que el movimiento de una parte arrastre a sus descendientes de forma predecible.
- La cara expresa estados distintos sin perder la identidad y puede parpadear o hablar mientras mantiene otra expresión.
- Las transiciones tienen ritmo, aceleración y pausas intencionales; no son sólo una secuencia de ángulos interpolados.
- La persona puede inspeccionar, corregir y deshacer las decisiones automáticas.
- El archivo conserva fuentes, máscaras, capas, pivotes, pesos, expresiones y clips en una representación reproducible.

La fidelidad a la pose de reposo es un requisito medible: al reconstruir las capas sin animación, el resultado compuesto debe coincidir con la fuente dentro de la tolerancia de remuestreo. Las reconstrucciones bajo las piezas no deben cambiar lo que se ve en reposo.

## 2. Preparación de la ilustración

### Descomposición anime de una sola imagen

Para anime y personajes estilizados, RigM ofrece una ruta de descomposición completa mediante [See-through](https://github.com/shitagaki-lab/see-through), un proyecto de investigación que genera hasta 23 capas semánticas, orden de dibujo, capas transparentes e inferencia de profundidad. Su pipeline combina generación de capas e inpainting con un modelo de profundidad anime y exporta PSD. El espacio público está alojado en [Hugging Face](https://huggingface.co/spaces/24yearsold/see-through-demo); RigM importa el PSD como piezas y ajusta el padding del lienzo para no recortar una ilustración vertical.

El servicio envía la imagen fuera del equipo, por eso RigM solicita confirmación en cada ejecución. El espacio requiere sesión de Hugging Face y tiene límites de disponibilidad/cuota; la revisión del resultado sigue siendo obligatoria. El repo indica que la inferencia local a 1280 px puede requerir 12–16 GB de VRAM, por lo que el backend remoto es la opción práctica cuando el equipo no dispone de GPU compatible. La integración de RigM aún debe verificarse con la salida real de Meido antes de considerar correcto el orden, las máscaras y el inpainting.

### Entrada y análisis

Guardar la fuente original sin modificarla y registrar su tamaño, perfil de color, canal alfa, orientación y hash. Trabajar en una copia normalizada, sin reducir resolución. Primero distinguir personaje y fondo; luego detectar regiones semánticas y contornos. No comenzar por el esqueleto: una articulación no ayuda si la imagen todavía no está separada correctamente.

La separación debe producir máscaras editables para, como mínimo, cabeza/cabello, torso, brazo superior e inferior por lado, manos, muslos, pantorrillas y pies. Según la ilustración harán falta más piezas: mechones delanteros y traseros, ropa, accesorios, ojos, cejas, boca y elementos que cambien de orden. Mantener una capa de referencia intacta y mostrar la pila con nombres y orden de dibujo.

### Capas y solapamiento

Cada pieza móvil necesita una máscara con suficiente extensión dentro de su articulación. Las capas deben solaparse bajo la unión para que un cambio pequeño de ángulo no descubra una línea transparente. También debe existir un control de orden de dibujo: brazo detrás del torso, mano sobre manga, cabello delantero sobre cara, etc. El orden puede variar durante ciertas poses, así que conviene permitir cambios de orden animados en vez de asumir una única pila global. Live2D permite manejar jerarquías y orden de dibujo por partes; su guía de separación también explica preparar capas y máscaras para importar los materiales. [Separación de materiales](https://docs.live2d.com/4.2/en/cubism-editor-manual/divide-the-material/) · [Orden de dibujo](https://docs.live2d.com/en/cubism-editor-manual/draworder/)

No se debe recortar y rotar cada segmento como un rectángulo rígido cuando el resultado produce codos de cartón. Los dedos, guantes, botas y placas rígidas pueden rotar como piezas; hombros, codos, rodillas, cabello y telas suelen necesitar mallas deformables o una combinación de malla y partes rígidas. Los programas de rigging combinan mallas, pesos y huesos para deformar una imagen; los pesos de cada vértice determinan cuánto lo afecta cada hueso. [Mallas de Spine](https://en.esotericsoftware.com/spine-meshes) · [Pesos de Spine](https://us.esotericsoftware.com/spine-weights)

### Zonas ocultas

Al separar una pieza hay que rellenar el hueco que deja en la capa trasera y extender la propia pieza unos píxeles detrás de la unión. Conviene ofrecer tres operaciones separadas: conservar bordes originales, rellenar desde píxeles vecinos y generar una reconstrucción semántica. Una herramienta de inpainting puede completar áreas grandes, pero no sabe cuál es el diseño correcto de una manga o una mano que nunca estuvo visible. La salida generada debe guardarse como capa derivada, con máscara, modelo y parámetros registrados, y compararse con el original mediante una vista antes/después.

LaMa es una línea de investigación de inpainting para máscaras grandes y contexto global; puede ser una opción de relleno, no una garantía de continuidad artística. [Artículo de LaMa](https://arxiv.org/abs/2109.07161). Para pixel art, line art o estilos planos, ofrecer además relleno por propagación de color y clonación de bordes: un modelo generativo puede introducir textura o líneas incompatibles.

## 3. El esqueleto y las deformaciones

### Jerarquía

La estructura debe tener un hueso raíz, pelvis/torso, cuello/cabeza, brazos y piernas por lado. Una cadena de brazo típica es hombro → codo → muñeca; la pierna, cadera → rodilla → tobillo. Cabello y accesorios cuelgan de la cabeza o el torso, pero pueden tener una jerarquía secundaria independiente para conservar retraso y balanceo.

Los pivotes son coordenadas locales al proyecto y deben mostrarse, nombrarse, moverse y guardarse con precisión subpíxel. Una cadena descendiente sigue al padre; su pose neutral queda registrada como bind pose. El usuario debe poder cambiar parentesco y fijar una pieza al hueso adecuado. Un hueso no es un dibujo: es un transformador con traslación, rotación y escala, y posiblemente límites o restricciones.

### Rígido, articulado y deformable

- **Transformación rígida:** accesorios, antebrazo con armadura, mano, bota.
- **Malla con pesos:** zonas continuas como brazo, manga, muslo, cabello o tela que deben doblarse.
- **Formas clave (shape keys):** correcciones dibujadas para extremos que una rotación o un peso no resuelven bien.
- **Cambio de pieza/orden:** dedos, ojo, boca, mano abierta/cerrada o una extremidad que pasa delante/detrás del torso.

Usar pocos vértices en zonas casi rígidas y más resolución sólo donde el contorno realmente necesita doblarse. Los pesos por vértice deben sumar 1 (100 %) y ser escasos; muchos huesos por vértice encarecen el cálculo y pueden crear deformación blanda. Revisar las mallas en movimiento, no sólo en reposo. La guía de Spine recomienda probar pesos con la animación en el rango máximo para hacer visibles los defectos. [Pesos y pruebas de deformación](https://us.esotericsoftware.com/spine-weights)

Para manos y pies se necesitan controles adicionales: orientación de palma, apertura/cierre, dedos agrupados o pose simplificada, punta del pie y apoyo. No se debe animar una mano recortada como si fuera un punto sin tamaño. En un primer producto, ofrecer mano abierta/cerrada y poses de saludo predefinidas con edición manual.

### Restricciones

Una solución IK de dos huesos resulta útil para colocar una mano o un pie en un objetivo sin ajustar cada ángulo por separado. Aun así debe poder alternarse con FK (rotación directa de cada hueso), porque el IK no representa por sí solo una pose expresiva. Incorporar límites configurables, dirección del codo, bloqueo de pie y modo de prueba. Un cambio de pivote, pesos o bind pose debe poder deshacerse.

## 4. Cara y expresiones

La cara requiere una ruta de trabajo propia: pelo frontal/trasero, blanco del ojo, iris/pupila, párpados, cejas, nariz, boca y sombras/reflejos si están dibujados. Si la fuente sólo contiene una cara, RigM tendrá que separar esos rasgos y reconstruir las zonas que estaban detrás. Si la escala de la imagen no permite una separación limpia, debe avisarlo y pedir una fuente mejor o permitir dibujar variantes.

Parámetros base recomendados:

- Ojo izquierdo/derecho: abrir-cerrar, sonrisa/entrecerrar y dirección de mirada X/Y.
- Cejas izquierda/derecha: altura, ángulo y curvatura.
- Boca: abrir-cerrar, anchura/forma, sonrisa y fonemas simplificados.
- Cabeza: inclinación, giro 2D y deformación sutil de mejillas/mandíbula.
- Rubor y efectos: intensidad independiente de la expresión principal.

Las expresiones deberían ser combinaciones de parámetros (neutral, feliz, curiosa, concentrada, preocupada, sorprendida, cansada) y no imágenes completas que sustituyen toda la cara. Así el parpadeo, la mirada y la sincronía de boca siguen funcionando durante una emoción. Live2D advierte que las expresiones de ojos deben componerse correctamente con el parpadeo y que dejar la boca parcialmente abierta como valor base puede impedir que el lip-sync cierre la boca. [Parámetros estándar](https://docs.live2d.com/en/cubism-editor-manual/standard-parameter-list/) · [Sistema de expresiones faciales](https://docs.live2d.com/en/cubism-editor-manual/facial-expression-system/)

Para una entrada JPG, cada rasgo necesita verificación visual al 100 % y al tamaño final de uso. Una máscara que parece aceptable ampliada puede verse como una mancha a 46 px. La app debe permitir alternar la fuente con la cara recompuesta y animar ojos/boca en una vista aislada.

## 5. Principios de animación

Una animación se diseña con poses legibles y propósito, no haciendo oscilar todas las articulaciones a la vez. Disney Animation destaca timing, staging claro, anticipación, seguimiento y acción secundaria; Spine separa el bloqueo de poses principales del refinamiento de curvas y timing. [Principios de Disney Animation](https://disneyanimation.com/process/animation/) · [Flujo pose a pose y curvas de Spine](https://esotericsoftware.com/spine-first-steps)

Reglas prácticas para RigM:

1. **Bloquear primero las poses clave.** Inicio de acción, pose máxima, recuperación. Revisar siluetas antes de añadir interpolación.
2. **Dar masa al torso.** El centro de masa guía pelvis, caja torácica, cabeza y extremidades. Las piernas sostienen el peso; no deben oscilar sin desplazar el torso.
3. **Separar timing y spacing.** El tiempo entre claves define cuánto dura; la curva define cómo acelera. Movimiento lineal constante suele parecer mecánico. Usar curvas editables, con facilidad de entrada/salida y pausas donde la intención lo pide.
4. **Usar arcos y anticipación.** Mano y cabeza siguen trayectorias curvas. Acciones grandes anticipan, llegan a una pose clara y se asientan.
5. **Añadir seguimiento.** Cabello, faldón, mangas y accesorios responden después del torso con retraso y amortiguación. Debe ser secundario y no competir con la cara o la acción principal.
6. **Proteger los loops.** El primer y último fotograma deben empatar en posición, velocidad y dirección; no basta con igualar la posición.
7. **Mezclar clips por capas.** Respiración y parpadeo pueden seguir activos debajo de hablar, saludar o escuchar. El sistema debe saber qué pistas se suman y cuáles reemplazan valores.
8. **Probar a velocidad real y a cámara lenta.** A velocidad real se lee la personalidad; despacio se ven cortes, saltos y colisiones.

El reposo también necesita vida, pero de baja amplitud: respiración asimétrica, pequeños cambios de peso, mirada y parpadeos no regulares. Cada estado requiere una intención y una pose de silueta distinta. Evitar loops uniformes compartidos por todos los estados.

## 6. Uso responsable de IA

### Segmentación

Un modelo interactivo de segmentación puede proponer máscara por puntos, caja, ejemplo visual o texto. SAM 2 admite prompts visuales y SAM 3 añade prompts de concepto con texto/ejemplos; ninguno de esos hechos garantiza máscaras perfectas de piezas anatómicas en arte estilizado. [Investigación SAM 2](https://ai.meta.com/research/publications/sam-2-segment-anything-in-images-and-videos/) · [SAM 3 y prompts de concepto](https://ai.meta.com/research/publications/sam-3-segment-anything-with-concepts/)

En RigM, la IA debe entregar candidatos con confianza y permitir sumar/restar máscara mediante clics, pincel, lazo y refinamiento de borde. Primero localizar al personaje; después proponer sus componentes en contexto. Mantener agujas, cabellos finos y contornos semitransparentes como zonas de incertidumbre, no convertirlos a alfa binario sin opción.

### Pose y pivotes

Un estimador de pose puede proponer hombros, codos, muñecas, caderas, rodillas y tobillos, pero el usuario debe confirmarlos. Las poses de anime, chibi, pixel art, armadura, cabello que tapa hombros y manos pequeñas pueden romper supuestos de anatomía humana. Guardar coordenada y confianza por punto; ofrecer arrastre, simetría opcional y una herramienta para ubicar pivote sobre el segmento.

### Reconstrucción

Relleno clásico, inpainting especializado e inpainting generativo tienen diferentes compromisos. Deben probarse en un conjunto de ilustraciones con máscara realista de brazo/cabello/torso. La reconstrucción generativa debería tener salida editable, controles de variación y comparación A/B; nunca reemplazar silenciosamente la fuente. Si una región tiene baja confianza, pedir corrección antes de generar el rig.

SAM 3 tiene requisitos técnicos considerables en su repositorio oficial (Python/PyTorch recientes y, para su configuración documentada, CUDA compatible). Conviene aislar estos modelos como componentes opcionales, instalables bajo demanda, y mantener el editor y los flujos manuales operativos sin GPU. Revisar licencia de pesos y condiciones de distribución antes de empaquetar o vender cualquier modelo. [Repositorio y requisitos de SAM 3](https://github.com/facebookresearch/sam3)

## 7. Diseño del producto RigM

La interfaz debe presentar un flujo reversible con etapas visibles:

1. **Fuente:** importar, recortar, escalar vista, conservar original y marcar personaje/fondo.
2. **Piezas:** lista jerárquica, máscara sobre fondo contrastado, pinceles de añadir/borrar, opacidad de fuente y orden de dibujo.
3. **Relleno:** mostrar zonas inferidas, nivel de confianza, comparación antes/después, regenerar sólo una selección y editar manualmente.
4. **Rig:** árbol de huesos, pivotes y mallas; modo FK/IK; límites; modo espejo; prueba de extremos.
5. **Cara:** rasgos, parámetros, expresiones, parpadeo y sincronía de boca.
6. **Animación:** timeline, curvas, marcadores de poses, clips y capas; controles de reproducir/pausar/avanzar un cuadro.
7. **Revisión/exportación:** informe de errores, vista a escala final, loop, transparencia y formato de salida.

Los cambios de IA son operaciones con historial y máscaras guardadas. La persona debería poder comparar el resultado con la referencia en cualquier paso. No hace falta llenar la pantalla de controles de animación antes de que las capas estén aprobadas.

### Biblioteca de rigs y operación autónoma

RigM también necesita una pantalla inicial de biblioteca, además del editor de cada personaje. Debe mostrar qué proyectos existen realmente en disco, cuál está en curso, qué etapa completó cada uno, cuándo cambió por última vez, qué versión de la fuente/modelo usa y si necesita atención. Cada ficha debería ofrecer miniatura, nombre, estado, progreso, último resultado y errores; abrir, continuar, duplicar, archivar y localizar el archivo. El catálogo se reconstruye desde manifiestos `.rigm` para que no afirme que existe un rig cuyo archivo falta. Los trabajos largos aparecen en una cola con pasos, tiempo, artefactos y logs legibles.

La IA debe poder completar el flujo sin obligar a la persona a operar manualmente las herramientas: importar y analizar la fuente; proponer y refinar máscaras; reconstruir oclusiones; crear capas, huesos, mallas y pesos; riggear cara; generar clips y expresiones; renderizar poses límite; ejecutar validación estructural y visual; corregir defectos detectables; y preparar exportaciones. La UI permite observarlo, revisar versiones y dirigirlo con instrucciones naturales. El control debe usar las mismas operaciones de dominio que usa el editor, no automatización frágil de clics sobre la interfaz.

La automatización completa requiere que las acciones de IA sean operaciones tipadas (por ejemplo, `ProposeMask`, `EditMask`, `FillOcclusion`, `CreateBone`, `SetWeights`, `GenerateClip`, `RenderPose`, `ValidateRig`, `ExportRig`). Cada operación declara entradas, salidas, artefactos, confianza, duración, dependencias y reversión. Un coordinador procesa un grafo de tareas, reintenta sólo errores recuperables, conserva puntos de control y evita repetir operaciones ya terminadas. Todas las acciones quedan asociadas a una versión de fuente, modelo, parámetros y artefactos.

“IA al 100 %” significa que puede llevar de principio a fin una solicitud declarativa (personaje, estilo que debe preservar, movimientos y formatos de salida), ejecutar y corregir su trabajo con las herramientas del proyecto, y entregar un rig verificable sin requerir que la persona mueva cada pivote. No significa que el sistema pueda conocer lo que la imagen no muestra ni prometer calidad perfecta sin evidencia. Cuando no pueda decidir con confianza, la UI muestra la zona, alternativas y efecto esperado; la persona puede indicar una corrección breve. Mantener capacidad de pausar, cancelar, deshacer y volver a cualquier versión.

Las operaciones reversibles dentro del proyecto pueden ejecutarse de forma autónoma. Borrar fuentes, compartir imágenes con servicios externos, publicar, comprar créditos o modificar archivos ajenos requieren una autorización explícita por operación. Preferir procesamiento local cuando sea viable; mostrar claramente el proveedor/modelo y si una fuente saldrá del dispositivo.

La validación autónoma tiene tres niveles: (1) estructural: referencias, ciclos, pesos, pivotes y claves; (2) render: pose neutral, extremos, expresiones y loops; (3) apariencia: comparación contra la fuente, silueta, costuras, artefactos y legibilidad al tamaño real. La IA guarda imágenes de evidencia con cada informe. Un estado “terminado” sólo se puede asignar si los criterios obligatorios pasan; en otro caso el proyecto queda como “requiere atención” con el motivo concreto.

## 8. Formato de proyecto y evaluación

El formato `.rigm` debe evolucionar a un contenedor versionado con identificadores estables y rutas relativas. Separar al menos:

- fuente y metadatos;
- capas raster/vectoriales y máscaras;
- capas de relleno/reconstrucción y procedencia;
- jerarquía de huesos, transformaciones de bind y límites;
- mallas, pesos y correcciones de forma;
- orden/visibilidad de dibujo;
- parámetros y expresiones faciales;
- clips, claves, curvas, loops y mezclas;
- historial y versión del formato.

No acoplar el modelo interno a un único exportador. Rive, Live2D, Inochi2D o un runtime propio representan estructuras diferentes; crear adaptadores y reportar las funciones que se pierden al exportar. Mantener el `.rigm` como fuente editable.

### Puertas de calidad

- **Reconstrucción neutra:** la composición en reposo coincide visualmente con la fuente; no hay agujeros ni bordes dobles.
- **Máscaras:** bordes limpios a escala 1:1 y a escala de uso; se inspeccionan manos, cabello y uniones.
- **Articulación:** revisar cada extremidad en sus rangos máximos a ambos lados, no sólo en un saludo.
- **Deformación:** detectar pliegues, aplastamiento, pérdida de volumen, texturas que se estiran o mallas que se cruzan.
- **Cara:** parpadeo durante emociones, boca que cierra tras hablar, ojos que miran sin salir de la cuenca.
- **Movimiento:** loop sin salto, transiciones sin pops, movimientos secundarios amortiguados y silueta legible a tamaño de uso.
- **Robustez:** guardar, cerrar, reabrir y reproducir sin perder rutas, máscaras, capas ni parámetros.
- **Exportación:** reimportar o abrir en el destino y comparar pose neutral, expresiones y clips.

La validación automática puede comprobar ciclos en jerarquía, huesos sin parent, referencias rotas, máscaras vacías, pesos que no suman 1, claves fuera de duración y archivos faltantes. La validación visual necesita imágenes de poses límite y revisión humana; un `verify` del archivo no demuestra que el movimiento se vea bien.

## 9. Ruta recomendada

1. Evaluar See-through en personajes anime y pixel art reales; comprobar geometría PSD, capas importadas, orden de dibujo, padding y zonas reconstruidas.
2. Mejorar máscaras con deshacer/rehacer, lazo, orden de dibujo y comparación antes/después; añadir relleno semántico supervisado.
3. Añadir cola de tareas y progreso verificable para agentes.
4. Añadir deformación de malla, pesos, FK/IK y controles de pose límite.
5. Mejorar el rig con edición de topología, curvas de animación, límites, poses faciales por rasgo y cambios de orden de dibujo.
6. Conectar un agente coordinador que ejecute el flujo, genere evidencia visual, proponga autocorrecciones y prepare adaptadores de exportación con reporte de compatibilidad.

El criterio de avance no debe ser “la IA creó huesos”. Debe ser que una persona corrija menos, que el personaje conserve su arte y que las poses solicitadas se vean sólidas.

## Fuentes principales

- Live2D, [Production Flow](https://docs.live2d.com/en/cubism-editor-manual/workflow/): etapas de separación, relleno y animación.
- Live2D, [Material Separation](https://docs.live2d.com/4.2/en/cubism-editor-manual/divide-the-material/) y [plugin de separación asistida](https://docs.live2d.com/en/cubism-editor-manual/material-separation-ps-plugin-manual/): capas, máscaras, corte y relleno de transparencia.
- Live2D, [Deformers](https://docs.live2d.com/en/cubism-editor-manual/deformer/), [draw order](https://docs.live2d.com/en/cubism-editor-manual/draworder/) y [standard parameters](https://docs.live2d.com/en/cubism-editor-manual/standard-parameter-list/): deformación, oclusión y grupos de control.
- Spine, [bones](https://eu.esotericsoftware.com/spine-bones), [meshes](https://en.esotericsoftware.com/spine-meshes), [weights](https://us.esotericsoftware.com/spine-weights), [keys](https://us.esotericsoftware.com/spine-keys) y [animation workflow](https://esotericsoftware.com/spine-first-steps): estructura, deformación y timing.
- Disney Animation, [Animation](https://disneyanimation.com/process/animation/): staging, timing, anticipación, seguimiento y acción secundaria.
- Meta AI, [SAM 2](https://ai.meta.com/research/publications/sam-2-segment-anything-in-images-and-videos/) y [SAM 3](https://ai.meta.com/research/publications/sam-3-segment-anything-with-concepts/): segmentación interactiva y por conceptos.
- [See-through: Single-image Layer Decomposition for Anime Characters](https://github.com/shitagaki-lab/see-through) y [Space de Hugging Face](https://huggingface.co/spaces/24yearsold/see-through-demo): descomposición semántica de anime e inpainting en PSD.
- Hugging Face, [SAM 2 en Transformers](https://huggingface.co/docs/transformers/model_doc/sam2) y [modelo Tiny oficial](https://huggingface.co/facebook/sam2.1-hiera-tiny): prompts de puntos/cajas, máscaras candidatas, ejecución y metadatos del modelo.
- Suvorov et al., [LaMa: Resolution-robust Large Mask Inpainting with Fourier Convolutions](https://arxiv.org/abs/2109.07161): reconstrucción para máscaras grandes.
