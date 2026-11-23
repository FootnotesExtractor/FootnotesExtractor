# HTTP API

*[Polska wersja](../pl/api.md)*

Everything the interface does is available over HTTP. The calls use the same accounts and the same access
rules as the interface: documents are private, shared through groups, and visible to administrators.

- [Authentication](#authentication)
- [Quick start](#quick-start)
- [Documents](#documents)
- [Results and exports](#results-and-exports)
- [Editing footnotes](#editing-footnotes)
- [Reviewing](#reviewing)
- [Reference lists](#reference-lists)
- [Groups and collections](#groups-and-collections)
- [Several documents at once](#several-documents-at-once)
- [Errors](#errors)

The examples use the local address `http://127.0.0.1:5002`.

## Authentication

Create a key on your **Profile** page. It is shown once, so copy it. Send it with every request as either
`X-API-Key: <key>` or `Authorization: Bearer <key>`. Without a valid key the answer is
`401 {"error": "Login required"}`.

## Quick start

```bash
KEY=fnk_...
BASE=http://127.0.0.1:5002

# upload
curl -H "X-API-Key: $KEY" -F "file=@book.pdf" $BASE/api/v1/documents
# {"job_id": "8ba00070-...", "job_ids": ["8ba00070-..."]}

# status
curl -H "X-API-Key: $KEY" $BASE/api/v1/documents/<job_id>

# export
curl -H "X-API-Key: $KEY" -o book.bib "$BASE/download/<job_id>?format=bibtex_expanded"
```

The same in Python, with several PDFs:

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

for pdf in ["paper1.pdf", "paper2.pdf"]:
    job = upload(pdf)
    status = wait(job)
    if status["status"] == "error":
        print(pdf, "failed:", status["error"])
        continue
    results = requests.get(f"{BASE}/api/results/{job}", headers=HEADERS).json()
    print(pdf, "footnotes:", len(results["results"]))
```

Documents are processed in order, so later uploads stay at `queued` until the earlier ones finish.

## Documents

| Method and path | What it does |
|---|---|
| `POST /api/v1/documents` | Upload one or more PDFs (multipart field `file`, or `files` repeated) |
| `GET /api/v1/documents` | List the documents you can see |
| `GET /api/v1/documents/<job_id>` | Status of one document |
| `GET /api/status/<job_id>` | Status with the processing time, or the time elapsed so far |
| `GET /api/v1/documents/<job_id>/pdf` | The uploaded PDF |
| `GET`, `PUT /api/v1/documents/<job_id>/book-metadata` | Read or change the metadata |
| `POST /api/job/<job_id>/reprocess` | Process again. Results, including corrections, are replaced |
| `POST /api/job/<job_id>/share` | `{"group_id": 3}` shares with a group, `null` makes it private |
| `DELETE /api/job/<job_id>` | Delete the document, its results and its PDF. Cannot be undone |

`status` is one of `queued`, `processing`, `completed`, `error`.

- **Reprocess** keeps the job id, PDF, options, metadata, owner and collections. The previous results are
  copied to `results/reprocess_backups/` first. `409` while the document is queued or processing, `422` if its
  PDF is gone.
- **Share** is for the document's owner or an administrator. **Delete** is also open to editors and owners of
  the group the document is shared with.

Optional form fields at upload:

| Field | Default | Meaning |
|---|---|---|
| `od_conf` | `0.5` | Confidence threshold of the footnote detector |
| `ref_conf`, `comp_conf` | `0.7` | Confidence thresholds for references and their parts |
| `batch_size` | `8` | Between 1 and 64 |
| `od_model` | `yolo11n_validated_v2` | Detection model |
| `ner_suite` | `validated_v2_xlmr_nested` | Parsing models. `validated_v2_nested` is available if it was downloaded with `--herbert` |
| `ocr_engine` | `surya` | Any other value uses the PDF's text layer |
| `split_footnotes` | `true` | Split areas by footnote number. Turn it off when the numbers are not continuous |
| `enable_ibidem` | `true` | Take over missing parts for *ibid.* and *op. cit.* |
| `split_multi_work` | `true` | Split a reference that holds several works into works |
| `enable_grobid` | `false` | Look for the book's own reference list; needs **Show References** |
| `enable_enrichment` | `false` | Look references up in external catalogues; needs **Metadata Enrichment** |
| `debug`, `cpu_mode` | `false` | Detailed statistics; processing on the CPU |
| `group_id` | none | Share with a group you belong to |
| `collection_id` | none | Add to a collection you can edit |
| `authors`, `title`, `year`, `publisher`, `license`, `isbn`, `issn`, `journal`, `doi` | none | Metadata for all files of the upload |

Accounts without **Advanced Options** always get the default models, thresholds and processing options. The
limits are 200 MB for each PDF, 900 PDFs and 850 MB for an upload. A request over a limit is refused with
`413`, and nothing is queued.

```bash
curl -X PUT -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"authors": "Jane Doe", "year": "2020"}' \
     $BASE/api/v1/documents/<job_id>/book-metadata
```

Fields you leave out keep their value; an empty string clears the field.

## Results and exports

`GET /api/results/<job_id>` returns the full result: `metadata` and `results`. `results` is a list of
footnotes. Each has `id`, `text`, `page`, `bbox`, `confidence` and `references`. A reference has `id`,
`text`, `start`, `end`, `confidence`, `citation_type` and `components`. A component has `id`, `label`,
`text`, `start`, `end`, `confidence` and `names`. The totals for a document are in `metadata` and in the
`summary` of its status.

`GET /download/<job_id>?format=...` returns a file:

| `format` | File |
|---|---|
| not given | JSON, the same as `/api/results` |
| `bibtex_standard` | BibTeX; *ibid.* and *op. cit.* stay as printed |
| `bibtex_expanded` | BibTeX; *ibid.* and *op. cit.* replaced by the work they refer to |
| `apa`, `chicago`, `mla` | Formatted bibliography, plain text, always with back-references replaced |
| `reference_strings` | Each reference as extracted |

Each work appears once, with every place it is cited. An unknown `format` returns JSON. The extraction is
not corrected here: a reference that was read wrong is exported as it was read.

`GET /api/v1/documents/<job_id>/annotations` returns the same content as `/api/results`, plus `footnotes`,
for viewers. `GET /api/v1/documents/<job_id>/inferred-components` returns what each *ibid.* and *op. cit.*
resolves to now.

## Editing footnotes

All paths below start with `/api/v1/documents/<job_id>`. You need edit rights: the owner, an administrator,
or an editor or owner of the group the document is shared with.

| Method and path | What it does |
|---|---|
| `POST /footnotes` | Add a footnote box: `{"page", "bbox": {"x1", "y1", "x2", "y2"}}` |
| `PUT`, `DELETE /footnotes/<id>` | Change or delete a box |
| `PUT /footnotes/<id>/text` | `{"text", "reclassify": true}` |
| `POST /footnotes/<id>/split` | `{"offset", "reclassify": true}`: two footnotes |
| `POST /footnotes/merge` | `{"footnote_ids": [...], "reclassify": true}`: one footnote |
| `POST /footnotes/<id>/restore` | Put a footnote back to an earlier snapshot |
| `POST /footnotes/reorder`, `POST /footnotes/<id>/move-page` | Order of the footnotes on a page |
| `POST /footnotes/classify-preview` | `{"text"}`: the parsing result, without saving anything |
| `POST`, `PUT`, `DELETE /footnotes/<id>/references[/<id>]` | References. Body: `{"start", "end"}` |
| `POST /footnotes/<id>/references/merge` | `{"ids": [...]}` |
| `POST /footnotes/<id>/references/<id>/split` | Divide a reference into works: `{}` or `{"cuts": [...]}` |
| `POST /footnotes/<id>/references/<id>/works` | Mark one work inside a reference: `{"start", "end"}` |
| `PUT /footnotes/<id>/references/<id>/works/<work_id>` | Move a work's edges |
| `POST /footnotes/<id>/references/<id>/works/merge` | Join neighbouring works: `{"ids": [...]}` |
| `PUT /footnotes/<id>/references/<id>/works/<work_id>/inherited` | Carried-over parts: `{"inherited": [{"source_component_id"}]}`, `[]` clears, or `{"from_previous": ["Author"]}` |
| `POST`, `PUT`, `DELETE /.../references/<id>/components[/<id>]` | Parts. Body: `{"start", "end", "label"}` |
| `POST`, `PUT`, `DELETE /.../components/<id>/names[/<id>]` | Names inside Author, Editor or Translator |
| `PUT /.../references/<id>/source` | `{"source_reference_id"}`: what an *ibid.* refers to, `null` for automatic |
| `PUT /.../references/<id>/inferred` | `{"inferred_removed", "inferred_added"}`: change what is taken over |

Offsets count characters of the footnote's `text`. Labels: `Author`, `Editor`, `Translator`, `Title`,
`BookTitle`, `Journal`, `Volume`, `Issue`, `Publisher`, `Pages`, `PublicationPlace`, `PublicationYear`,
`PublicationYearSuffix`, `DOI`, `Webpage`, `Ibidem`, `OpusCitatum`; for names `FirstName`, `LastName`.

- With `"reclassify": true` (the default) the references and parts are found again in the new text. With
  `false` the footnote is left untagged and `needs_classification` is `true`.
- **Text** replaces the text directly, without OCR; the box stays as it is. **Split** takes a character
  offset strictly inside the text and returns `{"first", "second"}`; both keep the original box. **Merge**
  joins the footnotes in reading order. When they are on different pages the first one's box is kept and the
  answer has `"bbox_approximate": true`.
- **Undo:** split and merge remove the originals. Delete the new footnotes and restore the originals with
  `restore`.
- **Models:** `GET /ner-status` and `GET /ocr-status` say which models are used for edits, whether they can be
  loaded and what the alternatives are. `PUT /ner-suite` (`{"ner_suite"}`) and `PUT /ocr-engine`
  (`{"ocr_engine": "surya"}` or `"pdfminer"`) change them for later edits; what is saved stays as it is.

## Reviewing

| Method and path | What it does |
|---|---|
| `GET /api/v1/documents/<job_id>/verification-status` | Whether it is verified, and by whom |
| `POST /api/v1/documents/<job_id>/verify` | `{"verified": true}` or `false` |
| `POST /api/v1/documents/<job_id>/pages/<page>/approval` | `{"approved": true}` or `false` |
| `GET /api/v1/documents/<job_id>/layout-prior` | What Layout Assist learned about the boxes, and its suggestions |
| `GET /api/v1/documents/<job_id>/ner-prior` | Its suggestions for labels |
| `GET /api/v1/documents/<job_id>/checks` | Consistency checks |
| `GET`, `POST /api/v1/documents/<job_id>/review-flags` | The marks "to review"; needs the **To Review** function |

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"verified": true}' $BASE/api/v1/documents/<job_id>/verify
# {"verified": true, "verified_at": "...", "verified_by": "..."}
```

## Reference lists

Needs the **Reference List Linker** function. Setting a list needs an administrator. Only BibTeX lists are
accepted at present; a list taken from the book itself is switched off.

| Method and path | What it does |
|---|---|
| `GET /api/v1/documents/<job_id>/reference-list` | Entries, links and counts. `?format=csv` returns a table |
| `POST /api/v1/documents/<job_id>/reference-list` | `{"source": "bibtex", "bibtex": "..."}` |
| `POST /api/v1/documents/<job_id>/reference-list/links` | `{"action", "reference_id", "entry_id"}` |
| `PATCH /api/v1/documents/<job_id>/reference-list/entries/<entry_id>` | Correct an entry |

`GET` returns `source`, `state`, `entries`, `links` and `counts`, plus what is not linked on either side.
`state` is `none`, `running`, `ready` or `error`. A link has `reference_id`, `entry_id`, `status` (`auto`,
`confirmed` or `added`), `rule` and `score`. Setting a list replaces the previous one; decisions on entries that
remain are kept, and the answer says how many were dropped. A `.bib` file can also be sent as a multipart field
`bibtex`.

The `action` is `confirm`, `reject`, `add` or `clear`. Links are worked out each time you read the list;
your decisions always win. Adding a link replaces the reference's other links.

## Groups and collections

| Method and path | What it does |
|---|---|
| `GET`, `POST /api/v1/groups` | List the groups, create one (`{"name"}`) |
| `GET`, `DELETE /api/v1/groups/<id>` | Details, delete |
| `POST /api/v1/groups/<id>/members` | `{"username", "role"}`: `viewer`, `editor` or `owner` |
| `PATCH`, `DELETE /api/v1/groups/<id>/members/<user_id>` | Change a role, remove a member |
| `GET`, `POST /api/v1/collections` | List, create (`{"name", "description", "group_id"}`) |
| `GET`, `PATCH`, `DELETE /api/v1/collections/<id>` | Details, rename, delete |
| `POST /api/v1/collections/<id>/share` | `{"group_id": 3}` or `null` |
| `POST /api/v1/collections/<id>/jobs` | Add a document: `{"job_id"}` |
| `DELETE /api/v1/collections/<id>/jobs/<job_id>` | Remove a document from the collection |

- **Group roles.** A viewer reads. An editor can also edit and delete documents shared with the group. An owner
  can also add and remove members and change roles. A group always keeps at least one owner (`422`).
- **Deleting a group** deletes no documents: everything shared with it becomes private.
- **Collections.** Every user has a default collection that cannot be deleted (`400`). Its owner, an
  administrator, or an editor or owner of its group can add and remove documents. Only its owner or an
  administrator can rename, share or delete it.

## Several documents at once

At most 500 documents in a request. Items you cannot act on are skipped and reported.

| Method and path | Body |
|---|---|
| `POST /api/jobs/bulk-delete` | `{"job_ids": [...]}` |
| `POST /api/jobs/bulk-add-to-collection` | `{"job_ids": [...], "collection_id": 5}` |
| `POST /api/jobs/bulk-remove-from-collection` | `{"job_ids": [...], "collection_id": 5}` |
| `POST /api/jobs/bulk-unshare` | `{"job_ids": [...], "group_id": 3}` |
| `GET /api/jobs/bulk-export-citations?job_ids=a&job_ids=b&format=apa` | none; one file with all documents, an unknown `format` gives `400` |

The answer lists what succeeded and what was `skipped`, with the reason. Adding to a collection can also take
`"new_collection": {"name": "..."}` in place of `collection_id`.

## Errors

Errors are `{"error": "..."}` with a status code.

| Code | Meaning |
|---|---|
| `400` | Wrong input |
| `401` | No valid key |
| `403` | Not allowed, or the function is off for your account |
| `404` | Not found, or no access |
| `409` | Conflict, for example overlapping spans |
| `413` | Upload too large |
| `422` | The request is valid but cannot be carried out in the document's current state |
