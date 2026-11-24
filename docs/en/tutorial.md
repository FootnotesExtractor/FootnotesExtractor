# Tutorial

*[Polska wersja](../pl/tutorial.md)*

1. [Before you start](#1-before-you-start)
2. [Install and run](#2-install-and-run)
3. [Create accounts](#3-create-accounts)
4. [Process a document](#4-process-a-document)
5. [Review it](#5-review-it)
6. [Export](#6-export)
7. [Work in a team](#7-work-in-a-team)
8. [Run it on a server](#8-run-it-on-a-server)
9. [If something goes wrong](#9-if-something-goes-wrong)

## 1. Before you start

| | Minimum |
|---|---|
| GPU | NVIDIA, 10 GB of memory (4 GB with the [small](models.md) models, 6 GB with the medium ones) |
| CPU | 4 cores |
| RAM | 12 GB |
| Disk | 6.1 GB for the app and Python packages, 6.7 GB for the models (1.7 GB for the small ones) |
| Software | Linux, Python 3.10 |

The installed PyTorch build needs an NVIDIA driver that supports CUDA 12.4. Check the driver with
`nvidia-smi` before you install.

## 2. Install and run

```bash
git clone https://github.com/FootnotesExtractor/FootnotesExtractor.git
cd FootnotesExtractor

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python setup_models.py
FOOTNOTES_INSECURE_COOKIES=1 python wsgi.py --host 127.0.0.1
```

Open <http://127.0.0.1:5002>.

- `setup_models.py` downloads the detection and parsing models (about 5.6 GB). Surya fetches its own OCR
  weights (about 1.1 GB) the first time it runs.
- `FOOTNOTES_INSECURE_COOKIES=1` allows you to log in over plain HTTP on your own machine. Never set it on
  a server.
- To use smaller models, see [Models](models.md): `python setup_models.py --size small` downloads 1.7 GB in
  place of 5.6 GB and needs less GPU memory.
- `python setup_models.py --herbert` also downloads a second set of parsing models. It needs 4.2 GB more
  disk space and more GPU memory than the minimum above.

## 3. Create accounts

1. Open `/register`. The app prints a one-time **setup token** in its log and saves it as
   `setup_token.txt` next to `app.db`.
2. Enter the token, a username and a password of at least 8 characters. This first account is the
   administrator, and registration then closes.
3. Create every further account in **Admin → Users**. There you also choose which optional functions each
   user sees (see [Functions you can switch on](features.md#8-functions-you-can-switch-on)).

You can switch the interface to Polish with the language menu in the top bar.

## 4. Process a document

1. Choose one or more PDFs on the home page (or drop them there). Each PDF can be up to 200 MB. One
   upload can hold up to 900 PDFs and 850 MB together.
2. Optional: pick a collection under **Add to collection**, or open **Advanced Options** to choose a model
   size ([Models](models.md)), single models, the OCR engine, confidence thresholds and processing options.
3. Press **Extract Footnotes**. The status page shows the progress. Documents wait in a queue, so several
   uploads are processed one after another.
4. When it finishes, open the **Interactive Viewer**.

## 5. Review it

The viewer shows the page on the left and, next to it, what was found.

| Area | What it is for |
|---|---|
| Page view | The PDF page with a box around every footnote |
| **Annotate** | The labels you can apply, and **Add Footnote Box** |
| **Footnote Predictions** | The text, references and parts of the footnotes on this page |
| **Reference List** | A bibliography linked to the footnotes (optional function) |
| **Footnote References** | Every reference in the document, with search and filters |

Common corrections:

- **A footnote was missed.** Press `B`, draw a box, and it is read and tagged.
- **A box is wrong.** Click it, then move its edges, or press `Delete` to remove it.
- **The text is wrong.** Use the pencil. The same window lets you split a footnote or merge it with a
  neighbour.
- **A label is wrong.** Select the part in the text and press the key of the right label, or click it in
  **Annotate**. To label new text, choose the label first and then mark the text.
- **An *ibid.* points to the wrong work.** Choose the right one; it takes over that work's author, title
  and year.

### Keyboard

| Key | Action |
|---|---|
| `←` / `A`, `→` / `D` | Previous page, next page |
| `B` | Draw a footnote box |
| `S` | Approve the page (with Layout Assist on) |
| `Ctrl+Z`, `Ctrl+Y` or `Ctrl+Shift+Z` | Undo, redo |
| `Delete` / `Backspace` | Delete the selected box or text span |
| `F`, `J` | Reference, work |
| `1`–`9`, `0` | Author, Editor, Translator, Title, BookTitle, Journal, Volume, Issue, Publisher, Pages |
| `Q` `W` `E` `R` `T` | Publication place, year, year suffix, DOI, web page |
| `Y` `U` | Ibidem, Opus citatum |
| `G` `H` | First name, last name |

### Approve and verify

With **Layout Assist** on, approve a page when you are done with it: click its icon in the Layout Assist
panel or press `S`. On an approved page Layout Assist changes nothing on its own, and whatever it still had
open there is closed. When the whole book is checked, the viewer offers to mark it as verified on its last page. The badge
then appears wherever the document is listed.

## 6. Export

Use **Export Citations** in the viewer, or select several documents in a collection and export them
together.

| Format | Contents |
|---|---|
| BibTeX – Standard | One entry per cited work; an *ibid.* or *op. cit.* stays as printed |
| BibTeX – Expanded | The same, with every *ibid.* and *op. cit.* replaced by the work it refers to |
| APA 7, Chicago 17, MLA 9 | A formatted bibliography as plain text |
| Reference Strings | Each reference exactly as extracted |
| JSON | Everything, with positions and confidence |

Each work appears once. Its note lists every place it is cited: the PDF page, the footnote number and the
reference as printed.

## 7. Work in a team

- A **collection** is a named set of documents.
- A **group** is a set of users. You share a collection with a group.
- A group member is a **viewer** (reads), an **editor** (can correct) or an **owner** (also manages the
  members).
- Documents are private until you share them. Administrators see everything.

## 8. Run it on a server

Bind the app to the loopback address and put a TLS-terminating reverse proxy in front:

```bash
python wsgi.py --host 127.0.0.1 --port 5002 --threads 8
```

The `deploy/` folder holds a Caddyfile, a self-signed variant and a systemd unit. Set
`FOOTNOTES_TRUSTED_PROXIES=1` when a proxy is in front. The full steps are in
[deploy/DEPLOY.md](../../deploy/DEPLOY.md).

Back up `uploads/`, `results/` and `app.db`. Updating the app leaves them alone.

## 9. If something goes wrong

| Problem | What to check |
|---|---|
| You cannot log in on your own machine | Start with `FOOTNOTES_INSECURE_COOKIES=1`, or use HTTPS |
| Registration asks for a token | It is in the app's log and in `setup_token.txt` |
| "Too large" at upload | 200 MB for each PDF, 850 MB for the whole upload |
| CUDA out of memory | Close other programs that use the GPU, or lower **Batch Size** under **Advanced Options** |
| Processing is slow | Check `nvidia-smi`; a CUDA mismatch makes PyTorch fall back to the CPU without an error |
| A document stays at "queued" | Documents are processed in order; it starts when the earlier ones finish |
