# 多模态数据规模只读核验

`scripts/audit_multimodal_volume.py` 用于核对已有日志、指标和调用链文件的实际字节数，不生成数据，也不修改输入目录。它按文件内容计算 SHA-256，跨目录去除相同内容及硬链接重复；不跟随符号链接，跳过特殊文件。目录必须互不重叠，输出必须是输入目录外尚不存在的文件。

在具备输入目录只读权限的测试服务器上运行：

```sh
python3 scripts/audit_multimodal_volume.py \
  --logs /path/to/logs \
  --metrics /path/to/metrics \
  --traces /path/to/traces \
  --output /tmp/multimodal-volume-audit.json
```

输出包括每类声明目录的文件数和字节数、去重后的全局字节数、十进制 GB 和 GiB、文件哈希、跳过项与错误。`meets_50_decimal_gb` 和 `meets_50_gib` 是对两种数量口径的分别比较；读取出错时为 `null`，不能当作通过。`all_declared_modalities_nonempty` 只表示每个指定目录有非空文件，不验证内容确属该模态。

该工具不会将目录名称、文件哈希或文件大小当作数据产生来源的证明。压缩文件与其解压内容的表示不同，内容哈希不会自动识别这种重复；三模态内容、时间范围、项目来源和正式计量口径仍须核验。遍历和读文件不是原子快照，扫描期间被发现变化或不可读的文件会使状态变为 `incomplete`；应优先核验冻结的数据。逐文件哈希需要读取全部文件，请结合存储负载安排执行。
