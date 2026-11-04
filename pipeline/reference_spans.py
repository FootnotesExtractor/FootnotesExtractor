"""Repair of reference spans the reference model cut in two.

The reference model decides every sub-word token on its own, and the decoder
(NEREngine.extract_entities) closes a reference at the first token it leaves
out. A single doubtful token - typically the "." of "s."/"p." - therefore cuts
"..., London 2014, s. 5." into "..., London 2014, s" and a reference "5.". In
the training data a page locator always lies inside its reference and no
reference consists of pages alone, so such a
piece is never a reference of its own.

What a span contains decides, never how long it is: only a span holding
nothing but a page locator (or no letter and no digit at all) stops being a
reference. "Kant, KrV", "Ibid., p. 5" or "idem, Dzieje" stay references
however short they are.

repair_reference_spans works on the raw spans of one footnote, before their
parts are tagged, so the component model then sees "London 2014, s. 5." and
tags the 5 as Pages itself. repair_stored_references does the same on stored
references with their parts, where the
tagged parts must agree too. Pure: no model, no torch.
"""
import copy
import re
import uuid

# Words that announce a page, column, volume, number, ... (with or without the
# full stop). Case matters: "S." is German Seite, "s." Polish strona.
LOCATOR_WORDS = ('s', 'ss', 'p', 'pp', 'S', 'SS', 'str', 'kol', 'col', 'nr', 'Nr', 't', 'vol', 'Vol', 'Bd',
                 'cz', 'fol', 'f', 'ff', 'art', 'cap', 'chap', 'Kap', 'rozdz')
# Abbreviations after which text is never cut into separate model chunks
# (NEREngine.split_long_text): the locator words and a few that introduce
# or sit inside a reference.
NO_CHUNK_BREAK_AFTER = set(LOCATOR_WORDS) | {'zob', 'por', 'vgl', 'cf', 'ed', 'red', 'hrsg', 'Hrsg', 'wyd', 'op', 'cit'}

_LOC = r'(?:' + '|'.join(sorted((re.escape(w) for w in LOCATOR_WORDS), key=len, reverse=True)) + r')'
# Valid Roman numerals only ("civil" or "mid" are words). Upper case may stand
# alone (a volume "II"); lower case only after a locator word ("p. ix"), so a
# split "umk" + "pl" of a web address is not taken for a page.
_ROMAN_UP = r'(?=[IVXLCDM])M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})'
_ROMAN_LOW = r'(?=[ivxlcdm])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})'
_NUM_ALONE = r'(?:\d+[a-z]?|' + _ROMAN_UP + r')'
_NUM_LOCATED = r'(?:\d+[a-z]?|' + _ROMAN_UP + r'|' + _ROMAN_LOW + r')'


def _item(num):
    return num + r'(?:\s*[-–—]\s*' + num + r')?(?:\s*ff?\.?)?'


_SEP = r'(?:\s*,\s*|\s+(?:i|and|et|u\.|oraz)\s+)'
# a locator word is followed by its full stop or a space: "s. 5", "S.34", "s 5"
_LOCATOR = r'(?:' + _LOC + r'(?:\.\s*|\s+))'
_PIECE = r'(?:' + _LOCATOR + _item(_NUM_LOCATED) + r'|' + _item(_NUM_ALONE) + r')'
# A whole span that is a page locator: "5.", "298–323", "S. 34, 107", "755ff.", "pp".
_TAIL_RE = re.compile(r'^(?:' + _PIECE + r'(?:' + _SEP + _PIECE + r')*|' + _LOC + r')\.?$')
_YEAR_RE = re.compile(r'^(1[4-9]\d\d|20\d\d)$')
# "f."/"ff." close a locator ("S. 366 ff."): no number follows them
_END_LOC = r'(?:' + '|'.join(sorted((re.escape(w) for w in LOCATOR_WORDS if w not in ('f', 'ff')), key=len, reverse=True)) + r')'
_ENDS_WITH_LOCATOR = re.compile(r'(?:^|[\s,.(:])' + _END_LOC + r'\.?$')
# The page number right after a reference that ends with a locator word.
_UNCOVERED_NUMBER = re.compile(r'^\.?\s?(' + _item(r'\d+[a-z]?') + r'(?:' + _SEP + _item(r'\d+[a-z]?') + r')*)')
# "653 s." (653 pages), "88 s." / "281 ss." (and following), "c.s.", "b.p.":
# after a number or a one-letter abbreviation the locator word announces no
# page. A number after it that a word follows, or that starts the next line,
# is the next footnote's ("Jouanna 2007, 653 s. 14 Arist. Poet." where two
# footnotes ran together), not a page.
_SUFFIX_LOCATOR = re.compile(r'(?:\d|(?<![^\W\d_])[^\W\d_]\.)\s*' + _END_LOC + r'\.?$')
_WORD_AFTER = re.compile(r'[ \t]+(?!(?:i|and|et|u\.|oraz)\s)[^\W\d_]')
_GAP_RE = re.compile(r'^[\s.,:()\[\]–—\-]{0,4}$')
_OUTER = ' \t\n.,:;()[]'
PAGE_LABELS = {'Pages', 'Volume', 'Issue'}


def has_word_or_number(s):
    return any(ch.isalnum() for ch in s or '')


def is_locator_tail(s):
    """True when the whole text is a page locator and nothing else."""
    core = (s or '').strip(_OUTER)
    if not core or _YEAR_RE.match(core):
        return False                       # "2001" alone: a year, i.e. another work
    return bool(_TAIL_RE.match(core))


def ends_with_locator_word(s):
    return bool(_ENDS_WITH_LOCATOR.search((s or '').rstrip()))


def no_chunk_break_before(text, dot_index):
    """Whether the "." at dot_index ends one of NO_CHUNK_BREAK_AFTER."""
    m = re.search(r'([^\W\d_]+)$', text[:dot_index])
    return bool(m) and m.group(1) in NO_CHUNK_BREAK_AFTER


def _gap_ok(text, prev, span):
    gap = text[prev['end']:span['start']]
    return bool(_GAP_RE.match(gap)) and not text[prev['start']:prev['end']].rstrip().endswith(';')


def _mergeable(text, prev, span):
    if not _gap_ok(text, prev, span):
        return False
    body = text[span['start']:span['end']]
    if is_locator_tail(body):
        return True
    # "..., s" + "5.": the locator word stayed with the reference, the number
    # did not; a year is a page here too ("s" + "2001"), a work is not ("14 Arist. Poet.")
    return ends_with_locator_word(text[prev['start']:prev['end']]) and bool(_TAIL_RE.match(body.strip(_OUTER)))


def _uncovered_number_end(text, span, next_start):
    """End of the page number right after a span ending with a locator word,
    or None. It must not run into the next span (that is a merge instead)."""
    if not ends_with_locator_word(text[span['start']:span['end']]):
        return None
    m = _UNCOVERED_NUMBER.match(text[span['end']:])
    if not m:
        return None
    end = span['end'] + m.end()
    while end > span['end'] and text[end - 1] in ' ,':
        end -= 1
    if end <= span['end'] or (next_start is not None and end > next_start):
        return None
    if not any(ch.isdigit() for ch in text[span['end']:end]):
        return None
    if _SUFFIX_LOCATOR.search(text[span['start']:span['end']].rstrip()) and (
            '\n' in text[span['end']:end] or _WORD_AFTER.match(text, end)):
        return None
    return end


def repair_reference_spans(text, spans):
    """The reference spans of one footnote, repaired: (spans, actions).

    1. a span with no letter and no digit is dropped;
    2. a span ending with a locator word takes in the page number after it,
       when no span covers it;
    3. a span that is only a page locator joins the reference just before it
       (gap of punctuation or space only, at most 4 characters, never ";").
    Spans are dicts with start, end, text and confidence; other keys of the
    kept span are left as they are."""
    spans = sorted((dict(s) for s in spans), key=lambda s: (s['start'], s['end']))
    actions = []
    kept = []
    for s in spans:
        if not has_word_or_number(text[s['start']:s['end']]):
            actions.append(('drop', text[s['start']:s['end']]))
        else:
            kept.append(s)
    for i, s in enumerate(kept):
        nxt = kept[i + 1]['start'] if i + 1 < len(kept) else None
        end = _uncovered_number_end(text, s, nxt)
        if end:
            actions.append(('extend', text[s['start']:s['end']], text[s['start']:end]))
            s['end'] = end
    out = []
    for s in kept:
        out.append(s)
        # the newest span joins the one before it while it is a page locator;
        # a piece that grew by joining is checked again ("12, s" + "19.")
        while len(out) >= 2 and _mergeable(text, out[-2], out[-1]):
            prev, s = out[-2], out.pop()
            actions.append(('merge', text[prev['start']:prev['end']], text[s['start']:s['end']]))
            prev['end'] = max(prev['end'], s['end'])
            if s.get('confidence') is not None:
                prev['confidence'] = max(prev.get('confidence') or 0, s['confidence'])
    for s in out:
        s['text'] = text[s['start']:s['end']]
    return out, actions


def _own_labels(ref):
    return {c.get('label') for c in ref.get('components') or [] if not c.get('inferred')}


def _extend_last_work(ref, end):
    """A reference divided into works (reference_split) keeps them; the last
    work now reaches the new end."""
    works = ref.get('works') or []
    if works:
        last = max(works, key=lambda w: w.get('start', 0))
        last['end'] = max(last.get('end', end), end)


def repair_stored_references(text, refs):
    """repair_reference_spans for the stored references of one footnote, with
    their parts: (references, actions, removed) where removed maps the id of
    each reference that was dropped or merged to the id it went into (None
    when dropped). Parts must agree with the text: a reference joins the one
    before it only if its own parts are Pages, Volume or Issue alone (or none);
    with any other part it stays, however short. References a reviewer
    edited are left alone, and so is their neighbour."""
    refs = sorted((copy.deepcopy(r) for r in refs), key=lambda r: (r.get('start', 0), r.get('end', 0)))
    actions, removed = [], {}
    kept = []
    for r in refs:
        body = text[r['start']:r['end']]
        if not has_word_or_number(body) and not _own_labels(r) - {None} and not r.get('edited'):
            actions.append(('drop', body))
            removed[r.get('id')] = None
        else:
            kept.append(r)
    for i, r in enumerate(kept):
        if r.get('edited'):
            continue
        nxt = kept[i + 1]['start'] if i + 1 < len(kept) else None
        end = _uncovered_number_end(text, r, nxt)
        if not end:
            continue
        number = text[r['end']:end].lstrip(' .')
        n_start = end - len(number)
        actions.append(('extend', text[r['start']:r['end']], text[r['start']:end]))
        r['end'] = end
        r['text'] = text[r['start']:end]
        r.setdefault('components', []).append({
            'id': str(uuid.uuid4()), 'label': 'Pages', 'start': n_start, 'end': end,
            'text': text[n_start:end], 'confidence': None, 'source': 'span_repair'})
        _extend_last_work(r, end)
    def joins(prev, r):
        return (not prev.get('edited') and not r.get('edited')
                and _own_labels(r) <= PAGE_LABELS and _mergeable(text, prev, r))

    out = []
    for r in kept:
        out.append(r)
        while len(out) >= 2 and joins(out[-2], out[-1]):         # checked again once it grew
            prev, r = out[-2], out.pop()
            actions.append(('merge', text[prev['start']:prev['end']], text[r['start']:r['end']]))
            prev['end'] = max(prev['end'], r['end'])
            prev['text'] = text[prev['start']:prev['end']]
            prev['components'] = sorted((prev.get('components') or [])
                                        + [c for c in r.get('components') or [] if not c.get('inferred')],
                                        key=lambda c: (c.get('start', 0), c.get('end', 0)))
            _extend_last_work(prev, prev['end'])
            removed[r.get('id')] = prev.get('id')
            for k, v in list(removed.items()):          # pieces merged into r went on into prev
                if v == r.get('id'):
                    removed[k] = prev.get('id')
    return out, actions, removed
