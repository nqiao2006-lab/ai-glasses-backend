"""Select an observed transcript when photos show the SAME text surface.

No characters are synthesized or corrected. Different medicine faces must not
be passed as a same-surface group, and this summary never overwrites fields.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import unicodedata

from ocr_enhance import quality


def normalized(text):
    return ''.join(char for char in unicodedata.normalize('NFC', text).casefold()
                   if not char.isspace())


def edit_distance(first, second):
    if len(first) < len(second):
        first, second = second, first
    previous = list(range(len(second) + 1))
    for row, a in enumerate(first, 1):
        current = [row]
        for column, b in enumerate(second, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (a != b)))
        previous = current
    return previous[-1]


def summarize_surface(pages):
    candidates = []
    seen_sources = set()
    seen_content = set()
    for page in pages:
        path = Path(page['source_image']).resolve()
        source = str(path)
        if source in seen_sources:
            continue
        seen_sources.add(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if digest is not None and digest in seen_content:
            continue
        if digest is not None:
            seen_content.add(digest)
        text = '\n'.join(line['text'] for line in page['lines']).strip()
        key = normalized(text)
        if not key:
            continue
        q = quality(page['raw_lines'])
        candidates.append(dict(text=text, source_image=source,
                               mean_confidence=q['mean_confidence'],
                               normalized_text=key))
    if not candidates:
        return dict(status='no_text', candidates=[], selected=None)
    for candidate in candidates:
        key = candidate['normalized_text']
        candidate['agreement'] = sum(
            1 - edit_distance(key, other['normalized_text']) /
            max(len(key), len(other['normalized_text']), 1)
            for other in candidates) / len(candidates)
    selected = max(candidates, key=lambda item: (item['agreement'], item['mean_confidence']))
    return dict(
        status='observed_candidate' if len(candidates) > 1 else 'single_readable_view',
        method='normalized-edit-distance medoid; selected text is copied from one image',
        selected={key: value for key, value in selected.items() if key != 'normalized_text'},
        candidates=[{key: value for key, value in item.items() if key != 'normalized_text'}
                    for item in candidates],
        review_required=True,
        note='仅用于同一文字面的多角度照片；保留全部候选，不覆盖药品字段。')
