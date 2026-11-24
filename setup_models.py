#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the model weights this app needs from the Hugging Face Hub.

The weights are far too large for git (the reference model alone is 2.1 GB), so
they live in a separate Hub repo and are pulled in here after cloning:

    pip install -r requirements.txt
    python setup_models.py
    python wsgi.py

`python setup_models.py --size small` (or medium; several may be given,
separated by commas) fetches a smaller model size, which needs less GPU memory
and disk space. Large is the default, what this script fetches without
--size. On an install without it the app uses the largest size that is there;
FOOTNOTES_MODEL_SIZE picks the default when there are several.

`python setup_models.py --herbert` also fetches a second NER suite (about 4.2 GB)
that you can choose instead of the default.

Idempotent — already-present files are skipped, so re-running it is cheap and
a partial download can simply be resumed by running it again.

Override the source with FOOTNOTES_MODELS_REPO if you host your own copy.
Surya's OCR weights (about 1.1 GB) are not part of this: Surya downloads them
itself the first time it runs.
"""
import os
import sys
from pathlib import Path

MODELS_REPO = os.environ.get("FOOTNOTES_MODELS_REPO",
                             "FootnotesExtractor/footnotes-extractor-models")
MODELS_DIR = Path(__file__).parent / "models"

# What the app loads by default (app.py: od_model / ner_suite defaults). The
# NER suite is three separate fine-tuned models — reference, component, names —
# applied as a cascade; all three are required.
REQUIRED = [
    "od/yolo11n_validated_v2.pt",
    "ner/validated_v2_xlmr_nested/NER_reference/",
    "ner/validated_v2_xlmr_nested/NER_component/",
    "ner/validated_v2_xlmr_nested/NER_names/",
]

# A second NER suite, with HerBERT for components and names. Too large to come
# along unasked, and the app does not need it, so it only comes with --herbert.
OPTIONAL = [
    "ner/validated_v2_nested/NER_reference/",
    "ner/validated_v2_nested/NER_component/",
    "ner/validated_v2_nested/NER_names/",
]


# The model sizes (app.py: MODEL_SIZES), for --size. A size lists all it needs,
# so it works alone. Large is the default set, REQUIRED; Surya, its OCR, fetches
# its own weights.
SIZES = {
    "small": [
        "od/yolo11n_validated_v2.pt",
        "ner/validated_v2_distilbert_nested/NER_reference/",
        "ner/validated_v2_distilbert_nested/NER_component/",
        "ner/validated_v2_distilbert_nested/NER_names/",
        "ocr/ppocr_small/",
    ],
    "medium": [
        "od/yolov8s_validated_v2.pt",
        "ner/validated_v2_mbert_nested/NER_reference/",
        "ner/validated_v2_mbert_nested/NER_component/",
        "ner/validated_v2_mbert_nested/NER_names/",
        "ocr/ppocr_medium/",
    ],
    "large": REQUIRED,
}
# Download sizes in GB (decimal), for the message before a download.
GB = {"large": 5.6, "herbert": 4.2, "small": 1.7, "medium": 2.3}


def sizes(argv) -> list:
    """The sizes named with --size small,medium or --size=small; SystemExit for
    a name that is no size."""
    named = []
    for i, arg in enumerate(argv):
        if arg == "--size":
            if i + 1 >= len(argv):
                raise SystemExit("--size needs a value: " + ", ".join(SIZES))
            named += argv[i + 1].split(",")
        elif arg.startswith("--size="):
            named += arg.split("=", 1)[1].split(",")
    named = [name.strip().lower() for name in named]
    unknown = [name for name in named if name not in SIZES]
    if unknown:
        raise SystemExit(f"unknown size '{unknown[0]}': choose from {', '.join(SIZES)}")
    return list(dict.fromkeys(named))


def wanted(argv) -> list:
    chosen = sizes(argv)
    paths = [path for size in chosen for path in SIZES[size]] if chosen else list(REQUIRED)
    if "--herbert" in argv:
        paths += OPTIONAL
    return list(dict.fromkeys(paths))


def main() -> int:
    required = wanted(sys.argv)
    patterns = [p + "**" if p.endswith("/") else p for p in required]

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is missing — install dependencies first:\n"
              "    pip install -r requirements.txt", file=sys.stderr)
        return 1

    print(f"Source     : {MODELS_REPO}")
    print(f"Destination: {MODELS_DIR}")
    chosen = sizes(sys.argv)
    total = sum(GB[name] for name in chosen or ["large"]) + (GB["herbert"] if "--herbert" in sys.argv else 0)
    print(f"Downloading (about {total:.1f} GB on a first run; resumable)...\n")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=MODELS_REPO,
            local_dir=str(MODELS_DIR),
            allow_patterns=patterns,
        )
    except Exception as e:
        print(f"\nDownload failed: {e}", file=sys.stderr)
        if type(e).__name__ in ("RepositoryNotFoundError", "GatedRepoError") or " 401 " in str(e):
            print(f"\nThe model weights were not found at {MODELS_REPO}.\nIf you have your own "
                  "copy, set FOOTNOTES_MODELS_REPO or place it under models/.", file=sys.stderr)
        return 1

    missing = [p for p in required
               if not (MODELS_DIR / p.rstrip("/")).exists()]
    if missing:
        print("\nDownload finished but these are still missing:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 1

    print("\nAll required models present. Start the app with:  python wsgi.py")
    if chosen:
        print(f"To make a size the default:  FOOTNOTES_MODEL_SIZE={chosen[0]} python wsgi.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
