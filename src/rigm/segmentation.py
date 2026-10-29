"""Local, promptable SAM 2 segmentation adapter."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

DEFAULT_MODEL = "facebook/sam2.1-hiera-tiny"


@lru_cache(maxsize=2)
def _load_model(model_id: str):
    try:
        import torch
        from transformers import Sam2Model, Sam2Processor
    except ImportError as exc:
        raise RuntimeError(
            "La segmentación SAM 2 requiere PyTorch y Transformers. Instala RigM con: "
            "pip install -e '.[segmentation]'"
        ) from exc

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    try:
        processor = Sam2Processor.from_pretrained(model_id)
        model = Sam2Model.from_pretrained(model_id).to(device).eval()
    except Exception as exc:
        raise RuntimeError(
            f"No pude cargar {model_id}. Comprueba la conexión inicial o que el modelo esté en la caché local: {exc}"
        ) from exc
    return torch, processor, model, device


def propose_masks(
    image_path: str | Path,
    positive_points: list[tuple[float, float]],
    negative_points: list[tuple[float, float]] | None = None,
    box: tuple[float, float, float, float] | None = None,
    model_id: str = DEFAULT_MODEL,
) -> list[tuple[Image.Image, float]]:
    """Return SAM's three ranked candidates for one prompt set, fully local at inference."""
    if not positive_points and box is None:
        raise ValueError("Añade al menos un punto positivo o una caja alrededor de la pieza.")
    negative_points = negative_points or []
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    points = list(positive_points) + list(negative_points)
    labels = [1] * len(positive_points) + [0] * len(negative_points)
    for x, y in points:
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("Hay un punto fuera de los límites de la imagen.")
    if box is not None:
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("La caja de selección está fuera de los límites o tiene tamaño cero.")
    torch, processor, model, device = _load_model(model_id)
    kwargs = {"images": image, "return_tensors": "pt"}
    if points:
        kwargs["input_points"] = [[[list(point) for point in points]]]
        kwargs["input_labels"] = [[labels]]
    if box is not None:
        kwargs["input_boxes"] = [[[float(value) for value in box]]]
    inputs = processor(**kwargs).to(device)
    with torch.inference_mode():
        outputs = model(**inputs, multimask_output=True)
    masks = processor.post_process_masks(outputs.pred_masks.cpu(), inputs["original_sizes"])[0]
    scores = outputs.iou_scores[0, 0].detach().float().cpu().tolist()
    candidates: list[tuple[Image.Image, float]] = []
    for index in range(masks.shape[1]):
        binary = masks[0, index] > 0
        mask_image = Image.fromarray(binary.byte().numpy() * 255, mode="L")
        candidates.append((mask_image, float(scores[index])))
    return sorted(candidates, key=lambda item: item[1], reverse=True)
