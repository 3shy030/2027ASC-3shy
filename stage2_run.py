#!/usr/bin/env python3
"""在现有实验包上运行第二轮实验，不安装依赖、不修改源码。"""
import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

@dataclass
class Case:
    label: str
    kind: str
    executable: str
    ranks: int
    threads: int = 1
    n: int = 32256
    nb: int = 192
    p: int = 2
    q: int = 4
    nx: int = 144
    ny: int = 144
    nz: int = 80
    seconds: int = 2100
    formal: bool = True

def cases():
    runs = [
        Case('stage2-hpcg-preflight-8x1','hpcg','xhpcg-opt',8,seconds=60,formal=False),
        Case('stage2-hpcg-base-4x1','hpcg','xhpcg-base',4,nz=160),
        Case('stage2-hpcg-opt-8x1','hpcg','xhpcg-opt',8),
    ]
    # 交替运行三种配置，避免全部基线在冷机、优化版在热机的固定顺序。
    for rep in range(1,4):
        runs.extend([
            Case(f'stage2-hpl-base-4x1-r{rep}','hpl','xhpl-base',4,nb=128,p=2,q=2),
            Case(f'stage2-hpl-opt-8x1-r{rep}','hpl','xhpl-opt',8),
            Case(f'stage2-hpl-opt-12x1-r{rep}','hpl','xhpl-opt',12,p=3,q=4),
        ])
    return runs

def command(case, memory_gib):
    cmd=[sys.executable,'tools.py','run',case.kind,'--exe','bin/'+case.executable,
         '--ranks',str(case.ranks),'--threads',str(case.threads),'--label',case.label,
         '--stage','formal' if case.formal else 'short']
    if case.kind=='hpl':
        cmd += ['--n',str(case.n),'--nb',str(case.nb),'--p',str(case.p),'--q',str(case.q)]
    else:
        cmd += ['--nx',str(case.nx),'--ny',str(case.ny),'--nz',str(case.nz),
                '--seconds',str(case.seconds),'--platform-memory-gib',str(memory_gib)]
    return cmd

def load_tools(root):
    spec=importlib.util.spec_from_file_location('experiment_tools',root/'tools.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def latest_result(root,label,not_before):
    found=[]
    for path in (root/'results').glob('*/manifest.json'):
        manifest=json.loads(path.read_text())
        if manifest.get('label')==label and manifest.get('start_epoch_s',0)>=not_before:
            found.append((manifest['start_epoch_s'],path.parent,manifest))
    if not found:
        raise RuntimeError('没有找到本轮运行日志：'+label)
    _,folder,manifest=max(found,key=lambda x:x[0])
    summary=json.loads((folder/'summary.json').read_text())
    return folder,manifest,summary

def validate(root,case,started,tool):
    folder,manifest,summary=latest_result(root,case.label,started)
    expected={'kind':case.kind,'ranks':case.ranks,'threads':case.threads,
              'stage':'formal' if case.formal else 'short'}
    if case.kind=='hpl':
        expected.update({'n':case.n,'nb':case.nb,'p':case.p,'q':case.q})
    else:
        expected.update({'nx':case.nx,'ny':case.ny,'nz':case.nz,'seconds':case.seconds})
    if any(manifest.get(k)!=v for k,v in expected.items()):
        raise RuntimeError('已有记录的参数不符：'+case.label)
    if manifest.get('exe_sha256')!=tool.digest(root/'bin'/case.executable):
        raise RuntimeError('已有记录的二进制指纹不符：'+case.label)
    summary=tool.summarize_one(folder)
    if manifest.get('returncode')!=0 or summary.get('program_check')!='PASS':
        raise RuntimeError(f'{case.label} 未通过程序检查，请保留 {folder}')
    if case.kind=='hpcg':
        if summary.get('memory_check')!='PASS':
            raise RuntimeError('HPCG 数据内存比例检查没有通过')
        reports=list(folder.glob('HPCG*.yaml'))
        if len(reports)!=1:
            raise RuntimeError('HPCG 原始报告数量不正确')
        text=reports[0].read_text()
        actual=tuple(tool.section_value(text,'Global Problem Dimensions','Global '+axis) for axis in ('nx','ny','nz'))
        if actual!=(288,288,160):
            raise RuntimeError(f'HPCG 全局网格不符，实际 {actual}，预期 (288,288,160)')
        if case.formal and summary.get('time_check')!='PASS':
            raise RuntimeError('HPCG 输出计时不足1800秒，停止后续实验以便处理')
    return folder,summary

def package(root,archive):
    archive.parent.mkdir(parents=True,exist_ok=True)
    names=['environment.txt','build-logs','results','stage2_results.csv',
           'stage2_progress.log','stage2_status.json','stage2_vmstat.log',
           'stage2_plan.json','stage2_executed.py']
    with tarfile.open(archive,'w:gz') as tf:
        for name in names:
            path=root/name
            if path.exists():
                tf.add(path,arcname=name)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.home()/'asc_lab/hpl_hpcg_kit')
    parser.add_argument('--archive',type=Path,default=Path('/mnt/d/asc_lab/stage2_logs.tar.gz'))
    parser.add_argument('--plan',action='store_true',help='只显示计划，不运行、不写文件')
    args=parser.parse_args()
    runs=cases()
    if args.plan:
        for i,case in enumerate(runs,1):
            print(f'{i:02d} '+shlex.join(command(case,15.338035583496094)))
        return 0
    root=args.root.expanduser().resolve()
    if not root.is_dir() or not (root/'tools.py').is_file():
        parser.error('没有找到实验目录：'+str(root))
    if os.geteuid()==0:
        parser.error('请使用 sunshine 等普通用户运行，不要使用 sudo')
    for name in ['xhpl-base','xhpl-opt','xhpcg-base','xhpcg-opt']:
        if not os.access(root/'bin'/name,os.X_OK):
            parser.error('缺少已编译程序：'+name)
    if len(os.sched_getaffinity(0))<12:
        parser.error('本计划需要至少12个当前可用逻辑处理器；请先反馈环境变化')
    memory_kib=next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
    memory_gib=memory_kib/1024**2
    if memory_gib<14:
        parser.error('当前 Linux 总内存不足14GiB，不执行这组规模，请先反馈')
    os.chdir(root)
    tool=load_tools(root)
    log=(root/'stage2_progress.log').open('a',encoding='utf-8',buffering=1)
    def say(message):
        line=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')+' '+str(message)
        print(line,flush=True);log.write(line+'\n')
    def execute(cmd):
        say('执行 '+shlex.join([str(x) for x in cmd]))
        with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1) as proc:
            for line in proc.stdout:
                print(line,end='',flush=True);log.write(line)
            code=proc.wait()
        if code:
            raise RuntimeError(f'命令返回 {code}，停止后续实验')
    script_source=Path(__file__).resolve()
    destination=root/'stage2_executed.py'
    if script_source!=destination:
        shutil.copyfile(script_source,destination)
    plan=[{'label':c.label,'command':command(c,memory_gib)} for c in runs]
    tool.dump(root/'stage2_plan.json',{'memory_gib':memory_gib,'global_hpcg_grid':[288,288,160],
              'hpl_n':32256,'runs':plan,'script_sha256':hashlib.sha256(destination.read_bytes()).hexdigest()})
    status={'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'completed':[],
            'power_measurement':'NOT_PROVIDED','status':'RUNNING'}
    tool.dump(root/'stage2_status.json',status)
    sampler=None;sample_file=None
    try:
        if shutil.which('vmstat'):
            sample_file=(root/'stage2_vmstat.log').open('w')
            env=os.environ.copy();env['TZ']='UTC';env['LC_ALL']='C'
            sampler=subprocess.Popen(['vmstat','-w','-t','5'],stdout=sample_file,stderr=subprocess.STDOUT,env=env)
        say('第二轮开始。共12次测试；两次 HPCG 请求时长各2100秒，另有验证开销。')
        say('功耗尚未提供，本脚本不把数值正确或时长通过解释为全部竞赛条件达标。')
        execute(['sha256sum','-c','build-logs/executables.sha256'])
        for i,case in enumerate(runs,1):
            # 同一脚本已完成的有效实验可复用，重新执行不会再跑一遍长测试。
            try:
                folder,summary=validate(root,case,0,tool)
                reuse=True
            except (RuntimeError,OSError,ValueError,KeyError):
                reuse=False
            say(f'[{i}/{len(runs)}] {case.label}'+(' 已有有效记录，复用' if reuse else ' 开始'))
            if not reuse:
                started=time.time()
                execute(command(case,memory_gib))
                folder,summary=validate(root,case,started,tool)
            status['completed'].append({'label':case.label,'folder':str(folder.relative_to(root)),
                   'gflops':summary.get('gflops'),'reported_seconds':summary.get('reported_seconds'),
                   'memory_gbytes':summary.get('memory_gbytes')})
            tool.dump(root/'stage2_status.json',status)
            say('通过 数值检查；GFLOP/s='+str(summary.get('gflops'))+
                '；输出计时='+str(summary.get('reported_seconds'))+
                '；数据内存GB='+str(summary.get('memory_gbytes')))
        execute([sys.executable,'tools.py','summarize','--output','stage2_results.csv'])
        status['status']='COMPLETED'
        say('全部实验完成。仍需单独确认功耗证据与课程对 WSL 平台的要求。')
    except KeyboardInterrupt:
        status['status']='INTERRUPTED'
        status['error']='用户中断运行'
        say('已中断，保留已经完成的实验记录。')
    except Exception as exc:
        status['status']='FAILED';status['error']=str(exc)
        say('停止：'+str(exc))
    finally:
        if sampler is not None:
            sampler.terminate()
            try:sampler.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sampler.kill();sampler.wait()
        if sample_file is not None:sample_file.close()
        status['ended_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        tool.dump(root/'stage2_status.json',status)
        log.flush()
        try:
            package(root,args.archive)
            say('日志包已保存：'+str(args.archive))
        except Exception as exc:
            say('日志打包失败：'+str(exc)+'。原始记录仍保存在 '+str(root))
        log.close()
    return 0 if status['status']=='COMPLETED' else 1

if __name__=='__main__':
    raise SystemExit(main())
