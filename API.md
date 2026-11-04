# FootnotesExtractor — API Reference

JSON API for uploading PDFs, tracking processing, retrieving/editing extracted
footnotes, and managing groups/collections. Every route below is authenticated
the same way a browser session is — the API key just resolves to a user
account, so all the usual ownership/sharing rules (private by default,
shared with a group, admin sees everything) apply exactly as they do in the
UI.

## Authentication

1. Log into the web app, open your **Profile** page (`/api-keys` leads there), and
   generate one. The raw key (`fnk_...`) is shown exactly once — copy it
   immediately.
2. Send it on every request as either:
   - `Authorization: Bearer <key>`
   - `X-API-Key: <key>`

No key/session → `401 {"error": "Login required"}`. Revoked or unknown key →
same response. Trying to act on something you don't own/aren't shared into →
`403` or `404` depending on the route (see each endpoint below).

All request/response bodies are JSON unless noted (`multipart/form-data` for
the file upload). Base URL below assumes the default local dev address —
replace `http://localhost:5002` with wherever the app is actually reachable.

## Quick start: process a batch of PDFs

```python
import time
import requests

BASE = "http://localhost:5002"
API_KEY = "fnk_..."  # from /api-keys
HEADERS = {"X-API-Key": API_KEY}

def upload(pdf_path, **options):
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{BASE}/api/v1/documents",
            headers=HEADERS,
            files={"file": (pdf_path, f, "application/pdf")},
            data=options,  # e.g. group_id=..., collection_id=..., ner_suite=...
        )
    resp.raise_for_status()
    return resp.json()["job_id"]

def wait_for_completion(job_id, poll_seconds=5, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(f"{BASE}/api/v1/documents/{job_id}", headers=HEADERS)
        resp.raise_for_status()
        status = resp.json()
        if status["status"] in ("completed", "error"):
            return status
        time.sleep(poll_seconds)
    raise TimeoutError(f"Job {job_id} did not finish in time")

def get_results(job_id):
    resp = requests.get(f"{BASE}/api/results/{job_id}", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()  # full structured footnotes/references/components/names

for pdf_path in ["paper1.pdf", "paper2.pdf", "paper3.pdf"]:
    job_id = upload(pdf_path)
    print(pdf_path, "->", job_id)
    status = wait_for_completion(job_id)
    if status["status"] == "error":
        print("  FAILED:", status.get("error"))
        continue
    results = get_results(job_id)
    print("  footnotes:", len(results.get("results", [])))
```

Processing is sequential on a single background worker, so with several PDFs
queued, later ones will show `"status": "queued"` until earlier ones finish —
this is normal, just keep polling.

---

## Documents

### `POST /api/v1/documents` — upload a PDF

`multipart/form-data`. File field: `file` (or `files`, repeatable, to match
the browser form — the JSON response covers all of them either way).

What an account may set depends on its features (Admin → Users): without
**Advanced Options** every model, threshold and processing option is the
default whatever is sent; `enable_grobid` (find the book's reference list)
needs **Show References**, `enable_enrichment` needs **Metadata Enrichment**.

Optional form fields (all have sane defaults if omitted):

| Field | Default | Notes |
|---|---|---|
| `od_conf` | `0.5` | Object-detection (bounding box) confidence threshold |
| `ref_conf` | `0.7` | Reference-classification confidence threshold |
| `comp_conf` | `0.7` | Component-extraction confidence threshold |
| `batch_size` | `8` | |
| `od_model` | `yolo11n_validated_v2` | Which detection model checkpoint to use |
| `ner_suite` | `validated_v2_nested` | Which NER model suite to use (XLM-R large / HerBERT large / HerBERT base, nested) |
| `ocr_engine` | `surya` | `surya` or anything else for the text-layer fallback |
| `enable_grobid` | `false` | `true`/`1`/`yes`/`on` all count as true |
| `enable_enrichment` | `false` | Google Books/Crossref/OpenAIRE metadata enrichment |
| `split_footnotes` | `true` | Re-segment each detected region by leading footnote number; turn off for PDFs where numbering isn't contiguous (`0`/`false`/`no`/`off` all count as false) |
| `enable_ibidem` | `true` | When a reference is classified ibidem/opus citatum, infer its missing components from the matching earlier reference. citation_type is classified either way — this only controls the component carry-forward. `0`/`false`/`no`/`off` all count as false |
| `split_multi_work` | `true` | Cut a reference span that holds several works ("Martines 2009, 2012", "Grilli, Title A (1994a) i Title B (2004)") into works ("Praca") inside the reference, which stays whole; exports make one entry per work. A later work carries over what the text leaves out (author, title, journal... before the year) as `inherited` fields, which exports include. Also applies to the models' re-predictions after box/text edits in this job. `0`/`false`/`no`/`off` all count as false |
| `debug` / `cpu_mode` | `false` | |
| `group_id` | *(none)* | Share this document with a group you belong to (private otherwise) |
| `collection_id` | *(none)* | File it into a collection you can access, immediately |

**Response** `201`:
```json
{ "job_id": "8ba00070-...", "job_ids": ["8ba00070-..."] }
```
(`job_ids` has one entry per uploaded file if you used the repeatable `files` field.)

### `GET /api/v1/documents` — list documents you can see

Owned by you, shared via a group you're in, or every document if you're an admin.

```json
{ "documents": [
  { "job_id": "...", "filename": "paper1.pdf", "status": "completed",
    "step": "Processing completed successfully!", "progress": 100,
    "summary": { "total_footnotes": 42, "total_references": 88, "...": "..." },
    "error": null, "owner_info": { "is_legacy": false, "owner_username": "you", "group_name": null } }
] }
```

### `GET /api/v1/documents/<job_id>` — one document's status

Same shape as a single entry above. `status` is one of `queued`, `processing`,
`completed`, `error`.

### `GET /api/status/<job_id>` — status with timing

Same job, plus `processing_time` (seconds, once completed) or `elapsed_time`
(seconds, while still processing).

### `GET /api/results/<job_id>` — full structured results

The complete extracted document: `results` (list of footnotes, each with
`text`, `bbox`, `page`, and nested `references` → `components` → `names`,
each carrying `confidence`/`label`/`start`/`end`), plus `metadata` and
`summary`. This is the same JSON you'd get from the "Download JSON" button.

### `GET /download/<job_id>` — download results as a file

Browser-style file download (`Content-Disposition: attachment`). Same auth,
works fine with an API key too if you just want the file on disk.

Optional `?format=` query param:

| `format` | Output |
|---|---|
| *(omitted)* | The full JSON results (default — same as `GET /api/results/<job_id>`) |
| `bibtex_standard` | A `.bib` file, one `@article`/`@book`/`@incollection`/`@online`/`@misc` entry per extracted reference. Each entry's `note` says where it came from: the reference as printed and the PDF page of its footnote (both pages when the footnote runs on), e.g. `note = {Original reference: "J. Czubaty, Księstwo Warszawskie, Warszawa 2011, s. 319."; PDF page 5}`. A work split out of a reference that cites several ("Martines 2009, 2012") gets the whole printed reference. Ibidem/opus-citatum references are kept as extracted — sparse, with the note starting `Ibid.;` / `Op. cit.;` |
| `bibtex_expanded` | Same, but ibidem/opus-citatum references are resolved back to the original work's full Author/Title/Journal/... (using the same inference the pipeline already does for ibidem detection), so every entry is a complete standalone citation |
| `apa` | Plain-text APA 7th Edition reference list (`.txt`), one numbered entry per reference |
| `chicago` | Plain-text Chicago 17th Edition (Notes-Bibliography, bibliography-list form) reference list |
| `mla` | Plain-text MLA 9th Edition reference list |
| `reference_strings` | Plain-text listing of each reference's own extracted text, verbatim — no reformatting, including a literal "Ibid."/"Op. cit." if that's what was printed |

`apa`/`chicago`/`mla` always resolve ibidem/opus-citatum references back to
the original work (there's no "standard" variant for these — a formal
reference list in any of these styles never contains a literal "Ibid."
entry, unlike BibTeX which is also used to represent the footnote stream
itself). Journal/book titles are wrapped in `*asterisks*` (Markdown-style)
since plain text can't render italics.

All of these are a best-effort transcription of what the pipeline
extracted, not citation-manager-grade formatting — they don't correct NER
mistakes (e.g. a journal name split across two spans by an intervening
quote mark comes through concatenated, not fixed) or guess fields that
weren't extracted.

### `DELETE /api/job/<job_id>` — delete a document

Owner or admin only. Irreversible — deletes the job, its results file, and
its uploaded PDF.

### `POST /api/job/<job_id>/reprocess` — run a document through the pipeline again

Anyone who can edit the document. Re-queues it in place: same job id, PDF,
processing options, book metadata, owner and collections. Its results are
replaced, corrections made in the viewer included; the previous results and
Layout Assist proposals are copied to `results/reprocess_backups/` first.
Returns `{"success": true, "job_id": ..., "status": "queued"}`; poll
`GET /api/v1/documents/<job_id>` until it is `completed`. `409` while the
document is already queued or processing, `422` if its PDF is gone.

### `POST /api/job/<job_id>/share` — share/unshare with a group

Owner or admin only. Body: `{"group_id": 3}` or `{"group_id": null}` to make
it private again.

---

## Editing extracted footnotes

The interactive viewer's full edit surface is also plain JSON, all under
`/api/v1/documents/<job_id>/...`. You generally won't need this for a batch
"upload and get results" script, but it's here for completeness. Every route
below requires edit access: the document's owner, an admin, or — for a
document shared via a group — a member whose role in that group is
`'editor'` or `'owner'`, not `'viewer'` (see Groups below). Read-only routes (`GET
.../annotations`, `/results/<job_id>`, `/viewer/<job_id>`, etc.) stay open to
any group member regardless of role.

| Action | Method & path |
|---|---|
| Add/resize/delete a footnote bounding box | `POST` / `PUT <footnote_id>` / `DELETE <footnote_id>` on `.../footnotes` |
| Restore a footnote to an exact prior snapshot (used by the viewer's undo) | `POST .../footnotes/<footnote_id>/restore` |
| Manually correct a footnote's OCR'd text (no bbox/OCR change) | `PUT .../footnotes/<footnote_id>/text` |
| Split one footnote into two at a text offset | `POST .../footnotes/<footnote_id>/split` |
| Merge two-or-more footnotes into one | `POST .../footnotes/merge` |
| Preview NER classification for arbitrary text, without saving anything | `POST .../footnotes/classify-preview` |
| Check whether this job's NER suite is currently loadable, and list alternatives | `GET .../ner-status` |
| Switch which NER suite future edits on this job use | `PUT .../ner-suite` |
| Check whether this job's OCR engine is currently loadable, and list alternatives | `GET .../ocr-status` |
| Switch which OCR engine future box add/resize edits on this job use | `PUT .../ocr-engine` |
| Add/resize/delete a reference span | `POST` / `PUT <reference_id>` / `DELETE <reference_id>` on `.../footnotes/<footnote_id>/references` |
| Divide a reference holding several works into works ("Praca"), keeping the reference whole | `POST .../references/<reference_id>/split` with `{}` (detected cut points) or `{"cuts": [offsets]}` (cuts the work holding each offset). Returns `{footnote, previous, reference}` |
| Mark one work inside a reference; the rest is split off into works of its own | `POST .../references/<reference_id>/works` with `{"start", "end"}` |
| Move a work's edges | `PUT .../references/<reference_id>/works/<work_id>` with `{"start", "end"}` |
| Join neighbouring works (one left = a plain reference again) | `POST .../references/<reference_id>/works/merge` with `{"ids": [...]}` |
| Set a work's carried-over fields | `PUT .../references/<reference_id>/works/<work_id>/inherited` with `{"inherited": [{"source_component_id"}]}` (`[]` clears) or `{"from_previous": ["Author"]}` |
| Merge neighbouring references into one | `POST .../footnotes/<footnote_id>/references/merge` with `{"ids": [...]}` |
| Add/resize/delete a component span | same shape, one level deeper, under `.../references/<reference_id>/components` |
| Add/resize/delete a name span (Author/Editor/Translator only) | same shape, under `.../components/<component_id>/names` |

Create/update bodies take `{"start": int, "end": int}` (character offsets
into the footnote's `text`) plus `"label"` for components/names (e.g.
`"Author"`, `"FirstName"`) and `{"page": int, "bbox": {"x1","y1","x2","y2"}}`
for footnotes. All return the created/updated object plus, where relevant,
`footnote_summary` with updated counts.

**`PUT .../footnotes/<footnote_id>/text`** — body `{"text": "...", "reclassify": true}`
(`reclassify` optional, defaults `true`). Replaces the footnote's text
directly — no OCR call, bbox/page untouched. With `reclassify: true` (the
default) the reference/component NER cascade re-runs on the corrected text,
same as what already happens after a bbox resize; with `false` the footnote
is left with empty `references` and `needs_classification: true` for manual
tagging via the label picker instead. Returns the updated footnote.

**`POST .../footnotes/<footnote_id>/split`** — body `{"offset": int,
"reclassify": true}` (`reclassify` optional, defaults `true`). `offset` is a
character offset strictly between `0` and `len(text)`. Splits the footnote's
text at that offset into two new footnotes; both keep the original's
`page`/`bbox`/`confidence` (a text split can't re-derive two tighter boxes
from one detected region). With `reclassify: true` both halves are freshly
NER-classified; with `false` that's skipped (faster) and both are left
`needs_classification: true`. The original is removed. Returns `{"first":
{...}, "second": {...}}`. Undo by deleting both new ids and `POST`ing the
original's pre-split snapshot to its `/restore`.

**`POST .../footnotes/merge`** — body `{"footnote_ids": ["id1", "id2", ...],
"reclassify": true}` (`footnote_ids` 2+ required, `reclassify` optional,
defaults `true`). Merges the given footnotes into one, in reading order
(page, then `bbox.y1`) regardless of the order they're listed in. `bbox` is
the union of all originals if they share a page; otherwise the earliest
footnote's `bbox` is kept and the response carries `"bbox_approximate":
true`. With `reclassify: false` the merged footnote skips NER and is left
`needs_classification: true`. The originals are removed. Returns the merged
footnote. Undo by deleting the merged id and restoring each original's
pre-merge snapshot.

**`POST .../footnotes/classify-preview`** — body `{"text": "..."}`. Runs the
reference+component NER cascade on the given text and returns `{"references":
[...], "needs_classification": bool}` — nothing about the job is read from or
written to storage, no footnote id involved. Useful for previewing what
classification a piece of text *would* get without creating/updating anything
(the interactive viewer's footnote-edit modal uses this to preview several
local split/merge steps before committing any of them via the endpoints
above).

**`GET .../ner-status`** — no body. Returns `{"ner_suite": "mbert_production",
"available": true, "alternatives": [{"key": "...", "name": "..."}, ...]}`.
`available` reflects whether this job's configured NER suite is actually
loadable right now, not whether an admin has disabled it for new uploads
(disabling one via `/admin/models` never affects existing jobs — only the
suite's files genuinely being missing from disk does). `alternatives` lists
every other currently-loadable suite, for use with the endpoint below.

**`PUT .../ner-suite`** — body `{"ner_suite": "xlmroberta_nested"}`. Switches
which suite *future* edits on this job use (reclassification, split, merge,
reference add/resize) — nothing already saved is touched, and nothing is
reclassified automatically; re-run whichever edit you want reclassified
after switching. 422 if the requested suite isn't loadable either.

**`GET .../ocr-status`** — no body. Returns `{"ocr_engine": "surya",
"available": true, "alternatives": [{"key": "...", "name": "..."}, ...]}`.
Mirrors `ner-status`: `available` reflects whether this job's configured OCR
engine is actually loadable right now, not whether an admin has disabled it
for new uploads. `alternatives` lists every other configured engine, for use
with the endpoint below.

**`PUT .../ocr-engine`** — body `{"ocr_engine": "pdfminer"}` (one of
`"surya"`/`"pdfminer"`). Switches which engine *future* box add/resize edits
on this job re-OCR with — nothing already saved is touched. 422 if the
requested engine isn't loadable either.

---

## Reference lists (Reference List Linker)

A document's reference list — the one found inside the book itself
(*internal*), or one given as BibTeX, e.g. the PolIndex records of an article
— linked to the references extracted from its footnotes. Links are worked out
each time they are read, from the document's current footnotes (titles at least
0.8 alike, or — without a title — the first author's surname and the year;
ibid./op. cit. through the work they refer back to). Reviewers' decisions are
stored and win over the automatic links. Reading and deciding need the
**Reference List Linker** feature on the account; setting a list needs an admin.

### `GET /api/v1/documents/<job_id>/reference-list`

`{source, engine, state, entries, items, links, unlinked_references,
unlinked_entries, counts, matcher_version}`. `state` is `none` (no list yet),
`running` (the book's own list is being looked for), `ready` or `error`.
Each link: `{reference_id, entry_id, status: auto|confirmed|added, rule,
score, matcher_version}`. `?format=csv` returns the pairs, then the unlinked
on both sides, as a table.

### `POST /api/v1/documents/<job_id>/reference-list` — set the list (admin)

`{"source": "bibtex", "bibtex": "<.bib text>"}`, a multipart `.bib` in the
field `bibtex`, or `{"source": "internal"}` (the book's own list, looked for
in the background: `202 {"state": "running"}`; poll GET). An entry's `note`
(or `howpublished`), if it has one, is shown as its text. Setting a list
replaces the previous one; decisions on entries that remain are kept, and
the response says how many were dropped.

### `POST /api/v1/documents/<job_id>/reference-list/links`

`{"action": "confirm"|"reject"|"add"|"clear", "reference_id", "entry_id"}` —
needs edit rights on the document. `add` links a footnote reference to an
entry (replacing its other link); `clear` removes a decision.

### `POST /api/jobs/bulk-link-reference-lists` — admin

`{"job_ids": [...], "source": "internal"}`: the book's own list for each
finished document, one at a time in the background.

## TO REVIEW marks

A footnote reference, or its link to a reference-list entry, marked "TO
REVIEW" to come back to it. The marks are stored per document, apart from the
results; a mark whose reference is gone (the footnote was classified again) is
kept but not shown. Reading needs the **To Review** feature on the account and
access to the document; marking needs edit rights too.

### `GET /api/v1/documents/<job_id>/review-flags`

`{"flags": [{kind, reference_id, entry_id, by, at}]}` — `kind` is
`reference` or `link`; `entry_id` is `null` for a reference.

### `POST /api/v1/documents/<job_id>/review-flags`

`{"kind": "reference", "reference_id"}` marks a footnote reference (or a work
of a divided one); `{"kind": "link", "reference_id", "entry_id"}` marks its
link to a list entry. `"flagged": false` removes the mark (default `true`).
Returns every mark of the document. `404` for an unknown reference or entry.

## Groups

Each membership has a role — `'viewer'`, `'editor'` (default), or `'owner'` —
a single permission tier, not two separate concepts. Editors *and* owners can
edit and delete documents shared with the group (see "Editing extracted
footnotes" above — every mutation route there requires editor-or-owner
access, not just membership); viewers can only browse. Owners can *also*
add/remove members and change roles (`can_manage`); editors/viewers can't.
Ownership isn't tied to who created the group — any owner can promote another
member to owner (or demote one), and the group's creator is just whoever
happened to create it (`groups.creator_user_id`, an immutable historical
fact, not a permission). A group must always keep at least one owner:
demoting or removing its last remaining owner is rejected (422) — promote
someone else to owner first.

| Action | Method & path | Body |
|---|---|---|
| List your groups | `GET /api/v1/groups` | — |
| Create a group | `POST /api/v1/groups` | `{"name": "..."}` (creator becomes its first owner) |
| Get a group + members | `GET /api/v1/groups/<id>` | — |
| Add a member (owner/admin only) | `POST /api/v1/groups/<id>/members` | `{"username": "...", "role": "editor"}` (`role` optional: `"viewer"`/`"editor"`/`"owner"`, defaults `"editor"`) |
| Change a member's role (owner/admin only) | `PATCH /api/v1/groups/<id>/members/<user_id>` | `{"role": "viewer"}` |
| Remove a member (owner/admin only) | `DELETE /api/v1/groups/<id>/members/<user_id>` | — |
| Delete a group (owner/admin only) | `DELETE /api/v1/groups/<id>` | — |

Deleting a group never deletes anything shared with it — any job or
collection shared to that group just becomes private again, same as
unsharing it by hand. Members are removed along with the group.

## Collections

A named, optionally-described, optionally-group-shared set of documents.
Every user has one auto-created default collection ("User's Uploads",
`is_default: true` in its JSON) that can't be deleted (rename/reshare are
fine) — it's backfilled once, the first time `GET /api/v1/collections` (or
the merged uploads/collections page) is hit for that user, with every job
they own that isn't already filed into another collection of theirs.

| Action | Method & path | Body |
|---|---|---|
| List collections you can see | `GET /api/v1/collections` | — |
| Create one | `POST /api/v1/collections` | `{"name": "...", "description": "...", "group_id": null}` (`name` required) |
| Get one + its document ids | `GET /api/v1/collections/<id>` | — |
| Rename / edit description (owner/admin) | `PATCH /api/v1/collections/<id>` | `{"name": "...", "description": "..."}` |
| Share/unshare (owner/admin) | `POST /api/v1/collections/<id>/share` | `{"group_id": 3}` or `{"group_id": null}` |
| Delete (owner/admin; 400 if it's the default collection) | `DELETE /api/v1/collections/<id>` | — |
| Add a document | `POST /api/v1/collections/<id>/jobs` | `{"job_id": "..."}` |
| Remove a document | `DELETE /api/v1/collections/<id>/jobs/<job_id>` | — |

The collection's own owner, a site admin, or a member with **editor** or
**owner** role (not viewer) in the collection's sharing group can add/remove
documents; only the collection's own owner or a site admin can rename,
reshare, or delete the collection itself.

## Bulk job actions

Act on several documents at once — used by the merged uploads/collections
page, group pages, and a collection's own detail page. All four degrade
gracefully per-item (skip and report, rather than failing the whole request)
if some ids don't exist or the caller doesn't have permission on them. Not
duplicated as single-job endpoints already aren't (see "Editing extracted
footnotes" above) — these work with API-key auth for free since every check
here only reads `flask.g.user`. Max 500 ids per request.

| Action | Method & path | Body |
|---|---|---|
| Delete several documents | `POST /api/jobs/bulk-delete` | `{"job_ids": ["...", "..."]}` |
| File several documents into a collection | `POST /api/jobs/bulk-add-to-collection` | `{"job_ids": [...], "collection_id": 5}` or `{"job_ids": [...], "new_collection": {"name": "...", "description": "...", "group_id": null}}` |
| Unshare several documents from a group | `POST /api/jobs/bulk-unshare` | `{"job_ids": [...], "group_id": 3}` |
| Remove several documents from one collection | `POST /api/jobs/bulk-remove-from-collection` | `{"job_ids": [...], "collection_id": 5}` |

`bulk-delete` requires editor access per job (same rule as the single-job
delete). `bulk-add-to-collection` requires edit access on the *destination*
collection once (not per job) and view access on each source job.
`bulk-unshare` is strict owner/admin per job, no group-editor escalation —
mirrors the single-job share endpoint. `bulk-remove-from-collection` only
checks edit access on the collection once (like `bulk-add-to-collection`'s
destination check) — removing a job from a collection needs no permission
on the job itself, same as the single-job remove endpoint; it only touches
that one collection's membership, not the job or any other collection it's
also filed under. Every response has the shape
`{"success": true, "<verb>": [...ids that succeeded...], "skipped": [{"job_id": "...", "reason": "forbidden"|"not_found"|"not_shared_with_group"|"not_in_collection"}, ...]}`
(`bulk-add-to-collection`'s response also includes `"collection_id"`, useful
when `new_collection` was used to create one inline).

---

## Errors

Always `{"error": "human-readable message"}` with an appropriate status code
(`400` bad input, `401` not authenticated, `403` authenticated but not
allowed, `404` not found/no access, `409` conflict e.g. overlapping spans,
`422` semantically invalid for this resource's current state).
