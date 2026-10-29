"""Anime-specific, cloud-assisted single-image layer decomposition."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from .project import ProjectError, import_decomposition_psd, load_project

SEE_THROUGH_SPACE = "24yearsold/see-through-demo"
SEE_THROUGH_RESOLUTION = 768  # Recomendación del Space para ZeroGPU gratuito.


def _find_output_file(value: Any) -> Path | None:
    """Find the PSD file among Gradio's single- or multi-output response shapes."""
    if isinstance(value, (str, Path)):
        candidate = Path(value)
        return candidate if candidate.is_file() else None
    if isinstance(value, dict):
        for key in ("path", "name"):
            candidate = value.get(key)
            if isinstance(candidate, (str, Path)) and Path(candidate).is_file():
                return Path(candidate)
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            candidate = _find_output_file(item)
            if candidate is not None:
                return candidate
    return None


def _square_canvas(image: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """Pad instead of crop so portrait character art, including feet and hair, survives."""
    image = image.convert("RGB")
    width, height = image.size
    side = max(width, height)
    corners = [image.getpixel(point) for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
    background = tuple(round(sum(pixel[channel] for pixel in corners) / len(corners)) for channel in range(3))
    offset = ((side - width) // 2, (side - height) // 2)
    canvas = Image.new("RGB", (side, side), background)
    canvas.paste(image, offset)
    return canvas, offset


def decompose_project_with_see_through(
    project_path: str | Path,
    *,
    seed: int = 42,
    token: str | None = None,
    group_layers: bool = True,
    split_left_right: bool = False,
) -> dict[str, Any]:
    """Run the anime layer model remotely, then import its PSD as editable RigM layers.

    The caller must obtain explicit user consent before calling this function because
    the source image is uploaded to the See-through Hugging Face Space.
    """
    project_path = Path(project_path).expanduser().resolve()
    data = load_project(project_path)
    source_path = Path(data["_image_path"])
    try:
        from gradio_client import Client
        from huggingface_hub import get_token
    except ImportError as exc:
        raise ProjectError(
            "La separación completa requiere gradio_client. Instala RigM con: "
            "pip install -e '.[decomposition]'"
        ) from exc
    token = token or get_token()
    if not token:
        raise ProjectError(
            "See-through requiere iniciar sesión en Hugging Face. Ejecuta `.venv/bin/hf auth login` "
            "y vuelve a intentarlo."
        )

    try:
        with Image.open(source_path) as opened:
            original = opened.convert("RGB")
        square, offset = _square_canvas(original)
        output_dir = project_path.parent / "assets" / "decompositions"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"see-through-{uuid.uuid4().hex[:12]}.psd"
        with tempfile.TemporaryDirectory(prefix="rigm-see-through-") as work_dir:
            input_path = Path(work_dir) / "input.png"
            square.save(input_path, format="PNG")
            client = Client(SEE_THROUGH_SPACE, token=token, verbose=False)
            # The current Space exposes image, resolution, seed and left/right split.
            # It returns both a PSD File and a gallery; 768 is recommended for ZeroGPU.
            # Left/right cuts stay off by default because they add failure-prone processing.
            result = client.predict(
                str(input_path), SEE_THROUGH_RESOLUTION, seed, split_left_right,
                api_name="/inference",
            )
            psd_result = _find_output_file(result)
            if psd_result is None:
                shape = type(result).__name__
                if isinstance(result, (list, tuple)):
                    shape += f" con {len(result)} salidas"
                raise ProjectError(f"See-through no devolvió un archivo PSD descargable (respuesta: {shape}).")
            shutil.copy2(psd_result, output_path)
        layer_count = import_decomposition_psd(
            project_path, output_path, square.size, original.size, offset,
            group_layers=group_layers,
        )
        imported = load_project(project_path).get("decomposition", {})
        quality = imported.get("quality", {})
        return {
            "psd": str(output_path), "layer_count": layer_count,
            "image_size": list(original.size), "quality_warnings": quality.get("warnings", []),
            "group_name": imported.get("group_name"),
        }
    except ProjectError:
        raise
    except Exception as exc:
        detail = str(exc)
        if "upstream Gradio app has raised an exception" in detail:
            raise ProjectError(
                "See-through recibió la solicitud, pero falló dentro del servidor de Hugging Face. "
                "El Space no publicó la excepción interna, así que RigM no puede identificar la causa exacta. "
                "Deja desactivada la opción experimental izquierda/derecha e inténtalo más tarde; "
                "la cuota GPU puede haberse consumido aunque no se haya generado el PSD."
            ) from exc
        raise ProjectError(
            "No se pudo completar See-through. Comprueba la sesión de Hugging Face, la conexión "
            f"y la disponibilidad del servicio: {detail}"
        ) from exc
