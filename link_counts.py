"""How many entries of a job's reference list are linked to a footnote
reference, for the job lists: "10/23" rather than "23".

Working the links out takes up to a second or two for a long list - too long
for a collection page of hundreds of jobs - so each job's counts are kept in
results/<job>_reference_links_count.json together with what they were worked
out from: the result's and the list's file times, the matcher's version and
the default level. A job list shows the counts that are up to date and has
the rest worked out in the background (app.py); the viewer's own load of the
list at the default level stores its counts at no extra cost.
"""
import json
from pathlib import Path

from pipeline import reference_linker


def _path(results_dir, job_id):
    return Path(results_dir) / f'{job_id}_reference_links_count.json'


def key(results_dir, job_id):
    """What the counts depend on, or None for a job without a list."""
    results_dir = Path(results_dir)
    try:
        listed = (results_dir / f'{job_id}_reference_list.json').stat().st_mtime_ns
        result = (results_dir / f'{job_id}_results.json').stat().st_mtime_ns
    except OSError:
        return None
    return [result, listed, reference_linker.MATCHER_VERSION, reference_linker.DEFAULT_LEVEL]


def cached(results_dir, job_id):
    """{'linked_entries', 'entries'} when up to date, else None."""
    current = key(results_dir, job_id)
    if current is None:
        return None
    try:
        data = json.loads(_path(results_dir, job_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if data.get('key') == current else None


def store(results_dir, job_id, counts, worked_out_from):
    """Keeps a build_view's counts; worked_out_from: key() taken before the
    files were read, so an edit made meanwhile leaves them out of date."""
    if worked_out_from is None:
        return
    path = _path(results_dir, job_id)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'key': worked_out_from, 'linked_entries': counts['linked_entries'],
                               'entries': counts['entries']}), encoding='utf-8')
    tmp.replace(path)


def compute(results_dir, job_id):
    """Works the counts out from the stored files and keeps them; None
    without a list."""
    results_dir = Path(results_dir)
    current = key(results_dir, job_id)
    if current is None:
        return None
    result = json.loads((results_dir / f'{job_id}_results.json').read_text(encoding='utf-8'))
    stored = json.loads((results_dir / f'{job_id}_reference_list.json').read_text(encoding='utf-8'))
    n = 0
    for footnote in result.get('results') or []:          # a result the app never opened has no ids yet
        for ref in footnote.get('references') or []:
            if 'id' not in ref:
                n += 1
                ref['id'] = f'unsaved-{n}'
    counts = reference_linker.build_view(result, stored, reference_linker.DEFAULT_LEVEL)['counts']
    store(results_dir, job_id, counts, current)
    return counts
