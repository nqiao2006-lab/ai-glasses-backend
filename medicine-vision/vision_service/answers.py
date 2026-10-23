"""Question routing over visible OCR evidence, without a generative model."""
import re
from medicine_ocr import extract_fields


def intent(question):
    q = question.lower()
    if any(s in q for s in ['生产日期','制造日期','出厂日期','manufacturing date','manufacture date','mfg']):
        return '生产日期'
    if any(s in q for s in ['有效期至','失效','到期','expiry','expiration','expire','过期']) or (
        '有效期' in q and any(s in q for s in ['什么时候','哪天','日期'])):
        return '有效期至'
    if any(s in q for s in ['保质期','有效期','shelf life']):
        return '有效期'
    if any(s in q for s in ['批号','lot','batch']):
        return '产品批号'
    if any(s in q for s in ['使用说明','用法','怎么用','如何使用','怎么吃','用量','usage']):
        return '用法用量'
    if any(s in q for s in ['什么药','药名','药品名称']):
        return '药品名称'
    if any(s in q for s in ['厂家','生产企业','manufacturer']):
        return '生产企业'
    if any(s in q for s in ['写了什么','文字','读一下','read','text','ocr']):
        return 'read_text'
    if any(s in q for s in ['什么东西','什么物品','这是什么','物体','object','what is']):
        return 'object_recognition'
    return 'unsupported_question'


def public_evidence(value, source_map):
    if isinstance(value, list):
        return [public_evidence(x,source_map) for x in value]
    if isinstance(value, dict):
        result = {k:public_evidence(v,source_map) for k,v in value.items()
                  if k not in {'source_image','report_image'}}
        if 'source_image' in value:
            result['frame_id'] = source_map.get(str(value['source_image']))
        return result
    return value


def make_result(pages, frames, question):
    source_map = {str(frame['path']):frame['frame_id'] for frame in frames}
    fields = public_evidence(extract_fields(pages),source_map)
    output_frames = []
    for page, frame in zip(pages,frames):
        output_frames.append(dict(frame_id=frame['frame_id'],sha256=frame['sha256'],quality=frame['quality'],
                                  ocr_text='\n'.join(x['text'] for x in page['lines']),
                                  lines=public_evidence(page['lines'],source_map)))
    kind = intent(question)
    all_text = '\n\n'.join(x['ocr_text'] for x in output_frames)
    found = fields.get(kind, [])
    if kind == 'read_text':
        answer = all_text or '没有识别到可见文字，请调整拍摄位置后重试。'
        answer_type, evidence_text = ('ocr_text' if all_text else 'not_found'), all_text
    elif kind in fields:
        if not found:
            answer = f'未识别到有明确标签的{kind}，请近距离拍摄对应位置。'
            answer_type, evidence_text = 'not_found', ''
        else:
            values = [x['value'] for x in found]
            answer_type = 'field_candidate' if len(values)==1 else 'conflicting_candidates'
            answer = (f'{kind}识别候选：'+'；'.join(values)+'。请对照原图确认。')
            evidence_text = '\n'.join(dict.fromkeys(e['text'] for x in found for e in x['evidence']))
    else:
        answer_type, evidence_text = 'unsupported', ''
        answer = ('当前服务支持文字识别和字段提取，尚未部署物体识别模型。' if kind == 'object_recognition'
                  else '当前支持读文字、查生产日期、有效期、批号、药名和用法用量；此问题暂不支持。')
    return dict(answer=answer,answer_type=answer_type,intent=kind,evidence_text=evidence_text,
                review_required=bool(found),fields=fields,frames=output_frames,
                capabilities=dict(ocr=True,explicit_field_extraction=True,object_recognition=False,generative_vqa=False))
