#!/usr/bin/env python3
"""
compare_drd_odi_v16_2_delta_safe.py

Generic, regression-safe DRD/ODI delta comparator.

Core idea:
- Use proven v15 comparison logic as the source of truth for mismatch sets.
- Add multi-step resolved ODI lineage diff as an overlay to detect upstream fixes
  that final S.COL comparison cannot see.

This avoids false regressions from speculative generic heuristics and keeps TaxLot
results stable while still surfacing AVY upstream changes.
"""
from __future__ import annotations

import argparse, csv, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from openpyxl import load_workbook
import compare_drd_odi_universal as base

__VERSION__ = '16.4'

def clean(v): return base.clean_text(v)
def norm(v): return base.normalize_space(v)
def ident(v): return base.normalize_identifier(v)

def write_csv(path: Path, rows: Sequence[Dict[str,str]], fields: Sequence[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f, fieldnames=list(fields), extrasaction='ignore')
        w.writeheader()
        for r in rows: w.writerow({k:r.get(k,'') for k in fields})

def canonical(expr: str, limit:int=12000) -> str:
    e=clean(expr)
    if len(e)>limit: e=e[:limit]
    e=re.sub(r'/\*.*?\*/',' ',e,flags=re.S)
    e=re.sub(r'--.*?$',' ',e,flags=re.M)
    e=re.sub(r'\s+',' ',e).strip().upper()
    e=re.sub(r'\s+AS\s+[A-Z_][A-Z0-9_#$]*$','',e)
    return e

def short(expr:str, n:int=1600)->str:
    e=norm(expr)
    return e if len(e)<=n else e[:n-3]+'...'

def is_passthrough(expr: str):
    e=norm(expr).strip('() ')
    m=re.match(r'^([A-Za-z_][A-Za-z0-9_#$]*)\.([A-Za-z_][A-Za-z0-9_#$]*)(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_#$]*)?$', e, flags=re.I)
    if m: return ident(m.group(1)), ident(m.group(2))
    return None

def build_stage_index(lineage):
    idx=defaultdict(list)
    for r in lineage:
        c=ident(r.get('target_column',''))
        if c: idx[c].append(r)
    return idx

def resolve_one(col, final_row, lineage, stage_idx, max_depth=8):
    current=final_row; path=[]; visited=set()
    for _ in range(max_depth):
        expr=clean(current.get('expression',''))
        step=current.get('step_no',''); task=current.get('task_no','')
        key=(step,task,ident(current.get('target_column',col)),expr[:300].upper())
        if key in visited: break
        visited.add(key)
        path.append(f"step={step}/task={task}/col={ident(current.get('target_column',col))}/expr={short(expr,180)}")
        pt=is_passthrough(expr)
        if not pt: break
        _, src_col = pt
        best=None; best_score=-1; expr_u=expr.upper().strip()
        for r in stage_idx.get(src_col,[]):
            if r is current: continue
            rexpr=clean(r.get('expression',''))
            if not rexpr or rexpr.upper().strip()==expr_u: continue
            score=0
            ru=rexpr.upper()
            if 'CASE' in ru: score+=50
            if not is_passthrough(rexpr): score+=25
            try: score += max(0,1000-int(r.get('step_no','999')))/1000
            except Exception: pass
            if score>best_score:
                best_score=score; best=r
        if best is None: break
        current=best
    return {
        'target_column': ident(col),
        'final_expression': final_row.get('expression',''),
        'resolved_expression': clean(current.get('expression', final_row.get('expression',''))),
        'resolved_logic_full': short(current.get('xml_logic_full', final_row.get('xml_logic_full','')), 3000),
        'resolved_step': current.get('step_no',''),
        'resolved_task': current.get('task_no',''),
        'resolution_depth': str(max(0,len(path)-1)),
        'lineage_path': '\n'.join(path),
    }

def resolve_final(final_lineage, all_lineage):
    idx=build_stage_index(all_lineage)
    out={}
    for fr in final_lineage:
        c=ident(fr.get('target_column',''))
        if c: out[c]=resolve_one(c, fr, all_lineage, idx)
    return out

def load_odi(xml: Path):
    objects=base.parse_odi_objects(xml)
    targets=base.extract_target_resources_from_xml(objects)
    _,_,blocks=base.extract_odi_summary(objects)
    lineage=base.build_odi_lineage(blocks)
    final=base.select_final_target_lineage(lineage)
    return targets, blocks, lineage, final

def load_drd(xlsx: Path, xml_targets: List[str], args):
    wb=load_workbook(xlsx, read_only=True, data_only=True)
    detection=base.auto_detect_mapping(
        wb, xml_targets=xml_targets,
        target_table_override=args.target_table or '',
        mapping_sheet_override=args.mapping_sheet or '',
        header_row_override=args.header_row,
        target_col_override=args.target_col or '',
        source_cols_override=args.source_cols or '',
        rule_col_override=args.rule_col or '',
    )
    rows, notes=base.extract_mapping_from_xlsx(xlsx, detection)
    return detection, rows, notes

def area_cols(area: str):
    cols=re.findall(r'`([^`]+)`', area or '')
    if not cols and re.fullmatch(r'[A-Z0-9_#$]+', area or ''): cols=[area]
    return [ident(c) for c in cols if ident(c)]

def v15_compare(mapping_rows, final_lineage, all_lineage, sql_blocks, detection, profile):
    column_diff=base.compare_columns(mapping_rows, final_lineage)
    # AVY uses curated v15 review rows; building all generic logic candidates is expensive
    # and does not improve the curated AVY mismatch set. TaxLot/generic still use it.
    logic_rows=[] if profile=='avy' else base.build_logic_diff_candidates(mapping_rows, all_lineage, sql_blocks)
    raw=[]; used_curated=False
    if profile=='avy':
        raw=base.build_avy_review_rules_diff(column_diff, logic_rows, detection)
        used_curated=bool(raw)
    if not raw:
        raw=base.build_full_drd_vs_odi_xml_rules_diff(column_diff, logic_rows)
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
            if c in by:
                by[c].update({'v15_class':'REVIEW_REQUIRED','v15_reason':r.get('Conclusion','') or r.get('Difference Type',''),'difference_type':r.get('Difference Type',''),'area':r.get('Area / Columns','')})
    for r in equivalent:
        for c in area_cols(r.get('Area / Columns','')):
            if c in by:
                by[c].update({'v15_class':'MATCH_EQUIVALENT','v15_reason':r.get('Conclusion','MATCH_EQUIVALENT'),'difference_type':'MATCH_EQUIVALENT','area':r.get('Area / Columns','')})
    return by, column_diff, mismatches, equivalent

def infer_profile(detection, requested):
    if requested!='auto': return requested
    blob=(detection.target_table_from_sheet+' '+' '.join(detection.target_resources_from_xml)).upper()
    if 'AVY_FACT' in blob: return 'avy'
    if 'TAX_LOT' in blob or 'TAXLOTS' in blob: return 'taxlot'
    return 'generic'

def xml_delta(orig_res, fixed_res):
    rows=[]
    for c in sorted(set(orig_res)|set(fixed_res)):
        o=orig_res.get(c); f=fixed_res.get(c)
        if o and not f: st='REMOVED_IN_FIXED'
        elif f and not o: st='ADDED_IN_FIXED'
        else: st='CHANGED' if canonical(o.get('resolved_expression',''))!=canonical(f.get('resolved_expression','')) else 'UNCHANGED'
        rows.append({
            'target_column':c,'xml_delta_status':st,
            'original_final_expression':o.get('final_expression','') if o else '',
            'fixed_final_expression':f.get('final_expression','') if f else '',
            'original_resolved_expression':o.get('resolved_expression','') if o else '',
            'fixed_resolved_expression':f.get('resolved_expression','') if f else '',
            'original_resolution_depth':o.get('resolution_depth','') if o else '',
            'fixed_resolution_depth':f.get('resolution_depth','') if f else '',
            'original_lineage_path':o.get('lineage_path','') if o else '',
            'fixed_lineage_path':f.get('lineage_path','') if f else '',
        })
    return rows

def delta_report(orig_v15, fixed_v15, xdelta):
    xd={r['target_column']:r for r in xdelta}
    cols=sorted(set(orig_v15)|set(fixed_v15)|set(xd))
    closed={'IN_BOTH_NO_REVIEW','MATCH_EQUIVALENT'}; open_={'REVIEW_REQUIRED','MISSING_IN_ODI'}
    rows=[]
    for c in cols:
        o=orig_v15.get(c, {'v15_class':'NO_FINAL_OR_DRD'}); f=fixed_v15.get(c, {'v15_class':'NO_FINAL_OR_DRD'})
        os=o.get('v15_class',''); fs=f.get('v15_class','')
        xmlst=xd.get(c,{}).get('xml_delta_status','')
        if os in open_ and fs in closed: ds='FIXED_BY_FINAL_COMPARE'
        elif os in open_ and fs in open_ and xmlst=='CHANGED': ds='FIX_CANDIDATE_UPSTREAM_CHANGED'
        elif os in open_ and fs in open_: ds='STILL_OPEN'
        elif os in closed and fs in open_: ds='NEW_REGRESSION_BY_FINAL_COMPARE'
        elif xmlst=='CHANGED': ds='UPSTREAM_CHANGED_NO_FINAL_MISMATCH'
        else: ds='UNCHANGED'
        rows.append({
            'target_column':c,'delta_status':ds,
            'original_v15_class':os,'fixed_v15_class':fs,
            'original_reason':o.get('v15_reason',''),'fixed_reason':f.get('v15_reason',''),
            'original_difference_type':o.get('difference_type',''),'fixed_difference_type':f.get('difference_type',''),
            'xml_delta_status':xmlst,
            'original_resolved_expression':xd.get(c,{}).get('original_resolved_expression',''),
            'fixed_resolved_expression':xd.get(c,{}).get('fixed_resolved_expression',''),
            'original_lineage_path':xd.get(c,{}).get('original_lineage_path',''),
            'fixed_lineage_path':xd.get(c,{}).get('fixed_lineage_path',''),
        })
    return rows

def sql_block_diff(orig_blocks, fixed_blocks):
    def key(b): return (b.get('step_no',''),b.get('task_no',''),b.get('task_name',''))
    ob={key(b):b for b in orig_blocks}; fb={key(b):b for b in fixed_blocks}
    rows=[]
    for k in sorted(set(ob)|set(fb)):
        o=ob.get(k,{}); f=fb.get(k,{})
        if o and not f: st='REMOVED_IN_FIXED'
        elif f and not o: st='ADDED_IN_FIXED'
        else: st='CHANGED' if canonical(o.get('sql',''))!=canonical(f.get('sql','')) else 'UNCHANGED'
        rows.append({'step_no':k[0],'task_no':k[1],'task_name':k[2],'sql_delta_status':st,'original_sql_excerpt':short(o.get('sql',''),2000),'fixed_sql_excerpt':short(f.get('sql',''),2000)})
    return rows

def run(args):
    out=Path(args.out).expanduser().resolve(); out.mkdir(parents=True, exist_ok=True)
    xlsx=Path(args.xlsx).expanduser().resolve(); ox=Path(args.original_xml).expanduser().resolve(); fx=Path(args.fixed_xml).expanduser().resolve()
    ot, ob, ol, of=load_odi(ox)
    ft, fb, fl, ff=load_odi(fx)
    detection, mapping_rows, notes=load_drd(xlsx, list(dict.fromkeys(ft+ot)), args)
    profile=infer_profile(detection,args.profile)
    orig_v15, orig_col_diff, orig_mism, orig_eq = v15_compare(mapping_rows, of, ol, ob, detection, profile)
    fixed_v15, fixed_col_diff, fixed_mism, fixed_eq = v15_compare(mapping_rows, ff, fl, fb, detection, profile)
    # Resolve after v15 comparison. Resolve both independently and keep it regex-light.
    fixed_res=resolve_final(ff, fl)
    orig_res=resolve_final(of, ol)
    xd=xml_delta(orig_res, fixed_res)
    delta=delta_report(orig_v15, fixed_v15, xd)
    sdiff=sql_block_diff(ob, fb)
    # outputs
    write_csv(out/'original_vs_fixed_resolved_xml_delta.csv', xd, ['target_column','xml_delta_status','original_final_expression','fixed_final_expression','original_resolved_expression','fixed_resolved_expression','original_resolution_depth','fixed_resolution_depth','original_lineage_path','fixed_lineage_path'])
    v15_fields=['target_column','v15_class','v15_reason','difference_type','area']
    write_csv(out/'drd_vs_original_v15_classification.csv', list(orig_v15.values()), v15_fields)
    write_csv(out/'drd_vs_fixed_v15_classification.csv', list(fixed_v15.values()), v15_fields)
    write_csv(out/'delta_report_fixed_still_open_regression.csv', delta, ['target_column','delta_status','original_v15_class','fixed_v15_class','original_reason','fixed_reason','original_difference_type','fixed_difference_type','xml_delta_status','original_resolved_expression','fixed_resolved_expression','original_lineage_path','fixed_lineage_path'])
    write_csv(out/'sql_block_differences.csv', sdiff, ['step_no','task_no','task_name','sql_delta_status','original_sql_excerpt','fixed_sql_excerpt'])
    # keep raw v15 mismatch rows
    write_csv(out/'original_v15_mismatch_rows.csv', orig_mism, sorted(set().union(*(r.keys() for r in orig_mism))) if orig_mism else ['empty'])
    write_csv(out/'fixed_v15_mismatch_rows.csv', fixed_mism, sorted(set().union(*(r.keys() for r in fixed_mism))) if fixed_mism else ['empty'])
    summary={
        'version':__VERSION__,'profile':profile,'mapping_rows':len(mapping_rows),
        'original_final_columns':len(of),'fixed_final_columns':len(ff),
        'original_v15_class_counts':dict(Counter(r['v15_class'] for r in orig_v15.values())),
        'fixed_v15_class_counts':dict(Counter(r['v15_class'] for r in fixed_v15.values())),
        'xml_column_delta_counts':dict(Counter(r['xml_delta_status'] for r in xd)),
        'sql_block_delta_counts':dict(Counter(r['sql_delta_status'] for r in sdiff)),
        'delta_status_counts':dict(Counter(r['delta_status'] for r in delta)),
        'fixed_by_final_compare':[r['target_column'] for r in delta if r['delta_status']=='FIXED_BY_FINAL_COMPARE'],
        'fix_candidate_upstream_changed':[r['target_column'] for r in delta if r['delta_status']=='FIX_CANDIDATE_UPSTREAM_CHANGED'],
        'new_regression_by_final_compare':[r['target_column'] for r in delta if r['delta_status']=='NEW_REGRESSION_BY_FINAL_COMPARE'],
        'still_open':[r['target_column'] for r in delta if r['delta_status']=='STILL_OPEN'],
    }
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (out/'detected_layout.json').write_text(json.dumps(detection.as_human(),indent=2),encoding='utf-8')
    (out/'README.md').write_text('# v16.2 Delta Safe Report\n\n```json\n'+json.dumps(summary,indent=2)+'\n```\n',encoding='utf-8')
    if not args.quiet: print(json.dumps(summary,indent=2))
    return out

def parser():
    p=argparse.ArgumentParser(description='Generic v16.2 safe DRD/ODI multi-step delta comparator')
    p.add_argument('--xlsx',required=True); p.add_argument('--original-xml',required=True); p.add_argument('--fixed-xml',required=True); p.add_argument('--out',default='v16_2_delta_output')
    p.add_argument('--profile',default='auto',choices=['auto','generic','avy','taxlot'])
    p.add_argument('--target-table',default=''); p.add_argument('--mapping-sheet',default=''); p.add_argument('--target-col',default=''); p.add_argument('--source-cols',default=''); p.add_argument('--rule-col',default=''); p.add_argument('--header-row',type=int,default=None)
    p.add_argument('--quiet',action='store_true')
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        run(args); return 0
    except Exception as e:
        print('ERROR:',e,file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
