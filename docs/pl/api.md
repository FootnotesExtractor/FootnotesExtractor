# HTTP API

*[English version](../en/api.md)*

Wszystko, co robi interfejs, jest dostępne przez HTTP. Obowiązują te same konta i te same zasady dostępu:
dokumenty są prywatne, udostępniane przez grupy, a administratorzy widzą wszystko.

- [Uwierzytelnianie](#uwierzytelnianie)
- [Szybki start](#szybki-start)
- [Dokumenty](#dokumenty)
- [Wyniki i eksport](#wyniki-i-eksport)
- [Edycja przypisów](#edycja-przypisów)
- [Sprawdzanie](#sprawdzanie)
- [Bibliografie](#bibliografie)
- [Grupy i kolekcje](#grupy-i-kolekcje)
- [Kilka dokumentów naraz](#kilka-dokumentów-naraz)
- [Błędy](#błędy)

Przykłady używają adresu lokalnego `http://127.0.0.1:5002`.

## Uwierzytelnianie

Klucz tworzy się na stronie **Profil**. Jest pokazywany tylko raz, więc trzeba go od razu skopiować. Wysyła
się go z każdym żądaniem jako `X-API-Key: <klucz>` albo `Authorization: Bearer <klucz>`. Bez ważnego klucza
odpowiedź to `401 {"error": "Login required"}`.

## Szybki start

```bash
KEY=fnk_...
BASE=http://127.0.0.1:5002

# przesłanie
curl -H "X-API-Key: $KEY" -F "file=@ksiazka.pdf" $BASE/api/v1/documents
# {"job_id": "8ba00070-...", "job_ids": ["8ba00070-..."]}

# status
curl -H "X-API-Key: $KEY" $BASE/api/v1/documents/<job_id>

# eksport
curl -H "X-API-Key: $KEY" -o ksiazka.bib "$BASE/download/<job_id>?format=bibtex_expanded"
```

To samo w Pythonie, dla kilku plików PDF:

```python
import time
import requests

BASE = "http://127.0.0.1:5002"
HEADERS = {"X-API-Key": "fnk_..."}

def upload(path, **options):
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/api/v1/documents", headers=HEADERS,
                          files={"file": (path, f, "application/pdf")}, data=options)
    r.raise_for_status()
    return r.json()["job_id"]

def wait(job_id, seconds=5, timeout=3600):
    end = time.time() + timeout
    while time.time() < end:
        status = requests.get(f"{BASE}/api/v1/documents/{job_id}", headers=HEADERS).json()
        if status["status"] in ("completed", "error"):
            return status
        time.sleep(seconds)
    raise TimeoutError(job_id)

for pdf in ["artykul1.pdf", "artykul2.pdf"]:
    job = upload(pdf)
    status = wait(job)
    if status["status"] == "error":
        print(pdf, "błąd:", status["error"])
        continue
    results = requests.get(f"{BASE}/api/results/{job}", headers=HEADERS).json()
    print(pdf, "przypisy:", len(results["results"]))
```

Dokumenty są przetwarzane po kolei, więc późniejsze pozostają w stanie `queued` do zakończenia wcześniejszych.

## Dokumenty

| Metoda i adres | Działanie |
|---|---|
| `POST /api/v1/documents` | Przesłanie jednego lub kilku PDF (pole multipart `file` albo powtarzane `files`) |
| `GET /api/v1/documents` | Lista widocznych dokumentów |
| `GET /api/v1/documents/<job_id>` | Status dokumentu |
| `GET /api/status/<job_id>` | Status z czasem przetwarzania albo czasem, który upłynął |
| `GET /api/v1/documents/<job_id>/pdf` | Przesłany plik PDF |
| `GET`, `PUT /api/v1/documents/<job_id>/book-metadata` | Odczyt lub zmiana metadanych |
| `POST /api/job/<job_id>/reprocess` | Ponowne przetworzenie. Wyniki, łącznie z poprawkami, zostają zastąpione |
| `POST /api/job/<job_id>/share` | `{"group_id": 3}` udostępnia grupie, `null` przywraca prywatność |
| `DELETE /api/job/<job_id>` | Usunięcie dokumentu, jego wyników i pliku PDF. Nie można tego cofnąć |

`status` ma wartość `queued`, `processing`, `completed` lub `error`.

- **Ponowne przetworzenie** zachowuje identyfikator, PDF, opcje, metadane, właściciela i kolekcje. Poprzednie
  wyniki są najpierw kopiowane do `results/reprocess_backups/`. `409`, gdy dokument czeka lub jest
  przetwarzany, `422`, gdy brakuje pliku PDF.
- **Udostępnia** właściciel dokumentu lub administrator. **Usuwać** mogą także edytorzy i właściciele grupy, której
  dokument jest udostępniony.

Opcjonalne pola formularza przy przesyłaniu:

| Pole | Domyślnie | Znaczenie |
|---|---|---|
| `od_conf` | `0.5` | Próg pewności wykrywania przypisów |
| `ref_conf`, `comp_conf` | `0.7` | Progi pewności referencji i ich części |
| `batch_size` | `8` | Od 1 do 64 |
| `model_size` | brak | `small`, `medium` lub `large`: ustawia trzy modele poniżej (zob. [Modele](models.md)). Model podany osobno ma pierwszeństwo. `400`, gdy rozmiar jest nieznany lub niezainstalowany |
| `od_model` | `yolo11n_validated_v2` | Model wykrywania |
| `ner_suite` | `validated_v2_xlmr_nested` | Modele analizy. `validated_v2_nested` jest dostępny, jeśli pobrano go z `--herbert` |
| `ocr_engine` | `surya` | `surya`, `ppocr_small`, `ppocr_medium` albo `pdfminer` dla warstwy tekstowej PDF. Nieznana nazwa też oznacza warstwę tekstową; silnik niezainstalowany daje `400` |
| `split_footnotes` | `true` | Podział obszarów według numerów przypisów. Wyłączyć, gdy numeracja nie jest ciągła |
| `enable_ibidem` | `true` | Przejmowanie brakujących części dla *ibid.* i *op. cit.* |
| `split_multi_work` | `true` | Podział referencji zawierającej kilka prac |
| `enable_grobid` | `false` | Szukanie bibliografii w samej książce; wymaga **Pokaż odwołania** |
| `enable_enrichment` | `false` | Wyszukiwanie referencji w zewnętrznych katalogach; wymaga **Wzbogacanie metadanych** |
| `debug`, `cpu_mode` | `false` | Szczegółowe statystyki; przetwarzanie na CPU |
| `group_id` | brak | Udostępnienie grupie, do której należy użytkownik |
| `collection_id` | brak | Dodanie do kolekcji, którą można edytować |
| `authors`, `title`, `year`, `publisher`, `license`, `isbn`, `issn`, `journal`, `doi` | brak | Metadane wszystkich plików z przesłania |

Konta bez **Opcji zaawansowanych** zawsze otrzymują domyślne modele, progi i opcje przetwarzania. Limity to
200 MB na plik PDF oraz 900 plików i 850 MB na przesłanie. Przekroczenie limitu daje odpowiedź `413`, a nic nie
trafia do kolejki.

```bash
curl -X PUT -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"authors": "Jan Kowalski", "year": "2020"}' \
     $BASE/api/v1/documents/<job_id>/book-metadata
```

Pominięte pola zachowują wartość, a pusty tekst czyści pole.

## Wyniki i eksport

`GET /api/results/<job_id>` zwraca pełny wynik: `metadata` i `results`. `results` to lista przypisów. Każdy ma
`id`, `text`, `page`, `bbox`, `confidence` i `references`. Referencja ma `id`, `text`, `start`, `end`,
`confidence`, `citation_type` i `components`. Część referencji ma `id`, `label`, `text`, `start`, `end`,
`confidence` i `names`. Sumy dla dokumentu są w `metadata` oraz w `summary` jego statusu.

`GET /download/<job_id>?format=...` zwraca plik:

| `format` | Plik |
|---|---|
| brak | JSON, taki sam jak `/api/results` |
| `bibtex_standard` | BibTeX; *ibid.* i *op. cit.* pozostają w zapisanej postaci |
| `bibtex_expanded` | BibTeX; *ibid.* i *op. cit.* zastąpione pracą, do której się odnoszą |
| `apa`, `chicago`, `mla` | Sformatowana bibliografia, zwykły tekst, zawsze z zastąpionymi odsyłaczami |
| `reference_strings` | Każda referencja tak, jak została wyodrębniona |

Każda praca pojawia się raz, ze wszystkimi miejscami cytowania. Nieznany `format` zwraca JSON. Eksport nie
poprawia odczytu: referencja odczytana błędnie zostaje zapisana tak, jak została odczytana.

`GET /api/v1/documents/<job_id>/annotations` zwraca to samo co `/api/results` oraz dodatkowo `footnotes` dla
widoków. `GET /api/v1/documents/<job_id>/inferred-components` zwraca, do czego odnosi się obecnie każde *ibid.*
i *op. cit.*

## Edycja przypisów

Wszystkie adresy poniżej zaczynają się od `/api/v1/documents/<job_id>`. Wymagane są prawa edycji: właściciel,
administrator albo edytor lub właściciel grupy, której dokument jest udostępniony.

| Metoda i adres | Działanie |
|---|---|
| `POST /footnotes` | Dodanie ramki przypisu: `{"page", "bbox": {"x1", "y1", "x2", "y2"}}` |
| `PUT`, `DELETE /footnotes/<id>` | Zmiana lub usunięcie ramki |
| `PUT /footnotes/<id>/text` | `{"text", "reclassify": true}` |
| `POST /footnotes/<id>/split` | `{"offset", "reclassify": true}`: dwa przypisy |
| `POST /footnotes/merge` | `{"footnote_ids": [...], "reclassify": true}`: jeden przypis |
| `POST /footnotes/<id>/restore` | Przywrócenie przypisu do wcześniejszego stanu |
| `POST /footnotes/reorder`, `POST /footnotes/<id>/move-page` | Kolejność przypisów na stronie |
| `POST /footnotes/classify-preview` | `{"text"}`: wynik analizy bez zapisywania |
| `POST`, `PUT`, `DELETE /footnotes/<id>/references[/<id>]` | Referencje. Treść: `{"start", "end"}` |
| `POST /footnotes/<id>/references/merge` | `{"ids": [...]}` |
| `POST /footnotes/<id>/references/<id>/split` | Podział referencji na prace: `{}` lub `{"cuts": [...]}` |
| `POST /footnotes/<id>/references/<id>/works` | Zaznaczenie jednej pracy w referencji: `{"start", "end"}` |
| `PUT /footnotes/<id>/references/<id>/works/<work_id>` | Przesunięcie granic pracy |
| `POST /footnotes/<id>/references/<id>/works/merge` | Połączenie sąsiednich prac: `{"ids": [...]}` |
| `PUT /footnotes/<id>/references/<id>/works/<work_id>/inherited` | Przejęte części: `{"inherited": [{"source_component_id"}]}`, `[]` usuwa, lub `{"from_previous": ["Author"]}` |
| `POST`, `PUT`, `DELETE /.../references/<id>/components[/<id>]` | Części referencji. Treść: `{"start", "end", "label"}` |
| `POST`, `PUT`, `DELETE /.../components/<id>/names[/<id>]` | Nazwiska w częściach Author, Editor i Translator |
| `PUT /.../references/<id>/source` | `{"source_reference_id"}`: do czego odnosi się *ibid.*, `null` przywraca wybór automatyczny |
| `PUT /.../references/<id>/inferred` | `{"inferred_removed", "inferred_added"}`: zmiana przejmowanych części |

Położenia liczą znaki w polu `text` przypisu. Etykiety: `Author`, `Editor`, `Translator`, `Title`, `BookTitle`,
`Journal`, `Volume`, `Issue`, `Publisher`, `Pages`, `PublicationPlace`, `PublicationYear`,
`PublicationYearSuffix`, `DOI`, `Webpage`, `Ibidem`, `OpusCitatum`; dla nazwisk `FirstName`, `LastName`.

- Przy `"reclassify": true` (domyślnie) referencje i ich części są wyszukiwane od nowa w zmienionym tekście.
  Przy `false` przypis pozostaje nieoznaczony, a `needs_classification` ma wartość `true`.
- **Tekst** zostaje zastąpiony bez OCR, a ramka się nie zmienia. **Podział** przyjmuje położenie znaku wewnątrz
  tekstu i zwraca `{"first", "second"}`; obie części zachowują ramkę oryginału. **Połączenie** układa przypisy
  w kolejności czytania. Gdy są na różnych stronach, zostaje ramka pierwszego, a odpowiedź zawiera
  `"bbox_approximate": true`.
- **Cofanie:** podział i połączenie usuwają oryginały. Należy usunąć nowe przypisy i przywrócić oryginały przez
  `restore`.
- **Modele:** `GET /ner-status` i `GET /ocr-status` podają, które modele służą do poprawek, czy da się je
  wczytać i jakie są inne. `PUT /ner-suite` (`{"ner_suite"}`) oraz `PUT /ocr-engine`
  (`{"ocr_engine": "surya"}`, `"ppocr_small"`, `"ppocr_medium"` lub `"pdfminer"`) zmieniają je dla kolejnych poprawek, a zapisane dane zostają.

## Sprawdzanie

| Metoda i adres | Działanie |
|---|---|
| `GET /api/v1/documents/<job_id>/verification-status` | Czy dokument jest zweryfikowany i przez kogo |
| `POST /api/v1/documents/<job_id>/verify` | `{"verified": true}` lub `false` |
| `POST /api/v1/documents/<job_id>/pages/<page>/approval` | `{"approved": true}` lub `false` |
| `GET /api/v1/documents/<job_id>/layout-prior` | Czego Asystent Układu nauczył się o ramkach i jego sugestie |
| `GET /api/v1/documents/<job_id>/ner-prior` | Jego sugestie etykiet |
| `GET /api/v1/documents/<job_id>/checks` | Kontrole spójności |
| `GET`, `POST /api/v1/documents/<job_id>/review-flags` | Znaczniki „do przejrzenia”; wymagana funkcja **Do przejrzenia** |

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"verified": true}' $BASE/api/v1/documents/<job_id>/verify
# {"verified": true, "verified_at": "...", "verified_by": "..."}
```

## Bibliografie

Wymagana funkcja **Łączenie bibliografii**. Bibliografię ustawia administrator. Obecnie przyjmowane są tylko
bibliografie BibTeX; pobieranie bibliografii z samej książki jest wyłączone.

| Metoda i adres | Działanie |
|---|---|
| `GET /api/v1/documents/<job_id>/reference-list` | Pozycje, połączenia i liczby. `?format=csv` zwraca tabelę |
| `POST /api/v1/documents/<job_id>/reference-list` | `{"source": "bibtex", "bibtex": "..."}` |
| `POST /api/v1/documents/<job_id>/reference-list/links` | `{"action", "reference_id", "entry_id"}` |
| `PATCH /api/v1/documents/<job_id>/reference-list/entries/<entry_id>` | Poprawienie pozycji |

`GET` zwraca `source`, `state`, `entries`, `links` i `counts` oraz elementy niepołączone po obu stronach.
`state` to `none`, `running`, `ready` lub `error`. Połączenie ma `reference_id`, `entry_id`, `status` (`auto`,
`confirmed` lub `added`), `rule` i `score`. Ustawienie bibliografii zastępuje poprzednią; decyzje dotyczące
pozostałych pozycji są zachowane, a odpowiedź podaje, ile odpadło. Plik `.bib` można też wysłać jako pole
multipart `bibtex`.

`action` to `confirm`, `reject`, `add` lub `clear`. Połączenia są wyznaczane przy każdym odczycie listy, a
decyzje użytkownika zawsze mają pierwszeństwo. Dodanie połączenia zastępuje inne połączenia danej referencji.

## Grupy i kolekcje

| Metoda i adres | Działanie |
|---|---|
| `GET`, `POST /api/v1/groups` | Lista grup, utworzenie grupy (`{"name"}`) |
| `GET`, `DELETE /api/v1/groups/<id>` | Szczegóły, usunięcie |
| `POST /api/v1/groups/<id>/members` | `{"username", "role"}`: `viewer`, `editor` lub `owner` |
| `PATCH`, `DELETE /api/v1/groups/<id>/members/<user_id>` | Zmiana roli, usunięcie członka |
| `GET`, `POST /api/v1/collections` | Lista, utworzenie (`{"name", "description", "group_id"}`) |
| `GET`, `PATCH`, `DELETE /api/v1/collections/<id>` | Szczegóły, zmiana nazwy, usunięcie |
| `POST /api/v1/collections/<id>/share` | `{"group_id": 3}` lub `null` |
| `POST /api/v1/collections/<id>/jobs` | Dodanie dokumentu: `{"job_id"}` |
| `DELETE /api/v1/collections/<id>/jobs/<job_id>` | Usunięcie dokumentu z kolekcji |

- **Role w grupie.** Widz czyta. Edytor może też edytować i usuwać dokumenty udostępnione grupie. Właściciel może
  też dodawać i usuwać członków oraz zmieniać role. Grupa zawsze ma co najmniej jednego właściciela (`422`).
- **Usunięcie grupy** nie usuwa dokumentów: wszystko, co było jej udostępnione, staje się prywatne.
- **Kolekcje.** Każdy użytkownik ma kolekcję domyślną, której nie można usunąć (`400`). Dokumenty dodaje i usuwa
  jej właściciel, administrator albo edytor lub właściciel jej grupy. Nazwę i udostępnianie zmienia oraz kolekcję
  usuwa tylko jej właściciel lub administrator.

## Kilka dokumentów naraz

Najwyżej 500 dokumentów w żądaniu. Pozycje, do których brak uprawnień, zostają pominięte i wymienione w
odpowiedzi.

| Metoda i adres | Treść |
|---|---|
| `POST /api/jobs/bulk-delete` | `{"job_ids": [...]}` |
| `POST /api/jobs/bulk-add-to-collection` | `{"job_ids": [...], "collection_id": 5}` |
| `POST /api/jobs/bulk-remove-from-collection` | `{"job_ids": [...], "collection_id": 5}` |
| `POST /api/jobs/bulk-unshare` | `{"job_ids": [...], "group_id": 3}` |
| `GET /api/jobs/bulk-export-citations?job_ids=a&job_ids=b&format=apa` | brak; jeden plik ze wszystkimi dokumentami, nieznany `format` daje `400` |

Odpowiedź wymienia to, co się udało, i to, co pominięto (`skipped`), z powodem. Zamiast `collection_id` przy
dodawaniu do kolekcji można podać `"new_collection": {"name": "..."}`.

## Błędy

Błędy mają postać `{"error": "..."}` z kodem stanu.

| Kod | Znaczenie |
|---|---|
| `400` | Błędne dane wejściowe |
| `401` | Brak ważnego klucza |
| `403` | Brak uprawnień lub funkcja wyłączona dla konta |
| `404` | Nie znaleziono albo brak dostępu |
| `409` | Konflikt, na przykład nakładające się zakresy |
| `413` | Przesyłany plik jest za duży |
| `422` | Żądanie jest poprawne, ale nie da się go wykonać w obecnym stanie dokumentu |
