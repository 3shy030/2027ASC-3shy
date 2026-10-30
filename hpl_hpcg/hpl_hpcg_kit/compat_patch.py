#!/usr/bin/env python3
"""修正 HPCG 3.0 在较新 GCC 的 OpenMP default(none) 下缺少 n 声明的问题。"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
path = root/'src/ComputeResidual.cpp'
original = 'shared(local_residual, v1v, v2v)'
replacement = 'shared(local_residual, v1v, v2v, n)'
source = path.read_text()
if source.count(original)==1:
    path.write_text(source.replace(original,replacement))
    print('ComputeResidual.cpp: 在 shared 列表加入只读循环上界 n；计算语义不变。')
elif replacement in source:
    print('兼容性修正已存在。')
else:
    raise SystemExit('源码不符合已核对的 HPCG 3.0 版本，停止修改。')
