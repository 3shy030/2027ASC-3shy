#!/usr/bin/env python3
"""标准库实现的单节点实验工具。正式成绩以原始输出及整机功耗记录为准。"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

NUM = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'

def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError('必须为正整数')
    return n

def digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest() if hasattr(hashlib,'file_digest') else hashlib.sha256(f.read()).hexdigest()

def dump(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def hpl_dat(n, nb, p, q, depth=1, bcast=1):
    return f'''HPLinpack benchmark input file
Ubuntu22 reproducible experiment
HPL.out
6
1
{n}
1
{nb}
0
1
{p}
{q}
16.0
1
2
1
4
1
2
1
2
1
{bcast}
1
{depth}
2
64
0
0
1
8
'''

def hpcg_dat(nx, ny, nz, seconds):
    return f'HPCG benchmark input file\nUbuntu22 reproducible experiment\n{nx} {ny} {nz}\n{seconds}\n'

def section_value(text, section, key):
    # 3.0 的缩进 YAML；也兼容后续版本的扁平 key=value 文件。
    flat = re.search(r'^'+re.escape(section)+'::'+re.escape(key)+r'\s*=\s*('+NUM+r')',text,re.M)
    if flat:
        return float(flat.group(1))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().rstrip(':') != section:
            continue
        base = len(line)-len(line.lstrip())
        for following in lines[i+1:]:
            if not following.strip():
                continue
            indent = len(following)-len(following.lstrip())
            if indent <= base:
                break
            match = re.match(r'\s*'+re.escape(key)+r'\s*:\s*('+NUM+r')', following)
            if match:
                return float(match.group(1))
    return None

def parse_hpl(text):
    rows = re.findall(r'^\s*W\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+('+NUM+r')\s+('+NUM+r')\s*$', text, re.M)
    passed = re.search(r'(\d+)\s+tests completed and passed residual checks',text)
    failed = re.search(r'(\d+)\s+tests completed and failed residual checks',text)
    skipped = re.search(r'(\d+)\s+tests skipped because of illegal input values',text)
    ok = bool(len(rows)==1 and passed and int(passed[1])==1 and failed and int(failed[1])==0 and skipped and int(skipped[1])==0)
    return {'gflops':float(rows[0][5]) if len(rows)==1 else None,
            'reported_seconds':float(rows[0][4]) if len(rows)==1 else None,
            'program_check':'PASS' if ok else 'FAIL_OR_UNPARSED', 'memory_gbytes':None}

def parse_hpcg(text):
    match = re.search(r'HPCG result is VALID with a GFLOP/s rating of\s*[:=]\s*('+NUM+r')',text)
    bad = re.search(r'HPCG result is\s*[:=]\s*INVALID|\bFAILED\b',text)
    version = re.search(r'HPCG-Benchmark version\s*:\s*(\d+\.\d+)',text)
    check = 'PASS' if match and not bad else 'FAIL_OR_UNPARSED'
    if not version or version[1]!='3.0':
        check = 'WRONG_OR_UNPARSED_VERSION'
    if 'You have selected the QuickPath option' in text:
        check = 'QUICK_PATH_FORBIDDEN'
    return {'gflops':float(match[1]) if match else None,
            'reported_seconds':section_value(text,'Benchmark Time Summary','Total'),
            'memory_gbytes':section_value(text,'Memory Use Information','Total memory used for data (Gbytes)'),
            'program_check':check}

def run_experiment(a):
    exe = Path(a.exe).resolve()
    if not exe.is_file() or not os.access(exe, os.X_OK):
        raise ValueError('找不到可执行程序：'+str(exe))
    if a.kind=='hpl' and a.p*a.q!=a.ranks:
        raise ValueError('HPL 必须满足 P × Q = MPI ranks')
    if a.kind=='hpcg':
        if a.seconds<10:
            raise ValueError('HPCG 3.0 的命令行解析会将小于10的 rt 归零，短测也须至少10秒')
        if min(a.nx,a.ny,a.nz)<16 or any(x%8 for x in (a.nx,a.ny,a.nz)):
            raise ValueError('本方案要求局部 nx ny nz 至少为16，且为8的倍数')
        if a.stage=='formal' and a.seconds<1800:
            raise ValueError('正式 HPCG 的请求时长不得小于1800秒；禁止 Quick Path')
    mpirun = shutil.which('mpirun')
    if not mpirun:
        raise ValueError('未安装 mpirun')
    version = subprocess.check_output([mpirun,'--version'],text=True,stderr=subprocess.STDOUT)
    if 'Open MPI' not in version:
        raise ValueError('该运行器需要 Open MPI')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = Path(a.out).resolve()/(stamp+'-'+a.kind+'-'+uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True)
    dat_name = 'HPL.dat' if a.kind=='hpl' else 'hpcg.dat'
    dat_text = hpl_dat(a.n,a.nb,a.p,a.q,a.depth,a.bcast) if a.kind=='hpl' else hpcg_dat(a.nx,a.ny,a.nz,a.seconds)
    (run_dir/dat_name).write_text(dat_text)
    env = os.environ.copy()
    settings = {'OMP_NUM_THREADS':str(a.threads),'OMP_DYNAMIC':'FALSE',
                'OMP_PLACES':'cores','OMP_PROC_BIND':a.bind,
                'OPENBLAS_NUM_THREADS':str(a.threads if a.kind=='hpl' else 1)}
    env.update(settings)
    command = [mpirun,'-np',str(a.ranks),'--map-by',f'{a.mapping}:PE={a.threads}',
               '--bind-to','core','--report-bindings',str(exe)]
    if a.kind=='hpcg':
        # 3.0 init.cpp 的 rt 指针判断会使未传 --rt 的运行保留为0。
        # 同时显式传参并写入相同的 hpcg.dat 值，避免意外进入 Quick Path。
        command.append('--rt='+str(a.seconds))
    manifest = {k:v for k,v in vars(a).items() if k!='func'}
    manifest.update({'exe':str(exe),'exe_sha256':digest(exe),
                     'input_sha256':digest(run_dir/dat_name),'command':command,
                     'environment':settings,'mpi_version':version,
                     'start_epoch_s':time.time(),'returncode':None})
    dump(run_dir/'manifest.json',manifest)
    print('运行目录：',run_dir,flush=True)
    print(shlex.join(command),flush=True)
    try:
        with (run_dir/'console.log').open('w') as log:
            completed = subprocess.run(command,cwd=run_dir,env=env,stdout=log,stderr=subprocess.STDOUT)
        manifest['returncode'] = completed.returncode
    finally:
        manifest['end_epoch_s'] = time.time()
        dump(run_dir/'manifest.json',manifest)
    summarize_one(run_dir)
    if manifest['returncode']:
        raise SystemExit(manifest['returncode'])

def summarize_one(folder):
    folder = Path(folder)
    m = json.loads((folder/'manifest.json').read_text())
    text = ''
    if m['kind']=='hpl':
        text = (folder/'console.log').read_text(errors='replace')
        result = parse_hpl(text)
    else:
        reports = list(folder.glob('HPCG*.yaml'))+list(folder.glob('HPCG*.txt'))
        # 不拼接不同运行产生的报告，避免错误匹配旧的 VALID。
        if len(reports)!=1:
            result = {'gflops':None,'reported_seconds':None,'memory_gbytes':None,'program_check':'MISSING_OR_MULTIPLE_REPORTS'}
        else:
            text = reports[0].read_text(errors='replace')
            result = parse_hpcg(text)
            result['report_file'] = reports[0].name
    if m.get('returncode')!=0:
        result['program_check']='PROCESS_FAILED'
    dat_path=folder/('HPL.dat' if m['kind']=='hpl' else 'hpcg.dat')
    if digest(dat_path)!=m.get('input_sha256'):
        result['program_check']='INPUT_FILE_CHANGED'
    if m['kind']=='hpcg' and 'You have selected the QuickPath option' in text:
        result['program_check']='QUICK_PATH_FORBIDDEN'
    t=result.get('reported_seconds')
    result['time_check']='NOT_APPLICABLE' if m['kind']=='hpl' else ('PASS' if t is not None and t>=1800 and m.get('seconds',0)>=1800 else 'SHORT_OR_UNPARSED')
    used=result.get('memory_gbytes')
    total=m.get('platform_memory_gib')
    result['memory_fraction'] = used*1e9/(total*2**30) if used is not None and total else None
    result['memory_check']='NOT_APPLICABLE' if m['kind']=='hpl' else ('UNKNOWN' if result['memory_fraction'] is None else ('PASS' if result['memory_fraction']>=0.25 else 'BELOW_QUARTER'))
    power_path=folder/'power_audit.json'
    power=json.loads(power_path.read_text()) if power_path.exists() else {'power_check':'MISSING'}
    if power_path.exists() and digest(folder/'power.csv')!=power.get('file_sha256'):
        power={'power_check':'FILE_CHANGED'}
    result.update(power)
    result.update({'run':folder.name,'label':m.get('label',''),'kind':m['kind'],'stage':m['stage'],
                   'ranks':m['ranks'],'threads':m['threads'],'exe_sha256':m.get('exe_sha256')})
    checks=[result['program_check'],result['power_check']]
    if m['kind']=='hpcg':
        checks += [result['time_check'],result['memory_check']]
    # 仅表示这些自动检查通过；最终仍核对 OS、计量范围及源代码。
    result['automatic_checks']='PASS_REQUIRES_MANUAL_REVIEW' if m['stage']=='formal' and all(x=='PASS' for x in checks) else 'NOT_READY'
    dump(folder/'summary.json',result)
    return result

def attach_power(a):
    folder=Path(a.run_dir).resolve()
    m=json.loads((folder/'manifest.json').read_text())
    if m.get('end_epoch_s') is None:
        raise ValueError('实验尚未结束')
    with open(a.file,newline='') as f:
        rows=list(csv.DictReader(f))
    samples=[(float(r['epoch_s']),float(r['watts'])) for r in rows]
    if len(samples)<2 or any(not math.isfinite(t) or not math.isfinite(w) or w<=0 for t,w in samples):
        raise ValueError('功耗记录需至少两个有限正功率采样')
    if any(t1<=t0 for (t0,_),(t1,_) in zip(samples,samples[1:])):
        raise ValueError('时间戳必须严格递增')
    start,end=m['start_epoch_s'],m['end_epoch_s']
    if not (samples[0][0]<=start<end<=samples[-1][0]):
        raise ValueError('功耗记录必须覆盖整个运行区间，并在开始前和结束后各留采样')
    energy=0.0; peaks=[]; gaps=[]
    for (t0,w0),(t1,w1) in zip(samples,samples[1:]):
        left,right=max(start,t0),min(end,t1)
        if left>=right:
            continue
        wl=w0+(w1-w0)*(left-t0)/(t1-t0)
        wr=w0+(w1-w0)*(right-t0)/(t1-t0)
        energy+=(wl+wr)/2*(right-left)
        peaks.extend([w0,w1]); gaps.append(t1-t0)
    peak=max(peaks); average=energy/(end-start)
    ok=peak<3000 and max(gaps)<=a.max_gap and a.scope=='full_platform'
    destination=folder/'power.csv'
    if Path(a.file).resolve()!=destination:
        shutil.copyfile(a.file,destination)
    dump(folder/'power_audit.json',{
        'power_check':'PASS' if ok else 'FAIL_OR_INCOMPLETE_SCOPE',
        'sample_peak_w':peak,'time_weighted_mean_w':average,'energy_j':energy,
        'max_observed_gap_s':max(gaps),'allowed_gap_s':a.max_gap,
        'source':a.source,'scope':a.scope,'file_sha256':digest(destination),
        'note':'采样点未超限不等于证明任意瞬间均未超限；需人工核对仪表峰值保持及计量范围。'})
    print(json.dumps(summarize_one(folder),ensure_ascii=False,indent=2))

def summary_all(a):
    rows=[summarize_one(p.parent) for p in sorted(Path(a.root).glob('*/manifest.json'))]
    if not rows:
        raise ValueError('未找到实验；不要把空表当成实测结果')
    fields=['run','label','kind','stage','ranks','threads','gflops','reported_seconds','program_check',
            'time_check','memory_fraction','memory_check','sample_peak_w','time_weighted_mean_w','power_check','automatic_checks']
    with open(a.output,'w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    print(f'已汇总 {len(rows)} 次运行至 {a.output}')

def memory(a):
    if not (math.isfinite(a.memory_gib) and a.memory_gib>0 and math.isfinite(a.fraction) and 0<a.fraction<1):
        raise ValueError('内存必须为正，使用比例必须位于0到1之间')
    align=a.nb*math.lcm(a.p,a.q)
    raw=math.isqrt(int(a.memory_gib*2**30*a.fraction/8))
    n=raw//align*align
    if n<align:
        raise ValueError('内存预算不足')
    print(json.dumps({'N':n,'alignment':align,'matrix_gib':8*n*n/2**30,
                      'note':'仅估算主矩阵；仍需给通信缓冲、BLAS和OS留空间，核对各NUMA节点占用。'},ensure_ascii=False,indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    r=sub.add_parser('run')
    r.add_argument('kind',choices=['hpl','hpcg']); r.add_argument('--exe',required=True)
    r.add_argument('--ranks',type=positive,required=True); r.add_argument('--threads',type=positive,default=1)
    r.add_argument('--stage',choices=['short','formal'],default='short')
    r.add_argument('--label',default=''); r.add_argument('--out',default='results')
    r.add_argument('--mapping',choices=['slot','numa','socket'],default='slot')
    r.add_argument('--bind',choices=['close','spread'],default='close')
    r.add_argument('--n',type=positive,default=4096); r.add_argument('--nb',type=positive,default=128)
    r.add_argument('--p',type=positive,default=1); r.add_argument('--q',type=positive,default=1)
    r.add_argument('--depth',type=int,choices=[0,1,2],default=1)
    r.add_argument('--bcast',type=int,choices=list(range(6)),default=1)
    for axis in ('nx','ny','nz'):
        r.add_argument('--'+axis,type=positive,default=32)
    r.add_argument('--seconds',type=positive,default=60)
    r.add_argument('--platform-memory-gib',type=float)
    r.set_defaults(func=run_experiment)
    m=sub.add_parser('memory'); m.add_argument('--memory-gib',type=float,required=True)
    m.add_argument('--fraction',type=float,default=.70)
    for arg,default in [('nb',192),('p',1),('q',1)]:
        m.add_argument('--'+arg,type=positive,default=default)
    m.set_defaults(func=memory)
    w=sub.add_parser('power'); w.add_argument('run_dir'); w.add_argument('file')
    w.add_argument('--scope',choices=['full_platform','cpu_only','gpu_only','partial_platform'],required=True)
    w.add_argument('--source',required=True); w.add_argument('--max-gap',type=float,default=3)
    w.set_defaults(func=attach_power)
    s=sub.add_parser('summarize'); s.add_argument('--root',default='results')
    s.add_argument('--output',default='results_summary.csv'); s.set_defaults(func=summary_all)
    a=p.parse_args()
    if getattr(a,'platform_memory_gib',None) is not None and (not math.isfinite(a.platform_memory_gib) or a.platform_memory_gib<=0):
        p.error('platform-memory-gib 必须为有限正数')
    if a.action=='power' and (not math.isfinite(a.max_gap) or a.max_gap<=0):
        p.error('max-gap 必须为有限正数')
    try:
        a.func(a)
    except (ValueError,OSError,subprocess.SubprocessError,KeyError) as e:
        p.exit(1,'错误：'+str(e)+'\n')

if __name__=='__main__':
    main()
