"""Local PaddleOCR 3.x tool. Extract source-backed candidates, never infer dosage/dates."""
from __future__ import annotations

import argparse
import csv
import html
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'}
LABELS = {
    '药品名称': ['药品名称', '通用名称', '通用名'],
    '商品名称': ['商品名称', '商品名'],
    '规格': ['规格'], '成份': ['成份', '成分'],
    '适应症': ['适应症', '功能主治'], '用法用量': ['用法用量'],
    '不良反应': ['不良反应'], '禁忌': ['禁忌'],
    '注意事项及不良反应': ['注意事项及不良反应'],
    '注意事项': ['注意事项'], '贮藏': ['贮藏', '储藏'],
    '生产企业': ['生产企业', '生产厂家', '企业名称'],
    '批准文号': ['批准文号'], '生产日期': ['生产日期', '生产年月', '制造日期', 'MFG'],
    '有效期至': ['有效期至', '失效日期', '失效期', 'EXP'],
    '有效期': ['有效期'], '产品批号': ['产品批号', '生产批号', '批号', 'LOT'],
}
REVERSE_LABELS = {label: field for field, labels in LABELS.items() for label in labels}
LABEL_PATTERN = '|'.join(
    re.escape(x) for x in sorted(REVERSE_LABELS, key=len, reverse=True)
)
LABEL_RE = re.compile(r'^\s*[【\[一—-]?\s*(' + LABEL_PATTERN +
                      r')\s*[】\]]?\s*[:：]?\s*(.*)$', re.IGNORECASE)
INLINE_LABEL_RE = re.compile(r'(?:[【\[]\s*(?:' + LABEL_PATTERN +
                             r')\s*[】\]]|(?:' + LABEL_PATTERN + r')\s*[:：])', re.IGNORECASE)
ANY_HEADING = re.compile(r'^\s*[【\[]')
SECTION_BOUNDARY = re.compile(r'^\s*(?:性状|包装|生产地址|网址|儿童用药|老年用药|'
                              r'孕妇及哺乳期妇女用药|药物相互作用|药理毒理|药代动力学|'
                              r'药物过量|执行标准|说明书修订日期|核准日期|修改日期)\s*[:：]?')
SHORT_FIELDS = {'药品名称', '商品名称', '生产企业', '批准文号', '生产日期', '有效期至', '有效期', '产品批号'}
IDENTIFIER_FIELDS = {'生产日期', '有效期至', '产品批号'}
DATE_RE = re.compile(r'(?<!\d)(?:20\d{2}|19\d{2})(?:(?:[-./年]\s*\d{1,2})(?:(?:[-./月]\s*\d{1,2})\s*日?)?\s*月?|\d{4})(?!\d)')


def dump_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def plain(value):
    if hasattr(value, 'tolist'):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(x) for x in value]
    if hasattr(value, 'item'):
        return value.item()
    return value


def evidence(line):
    keys = ('source_image', 'line_index', 'text', 'bbox', 'layout_bbox',
            'recognition_variant', 'ocr_confidence', 'source_fragments')
    return {key: line.get(key) for key in keys if key in line}


def split_inline_labels(line):
    """Split explicitly delimited labels while retaining the original evidence."""
    matches = list(INLINE_LABEL_RE.finditer(line['text']))
    if len(matches) < 2 or matches[0].start() > 1:
        return [line]
    pieces = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line['text'])
        piece = dict(line, text=line['text'][match.start():end].strip())
        piece['source_fragments'] = line.get('source_fragments', [dict(line)])
        pieces.append(piece)
    return pieces


def adjacent_identifier(label, value):
    """Require spatial evidence before assigning a detached date/batch value."""
    a = label.get('layout_bbox', label.get('bbox'))
    b = value.get('layout_bbox', value.get('bbox'))
    if not a or not b:
        return False
    ax, ay = [p[0] for p in a], [p[1] for p in a]
    bx, by = [p[0] for p in b], [p[1] for p in b]
    height = max(max(ay) - min(ay), max(by) - min(by), 1)
    overlap = min(max(ax), max(bx)) - max(min(ax), min(bx))
    same_row = abs((min(ay) + max(ay) - min(by) - max(by)) / 2) <= height * 0.6
    right = min(bx) >= max(ax) - height and min(bx) - max(ax) <= height * 12
    below = 0 <= min(by) - max(ay) <= height * 2.5
    aligned = overlap > 0 or abs(min(ax) - min(bx)) <= height
    return (same_row and right) or (below and aligned)


def valid_date(value):
    digits = re.findall(r'\d+', value)
    if len(digits) == 1 and len(digits[0]) == 8:
        digits = [digits[0][:4], digits[0][4:6], digits[0][6:]]
    try:
        year, month = int(digits[0]), int(digits[1])
        datetime(year, month, int(digits[2]) if len(digits) > 2 else 1)
        return True
    except (ValueError, IndexError):
        return False


def field_views(pages):
    """Parse each OCR retry separately so an alternative date cannot disappear.

    A retry is evidence from the same photo, never an additional independent
    photograph. Keeping each transcript separate also prevents a label in one
    direction being joined to a number from a different direction.
    """
    for page in pages:
        candidates = page.get('recognition_candidates')
        if candidates:
            for candidate in candidates:
                yield dict(source_image=page['source_image'], lines=candidate['lines'])
        else:
            yield page


def extract_fields(pages):
    fields = {field: [] for field in LABELS}
    for page in field_views(pages):
        lines = page['lines']
        active = None
        pieces = []
        sources = []

        def flush():
            if not active or not pieces:
                return
            raw_value = '\n'.join(pieces).strip()
            if not raw_value:
                return
            value = raw_value
            notes = []
            if active in ('生产日期', '有效期至'):
                dates = DATE_RE.findall(raw_value)
                if len(dates) == 1 and valid_date(dates[0]):
                    value = dates[0].strip()
                else:
                    # Templates such as “年·月” and ambiguous/invalid dates remain in OCR only.
                    return
            if active == '产品批号' and not re.search(r'[0-9A-Za-z]', raw_value):
                return
            if active == '有效期':
                notes.append('这是原文期限信息，不自动推算截止日期。')
            entry = {'value': value, 'raw_text': raw_value,
                     'evidence': [evidence(line) for line in sources],
                     'review_required': True, 'notes': notes}
            normalized = re.sub(r'\s+', '', value)
            existing = next((x for x in fields[active]
                             if re.sub(r'\s+', '', x['value']) == normalized), None)
            if existing:
                existing['evidence'].extend(entry['evidence'])
            else:
                fields[active].append(entry)

        lines = [part for line in lines for part in split_inline_labels(line)]
        for line in lines:
            text = line['text'].strip()
            if active and sources and (line.get('layout_orientation') == 'vertical' or
                                       sources[0].get('layout_orientation') == 'vertical'):
                if line.get('layout_group_id') != sources[0].get('layout_group_id'):
                    flush()
                    active, pieces, sources = None, [], []
            match = LABEL_RE.match(text)
            if match:
                flush()
                label = match.group(1)
                active = REVERSE_LABELS.get(label) or REVERSE_LABELS.get(label.upper())
                pieces = [match.group(2)] if match.group(2) else []
                sources = [line]
            elif ANY_HEADING.match(text) or SECTION_BOUNDARY.match(text):
                flush()
                active, pieces, sources = None, [], []
            elif active:
                if active in IDENTIFIER_FIELDS and not pieces and not adjacent_identifier(sources[0], line):
                    active, pieces, sources = None, [], []
                    continue
                # Long sections retain complete OCR text. Short fields take only one next line.
                if active in SHORT_FIELDS and pieces:
                    flush()
                    active, pieces, sources = None, [], []
                else:
                    pieces.append(text)
                    sources.append(line)
        flush()
    return fields


def create_engine():
    from ocr_runtime import configure_runtime
    configure_runtime()
    try:
        from paddleocr import PaddleOCR
    except ImportError as error:
        raise RuntimeError('尚未安装 PaddleOCR。请先运行 install.cmd，安装完成后再识别。') from error
    print('正在加载中文 OCR 模型；首次使用需要下载模型，请稍候。', flush=True)
    return PaddleOCR(device='cpu',
                       text_detection_model_name='PP-OCRv6_medium_det',
                       text_recognition_model_name='PP-OCRv6_medium_rec',
                       enable_mkldnn=False, cpu_threads=4,
                       use_doc_orientation_classify=False, use_doc_unwarping=False,
                       use_textline_orientation=False)


def recognize(engine, image, source):
    """Adapt one PaddleOCR image result without changing recognized text."""
    outputs = list(engine.predict(input=image))
    if len(outputs) != 1:
        raise RuntimeError('单张图片未返回唯一 OCR 结果。')
    data = outputs[0].json
    if callable(data):
        data = data()
    if isinstance(data, str):
        data = json.loads(data)
    data = plain(data.get('res', data))
    texts = data.get('rec_texts')
    if texts is None:
        raise RuntimeError('OCR 返回格式不符合预期，未找到 rec_texts。')
    scores = data.get('rec_scores', [])
    boxes = data.get('rec_polys', data.get('rec_boxes', []))

    def polygon(index):
        box = boxes[index] if index < len(boxes) else None
        if box is not None and len(box) == 4 and all(isinstance(x, (int, float)) for x in box):
            x1, y1, x2, y2 = box
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        return box

    return [dict(text=str(text), line_index=n,
                 ocr_confidence=float(scores[n]) if n < len(scores) else None,
                 bbox=polygon(n),
                 source_image=str(source)) for n, text in enumerate(texts)]


def infer(images, *, enhance=False, perspective=False, engine=None):
    from ocr_layout import normalize_layout
    engine = engine if engine is not None else create_engine()
    pages = []
    for index, path in enumerate(images, 1):
        print(f'[{index}/{len(images)}] 识别 {path.name}', flush=True)
        if enhance or perspective:
            import cv2
            import numpy as np
            from PIL import Image
            from ocr_enhance import recognize_candidates
            with Image.open(path) as photo:
                image = cv2.cvtColor(np.asarray(photo.convert('RGB')), cv2.COLOR_RGB2BGR)
            page = recognize_candidates(image, lambda pixels: recognize(engine, pixels, path),
                                        perspective=perspective)
            page['source_image'] = str(path)
            print('  角度增强选择：' + page['enhancement']['selected'], flush=True)
        else:
            lines = recognize(engine, str(path), path)
            page = dict(source_image=str(path), raw_lines=lines, lines=normalize_layout(lines))
        pages.append(page)
    return pages


def write_outputs(output, pages, group_name, *, same_surface=False):
    output.mkdir(parents=True, exist_ok=True)
    assets = output / 'images'
    assets.mkdir(exist_ok=True)
    for n, page in enumerate(pages, 1):
        source = Path(page['source_image'])
        if source.suffix.lower() in {'.tif', '.tiff'}:
            from PIL import Image
            destination = assets / f'{n:03d}.png'
            with Image.open(source) as image:
                image.convert('RGB').save(destination)
        else:
            destination = assets / f'{n:03d}{source.suffix.lower()}'
            shutil.copy2(source, destination)
        page['report_image'] = destination.relative_to(output).as_posix()
    fields = extract_fields(pages)
    record = {'group_name': group_name, 'created_at': datetime.now().astimezone().isoformat(),
              'method': 'PaddleOCR + explicit-label extraction; no inferred dates or dosage',
              'grouping': '用户选择的照片作为同一实物；程序未验证图片之间的对象一致性。',
              'fields': fields,
              'conflicting_fields': [key for key, values in fields.items() if len(values) > 1],
              'notes': ['所有字段均需对照原图核对，尤其日期、剂量、小数点和单位。',
                        '未发现的字段保留空数组；不同候选不自动决断。',
                        '开启角度增强时，每次尝试的字段分别提取；同图不同方向不算独立照片，分歧均保留。',
                        '纵排文字按位置尝试合并同列并从右至左排序；多栏、斜拍和花式排版仍需核对。',
                        '规则提取可能漏掉未使用明确标题的信息；请同时查看全文。']}
    if same_surface:
        from ocr_multiview import summarize_surface
        record['same_surface_summary'] = summarize_surface(pages)
    dump_json(output / 'ocr.json', pages)
    dump_json(output / 'record.json', record)
    transcript = '\n\n'.join(f'=== {Path(p["source_image"]).name} ===\n' +
                              '\n'.join(line['text'] for line in p['lines']) for p in pages)
    (output / 'transcript.txt').write_text(transcript, encoding='utf-8')
    with (output / 'fields.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(['字段', '识别候选', '来源图片', '状态', '识别方向'])
        for key, values in fields.items():
            for item in values:
                # Keep OCR text from being interpreted as spreadsheet formulas.
                value = item['value']
                if value.lstrip().startswith(('=', '+', '-', '@')):
                    value = "'" + value
                writer.writerow([key, value, '; '.join(sorted({Path(e['source_image']).name
                                  for e in item['evidence']})), '有多个候选，请核对' if len(values) > 1 else '请核对原图',
                                 '; '.join(sorted({e.get('recognition_variant', '原图') for e in item['evidence']}))])
    e = html.escape
    rows = []
    for key, values in fields.items():
        value_html = '<br><br>'.join('<pre>' + e(x['value']) + '</pre><small>来源：' +
                                    e(', '.join(sorted({Path(s['source_image']).name +
                                      (' / ' + s['recognition_variant'] if s.get('recognition_variant') else '')
                                      for s in x['evidence']}))) +
                                    '</small>' for x in values) or '<span class="muted">未提取到</span>'
        state = '多个候选，需核对' if len(values) > 1 else '待核对' if values else ''
        rows.append(f'<tr><th>{e(key)}</th><td>{value_html}</td><td>{state}</td></tr>')
    page_html = []
    for page in pages:
        line_html = ''.join('<tr><td>' + e(line['text']) + '</td><td>' +
                            (f"{line['ocr_confidence']:.3f}" if line['ocr_confidence'] is not None else '') +
                            '</td></tr>' for line in page['lines'])
        raw_text = '\n'.join(line['text'] for line in page.get('raw_lines', page['lines']))
        enhancements = ''
        if page.get('enhancement'):
            alternatives = '\n\n'.join(candidate['name'] + '\n' +
                '\n'.join(line['text'] for line in candidate['raw_lines'])
                for candidate in page['recognition_candidates'])
            enhancements = ('<p>已启用角度增强，所选结果：' + e(page['enhancement']['selected']) +
                '。其他尝试结果保留供复核。</p><details><summary>查看所有角度的识别文字</summary><pre>' +
                e(alternatives) + '</pre></details>')
        page_html.append(f'<section><h2>{e(Path(page["source_image"]).name)}</h2><div class="split">'
                         f'<a href="{e(page["report_image"], quote=True)}"><img src="{e(page["report_image"], quote=True)}"></a>'
                         f'<table><thead><tr><th>排列后的 OCR 文字</th><th>模型置信度</th></tr></thead><tbody>{line_html}</tbody></table></div>'
                         f'<details><summary>查看所选结果的原始文字顺序</summary><pre>{e(raw_text)}</pre></details>{enhancements}</section>')
    report = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>药品文字识别结果</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:1280px;margin:32px auto;padding:0 24px;color:#172b3a;background:#f6f8fb}h1{font-size:28px}h2{font-size:21px}section,header{background:white;padding:24px;border-radius:12px;margin:20px 0}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #dce4eb;padding:12px;text-align:left;vertical-align:top}th{white-space:nowrap}pre{white-space:pre-wrap;font-family:inherit;margin:0 0 8px}small,.muted{color:#607585}.notice{background:#fff3d8;padding:16px;border-radius:8px;line-height:1.8}.split{display:grid;grid-template-columns:minmax(240px,1fr) minmax(320px,1fr);gap:24px}.split img{width:100%;height:auto}a{color:#1766a6}@media(max-width:800px){.split{grid-template-columns:1fr}}</style>'''
    report += f'<header><h1>{e(group_name)} · 文字识别结果</h1><p>共 {len(pages)} 张图片</p><p class="notice">识别结果需对照原图核对。缺失字段留空，冲突保留候选。生产日期、有效期至、期限和批号分别保存；不推算日期或生成用药建议。</p><a href="record.json">结构化 JSON</a> · <a href="transcript.txt">识别全文</a> · <a href="fields.csv">字段 CSV</a></header>'
    report += '<section><h2>字段候选</h2><table><tr><th>字段</th><th>内容</th><th>状态</th></tr>' + ''.join(rows) + '</table></section>'
    if same_surface:
        summary = record['same_surface_summary']
        selected = summary['selected']
        if selected:
            report += ('<section><h2>同一文字面 · 多角度复核</h2><p>从实际识别结果中选择与其他视角最接近的一份全文，未改写文字；药品字段仍保留全部候选。</p><pre>' +
                       e(selected['text']) + '</pre><small>来源：' +
                       e(Path(selected['source_image']).name) + '</small><details><summary>查看各视角候选</summary>')
            report += ''.join('<p>' + e(Path(item['source_image']).name) + '</p><pre>' +
                              e(item['text']) + '</pre>' for item in summary['candidates'])
            report += '</details></section>'
    report += ''.join(page_html) + '</html>'
    (output / 'report.html').write_text(report, encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser(description='中文药品多图 OCR；每次选择同一盒/瓶药的照片。')
    parser.add_argument('--input', type=Path, required=True, help='图片文件或只包含同一实物照片的文件夹')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--group-name', default='药品记录')
    parser.add_argument('--enhance', action='store_true', help='尝试四个旋转方向；约需四倍识别时间')
    parser.add_argument('--perspective', action='store_true', help='额外尝试检测平面边框并校正透视；适合单张纸页/标签')
    parser.add_argument('--same-surface', action='store_true', help='仅当所有照片拍摄同一文字面时，额外生成跨视角全文候选')
    args = parser.parse_args()
    source = args.input.resolve()
    images = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS) if source.is_dir() else [source]
    images = [p for p in images if p.is_file() and p.suffix.lower() in EXTENSIONS]
    if not images:
        parser.error('没有找到支持的图片，请选择 JPG、PNG 等照片。')
    if args.output.exists() and any((args.output / name).exists() for name in ('ocr.json', 'record.json', 'report.html')):
        parser.error('该输出目录已有识别报告，请选择新的输出目录以保留结果。')
    try:
        pages = infer(images, enhance=args.enhance, perspective=args.perspective)
        record = write_outputs(args.output.resolve(), pages, args.group_name,
                               same_surface=args.same_surface)
    except Exception as error:
        print(f'识别失败：{error}', file=sys.stderr, flush=True)
        return 1
    print(f'已完成 {len(pages)} 张图片。报告：{args.output.resolve() / "report.html"}', flush=True)
    print('存在多个候选的字段：' + ('、'.join(record['conflicting_fields']) or '无；仍需核对原图'), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
