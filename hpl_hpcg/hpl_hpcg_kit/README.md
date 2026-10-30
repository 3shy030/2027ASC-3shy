# HPL 与 HPCG 性能优化实验包

配套报告是待实测稿。包内没有虚构的性能、功耗或提升比例。目标为 Ubuntu 22.04、单节点 CPU、Open MPI 和 OpenBLAS pthread 版；有 GPU 或多节点时，应依据硬件另选实现和进程映射，不能据此声称已经获得整个平台的最高成绩。

## 1 开始前

解压后在本目录执行。完整实验先小规模验证，再筛选参数，最后正式测量。不要把每个候选配置都运行30分钟。

```bash
bash build.sh --deps
bash build.sh
```

`--deps` 使用 sudo 安装编译依赖；普通编译不需要 sudo。脚本固定 HPL 2.3、HPCG 3.0，原始发布包已随附，离线时可免去源码下载。HPCG 的 SHA256 与官方发布页一致；HPL 的 SHA256 是本次从 Netlib 下载后记录的复现指纹，未使用额外的上游签名验证。

两个版本均保留正确性检查。base 为 `-O2 -fno-fast-math`，opt 为 `-O3 -march=native -mtune=native -fno-fast-math`；两者的 HPCG 均开启 OpenMP。`-march=native` 生成的程序只应在兼容的 CPU 上使用。HPL 使用 CBLAS 接口，OpenBLAS 的库实现不会因给 HPL 增加编译参数而重新编译。

HPCG 3.0 的 `ComputeResidual.cpp` 在 OpenMP `default(none)` 的 shared 列表中遗漏了循环上界 `n`，新 GCC 会报错。`compat_patch.py` 只加入该只读变量，不改变运算。基线和优化版使用相同补丁，过程记录在 `build-logs`。

另一个版本细节是：3.0 发布包 `init.cpp` 的运行时长指针判断会使未传 `--rt` 的运行保持0，从而意外进入 Quick Path。本工具会在写入 `hpcg.dat` 的同时显式传入同值 `--rt=秒数`，并检查输出是否出现 QuickPath。命令行解析还会将小于10的 rt 归零，因此短测也至少设置10秒。

```bash
bash collect_env.sh > environment.txt 2>&1
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
```

记录 CPU 型号、物理核、内存容量和通道、NUMA 拓扑、编译器/MPI/BLAS 版本、功耗仪表型号和测量范围。物理核数量应按 socket/core 去重，不能直接把 `nproc` 当作物理核数。有作业调度器时只使用获配资源，并服从其启动方式。本运行器面向独占的单节点 Open MPI 环境，不含 SLURM 和多节点调度适配。

## 2 验证安装

下面两条只验证软件能运行。HPCG 不是正式成绩，且没有用 `rt=0`。

```bash
python3 tools.py run hpl --exe bin/xhpl-base --ranks 1 --threads 1 \
  --n 4096 --nb 128 --p 1 --q 1 --label smoke
python3 tools.py run hpcg --exe bin/xhpcg-base --ranks 1 --threads 1 \
  --nx 32 --ny 32 --nz 32 --seconds 60 --label smoke
```

每次生成独立运行目录，保存输入文件、控制台输出、HPCG 原始报告、二进制和输入文件哈希、MPI 命令、开始结束时间及结果摘要。Open MPI 的绑定信息保存在 `console.log`；如果提示绑定失败，应先修正，不要忽略。不要以 root 身份运行基准程序。

## 3 HPL 调参

先估算 N。这里的 64 GiB 是命令用法示例，执行时要改成实际参与实验的内存预算；内存受容器/作业限额约束时同时核对上限。

```bash
python3 tools.py memory --memory-gib 64 --fraction 0.70 --nb 192 --p 2 --q 4
```

估算基于 `8*N*N` 字节，只包括主矩阵，不能当作实际峰值占用。起点使用70%左右容量，观察 `vmstat 1` 中 si/so、可用内存和各 NUMA 节点占用，再逐步增加。避免内存交换。给 OS、BLAS 和通信缓冲保留余量。

先固定同一 N 比较 NB=128/192/256/384；再比较接近方形的网格以及长条网格；最后只对表现好的组合比较 DEPTH=0/1/2、BCAST=1/3。始终满足 P*Q=ranks。本工具每次仅运行一个参数组合。

如果机器至少有8个获配物理核，可按下列方法比较相同核数预算下的布局。N=6144 仅是小规模示例，正式跑分应使用上一步确认的规模。

```bash
python3 tools.py run hpl --exe bin/xhpl-opt --ranks 8 --threads 1 \
  --n 6144 --nb 192 --p 2 --q 4 --depth 1 --label hpl-layout-8x1
python3 tools.py run hpl --exe bin/xhpl-opt --ranks 4 --threads 2 \
  --n 6144 --nb 192 --p 2 --q 2 --depth 1 --label hpl-layout-4x2
```

不要把改变 N 得到的成绩直接归为编译或绑定带来的收益。比较某项优化时，保持其余参数和功耗约束相同。每个入围配置重复3次，保留全部日志，报告中给出中位数及范围；若题目按最佳单次成绩计分，再附上对应有效记录。

正式 HPL 在相同命令后加 `--stage formal`，并同时采集整个平台功耗。最终必须是残差检查通过的那一条性能行。阈值固定为16，不允许通过改成负数跳过校验。

## 4 HPCG 调参

`nx ny nz` 是每个 MPI 进程的局部网格，不是全局尺寸。本方案取至少16且为8的倍数，以适应默认多重网格层级。报告以输出中的 Processor Dimensions 和 Global Problem Dimensions 为准。

先以60至120秒进行短测，比较纯 MPI 与 MPI+OpenMP、`--mapping slot/numa/socket` 和 `--bind close/spread`。改变 MPI 进程数会改变全局问题规模，进行同规模对比时应调整局部网格，核对输出全局维度一致。做不到同规模时应明确写成规模与布局的联合比较。

参考实现的 Gauss-Seidel 前后扫描存在依赖，不能直接给外层循环加 `omp parallel for`。不要预设线程越多成绩越高。先观察 SpMV、MG、DDOT 的计时，决定是调整内存布局/局部性，还是减少归约与通信。

官方规则还要求数据规模足够大，至少占参与平台主存的四分之一。用短测输出的 `Memory Use Information` 中 `Total memory used for data (Gbytes)` 检查，它是十进制 GB；平台内存参数是 GiB，工具会换算单位。不要只按向量或单个稀疏矩阵的大小估算。合适规模还应为运行时及 OS 留余量。

正式实验至少1800秒。可先设置1860秒留出余量，但最终仍以输出的 Benchmark Time Summary/Total 为准。下面用已经通过短测确认的参数启动；先设置这些变量，不能把任意示例网格直接当作最终规模。

```bash
: "${TASK_RANKS:?先设置 MPI 进程数}" "${TASK_THREADS:?先设置每进程线程数}"
: "${TASK_NX:?先设置合格的局部 nx}" "${TASK_NY:?先设置局部 ny}" "${TASK_NZ:?先设置局部 nz}"
: "${TASK_MEM_GIB:?先设置实际平台总内存 GiB}"
python3 tools.py run hpcg --exe bin/xhpcg-opt \
  --ranks "$TASK_RANKS" --threads "$TASK_THREADS" \
  --nx "$TASK_NX" --ny "$TASK_NY" --nz "$TASK_NZ" \
  --seconds 1860 --stage formal \
  --platform-memory-gib "$TASK_MEM_GIB" --label hpcg-final
```

正式基线也要满足1800秒及相同的规模/功耗要求。短测仅用于筛选，不能放在正式对比表中充当基线。最终抄录 `HPCG result is VALID with a GFLOP/s rating of` 后面的数值，不能用 Raw Total 或历史2.4分数代替。

## 5 功耗记录

两项测试都要求整个平台在运行时低于3000 W。推荐由实验室 PDU/外接功率计采集，时间戳与实验机同步，覆盖初始化、验证和测试全过程，在运行前后各保留数秒采样。若有多个计量点，先按共同时间轴同步求和，再提供总功率。计入哪些设备以课程规定的平台边界为准，并在报告写清楚。

文件是 UTF-8 CSV，两列：`epoch_s,watts`。epoch_s 为 UTC Unix 秒，可带小数；watts 为同一时刻整个平台功率。不要使用 CPU RAPL 或 nvidia-smi 的单器件功率作为整个平台的证明。无测量条件时写“未测”，不能由TDP相加推出达标。

```bash
python3 tools.py power results/某次运行目录 /实际路径/pdu_power.csv \
  --scope full_platform --source '实际仪表型号及计量范围' --max-gap 3
python3 tools.py summarize --root results --output results_summary.csv
```

汇总工具计算时间加权平均功率及采样峰值，任一采样达到3000 W即不通过；记录不覆盖运行区间时拒绝附加。`--max-gap 3` 是本方案对采样间隔的检查参数，并非题目给定的规则。还要查看仪表的峰值保持/告警记录，因为离散采样可能漏掉瞬时尖峰。实际运行应保留功率余量。

## 6 完成报告

将 `environment.txt` 中的实际硬件与软件信息填入报告表。基线与最终配置都保存原始日志和功耗文件；按汇总结果补入 GFLOP/s、实测时长、平均及峰值功率。提升比例为 `(优化值/基线值-1)*100%`。若某项检查缺失或失败，先说明原因并补测，不填写“达标”。

工具的 `automatic_checks=PASS_REQUIRES_MANUAL_REVIEW` 只代表其可解析的字段通过检查，不能替代人工核对 Ubuntu 版本、源代码、实际绑定、仪表范围和连续功率边界。`VALID` 本身也不能证明1800秒和功耗要求均已满足。

截至交付：已核对发布源码和脚本逻辑；在当前 Ubuntu 24.04 环境对 HPCG 3.0 做了非 MPI 的小规模开发验证。该结果不计入课程正式成绩。当前环境没有 MPI，HPL/MPI 构建和 Ubuntu 22.04 正式实测仍须在目标机器完成。

## 来源与许可

- 课程截图中的代码入口：https://github.com/fanchenyou/scnu_asc （本次未能取得仓库内容，未假定其中存在额外配置或规则）。
- HPL 官网及调参文档：https://www.netlib.org/benchmark/hpl/ 和 https://www.netlib.org/benchmark/hpl/tuning.html
- HPCG 官方发布页：https://www.hpcg-benchmark.org/software/index.html
- HPCG 官方仓库：https://github.com/hpcg-benchmark/hpcg

随附发布归档保留原始许可证，修改内容仅限另附构建配置及已说明的兼容性补丁。
This product includes software developed at the University of Tennessee, Knoxville, Innovative Computing Laboratory.
