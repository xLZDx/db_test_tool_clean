import json, re, csv, shutil, os, difflib
from pathlib import Path
from collections import Counter, defaultdict

SQL_FUNCS={"TO_DATE","TO_CHAR","CAST","NVL","COALESCE","DECODE","SUBSTR","TRIM","ROUND","REGEXP_REPLACE","REGEXP_SUBSTR","UPPER","LOWER","CASE","NULLIF","TO_NUMBER","INSTR","REGEXP_LIKE","EXISTS"}
KEYWORDS={"CASE","WHEN","THEN","ELSE","END","NULL","IS","NOT","AND","OR","IN","LIKE","BETWEEN","EXISTS","SELECT","FROM","WHERE","AS","DISTINCT","TRUE","FALSE","ON","JOIN","LEFT","RIGHT","INNER","OUTER"}|SQL_FUNCS

def norm(x): return (x or '').strip().strip('\"').upper()


def load_resolution_profile(path=''):
    if not path:
        return {}
    return json.load(open(path, encoding='utf-8'))


def norm2(x):
    return re.sub(r'[^A-Z0-9]', '', norm(x))


def generic_owner_variants(owner):
    owner = norm(owner)
    out = [owner]
    if owner.endswith('_REPL_OWNER'):
        out.append(owner.replace('_REPL_OWNER', '_OWNER'))
    # generic plural-to-singular owner family for legacy plural schema names.
    m = re.match(r'(.+?)S(_OWNER)$', owner)
    if m:
        out.append(m.group(1) + m.group(2))
    return list(dict.fromkeys(out))


def generic_column_candidates(col, cols):
    cu = norm(col)
    if cu in cols:
        return [cu]
    token_map = {
        'CD': 'CODE', 'NM': 'NAME', 'DSC': 'DESCRIPTION', 'DESC': 'DESCRIPTION',
        'DT': 'DATE', 'EFF': 'EFFECTIVE', 'FM': 'FROM', 'TO': 'TO',
        'ID': 'ID', 'TP': 'TYPE', 'CCY': 'CURRENCY', 'AMT': 'AMOUNT',
    }
    parts = cu.split('_')
    expanded = '_'.join(token_map.get(p, p) for p in parts)
    variants = [expanded, expanded.replace('_DESCRIPTION', '_DSC'), expanded.replace('_TYPE', '_TP')]
    hits = [v for v in variants if v in cols]
    if hits:
        return hits
    # similarity fallback only if a single strong match exists.
    candidates = []
    ncu = norm2(cu)
    for c in cols:
        nc = norm2(c)
        ratio = difflib.SequenceMatcher(None, ncu, nc).ratio()
        # require same first token or clear suffix relation to avoid unsafe mapping.
        if ratio >= 0.86 or (cu.endswith('_CD') and c.endswith('_CODE') and ncu[:-2] == nc[:-4]):
            candidates.append((ratio, c))
    candidates.sort(reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.04):
        return [candidates[0][1]]
    return []

def load_kb(path):
    data=json.load(open(path,encoding='utf-8'))
    objects={}; table_owners=defaultdict(list); owners=set()
    for s in data['pdm']['schemas']:
        for t in s.get('tables',[]):
            owner=norm(t.get('schema') or s.get('schema'))
            name=norm(t.get('name'))
            cols={norm(c['name']) for c in t.get('columns',[])}
            objects[(owner,name)]={'cols':cols,'type':t.get('type','')}
            table_owners[name].append(owner)
            owners.add(owner)
    return objects,table_owners,owners,data

def strip_comments(sql):
    sql=re.sub(r"/\*.*?\*/"," ",sql,flags=re.S)
    sql=re.sub(r"--[^\n]*"," ",sql)
    return sql

def split_top_commas(text):
    parts=[]; cur=[]; depth=0; ins=False; ind=False; i=0
    while i<len(text):
        ch=text[i]
        if ch=="'" and not ind:
            if i+1<len(text) and text[i+1]=="'": cur.extend([ch,text[i+1]]); i+=2; continue
            ins=not ins
        elif ch=='"' and not ins:
            ind=not ind
        elif not ins and not ind:
            if ch=='(': depth+=1
            elif ch==')': depth-=1
            elif ch==',' and depth==0:
                parts.append(''.join(cur).strip()); cur=[]; i+=1; continue
        cur.append(ch); i+=1
    if cur: parts.append(''.join(cur).strip())
    return parts

def find_matching(s,pos):
    depth=0; ins=False; ind=False
    for i in range(pos,len(s)):
        ch=s[i]
        if ch=="'" and not ind:
            if i+1<len(s) and s[i+1]=="'": continue
            ins=not ins
        elif ch=='"' and not ins:
            ind=not ind
        elif not ins and not ind:
            if ch=='(': depth+=1
            elif ch==')':
                depth-=1
                if depth==0: return i
    return -1

def parse_insert_select(sql):
    m=re.search(r"\bINSERT\s+INTO\s+([A-Z0-9_$#]+)\.([A-Z0-9_$#]+)\s*\(",sql,re.I)
    if not m: raise ValueError('no INSERT INTO schema.table')
    target=(norm(m.group(1)),norm(m.group(2)))
    p=sql.find('(',m.end()-1); e=find_matching(sql,p)
    cols=[norm(x) for x in split_top_commas(sql[p+1:e])]
    sm=re.search(r"\)\s*SELECT\s", sql[e:], re.I)
    sel_start=e+sm.end()
    fm=re.search(r"\nFROM\s", sql[sel_start:], re.I)
    if not fm: raise ValueError('no FROM')
    sel_end=sel_start+fm.start()
    select_text=sql[sel_start:sel_end]
    projections=split_top_commas(select_text)
    from_part=sql[sel_end:]
    return m.start(),m.end(),target,cols,projections,from_part

def expr_alias(proj):
    m=re.search(r"\s+AS\s+([A-Z0-9_$#\"]+)\s*$",proj.strip(),re.I)
    return norm(m.group(1)) if m else ''

def render_sql(target, cols, projections, from_part):
    col_sql=',\n'.join(f'    {c}' for c in cols)
    proj_sql=',\n'.join('    '+p.strip() for p in projections)
    return f"INSERT INTO {target[0]}.{target[1]} (\n{col_sql}\n)\nSELECT\n{proj_sql}\n{from_part.strip()}\n"

def choose_table(owner, table, objects, table_owners, used_cols=None, profile=None):
    owner, table = norm(owner), norm(table)
    used_cols = {norm(c) for c in (used_cols or [])}
    profile = profile or {}
    if (owner, table) in objects:
        return (owner, table, 'EXACT')

    # Explicit profile overrides are data/config, not engine hardcoding.
    for ov in profile.get('table_overrides', []):
        if norm(ov.get('from_owner')) == owner and norm(ov.get('from_table')) == table:
            to = (norm(ov.get('to_owner')), norm(ov.get('to_table')))
            if to in objects:
                return (to[0], to[1], 'PROFILE_OVERRIDE')

    candidates = []
    owner_variants = generic_owner_variants(owner)
    table_variants = [table]
    # Profile table-name aliases, e.g. legacy table name -> KB table name.
    for ov in profile.get('table_name_aliases', []):
        if norm(ov.get('from')) == table:
            table_variants.append(norm(ov.get('to')))
    table_variants = list(dict.fromkeys(table_variants))

    for ov in owner_variants:
        for tv in table_variants:
            if (ov, tv) in objects:
                candidates.append((ov, tv, 'GENERIC_VARIANT'))

    for tv in table_variants:
        for ov in table_owners.get(tv, []):
            candidates.append((ov, tv, 'TABLE_NAME'))

    # Generic fuzzy fallback: same/variant owner and table name similarity with required cols.
    if not candidates:
        for (ov, tv), meta in objects.items():
            if ov not in owner_variants and table not in table_owners:
                # still allow strong table match if columns prove it.
                pass
            ratio = difflib.SequenceMatcher(None, norm2(table), norm2(tv)).ratio()
            if ratio >= 0.82:
                candidates.append((ov, tv, 'FUZZY_TABLE'))

    # de-dupe
    dedup = []
    seen = set()
    for c in candidates:
        if c[:2] not in seen:
            seen.add(c[:2]); dedup.append(c)
    candidates = dedup
    if not candidates:
        return (owner, table, 'MISSING')

    if used_cols:
        with_cols = [c for c in candidates if used_cols <= objects[c[:2]]['cols']]
        if with_cols:
            candidates = with_cols

    def score(c):
        o, t, kind = c; score = 0
        if o == owner: score += 60
        if o in owner_variants: score += 40
        if t == table: score += 50
        if kind == 'PROFILE_OVERRIDE': score += 100
        if kind == 'GENERIC_VARIANT': score += 30
        if kind == 'TABLE_NAME': score += 20
        if kind == 'FUZZY_TABLE': score += int(30 * difflib.SequenceMatcher(None, norm2(table), norm2(t)).ratio())
        if used_cols and used_cols <= objects[c[:2]]['cols']: score += 70
        if 'SCRATCH' in o: score -= 100
        return -score
    candidates = sorted(candidates, key=score)
    return candidates[0]

def alias_map(sql,objects,table_owners,profile=None):
    clean=strip_comments(sql)
    # First gather raw aliases and used columns around full SQL; used cols per alias from refs
    raw=[]
    for m in re.finditer(r"\b(?:FROM|JOIN|LEFT\s+JOIN|INNER\s+JOIN|RIGHT\s+JOIN|FULL\s+JOIN)\s+([A-Z0-9_$#]+)\.([A-Z0-9_$#]+)\s+([A-Z][A-Z0-9_$#]*)\b(?=\s+(?:ON|LEFT|RIGHT|INNER|FULL|JOIN|WHERE|GROUP|ORDER|;|$))",clean,re.I):
        raw.append((m.group(1),m.group(2),m.group(3)))
    refs=defaultdict(set)
    for m in re.finditer(r"\b([A-Z_][A-Z0-9_$#]*)\.([A-Z_][A-Z0-9_$#]*)\b",clean,re.I):
        a,c=norm(m.group(1)),norm(m.group(2)); refs[a].add(c)
    amap={}
    for o,t,a in raw:
        amap[norm(a)]=choose_table(o,t,objects,table_owners,refs.get(norm(a),set()),profile=profile)[:2]
    return amap

def fix_column_ref(alias, col, obj, context='', profile=None):
    cols = obj['cols']; au = norm(alias); cu = norm(col)
    profile = profile or {}
    if cu in cols:
        return col, 'EXACT'

    for ov in profile.get('column_overrides', []):
        if (not ov.get('alias') or norm(ov.get('alias')) == au) and norm(ov.get('from')) == cu:
            to = norm(ov.get('to'))
            if to in cols:
                return to, f'PROFILE_COLUMN:{cu}->{to}'

    hits = generic_column_candidates(cu, cols)
    if hits:
        return hits[0], f'GENERIC_COLUMN:{cu}->{hits[0]}'

    # Context-sensitive generic fallback only when the target column exists.
    for ov in profile.get('context_column_overrides', []):
        if norm(ov.get('alias')) == au and norm(ov.get('from')) == cu and re.search(ov.get('context_regex','.*'), context, re.I):
            to = norm(ov.get('to'))
            if to in cols:
                return to, f'PROFILE_CONTEXT:{cu}->{to}'

    return col, 'MISSING'

def validate_sql(sql,objects,table_owners,profile=None):
    clean=strip_comments(sql)
    errors=[]
    # target table/cols
    try:
        _,_,target,cols,projs,from_part=parse_insert_select(sql)
        resolved=choose_table(*target,objects,table_owners)[:2]
        if resolved not in objects:
            errors.append(('TARGET_TABLE_NOT_IN_KB','.'.join(target),''))
        else:
            for c in cols:
                if c not in objects[resolved]['cols']:
                    errors.append(('TARGET_COLUMN_NOT_IN_KB',c,'.'.join(resolved)))
    except Exception as e:
        errors.append(('PARSE_ERROR','',str(e)))
    amap=alias_map(sql,objects,table_owners,profile=profile)
    # table errors
    for a,obj in amap.items():
        if obj not in objects:
            errors.append(('TABLE_NOT_IN_KB',a,'.'.join(obj)))
    # refs
    owners={o for o,t in objects}
    for m in re.finditer(r"\b([A-Z_][A-Z0-9_$#]*)\.([A-Z_][A-Z0-9_$#]*)\b",clean,re.I):
        a,c=norm(m.group(1)),norm(m.group(2))
        if (a,c) in objects: continue
        if a in owners: continue
        if a not in amap:
            errors.append(('ALIAS_NOT_DECLARED',f'{a}.{c}',''))
        else:
            obj=amap[a]
            if obj in objects and c not in objects[obj]['cols']:
                errors.append(('COLUMN_NOT_IN_KB',f'{a}.{c}', '.'.join(obj)))
    # dedupe
    out=[]; seen=set()
    for e in errors:
        if e not in seen: seen.add(e); out.append(e)
    return out

def apply_kb_patch(sql,objects,table_owners,drop_bad_target_cols=True,profile=None):
    audit=[]
    profile = profile or {}
    # resolve target, columns/projections
    ins_start,ins_end,target,cols,projections,from_part=parse_insert_select(sql)
    target_res=choose_table(*target,objects,table_owners,profile=profile)[:2]
    if target_res != target:
        audit.append(('TARGET_TABLE_RESOLVED','.'.join(target),'-> '+'.'.join(target_res)))
        target=target_res
    # align projections by alias fallback
    if len(cols)!=len(projections):
        audit.append(('COLUMN_PROJECTION_COUNT_MISMATCH',str(len(cols)),str(len(projections))))
    if drop_bad_target_cols and target in objects:
        keep_cols=[]; keep_proj=[]
        for c,p in zip(cols,projections):
            if c not in objects[target]['cols']:
                audit.append(('TARGET_COLUMN_EXCLUDED_BY_KB',c,'.'.join(target)))
                continue
            keep_cols.append(c); keep_proj.append(p)
        cols,projections=keep_cols,keep_proj
    sql=render_sql(target,cols,projections,from_part)
    # Optional profile-based SQL text replacements. These are external configuration,
    # not tied to any datasource id or KB filename.
    for repl in profile.get('sql_replacements', []):
        pattern = repl.get('pattern')
        replacement = repl.get('replacement', '')
        if pattern and re.search(pattern, sql, re.I|re.S):
            sql = re.sub(pattern, replacement, sql, flags=re.I|re.S)
            audit.append(('PROFILE_SQL_REPLACEMENT', repl.get('name','unnamed'), pattern))
    for inj in profile.get('join_injections', []):
        alias = norm(inj.get('alias'))
        join_sql = inj.get('sql','')
        apply_when = inj.get('apply_when_regex', '')
        if apply_when and not re.search(apply_when, sql, re.I | re.S):
            continue
        if alias and join_sql and not re.search(r"\bJOIN\s+[A-Z0-9_$#]+\.[A-Z0-9_$#]+\s+" + re.escape(alias) + r"\b", sql, re.I):
            sql = re.sub(r'\s*;\s*$', '\n' + join_sql.rstrip() + '\n;', sql, flags=re.S)
            audit.append(('PROFILE_JOIN_INJECTED', alias, inj.get('reason','')))
    # Resolve table refs in FROM/JOIN using KB
    clean=strip_comments(sql)
    # used cols per alias
    refs=defaultdict(set)
    for m in re.finditer(r"\b([A-Z_][A-Z0-9_$#]*)\.([A-Z_][A-Z0-9_$#]*)\b",clean,re.I): refs[norm(m.group(1))].add(norm(m.group(2)))
    def table_repl(m):
        kw, owner, table, alias=m.group(1),norm(m.group(2)),norm(m.group(3)),norm(m.group(4))
        new=choose_table(owner,table,objects,table_owners,refs.get(alias,set()),profile=profile)[:2]
        if new!=(owner,table): audit.append(('TABLE_REF_RESOLVED',f'{owner}.{table} {alias}','-> '+'.'.join(new)))
        return f"{kw} {new[0]}.{new[1]} {alias}"
    sql=re.sub(r"\b(FROM|JOIN|LEFT\s+JOIN|INNER\s+JOIN|RIGHT\s+JOIN|FULL\s+JOIN)\s+([A-Z0-9_$#]+)\.([A-Z0-9_$#]+)\s+([A-Z][A-Z0-9_$#]*)", table_repl, sql, flags=re.I)
    # resolve alias columns by KB iteratively
    for _ in range(3):
        amap=alias_map(sql,objects,table_owners,profile=profile)
        changed=False
        def col_repl(m):
            nonlocal changed
            a,c=m.group(1),m.group(2); au,cu=norm(a),norm(c)
            if (au,cu) in objects: return m.group(0)
            if au not in amap or amap[au] not in objects: return m.group(0)
            context=sql[max(0,m.start()-120):m.end()+120]
            new,why=fix_column_ref(au,cu,objects[amap[au]],context,profile=profile)
            if why!='EXACT' and why!='MISSING':
                changed=True
                audit.append(('COLUMN_REF_RESOLVED',f'{au}.{cu}',f'-> {au}.{new} ({why})'))
                return f'{a}.{new}'
            return m.group(0)
        sql2=re.sub(r"\b([A-Z_][A-Z0-9_$#]*)\.([A-Z_][A-Z0-9_$#]*)\b", col_repl, sql, flags=re.I)
        sql=sql2
        if not changed: break
    return sql,audit

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument('--kb',required=True)
    ap.add_argument('--sql',required=True)
    ap.add_argument('--out',required=True)
    ap.add_argument('--resolution-profile', default='')
    args=ap.parse_args()
    objects,table_owners,owners,data=load_kb(args.kb)
    profile=load_resolution_profile(args.resolution_profile)
    sql=Path(args.sql).read_text(encoding='utf-8')
    patched,audit=apply_kb_patch(sql,objects,table_owners,profile=profile)
    Path(args.out).write_text(patched,encoding='utf-8')
    errs=validate_sql(patched,objects,table_owners,profile=profile)
    print('audit',len(audit),'errors',len(errs))
    for a in audit: print(a)
    for e in errs: print('ERR',e)
if __name__=='__main__': main()
