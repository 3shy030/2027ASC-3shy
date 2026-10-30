#!/usr/bin/env bash
set -u
for spec in os cpu memory numa compiler mpi blas affinity clock; do
  printf '\n[%s]\n' "$spec"
  case "$spec" in
    os) cat /etc/os-release; uname -r ;;
    cpu) lscpu; lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE ;;
    memory) free -b; cat /proc/meminfo ;;
    numa) numactl --hardware ;;
    compiler) gcc --version | head -1; g++ --version | head -1; gfortran --version | head -1 ;;
    mpi) mpirun --version | head -3; mpicc --showme; mpicxx --showme ;;
    blas) pkg-config --modversion openblas; pkg-config --libs openblas
          dpkg-query -W 'libopenblas*' 2>/dev/null ;;
    affinity) taskset -pc $$; cat /proc/self/cgroup ;;
    clock) date -Is; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null ;;
  esac
done
printf '\n[linked_libraries]\n'
for exe in bin/xhpl-* bin/xhpcg-*; do
  [[ -f "$exe" ]] && { printf '%s\n' "$exe"; ldd "$exe"; }
done
