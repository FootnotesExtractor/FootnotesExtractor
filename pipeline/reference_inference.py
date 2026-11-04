"""Ibid. and op. cit. resolved to the work they point back to.

Such a reference gets components_inferred: its own parts plus the parts of
that work it leaves out (author, title, journal, ...), and its footnote lists
what was added in inferred_components - the fields the pipeline has always
written, read by the exports (expanded BibTeX), the Reference List Linker
and the viewer. Each one also records its source, inferred_from.

The app runs reinfer() when a document is processed and again on every save
of its result, so an edit is reflected at once (with
reference A and an ibid. after it, a reference B added between them must
become the ibid.'s source). It works on the stored result alone, in the
order the viewer shows the document: pages in turn, each page's footnotes in
reading order (the viewer's footnotesInReadingOrder - by their printed
numbers when the page has them), each footnote's references by position.

The rules:

  type       ibid./op. cit. by the text's patterns (pipeline/citation_types);
             otherwise an Ibidem part ("Idem", "Tenże", ...) makes an ibid.
             and an OpusCitatum part an op. cit.; written to citation_type
  ibid.      the work of the citation just before it, whatever its kind: a
             full reference (the last work of a divided one), or the work an
             ibid./op. cit. before it resolved to
  chains     inferred_from names the full reference at the head of a chain
            : an ibid. after an op. cit. or another ibid.
             points to the work's full reference, not to that citation
  idem       an ibid. written with "idem", "eadem", "tenże", "ders." ... (the same
             AUTHOR) and with its own title ("Idem, Trzy Europy, Lublin 1995"):
             a new work - it takes only the author/editor, and counts as that
             author's work from then on. "Ibidem", "ibid.", "tamże" (the same
             WORK) never start a new work, even with a title-like part such as
             a quoted phrase ("„regionale und chronologische
             Reichweite...”, ibidem. s. 9" is still Zernack's Osteuropa)
  op. cit.   the nearest earlier work by the same person: names compared as
             the linker does (reference_linker._same_person - "A. Smith" =
             "Adam Smith", "A. Smith" in "A. Smith and P. Wells", but never
             "K. Lepszy" = "K. Górski"); never a work cited after it
  the parts  ibid.: author, editor, translator, title, book title, journal,
             publisher, year, place, volume, issue; op. cit. the same without
             the author; never pages (they belong to each citation). A label
             the reference already has, or text it already holds, is not
             added.
  by hand    a source chosen in the viewer (source_reference_id) wins while
             that reference exists. A part of the source can be removed from
             what a reference takes, or one the rules leave out added
             (inferred_removed / inferred_added, each [{label, text}]); it then holds while the source has that part. The
             source's parts it does not take are listed in
             inference_left_out, for the viewer to offer.
  no work    a "reference" naming nothing of a work - only pages, or the
             footnote's own number read as one ("13 ") - is no one's source.

Departures from the pipeline's original pass (EnhancedFootnoteProcessor.
handle_enhanced_reference_inference), all asked for: op. cit. only looks
back and needs the same person, not a shared initial; ibid. follows the
citation before it instead of skipping back to the last full reference;
idem; translator/editor/book title carried over and pages not.
"""
import re

from pipeline.citation_types import classify_citation_type_standalone
from pipeline.reference_linker import _people, _same_person, printed_number

IBIDEM_LABELS = {'Author', 'Editor', 'Translator', 'Title', 'BookTitle', 'Journal', 'Publisher', 'PublicationYear',
                 'PublicationPlace', 'Volume', 'Issue'}
OPUS_CITATUM_LABELS = IBIDEM_LABELS - {'Author'}
IDEM_LABELS = {'Author', 'Editor'}
PEOPLE_LABELS = ('Author', 'Editor')
MARKER_LABELS = {'Ibidem', 'OpusCitatum'}          # "ibid.", "op. cit." themselves: nothing to take over
# The same author (not the same work): idem, eadem, eidem, iidem; tenże, taż, ciż,
# tychże; ders., dies., derselbe, dieselbe.
_IDEM = re.compile(r'(?<!\w)(idem|eadem|eidem|iidem|tenże|taż|ciż|tychże|ders\.?|dies\.?|derselbe|dieselbe)(?!\w)',
                   re.IGNORECASE)

# The viewer's reading order (footnotesInReadingOrder / groupFootnotesByRegion)
COLUMN_GAP = 60
BBOX_GROUP_TOLERANCE = 4


def _list_page(footnote):
    """The page whose footnote list a footnote is in (app._footnote_list_page)."""
    page = footnote.get('list_page')
    return page if page is not None else footnote.get('page')


def _by_numbers(footnotes):
    """Sorted by printed number when every one has a different number, else None."""
    numbers = [printed_number(f) for f in footnotes]
    if footnotes and all(n is not None for n in numbers) and len(set(numbers)) == len(numbers):
        return sorted(footnotes, key=printed_number)
    return None


def page_in_reading_order(footnotes):
    """One page's footnotes as the viewer lists them (a
    footnote drawn or split off later must not end up out of sequence).

    A page ordered by hand keeps that order; a footnote added to it later goes
    before the first hand-ordered one with a higher printed number. Otherwise,
    when every footnote with a box has its own printed number, by those
    numbers; failing that, the detected regions by column, then from the top,
    footnotes split out of one region by their numbers (or as stored), and
    those without a box last."""
    if any(f.get('order_manual') for f in footnotes):
        out = [f for f in footnotes if f.get('order_manual')]
        for f in (f for f in footnotes if not f.get('order_manual')):
            n = printed_number(f)
            at = next((i for i, g in enumerate(out) if n is not None and (printed_number(g) or -1) > n), len(out))
            out.insert(at, f)
        return out
    placed = [f for f in footnotes if f.get('bbox')]
    unplaced = [f for f in footnotes if not f.get('bbox')]
    numbered = _by_numbers(placed)
    if numbered is not None:
        return numbered + unplaced
    groups = []
    for f in placed:
        b = f['bbox']
        for g in groups:
            a = g[0]['bbox']
            if all(abs(float(a[k]) - float(b[k])) <= BBOX_GROUP_TOLERANCE for k in ('x1', 'y1', 'x2', 'y2')):
                g.append(f)
                break
        else:
            groups.append([f])
    if len(groups) >= 2:
        xs = sorted({float(g[0]['bbox']['x1']) for g in groups})
        column, index = {}, 0
        for i, x in enumerate(xs):
            if i and x - xs[i - 1] > COLUMN_GAP:
                index += 1
            column[x] = index
        groups.sort(key=lambda g: (column[float(g[0]['bbox']['x1'])], float(g[0]['bbox']['y1'])))
    return [f for g in groups for f in (_by_numbers(g) or g)] + unplaced


def document_order(footnotes):
    """All footnotes, page by page, each page in reading order."""
    pages = {}
    for f in footnotes:
        pages.setdefault(_list_page(f), []).append(f)
    order = sorted(pages, key=lambda p: (p is None, p if isinstance(p, (int, float)) else 0))
    return [f for p in order for f in page_in_reading_order(pages[p])]


def citation_type(ref):
    """ibidem / opus_citatum / normal: the text's patterns, else its parts."""
    kind = classify_citation_type_standalone(ref.get('text') or '')
    if kind != 'normal':
        return kind
    labels = {c.get('label') for c in ref.get('components') or []}
    if 'Ibidem' in labels:
        return 'ibidem'
    if 'OpusCitatum' in labels:
        return 'opus_citatum'
    return 'normal'


def _says_idem(ref):
    """Whether the reference refers back to an author ("Idem", "Tenże") rather
    than to a work ("Ibidem", "Tamże"): its Ibidem part's words, else its text's
    start."""
    marks = [c.get('text') or '' for c in ref.get('components') or [] if c.get('label') == 'Ibidem']
    return any(_IDEM.search(t) for t in marks) if marks else bool(_IDEM.match((ref.get('text') or '').lstrip()))


def _people_of(parts):
    """Each person named in the parts' authors and editors, as folded name parts."""
    return [p for c in parts if c.get('label') in PEOPLE_LABELS for p in _people(c.get('text'))]


def _same_people(mine, theirs):
    return any(_same_person(p, q) for p in mine for q in theirs)


def _part_key(c):
    return c.get('label'), (c.get('text') or '').strip()


def _infer(ref, source, allowed, kind):
    """(taken, left out): the source's parts the reference takes - those the
    rules allow that it leaves out (the pipeline's
    infer_components_from_reference, with the labels above), plus those added
    by hand, less those removed by hand - and the source's other parts."""
    own = ref.get('components') or []
    text = (ref.get('text') or '').lower()
    labels = {c.get('label') for c in own}
    texts = {(c.get('text') or '').lower() for c in own}
    removed = {_part_key(c) for c in ref.get('inferred_removed') or []}
    added = {_part_key(c) for c in ref.get('inferred_added') or []}
    out, left, seen = [], [], set()
    for c in source:
        key = _part_key(c)
        if c.get('label') in MARKER_LABELS or not key[1] or key in seen:
            continue
        seen.add(key)
        t = (c.get('text') or '').lower()
        by_rule = c.get('label') in allowed and c.get('label') not in labels and t not in texts and t not in text
        if key not in removed and (by_rule or key in added):
            out.append({'label': c['label'], 'text': c.get('text'), 'confidence': c.get('confidence'),
                        'inferred': True, 'inference_type': kind})
        else:
            left.append({'label': c['label'], 'text': c.get('text')})
    return out, left


def _names_a_work(parts):
    """Whether the parts say anything about a work (not just pages or a number)."""
    return any(c.get('label') in IBIDEM_LABELS for c in parts)


def _units(ref):
    """(id, parts) of the work a full reference stands for, or of each work of
    a divided one, in order."""
    parts = ref.get('components') or []
    works = sorted(ref.get('works') or [], key=lambda w: w['start'])
    if not works:
        return [(ref.get('id'), parts)]
    return [(w.get('id'), [c for c in parts if w['start'] <= c.get('start', -1) < max(w['end'], w['start'] + 1)]
             + [h for h in (w.get('inherited') or []) if h.get('label')]) for w in works]


def reinfer(result):
    """Infer every ibid. and op. cit. of the document again, in place."""
    ordered = document_order(result.get('results') or [])
    refs = [(footnote, ref) for footnote in ordered
            for ref in sorted(footnote.get('references') or [], key=lambda r: r.get('start', 0))]
    parts_of = {uid: parts for _, ref in refs for uid, parts in _units(ref) if uid}
    resolved = {}                 # id -> the work an ibid./op. cit. stands for, once worked out
    head = {}                     # id -> the full reference at the head of its chain
    added = {id(footnote): [] for footnote in ordered}
    previous = previous_id = None  # the work of the citation just before
    works = []                     # (people, parts, id) of the works cited so far, most recent last
    for footnote, ref in refs:
        ref.pop('components_inferred', None)
        ref.pop('inferred_from', None)
        ref.pop('inference_left_out', None)
        kind = citation_type(ref)
        ref['citation_type'] = kind
        own = ref.get('components') or []
        if kind == 'normal':
            for key in ('source_reference_id', 'inferred_removed', 'inferred_added'):
                ref.pop(key, None)                         # only an ibid. or op. cit. has a source
            units = [(uid, parts) for uid, parts in _units(ref) if _names_a_work(parts)]
            if units:
                works.extend((_people_of(parts), parts, uid) for uid, parts in units)
                previous_id, previous = units[-1]
            continue
        chosen = ref.get('source_reference_id')
        if chosen and chosen in parts_of and chosen != ref.get('id'):
            source, source_id = resolved.get(chosen) or parts_of[chosen], chosen
        else:
            ref.pop('source_reference_id', None)
            if kind == 'ibidem':
                source, source_id = previous, previous_id
            else:
                mine = _people_of(own)
                source, source_id = next(((parts, uid) for people, parts, uid in reversed(works)
                                          if mine and _same_people(mine, people)), (None, None))
        idem = kind == 'ibidem' and _says_idem(ref) and any(c.get('label') == 'Title' for c in own)
        allowed = IDEM_LABELS if idem else IBIDEM_LABELS if kind == 'ibidem' else OPUS_CITATUM_LABELS
        if source is not None and source_id:
            ref['inferred_from'] = head.get(source_id, source_id)
        inferred, left = _infer(ref, source, allowed, kind) if source else ([], [])
        if left:
            ref['inference_left_out'] = left
        if inferred:
            ref['components_inferred'] = sorted(own + inferred, key=lambda c: c.get('label') or '')
            added[id(footnote)].extend(inferred)
        work = ref.get('components_inferred') or own
        if ref.get('id'):
            resolved[ref['id']] = work
            # an idem is a work of its own; one that resolved to nothing heads its chain too
            head[ref['id']] = ref['id'] if idem or not ref.get('inferred_from') else ref['inferred_from']
        if idem:                                           # a new work by that author
            works.append((_people_of(work), work, ref.get('id')))
        previous, previous_id = work, ref.get('id')
    for footnote in ordered:
        if added[id(footnote)]:
            footnote['inferred_components'] = added[id(footnote)]
        else:
            footnote.pop('inferred_components', None)
    return result
