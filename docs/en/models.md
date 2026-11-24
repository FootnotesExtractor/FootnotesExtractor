# Models: sizes and licences

*[Polska wersja](../pl/models.md)*

## Sizes

The app works with three sizes of models. A size is one choice for all three stages: finding the footnotes,
reading them (OCR) and parsing the references.

| | Detection | OCR | Parsing | Download |
|---|---|---|---|---|
| Small | YOLO11-n | PP-OCRv6 small | DistilBERT multilingual | 1.7 GB |
| Medium | YOLOv8-s | PP-OCRv6 medium | mBERT | 2.3 GB |
| Large (default) | YOLO11-n | Surya | XLM-R large and base | 5.6 GB |

Surya fetches its own weights (about 1.1 GB) the first time it runs. The small and medium sizes need no
Surya.

GPU memory while reading a document:

| | GPU memory |
|---|---|
| Small | 4 GB |
| Medium | 6 GB |
| Large | 10 GB |

The small and medium figures were measured on an 88-page scan (3.0 and 5.0 GB in use). Unusually large footnote
areas need more; a document that does not fit fails with a message.

Smaller models read less accurately. If the results matter, check a few pages of your own documents with
each size before you choose.

### Install a size

```bash
python setup_models.py --size small            # or medium; small,medium for both
FOOTNOTES_MODEL_SIZE=small python wsgi.py --host 127.0.0.1
```

`FOOTNOTES_MODEL_SIZE` sets the default for new uploads. Without it the app uses the largest size that is
on disk: the large one, which `python setup_models.py` without `--size` fetches, when it is there.

### Choose a size for one upload

- **In the browser:** open **Advanced Options** and pick a **Model size**. It sets detection, parsing and
  OCR together; changing one of them afterwards shows *Custom*. Sizes that are not installed are listed and
  cannot be picked.
- **Through the API:** send `model_size` with `POST /api/v1/documents`; see the [API](api.md#documents).
- Accounts without the **Advanced Options** function always use the default.
- A document remembers the models it was processed with, and later edits use the same ones.

## Licences

| Part | Licence |
|---|---|
| FootnotesExtractor, its trained models | AGPL-3.0 |
| Detection: Ultralytics YOLO11 and YOLOv8 | AGPL-3.0 (an Ultralytics enterprise licence is available from them) |
| Parsing, XLM-R large and base (Facebook AI) | MIT |
| Parsing, mBERT (Google) | Apache-2.0 |
| Parsing, DistilBERT multilingual | Apache-2.0 |
| Parsing, optional HerBERT set (Allegro) | CC BY 4.0: keep the attribution |
| OCR, PP-OCRv6 (PaddleOCR, Baidu; converted for RapidOCR) | Apache-2.0 |
| OCR, Surya: program | GPL-3.0-or-later |
| OCR, Surya: model weights | CC BY-NC-SA 4.0; its author waives the non-commercial part for organisations below 5 million USD in gross revenue over the last 12 months and 5 million USD in total venture or angel funding, if they do not compete with the Datalab API |
| PyMuPDF | AGPL-3.0 |
| RapidOCR | Apache-2.0 |
| PyTorch, transformers, OpenCV, Flask and the other libraries | BSD, MIT, Apache-2.0 and similar |

- **Commercial use.** Some of the books used for training are under non-commercial licences, so commercial
  use of the trained models would require retraining without them. This holds for every size. The small
  and medium sizes avoid Surya's weights, so that restriction does not apply to them.
- **Share-alike.** A modified version of the app that other people use over a network must offer its
  source code (AGPL-3.0).
- **Attribution for HerBERT.** If you publish models made from the HerBERT set, name HerBERT and its
  licence.
- This page is a summary. The licence texts and the model cards are what counts; check them, and get advice
  if you are unsure.
