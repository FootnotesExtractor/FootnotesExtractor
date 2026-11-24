# Modele: rozmiary i licencje

*[English version](../en/models.md)*

## Rozmiary

Program działa z trzema rozmiarami modeli. Rozmiar to jeden wybór dla wszystkich trzech etapów: znajdowania
przypisów, odczytu (OCR) i analizy referencji.

| | Wykrywanie | OCR | Analiza | Pobranie |
|---|---|---|---|---|
| Mały | YOLO11-n | PP-OCRv6 small | DistilBERT multilingual | 1,7 GB |
| Średni | YOLOv8-s | PP-OCRv6 medium | mBERT | 2,3 GB |
| Duży (domyślny) | YOLO11-n | Surya | XLM-R large i base | 5,6 GB |

Surya pobiera własne wagi (około 1,1 GB) przy pierwszym uruchomieniu. Rozmiar mały i średni nie potrzebują
Suryi.

Pamięć GPU podczas odczytu dokumentu:

| | Pamięć GPU |
|---|---|
| Mały | 4 GB |
| Średni | 6 GB |
| Duży | 10 GB |

Wartości dla rozmiaru małego i średniego zmierzono na skanie liczącym 88 stron (w użyciu 3,0 i 5,0 GB). Wyjątkowo
duże obszary przypisów wymagają więcej, a dokument, który się nie mieści, kończy się komunikatem o błędzie.

Mniejsze modele czytają mniej dokładnie. Jeśli wynik jest ważny, warto przed wyborem sprawdzić kilka stron
własnych dokumentów w każdym rozmiarze.

### Instalacja rozmiaru

```bash
python setup_models.py --size small            # lub medium; oba: small,medium
FOOTNOTES_MODEL_SIZE=small python wsgi.py --host 127.0.0.1
```

`FOOTNOTES_MODEL_SIZE` ustawia domyślny rozmiar nowych przesłań. Bez tej zmiennej program używa największego
rozmiaru, który jest na dysku: dużego, pobieranego przez `python setup_models.py` bez `--size`, jeśli jest
dostępny.

### Wybór rozmiaru dla jednego przesłania

- **W przeglądarce:** otworzyć **Opcje zaawansowane** i wybrać **Rozmiar modeli**. Ustawia on razem wykrywanie,
  analizę i OCR. Po zmianie jednego z nich widać **Własny wybór**. Rozmiary niezainstalowane są wymienione, ale
  nie można ich wybrać.
- **Przez API:** wysłać `model_size` w `POST /api/v1/documents`; zob. [API](api.md#dokumenty).
- Konta bez funkcji **Opcje zaawansowane** zawsze używają ustawień domyślnych.
- Dokument pamięta modele, którymi został przetworzony, i późniejsze poprawki korzystają z tych samych.

## Licencje

| Część | Licencja |
|---|---|
| FootnotesExtractor i jego wytrenowane modele | AGPL-3.0 |
| Wykrywanie: Ultralytics YOLO11 i YOLOv8 | AGPL-3.0 (Ultralytics oferuje też licencję komercyjną) |
| Analiza, XLM-R large i base (Facebook AI) | MIT |
| Analiza, mBERT (Google) | Apache-2.0 |
| Analiza, DistilBERT multilingual | Apache-2.0 |
| Analiza, opcjonalny zestaw HerBERT (Allegro) | CC BY 4.0: wymaga zachowania informacji o autorach |
| OCR, PP-OCRv6 (PaddleOCR, Baidu; przekształcone dla RapidOCR) | Apache-2.0 |
| OCR, Surya: program | GPL-3.0-or-later |
| OCR, Surya: wagi modeli | CC BY-NC-SA 4.0; autor uchyla część niekomercyjną dla organizacji o przychodach brutto poniżej 5 mln USD w ostatnich 12 miesiącach i z łącznym finansowaniem od inwestorów poniżej 5 mln USD, o ile nie konkurują z Datalab API |
| PyMuPDF | AGPL-3.0 |
| RapidOCR | Apache-2.0 |
| PyTorch, transformers, OpenCV, Flask i inne biblioteki | BSD, MIT, Apache-2.0 i podobne |

- **Użycie komercyjne.** Część książek użytych do trenowania jest na licencjach niekomercyjnych, więc komercyjne
  użycie wytrenowanych modeli wymagałoby ponownego trenowania bez nich. Dotyczy to każdego rozmiaru. Rozmiar
  mały i średni nie używają wag Suryi, więc to ograniczenie ich nie dotyczy.
- **Udostępnianie na tych samych warunkach.** Zmieniona wersja programu, z której inni korzystają przez sieć, musi
  udostępniać swój kod źródłowy (AGPL-3.0).
- **Informacja o autorach HerBERT.** Publikując modele oparte na zestawie HerBERT, trzeba podać HerBERT i jego
  licencję.
- Ta strona jest streszczeniem. Wiążące są teksty licencji i karty modeli. W razie wątpliwości warto zasięgnąć
  porady.
