#!/usr/bin/env python3
"""v16.6 fast generic DRD/ODI delta comparator with no hardcoded proof boosters.

Generic flow:
- v15 mismatch set remains baseline contract.
- resolve ODI lineage only for columns that are open/changed by v15, not every column.
- promote upstream changed candidates to FIXED_BY_RESOLVED_RULE_PROOF only using DRD-derived checks.
"""
from __future__ import annotations

import argparse, csv, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple
from openpyxl import load_workbook
import compare_drd_odi_universal as base

# Speed patch: v16.6 uses raw XML snippets for proof evidence, so the expensive
# v15 context extractor is not needed during lineage construction.
def _fast_context(sql, expression, target_col):
    return f"ODI attribute expression:\n{expression}\nTarget column: {target_col}"
base.extract_sql_context_for_expression = _fast_context

__VERSION__='16.6-fast-generic-rule-proof'

def clean(v): return base.clean_text(v)
def norm(v): return base.normalize_space(v)
def ident(v): return base.normalize_identifier(v)

def write_csv(path: Path, rows: Sequence[Dict[str,str]], fields: Sequence[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f, fieldnames=list(fields), extrasaction='ignore'); w.writeheader()
        for r in rows: w.writerow({k:r.get(k,'') for k in fields})

def canonical(expr: str, limit:int=12000) -> str:
    e=clean(expr)
    if len(e)>limit: e=e[:limit]
    e=re.sub(r'/\*.*?\*/',' ',e,flags=re.S); e=re.sub(r'--.*?$',' ',e,flags=re.M)
    e=re.sub(r'\s+',' ',e).strip().upper(); e=re.sub(r'\s+AS\s+[A-Z_][A-Z0-9_#$]*$','',e)
    return e

def short(expr:str, n:int=1800)->str:
    e=norm(expr); return e if len(e)<=n else e[:n-3]+'...'

def area_cols(area: str):
    cols=re.findall(r'`([^`]+)`', area or '')
    if not cols and re.fullmatch(r'[A-Z0-9_#$]+', area or ''): cols=[area]
    return [ident(c) for c in cols if ident(c)]

def infer_profile(detection, requested):
    if requested!='auto': return requested
    blob=(detection.target_table_from_sheet+' '+' '.join(detection.target_resources_from_xml)).upper()
    if 'AVY_FACT' in blob: return 'avy'
    if 'TAX_LOT' in blob or 'TAXLOTS' in blob: return 'taxlot'
    return 'generic'

def load_odi(xml: Path):
    objects=base.parse_odi_objects(xml)
    targets=base.extract_target_resources_from_xml(objects)
    _,_,blocks=base.extract_odi_summary(objects)
    lineage=base.build_odi_lineage(blocks)
    final=base.select_final_target_lineage(lineage)
    return targets, blocks, lineage, final

def load_drd(xlsx: Path, xml_targets: List[str], args):
    wb=load_workbook(xlsx, read_only=False, data_only=True)
    detection=base.auto_detect_mapping(wb, xml_targets=xml_targets, target_table_override=args.target_table or '', mapping_sheet_override=args.mapping_sheet or '', header_row_override=args.header_row, target_col_override=args.target_col or '', source_cols_override=args.source_cols or '', rule_col_override=args.rule_col or '')
    rows, notes=base.extract_mapping_from_xlsx(xlsx, detection)
    return detection, rows, notes

def v15_compare(mapping_rows, final_lineage, all_lineage, sql_blocks, detection, profile):
    column_diff=base.compare_columns(mapping_rows, final_lineage)
    logic_rows=[] if profile=='avy' else base.build_logic_diff_candidates(mapping_rows, all_lineage, sql_blocks)
    raw=[]; used_curated=False
    if profile=='avy':
        raw=base.build_avy_review_rules_diff(column_diff, logic_rows, detection); used_curated=bool(raw)
    if not raw: raw=base.build_full_drd_vs_odi_xml_rules_diff(column_diff, logic_rows)
    if profile=='generic' or used_curated:
        mismatches=raw; equivalent=[]
    else:
        mismatches,equivalent=base.split_mismatch_and_equivalent_rows(raw)
    by={}
    for r in column_diff:
        c=ident(r.get('target_column',''))
        if not c: continue
        s=r.get('status','')
        cls='MISSING_IN_ODI' if s=='MAPPING_ONLY' else ('ODI_ONLY' if s=='XML_ONLY' else 'IN_BOTH_NO_REVIEW')
        by[c]={'target_column':c,'v15_class':cls,'v15_reason':s,'difference_type':'','area':''}
    for r in mismatches:
        for c in area_cols(r.get('Area / Columns','')):
            if c in by: by[c].update({'v15_class':'REVIEW_REQUIRED','v15_reason':r.get('Conclusion','') or r.get('Difference Type',''),'difference_type':r.get('Difference Type',''),'area':r.get('Area / Columns','')})
    for r in equivalent:
        for c in area_cols(r.get('Area / Columns','')):
            if c in by: by[c].update({'v15_class':'MATCH_EQUIVALENT','v15_reason':r.get('Conclusion','MATCH_EQUIVALENT'),'difference_type':'MATCH_EQUIVALENT','area':r.get('Area / Columns','')})
    return by, column_diff, mismatches, equivalent

def is_passthrough(expr: str):
    e=norm(expr).strip('() ')
    m=re.match(r'^([A-Za-z_][A-Za-z0-9_#$]*)\.([A-Za-z_][A-Za-z0-9_#$]*)(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_#$]*)?$', e, flags=re.I)
    return (ident(m.group(1)), ident(m.group(2))) if m else None

def build_stage_index(lineage):
    idx=defaultdict(list)
    for r in lineage:
        c=ident(r.get('target_column',''))
        if c: idx[c].append(r)
    return idx

def resolve_one(col, final_row, stage_idx, max_depth=8):
    current=final_row; path=[]; visited=set()
    for _ in range(max_depth):
        expr=clean(current.get('expression','')); c=ident(current.get('target_column', col))
        key=(current.get('step_no',''),current.get('task_no',''),c,canonical(expr,3000))
        if key in visited: break
        visited.add(key); path.append(f"step={current.get('step_no','')}/task={current.get('task_no','')}/col={c}/expr={short(expr,220)}")
        p=is_passthrough(expr)
        if not p: break
        _, src_col=p
        candidates=stage_idx.get(src_col, [])
        best=None; best_score=-999
        for r in candidates:
            if r is current: continue
            rex=clean(r.get('expression',''))
            if not rex: continue
            score=0
            rc=canonical(rex,3000); ec=canonical(expr,3000)
            if rc!=ec: score+=10
            if 'CASE' in rc: score+=5
            if any(fn in rc for fn in ['SUBSTR','REGEXP','INSTR']): score+=4
            if any(tok in rc for tok in ['JOIN','SELECT']): score+=1
            try: score += max(0, 1000-int(r.get('step_no','999')))/1000
            except Exception: pass
            if score>best_score: best_score=score; best=r
        if best is None or canonical(best.get('expression',''),3000)==canonical(expr,3000): break
        current=best
    return {'target_column':ident(col),'final_expression':final_row.get('expression',''),'resolved_expression':clean(current.get('expression',final_row.get('expression',''))),'resolved_logic_full':short(current.get('xml_logic_full',final_row.get('xml_logic_full','')),3000),'resolved_step':current.get('step_no',''),'resolved_task':current.get('task_no',''),'resolution_depth':str(max(0,len(path)-1)),'lineage_path':'\n'.join(path)}

def resolve_selected(final_lineage, all_lineage, selected_cols):
    fmap={ident(r.get('target_column','')):r for r in final_lineage if ident(r.get('target_column',''))}
    idx=build_stage_index(all_lineage)
    out={}
    for c in sorted(selected_cols):
        if c in fmap: out[c]=resolve_one(c, fmap[c], idx)
    return out

def xml_delta(orig_res, fixed_res, selected_cols):
    rows=[]
    for c in sorted(selected_cols):
        o=orig_res.get(c); f=fixed_res.get(c)
        if o and not f: st='REMOVED_IN_FIXED'
        elif f and not o: st='ADDED_IN_FIXED'
        elif not o and not f: st='NO_RESOLVED_LINEAGE'
        else: st='CHANGED' if canonical(o.get('resolved_expression',''))!=canonical(f.get('resolved_expression','')) else 'UNCHANGED'
        rows.append({'target_column':c,'xml_delta_status':st,'original_final_expression':o.get('final_expression','') if o else '','fixed_final_expression':f.get('final_expression','') if f else '','original_resolved_expression':o.get('resolved_expression','') if o else '','fixed_resolved_expression':f.get('resolved_expression','') if f else '','original_resolution_depth':o.get('resolution_depth','') if o else '','fixed_resolution_depth':f.get('resolution_depth','') if f else '','original_lineage_path':o.get('lineage_path','') if o else '','fixed_lineage_path':f.get('lineage_path','') if f else ''})
    return rows

def delta_report(orig_v15, fixed_v15, xdelta):
    xd={r['target_column']:r for r in xdelta}; cols=sorted(set(orig_v15)|set(fixed_v15)|set(xd))
    closed={'IN_BOTH_NO_REVIEW','MATCH_EQUIVALENT'}; open_={'REVIEW_REQUIRED','MISSING_IN_ODI'}
    rows=[]
    for c in cols:
        o=orig_v15.get(c, {'v15_class':'NO_FINAL_OR_DRD'}); f=fixed_v15.get(c, {'v15_class':'NO_FINAL_OR_DRD'})
        os=o.get('v15_class',''); fs=f.get('v15_class',''); xmlst=xd.get(c,{}).get('xml_delta_status','')
        if os in open_ and fs in closed: ds='FIXED_BY_FINAL_COMPARE'
        elif os in open_ and fs in open_ and xmlst=='CHANGED': ds='FIX_CANDIDATE_UPSTREAM_CHANGED'
        elif os in open_ and fs in open_: ds='STILL_OPEN'
        elif os in closed and fs in open_: ds='NEW_REGRESSION_BY_FINAL_COMPARE'
        elif xmlst=='CHANGED': ds='UPSTREAM_CHANGED_NO_FINAL_MISMATCH'
        else: ds='UNCHANGED'
        rows.append({'target_column':c,'delta_status':ds,'original_v15_class':os,'fixed_v15_class':fs,'original_reason':o.get('v15_reason',''),'fixed_reason':f.get('v15_reason',''),'original_difference_type':o.get('difference_type',''),'fixed_difference_type':f.get('difference_type',''),'xml_delta_status':xmlst,'original_resolved_expression':xd.get(c,{}).get('original_resolved_expression',''),'fixed_resolved_expression':xd.get(c,{}).get('fixed_resolved_expression',''),'original_lineage_path':xd.get(c,{}).get('original_lineage_path',''),'fixed_lineage_path':xd.get(c,{}).get('fixed_lineage_path','')})
    return rows

def sql_block_diff(orig_blocks, fixed_blocks):
    def key(b): return (b.get('step_no',''),b.get('task_no',''),b.get('task_name',''))
    ob={key(b):b for b in orig_blocks}; fb={key(b):b for b in fixed_blocks}; rows=[]
    for k in sorted(set(ob)|set(fb)):
        o=ob.get(k,{}); f=fb.get(k,{})
        if o and not f: st='REMOVED_IN_FIXED'
        elif f and not o: st='ADDED_IN_FIXED'
        else: st='CHANGED' if canonical(o.get('sql',''))!=canonical(f.get('sql','')) else 'UNCHANGED'
        rows.append({'step_no':k[0],'task_no':k[1],'task_name':k[2],'sql_delta_status':st,'original_sql_excerpt':short(o.get('sql',''),2000),'fixed_sql_excerpt':short(f.get('sql',''),2000)})
    return rows

# Generic rule proof helpers copied from v16.6 postprocessor
def identifiers(text: str) -> List[str]:
    toks=[]
    stop={"CASE","WHEN","THEN","ELSE","END","FROM","JOIN","LEFT","RIGHT","INNER","OUTER","SELECT","WHERE","AND","OR","ON","AS","NULL","IS","NOT","IN","THE","FOR","USE","TO","WITH","VALUE","VALUES","LOOKUP","PARSE","EXTRACT","SOURCE","TARGET"}
    for t in re.findall(r'[A-Za-z_][A-Za-z0-9_#$]*', text or ''):
        u=ident(t)
        if not u or u in stop: continue
        toks.append(u)
    expanded=[]
    for t in toks:
        expanded.append(t)
        for part in t.split('_'):
            if len(part)>=2 and part not in {"ID","CD","NM","TP","DT","YR"}: expanded.append(part)
    out=[]; seen=set()
    for t in expanded:
        if len(t)<2 or t in seen: continue
        seen.add(t); out.append(t)
    return out

def snippets(xml: str, terms: Iterable[str], radius:int=3500, max_parts:int=12) -> str:
    parts=[]; seen=set()
    for term in sorted(set(t for t in terms if t), key=len, reverse=True):
        if len(term)<2: continue
        for m in re.finditer(re.escape(term), xml, flags=re.I):
            s=max(0,m.start()-radius); e=min(len(xml),m.end()+radius); key=(s,e)
            if key not in seen: parts.append(xml[s:e]); seen.add(key)
            if len(parts)>=max_parts: return '\n'.join(parts)
    return '\n'.join(parts)

def source_attr(drdrow): return ident(drdrow.get('source_3',''))
def source_table(drdrow): return ident(drdrow.get('source_2',''))
def target_suffix(col): return ident(col).split('_')[-1] if ident(col) else ''
def drd_text(col, drdrow): return ' '.join([col or '', drdrow.get('source_1','') or '', drdrow.get('source_2','') or '', drdrow.get('source_3','') or '', drdrow.get('drd_rule','') or ''])
def guards(text):
    out=[]
    for name,val in re.findall(r'\b([A-Za-z_][A-Za-z0-9_#$]*(?:\.[A-Za-z_][A-Za-z0-9_#$]*)?)\s*=\s*(\d+)\b', text or ''):
        nm=ident(name.split('.')[-1])
        if nm and val: out.append((nm,val))
    return list(dict.fromkeys(out))

def checks_from_drd(col, drdrow):
    raw=drd_text(col,drdrow); text=norm(raw); attr=source_attr(drdrow); tbl=source_table(drdrow); suffix=target_suffix(col); checks=[]
    for g,v in guards(raw): checks.append({'type':'source_guard','identifier':g,'value':v})
    if re.search(r'\b(PARSE|EXTRACT|SPLIT|SUBSTR|SUBSTRING|LAST\s+TWO|DIGITS?)\b', text, re.I): checks.append({'type':'parse_logic','identifier':attr})
    if re.search(r'\b(LOOKUP|LOOK\s+UP|JOIN|DIMENSION)\b', text, re.I): checks.append({'type':'lookup_logic','source_table':tbl,'source_attr':attr})
    if suffix in {'YR','YEAR'}: checks.append({'type':'year_logic','identifier':attr})
    if suffix in {'DT','DATE'} and attr: checks.append({'type':'source_attr_present','identifier':attr})
    if re.search(r'\b(CURRENCY|CCY)\b', text, re.I) and attr: checks.append({'type':'source_attr_present','identifier':attr})
    if suffix in {'CD','CODE','NM','NAME'} and attr and any(c['type']=='lookup_logic' for c in checks): checks.append({'type':'source_attr_present','identifier':attr})
    out=[]; seen=set()
    for c in checks:
        k=tuple(sorted(c.items()))
        if k not in seen: seen.add(k); out.append(c)
    return out

def has_id(ev, token):
    token=ident(token)
    return bool(token and re.search(r'(?<![A-Z0-9_#$])'+re.escape(token)+r'(?![A-Z0-9_#$])', ev, flags=re.I))

def eval_check(c, broad_evidence, target_evidence):
    typ=c['type']
    # Guard and parse/year checks must be local to the target field. Otherwise unrelated
    # CASE logic in the same XML can create false positives.
    local=norm(target_evidence)
    broad=norm(broad_evidence)
    if typ=='source_guard':
        g=c.get('identifier',''); v=c.get('value','')
        direct_eq = re.search(re.escape(g)+r'\s*=\s*'+re.escape(v)+r'\b', local) is not None
        direct_in = re.search(re.escape(g)+r'\s+IN\s*\([^)]*\b'+re.escape(v)+r'\b[^)]*\)', local) is not None
        ok=has_id(local,g) and (direct_eq or direct_in) and 'CASE' in local
        return ok, f'requires target-local CASE guard {g}={v}'
    if typ=='parse_logic':
        return any(fn in local for fn in ['SUBSTR','SUBSTRING','REGEXP','INSTR']), 'requires target-local parse function such as SUBSTR/REGEXP/INSTR'
    if typ=='lookup_logic':
        tbl=c.get('source_table',''); attr=c.get('source_attr',''); ok=((attr and has_id(broad,attr)) or (tbl and has_id(broad,tbl))) and any(tok in broad for tok in ['JOIN','SELECT','LOOKUP','CL_','DIM'])
        return ok, f'requires lookup/join evidence from DRD source table/attribute {tbl}.{attr}'
    if typ=='year_logic':
        return any(fn in local for fn in ['SUBSTR','SUBSTRING','REGEXP']) and any(tok in local for tok in ['TO_NUMBER','YEAR',"'20'","'19'",'20||','19||']), 'requires target-local parse + year/century/numeric construction evidence'
    if typ=='source_attr_present':
        attr=c.get('identifier',''); return bool(attr) and has_id(broad,attr), f'requires source/output attribute {attr}'
    return False, f'unknown check {typ}'

def prove(col, drdrow, xml_text, resolved_expr, lineage_path, use_xml_snippets=True):
    checks=checks_from_drd(col,drdrow)
    if not checks: return False, [], '', []
    # Evidence must be local to the current target/resolved lineage.
    # Do not use broad DRD-source terms as XML search boosters: they can find unrelated logic.
    derived_terms=identifiers(drd_text(col,drdrow)+' '+(resolved_expr or '')+' '+(lineage_path or ''))
    resolved_terms=[t for t in identifiers((resolved_expr or '')+' '+(lineage_path or '')) if t not in {'INLINE','VIEW','STEP','TASK','COL','EXPR'}]
    target_xml=snippets(xml_text, [col], radius=80000, max_parts=30) if use_xml_snippets else ''
    broad_xml='\n'.join([target_xml, snippets(xml_text, resolved_terms[:25], radius=8000, max_parts=10) if use_xml_snippets else ''])
    terms=derived_terms
    target_evidence=norm('\n'.join([resolved_expr or '', lineage_path or '', target_xml]))
    broad_evidence=norm('\n'.join([resolved_expr or '', lineage_path or '', broad_xml]))
    rows=[]; passed=True
    for c in checks:
        ok, detail=eval_check(c, broad_evidence, target_evidence); rows.append({'type':c.get('type'),'detail':detail,'passed':ok,'derived_check':c}); passed=passed and ok
    return passed, rows, broad_evidence[:4000], terms

def apply_rule_proof(delta, drdmap, original_xml, fixed_xml):
    new=[]; proof=[]
    for r in delta:
        nr=dict(r); col=r.get('target_column',''); dr=drdmap.get(col)
        if r.get('delta_status')=='FIX_CANDIDATE_UPSTREAM_CHANGED' and dr:
            op, oc, oe, ot = prove(col, dr, original_xml, r.get('original_resolved_expression',''), r.get('original_lineage_path',''), use_xml_snippets=False)
            fp, fc, fe, ft = prove(col, dr, fixed_xml, r.get('fixed_resolved_expression',''), r.get('fixed_lineage_path',''), use_xml_snippets=True)
            if fp and not op:
                nr['delta_status']='FIXED_BY_RESOLVED_RULE_PROOF'; nr['fixed_reason']='Fixed resolved ODI lineage satisfies DRD-derived generic proof checks; original did not.'
            proof.append({'target_column':col,'original_proof_passed':'Y' if op else 'N','fixed_proof_passed':'Y' if fp else 'N','original_checks':json.dumps(oc,ensure_ascii=False),'fixed_checks':json.dumps(fc,ensure_ascii=False),'original_terms_derived_from_input':' | '.join(ot[:80]),'fixed_terms_derived_from_input':' | '.join(ft[:80]),'original_evidence_excerpt':oe,'fixed_evidence_excerpt':fe})
        new.append(nr)
    return new, proof

def run(args):
    out=Path(args.out).expanduser().resolve(); out.mkdir(parents=True, exist_ok=True)
    xlsx=Path(args.xlsx).expanduser().resolve(); ox=Path(args.original_xml).expanduser().resolve(); fx=Path(args.fixed_xml).expanduser().resolve()
    ot, ob, ol, of=load_odi(ox); ft, fb, fl, ff=load_odi(fx)
    detection, mapping_rows, notes=load_drd(xlsx, list(dict.fromkeys(ft+ot)), args)
    profile=infer_profile(detection,args.profile)
    orig_v15, orig_col_diff, orig_mism, orig_eq=v15_compare(mapping_rows, of, ol, ob, detection, profile)
    fixed_v15, fixed_col_diff, fixed_mism, fixed_eq=v15_compare(mapping_rows, ff, fl, fb, detection, profile)
    open_={'REVIEW_REQUIRED','MISSING_IN_ODI'}
    selected=set()
    for c in set(orig_v15)|set(fixed_v15):
        oc=orig_v15.get(c,{}).get('v15_class',''); fc=fixed_v15.get(c,{}).get('v15_class','')
        if oc in open_ or fc in open_ or oc!=fc: selected.add(c)
    orig_res=resolve_selected(of, ol, selected); fixed_res=resolve_selected(ff, fl, selected)
    xd=xml_delta(orig_res, fixed_res, selected); delta=delta_report(orig_v15, fixed_v15, xd)
    drdmap={r['target_column']:r for r in mapping_rows}
    delta2, proof=apply_rule_proof(delta, drdmap, ox.read_text(errors='ignore'), fx.read_text(errors='ignore'))
    sdiff=sql_block_diff(ob, fb)
    write_csv(out/'original_vs_fixed_resolved_xml_delta.csv', xd, ['target_column','xml_delta_status','original_final_expression','fixed_final_expression','original_resolved_expression','fixed_resolved_expression','original_resolution_depth','fixed_resolution_depth','original_lineage_path','fixed_lineage_path'])
    v15_fields=['target_column','v15_class','v15_reason','difference_type','area']
    write_csv(out/'drd_vs_original_v15_classification.csv', list(orig_v15.values()), v15_fields)
    write_csv(out/'drd_vs_fixed_v15_classification.csv', list(fixed_v15.values()), v15_fields)
    fields=['target_column','delta_status','original_v15_class','fixed_v15_class','original_reason','fixed_reason','original_difference_type','fixed_difference_type','xml_delta_status','original_resolved_expression','fixed_resolved_expression','original_lineage_path','fixed_lineage_path']
    write_csv(out/'delta_report_v16_6_generic_rule_proof.csv', delta2, fields)
    write_csv(out/'delta_report_fixed_still_open_regression.csv', delta, fields)
    write_csv(out/'resolved_rule_proof_checks_v16_6.csv', proof, ['target_column','original_proof_passed','fixed_proof_passed','original_checks','fixed_checks','original_terms_derived_from_input','fixed_terms_derived_from_input','original_evidence_excerpt','fixed_evidence_excerpt'])
    write_csv(out/'sql_block_differences.csv', sdiff, ['step_no','task_no','task_name','sql_delta_status','original_sql_excerpt','fixed_sql_excerpt'])
    write_csv(out/'original_v15_mismatch_rows.csv', orig_mism, sorted(set().union(*(r.keys() for r in orig_mism))) if orig_mism else ['empty'])
    write_csv(out/'fixed_v15_mismatch_rows.csv', fixed_mism, sorted(set().union(*(r.keys() for r in fixed_mism))) if fixed_mism else ['empty'])
    summary={'version':__VERSION__,'profile':profile,'mapping_rows':len(mapping_rows),'selected_resolved_columns':len(selected),'original_final_columns':len(of),'fixed_final_columns':len(ff),'original_v15_class_counts':dict(Counter(r['v15_class'] for r in orig_v15.values())),'fixed_v15_class_counts':dict(Counter(r['v15_class'] for r in fixed_v15.values())),'xml_column_delta_counts_selected':dict(Counter(r['xml_delta_status'] for r in xd)),'sql_block_delta_counts':dict(Counter(r['sql_delta_status'] for r in sdiff)),'delta_status_counts_v16_6':dict(Counter(r['delta_status'] for r in delta2)),'fixed_by_resolved_rule_proof_v16_6':[r['target_column'] for r in delta2 if r['delta_status']=='FIXED_BY_RESOLVED_RULE_PROOF'],'fix_candidate_upstream_changed_v16_6':[r['target_column'] for r in delta2 if r['delta_status']=='FIX_CANDIDATE_UPSTREAM_CHANGED'],'still_open_v16_6':[r['target_column'] for r in delta2 if r['delta_status']=='STILL_OPEN'],'new_regression_v16_6':[r['target_column'] for r in delta2 if 'REGRESSION' in r['delta_status']]}
    (out/'summary_v16_6_generic_rule_proof.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (out/'detected_layout.json').write_text(json.dumps(detection.as_human(),indent=2),encoding='utf-8')
    (out/'README.md').write_text('# v16.6 Fast Generic Rule Proof Report\n\n```json\n'+json.dumps(summary,indent=2)+'\n```\n',encoding='utf-8')
    if not args.quiet: print(json.dumps(summary,indent=2))
    return out

def parser():
    p=argparse.ArgumentParser(description='v16.6 fast generic DRD/ODI delta comparator')
    p.add_argument('--xlsx',required=True); p.add_argument('--original-xml',required=True); p.add_argument('--fixed-xml',required=True); p.add_argument('--out',required=True)
    p.add_argument('--profile',default='auto',choices=['auto','generic','avy','taxlot'])
    p.add_argument('--target-table',default=''); p.add_argument('--mapping-sheet',default=''); p.add_argument('--target-col',default=''); p.add_argument('--source-cols',default=''); p.add_argument('--rule-col',default=''); p.add_argument('--header-row',type=int,default=None)
    p.add_argument('--quiet',action='store_true')
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    try: run(args); return 0
    except Exception as e: print('ERROR:',e,file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
