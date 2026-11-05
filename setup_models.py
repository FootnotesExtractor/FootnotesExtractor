#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the model weights this app needs from the Hugging Face Hub.

The weights are far too large for git (the reference model alone is 2.1 GB), so
they live in a separate Hub repo and are pulled in here after cloning:

    pip install -r requirements.txt
    python setup_models.py
    python wsgi.py

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


def wanted(argv) -> list:
    return REQUIRED + OPTIONAL if "--herbert" in argv else list(REQUIRED)


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
    size = "~10 GB" if len(required) > len(REQUIRED) else "~5.6 GB"
    print(f"Downloading ({size} on a first run; resumable)...\n")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
