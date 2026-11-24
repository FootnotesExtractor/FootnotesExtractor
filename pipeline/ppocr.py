#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PP-OCRv6 (PaddleOCR's models, run by RapidOCR through PyTorch): the OCR of the
small and medium model sizes. Far smaller than Surya (31 and 139 MB).

The weights come from models/ocr/<engine>/ (setup_models.py --size), never from
the network at run time. They run on the GPU whenever there is one: an engine
that ended up on the CPU beside a GPU refuses to start.
"""

from pathlib import Path
from typing import Any, Dict, List

import torch
from PIL import Image

MODELS_DIR = Path(__file__).resolve().parent.parent / 'models' / 'ocr'

# engine key -> PP-OCRv6 model size
ENGINES = {'ppocr_small': 'small', 'ppocr_medium': 'medium'}


def model_files(engine: str) -> Dict[str, Path]:
    """The files an engine needs. RapidOCR picks the network's architecture by
    the file name, so the names are fixed."""
    folder = MODELS_DIR / engine
    size = ENGINES[engine]
    return {'det': folder / f'PP-OCRv6_det_{size}.pth',
            'rec': folder / f'PP-OCRv6_rec_{size}.pth',
            'dict': folder / 'ppocrv6_dict.txt'}


def is_installed(engine: str) -> bool:
    return all(path.exists() for path in model_files(engine).values())


class PPOCR:
    def __init__(self, engine: str):
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        files = model_files(engine)
        missing = [str(path) for path in files.values() if not path.exists()]
        if missing:
            raise FileNotFoundError(f"PP-OCR model files not found: {', '.join(missing)} "
                                    f"(python setup_models.py --size {ENGINES[engine]})")
        on_gpu = torch.cuda.is_available()
        size = ModelType(ENGINES[engine])
        self._ocr = RapidOCR(params={
            'Global.log_level': 'error',
            'Global.use_cls': False,                 # footnote text is upright
            'Det.engine_type': EngineType.TORCH, 'Rec.engine_type': EngineType.TORCH,
            'Det.ocr_version': OCRVersion('PP-OCRv6'), 'Rec.ocr_version': OCRVersion('PP-OCRv6'),
            'Det.model_type': size, 'Rec.model_type': size,
            'Det.lang_type': LangDet('ch'), 'Rec.lang_type': LangRec('ch'),   # one multilingual model
            'Det.model_path': str(files['det']), 'Rec.model_path': str(files['rec']),
            'Rec.rec_keys_path': str(files['dict']),
            'EngineConfig.torch.use_cuda': on_gpu,
        })
        # RapidOCR loads lazily; load now, under the caller's MODEL_LOAD_LOCK
        for model in (self._ocr._load_det_model(), self._ocr._load_rec_model()):
            device = next(model.session.predictor.parameters()).device
            if on_gpu and device.type != 'cuda':
                raise RuntimeError(f'PP-OCR is on {device}, not on the GPU; not reading on the CPU')

    def read(self, image: Image.Image) -> List[Dict[str, Any]]:
        """The lines of one image: [{text, min_x, max_x, min_y, max_y}]."""
        import numpy as np

        # RapidOCR takes arrays in OpenCV's channel order
        result = self._ocr(np.asarray(image.convert('RGB'))[:, :, ::-1].copy())
        if result.txts is None:
            return []
        lines = []
        for box, text in zip(result.boxes, result.txts):
            text = text.strip()
            if text:
                xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
                lines.append({'text': text, 'min_x': min(xs), 'max_x': max(xs), 'min_y': min(ys), 'max_y': max(ys)})
        return lines
