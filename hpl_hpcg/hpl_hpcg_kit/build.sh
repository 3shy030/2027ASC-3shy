#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$TASK_ROOT"
if [[ "${1:-}" == "--deps" ]]; then
  sudo apt-get update
  sudo apt-get install -y build-essential gfortran make pkg-config curl \
    openmpi-bin libopenmpi-dev libopenblas-pthread-dev numactl hwloc python3
  exit 0
fi
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 22.04 ]]; then
  echo "正式实验要求 Ubuntu 22.04；本机是 $PRETTY_NAME。" >&2
  exit 1
fi
if [[ "$TASK_ROOT" =~ [[:space:]] ]]; then
  echo '请将实验包放在不含空格的路径下。' >&2; exit 1
fi
for x in mpicc mpicxx mpirun make pkg-config sha256sum python3; do
  command -v "$x" >/dev/null || { echo "缺少 $x；先执行 bash build.sh --deps" >&2; exit 1; }
done
mpirun --version | head -n 1 | grep -qi 'Open MPI' || {
  echo '本脚本运行参数针对 Open MPI，请勿混用 MPICH。' >&2; exit 1;
}
pkg-config --exists openblas
mkdir -p downloads sources build bin build-logs
[[ -f downloads/hpl-2.3.tar.gz ]] || curl -fL --retry 2 \
  https://www.netlib.org/benchmark/hpl/hpl-2.3.tar.gz -o downloads/hpl-2.3.tar.gz
[[ -f downloads/hpcg-3.0.tar.gz ]] || curl -fL --retry 2 \
  https://www.hpcg-benchmark.org/downloads/hpcg-3.0.tar.gz -o downloads/hpcg-3.0.tar.gz
sha256sum -c SHA256SUMS
TASK_JOBS=${JOBS:-4}
[[ "$TASK_JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS 必须是正整数' >&2; exit 1; }
for variant in base opt; do
  task_flags=-O2
  [[ "$variant" == opt ]] && task_flags='-O3 -march=native -mtune=native'
  hpl_src="$TASK_ROOT/sources/hpl-$variant"
  hpcg_src="$TASK_ROOT/sources/hpcg-$variant"
  mkdir -p "$hpl_src" "$hpcg_src"
  tar --no-same-owner -xzf downloads/hpl-2.3.tar.gz -C "$hpl_src" --strip-components=1
  tar --no-same-owner -xzf downloads/hpcg-3.0.tar.gz -C "$hpcg_src" --strip-components=1
  python3 compat_patch.py "$hpcg_src" >"build-logs/hpcg-$variant-compat.log"
  python3 - "$hpl_src" "$hpcg_src" "$task_flags" <<'PY'
import sys
from pathlib import Path
hpl, hpcg, flags = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
template = '''SHELL = /bin/sh
CD = cd
CP = cp
LN_S = ln -s -f
MKDIR = mkdir -p
RM = /bin/rm -f
TOUCH = touch
ARCH = UbuntuOpenBLAS
TOPdir = @ROOT@
INCdir = $(TOPdir)/include
BINdir = $(TOPdir)/bin/$(ARCH)
LIBdir = $(TOPdir)/lib/$(ARCH)
HPLlib = $(LIBdir)/libhpl.a
MPinc =
MPlib =
LAinc = $(shell pkg-config --cflags openblas)
LAlib = $(shell pkg-config --libs openblas) -lm
F2CDEFS =
HPL_INCLUDES = -I$(INCdir) -I$(INCdir)/$(ARCH) $(LAinc) $(MPinc)
HPL_LIBS = $(HPLlib) $(LAlib) $(MPlib)
HPL_OPTS = -DHPL_CALL_CBLAS -DHPL_DETAILED_TIMING
HPL_DEFS = $(F2CDEFS) $(HPL_OPTS) $(HPL_INCLUDES)
CC = mpicc
CCNOOPT = $(HPL_DEFS)
CCFLAGS = $(HPL_DEFS) @FLAGS@ -fno-fast-math
LINKER = mpicc
LINKFLAGS = $(CCFLAGS)
ARCHIVER = ar
ARFLAGS = r
RANLIB = ranlib
'''
(hpl/'Make.UbuntuOpenBLAS').write_text(template.replace('@ROOT@',str(hpl)).replace('@FLAGS@',flags))
# 保留原包的配置说明和许可证，只替换编译选项；不修改算法。
text = (hpcg/'setup/Make.MPI_GCC_OMP').read_text()
lines = text.splitlines()
for i, line in enumerate(lines):
    if line.startswith('CXXFLAGS '):
        lines[i] = 'CXXFLAGS = $(HPCG_DEFS) '+flags+' -fno-fast-math -fopenmp'
(hpcg/'setup/Make.Ubuntu').write_text('\n'.join(lines)+'\n')
PY
  make -C "$hpl_src" -j"$TASK_JOBS" arch=UbuntuOpenBLAS \
    >"build-logs/hpl-$variant.log" 2>&1 || {
      tail -n 50 "build-logs/hpl-$variant.log"; exit 1;
    }
  hpcg_bld="$TASK_ROOT/build/hpcg-$variant"
  mkdir -p "$hpcg_bld"
  (cd "$hpcg_bld"; "$hpcg_src/configure" Ubuntu; make -j"$TASK_JOBS") \
    >"build-logs/hpcg-$variant.log" 2>&1 || {
      tail -n 50 "build-logs/hpcg-$variant.log"; exit 1;
    }
  cp "$hpl_src/bin/UbuntuOpenBLAS/xhpl" "bin/xhpl-$variant"
  cp "$hpcg_bld/bin/xhpcg" "bin/xhpcg-$variant"
done
sha256sum bin/* > build-logs/executables.sha256
bash collect_env.sh > build-logs/environment.txt 2>&1
echo '编译完成。先按 README 做小规模验证，再调参和正式测试。'
