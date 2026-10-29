# Instrucciones para agentes de RigM

RigM es una aplicación de escritorio para convertir ilustraciones en personajes 2D articulados. La visión completa es que un agente de IA pueda gestionar un proyecto de principio a fin y que la interfaz muestre rigs, estados, progreso, evidencias y errores verificables.

## Estado real del producto

- Editor PyQt6: importa una imagen, coloca/mueve pivotes, conecta huesos, crea y pinta máscaras monocromas, abre y guarda `.rigm` portable.
- Biblioteca: registra proyectos, descubre `.rigm` en `~/RigM/Projects`, marca archivos e imágenes ausentes y muestra etapa/estado/progreso/conteo de capas.
- `rigmctl`: CLI estructurada para crear, inspeccionar, validar y modificar proyectos, pivotes, huesos, capas, máscaras, sprites extraídos, reconstrucciones y estado del trabajo. Usa las mismas operaciones de `rigm.project` que la interfaz.
- La biblioteca detecta cambios externos y el editor impide sobrescribirlos sin recargar.
- Las máscaras se crean vacías y se pintan a mano o se importan desde PNG; SAM 2 local propone tres candidatos mediante puntos/cajas y requiere aceptación explícita. Instala `.[segmentation]` para PyTorch/Transformers y `.[inpaint]` para OpenCV. RigM puede extraer sprites RGBA, reconstruir la base y exportar una composición. Aún no hay relleno semántico, mallas/pesos, deformaciones, cara, expresiones, clips, exportadores ni coordinador de IA.
- No declarar un proyecto como `ready` si la validación informa pendientes.

## Reglas de arquitectura

1. `rigm.project` es la fuente de verdad para lectura, escritura, validación y operaciones del modelo. La interfaz y los agentes no deben mantener formatos diferentes.
2. Toda operación de agente debe tener argumentos estructurados, resultados JSON, errores claros, historial y cambios atómicos. Añade cada capacidad a `rigmctl` sin automatizar clics de interfaz.
3. Mantén el `.rigm` versionado, portable y con rutas relativas. Migra formatos previos cuando cambie el esquema; no pierdas archivos de usuario.
4. Registra etapa, estado, progreso, identificador de tarea y último resultado en el manifiesto para que la biblioteca pueda supervisar el trabajo.
5. Las inferencias de IA deben seguir siendo editables, conservar la fuente y distinguir reconstrucciones de píxeles observados.
6. El estado `ready` requiere que todas las validaciones obligatorias pasen; no lo fuerces para que la interfaz se vea completa.
7. Mantén la aplicación operativa sin una GPU o proveedor de IA. Los modelos de visión deben ser adaptadores opcionales y mostrar modelo, confianza y privacidad.
8. Al modificar máscaras desde la GUI, comprueba el manifiesto antes de escribir los píxeles y conserva historial del cambio.
9. Mantén la fuente intacta. Etiqueta sprites y reconstrucciones con hashes de fuente/máscara; si cambian, no los presentes como resultados actuales.

## Desarrollo y operación

Instalar el paquete desde la raíz del proyecto con `python -m pip install -e .`. El editor se inicia con `rigm`; las operaciones para agentes se consultan con `rigmctl --help`.

Comandos útiles desde la raíz:

```bash
rigmctl list --json
rigmctl validate /ruta/al/personaje.rigm
rigmctl inspect /ruta/al/personaje.rigm
```

Para cambios de lógica del proyecto, valida el flujo CLI con un directorio temporal y abre una ventana Qt en modo offscreen cuando no haya escritorio disponible. No dejes imágenes o proyectos de smoke test en la biblioteca real.

## Diseño y calidad

Antes de implementar un paso de rigging, consulta [docs/INVESTIGACION_RIG_2D.md](docs/INVESTIGACION_RIG_2D.md). La pose neutral debe reconstruir la fuente sin costuras; cada articulación debe probarse en su rango límite; expresiones, parpadeo y voz deben combinarse; el movimiento debe revisarse a la escala de uso. El informe automático no sustituye la vista de evidencia.
