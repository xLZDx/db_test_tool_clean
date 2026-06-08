#!/usr/bin/env python3
"""
full_cycle_drd_odi_insert.py

End-to-end flow:
1. Run DRD/ODI comparison engine and generate Excel workbook tabs.
2. Run universal_insert_builder v6.2 config-driven to generate DRD-driven INSERT.
3. Add final workbook tab: DRD vs ODI vs INSERT.

Primary output:
  <out>/final_reports/final_full_cycle_report.xlsx

Optional reuse mode:
  --existing-compare-out <dir>  Use an existing v16.6 compare output directory.
  --existing-insert-out <dir>   Use an existing v6.2 config-driven insert-builder output directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd: Path):
    print('RUN:', ' '.join(str(x) for x in cmd))
    # Stream output instead of capture so long runs do not look frozen.
    rc = subprocess.call(cmd, cwd=str(cwd))
    if rc != 0:
        raise SystemExit(rc)


def copy_dir(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description='Full DRD/ODI/Generated INSERT comparison flow v17.3 (v16.6.5 compare + v6.2 config-driven insert)')
    p.add_argument('--xlsx', default='', help='DRD Excel file; required unless --existing-compare-out and --existing-insert-out are both provided')
    p.add_argument('--original-xml', default='', help='ODI File 1 / old XML')
    p.add_argument('--fixed-xml', default='', help='ODI File 2 / current XML')
    p.add_argument('--out', required=True)
    p.add_argument('--existing-compare-out', default='', help='Reuse existing v16.6 compare output directory')
    p.add_argument('--existing-insert-out', default='', help='Reuse existing v6.2 config-driven insert-builder output directory')
    p.add_argument('--profile', default='auto', choices=['auto','generic','avy','taxlot'])
    p.add_argument('--target-table', default='')
    p.add_argument('--target-schema', default='')
    p.add_argument('--primary-source', default='')
    p.add_argument('--mapping-sheet', default='')
    p.add_argument('--target-col', default='')
    p.add_argument('--source-cols', default='')
    p.add_argument('--rule-col', default='')
    p.add_argument('--header-row', default='')
    p.add_argument('--schema-kb', default='')
    p.add_argument('--resolution-profile', default='auto', help='auto uses bundled insert_builder/lh_ds3_resolution_profile.json if present; empty string disables')
    p.add_argument('--insert-xml', default='', help='XML evidence for insert builder; default = --fixed-xml')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    compare_out = out/'odi_compare'
    insert_out = out/'insert_builder'
    final_dir = out/'final_reports'
    final_dir.mkdir(parents=True, exist_ok=True)

    def publish_step1_report():
        src = compare_out/'independent_reports'/'independent_reports.xlsx'
        dst = final_dir/'step1_compare_report.xlsx'
        if not src.exists():
            raise SystemExit(f'Step 1 compare report missing: {src}')
        shutil.copy2(src, dst)
        step1_summary = {
            'step': 1,
            'report_type': 'DRD/ODI comparison workbook',
            'source_workbook': str(src),
            'published_workbook': str(dst),
            'exists': dst.exists(),
        }
        (final_dir/'step1_compare_report_summary.json').write_text(json.dumps(step1_summary, indent=2, ensure_ascii=False), encoding='utf-8')
        return dst

    compare_script = here/'compare_engine'/'compare_drd_odi_v16_6_generic_rule_proof.py'
    insert_script = here/'insert_builder'/'universal_insert_builder.py'
    add_tab_script = here/'add_generated_insert_tab.py'

    if args.existing_compare_out:
        copy_dir(Path(args.existing_compare_out).expanduser().resolve(), compare_out)
    else:
        if not args.xlsx or not args.original_xml or not args.fixed_xml:
            raise SystemExit('Missing --xlsx/--original-xml/--fixed-xml for compare run. Or use --existing-compare-out.')
        compare_cmd = [sys.executable, '-B', str(compare_script), '--xlsx', args.xlsx, '--original-xml', args.original_xml, '--fixed-xml', args.fixed_xml, '--out', str(compare_out), '--profile', args.profile]
        for opt, val in [('--target-table', args.target_table),('--mapping-sheet', args.mapping_sheet),('--target-col', args.target_col),('--source-cols', args.source_cols),('--rule-col', args.rule_col),('--header-row', args.header_row)]:
            if val: compare_cmd += [opt, val]
        if args.quiet: compare_cmd += ['--quiet']
        run(compare_cmd, here)

    # STEP 1 REPORT PUBLICATION: keep a standalone copy immediately after DRD/ODI compare.
    step1_report = publish_step1_report()

    if args.existing_insert_out:
        copy_dir(Path(args.existing_insert_out).expanduser().resolve(), insert_out)
    else:
        if not args.xlsx:
            raise SystemExit('Missing --xlsx for insert builder run. Or use --existing-insert-out.')
        insert_xml = args.insert_xml or args.fixed_xml
        insert_cmd = [sys.executable, '-B', str(insert_script), '--xlsx', args.xlsx, '--out', str(insert_out), '--profile', args.profile]
        if insert_xml:
            insert_cmd += ['--xml', insert_xml]
        for opt, val in [('--target-schema', args.target_schema),('--target-table', args.target_table),('--primary-source', args.primary_source),('--mapping-sheet', args.mapping_sheet),('--target-col', args.target_col),('--source-cols', args.source_cols),('--rule-col', args.rule_col),('--header-row', args.header_row),('--schema-kb', args.schema_kb)]:
            if val: insert_cmd += [opt, val]
        if args.resolution_profile:
            rp = args.resolution_profile
            if rp == 'auto':
                bundled = here/'insert_builder'/'profiles'/'lh_ds3_resolution_profile.json'
                rp = str(bundled) if bundled.exists() else ''
            if rp:
                insert_cmd += ['--resolution-profile', rp]
        if args.quiet: insert_cmd += ['--quiet']
        run(insert_cmd, here)

    final_workbook = final_dir/'final_full_cycle_report.xlsx'
    add_cmd = [sys.executable, '-B', str(add_tab_script), '--compare-out', str(compare_out), '--insert-out', str(insert_out), '--final-workbook', str(final_workbook)]
    run(add_cmd, here)

    # STEP 4 REPORT PUBLICATION: final workbook after generated INSERT tab is added.
    step4_report = final_dir/'step4_full_cycle_report.xlsx'
    if not final_workbook.exists():
        raise SystemExit(f'Step 4 final report missing: {final_workbook}')
    shutil.copy2(final_workbook, step4_report)
    step4_summary = {
        'step': 4,
        'report_type': 'DRD/ODI/generated INSERT full-cycle workbook',
        'source_workbook': str(final_workbook),
        'published_workbook': str(step4_report),
        'exists': step4_report.exists(),
    }
    (final_dir/'step4_full_cycle_report_summary.json').write_text(json.dumps(step4_summary, indent=2, ensure_ascii=False), encoding='utf-8')

    hardcode_gate_report = insert_out/'hardcode_gate_report.json'
    summary = {
        'version': '17.3',
        'out': str(out),
        'compare_out': str(compare_out),
        'insert_out': str(insert_out),
        'final_workbook': str(final_workbook),
        'step1_compare_report': str(step1_report),
        'step4_full_cycle_report': str(step4_report),
        'compare_workbook': str(compare_out/'independent_reports'/'independent_reports.xlsx'),
        'generated_insert': str(insert_out/'generated_insert_select_candidate.sql'),
        'hardcode_gate_report': str(hardcode_gate_report) if hardcode_gate_report.exists() else '',
        'full_cycle_summary': str(final_dir/'full_cycle_summary.json'),
        'used_existing_compare_out': bool(args.existing_compare_out),
        'used_existing_insert_out': bool(args.existing_insert_out),
        'compare_engine': 'v16.6.5',
        'insert_builder': 'v6.2 config-driven profile',
        'report_generation': 'twice: step1_compare_report.xlsx and step4_full_cycle_report.xlsx',
    }
    (out/'full_cycle_run_summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
