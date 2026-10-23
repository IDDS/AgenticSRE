# Agentic RL 被测环境部署指南

本文用 verl 作为 Agentic RL 的可替换参考实现，构造以下被测链路：

```text
训练控制器/Ray
  |-> actor/critic/reference 优化 worker
  |-> vLLM 或 SGLang rollout worker
  |-> Agent Loop -> 工具/环境/沙箱
  |-> reward function/model
  `-> 数据集、日志、Trace、checkpoint
```

本环境用于测试训练过程的可用性、性能和一致性，不用于评价具体模型能力。当前仓库
未包含可直接部署的 verl Kubernetes chart，因此本文状态是“参考栈，待目标集群
验收”。

> **可执行性边界**：第 3–4 节是固定上游版本的单机训练参考，
> 第 5 节是 Kubernetes 设计合同。本文没有完整 Job、PVC、RBAC、Ray、
> reward、工具和沙箱清单，因此不能仅照抄文中 YAML 片段完成集群部署。

## 1. 资源分级

| 级别 | 目的 | 最低建议 |
|---|---|---|
| 单机冒烟 | 验证数据、reward、rollout 和 checkpoint 链路 | 1 张至少 24 GiB 显存的 GPU，64 GiB RAM，100 GiB 临时盘 |
| 单节点故障实验 | 验证 worker、OOM、rollout、工具和 checkpoint | 2–4 GPU，128 GiB RAM，独立 checkpoint 卷 |
| 多节点通信实验 | 验证 Ray、NCCL/RDMA、节点和共享存储 | 2 个以上同构 GPU 节点，独立高速网络与共享存储 |

先完成单机冒烟，再扩展到多节点。不能在一个 GPU 上同时运行不可抢占的在线模型
服务和训练故障实验。

本轮核对的 verl 主分支要求 Python >= 3.10、CUDA >= 12.8；其当前文档支持 vLLM
0.18.0 及以上作为 rollout。目标环境必须按实际加速设备选择对应镜像和后端，不要
混装不兼容的 PyTorch、CUDA 和 vLLM。

## 2. 固定源码和容器

```bash
git clone https://github.com/verl-project/verl.git
cd verl
git checkout 668baad7455453eac8ab863f1b2d6fecaec746ed
git rev-parse HEAD
```

上游推荐基于预构建镜像安装 verl 本身。镜像标签仍可能漂移，部署时应记录 digest：

```bash
export VERL_IMAGE_TAG=validated-tag
docker pull "verlai/verl:${VERL_IMAGE_TAG}"
docker image inspect "verlai/verl:${VERL_IMAGE_TAG}" \
  --format '{{index .RepoDigests 0}}'
```

训练 Job 使用 digest，并将当前固定源码挂载到 `/workspace/verl` 或构建进派生镜像：

```bash
export VERL_TESTBED_TAG=validated-version
docker build -t "registry.example/verl-testbed:${VERL_TESTBED_TAG}" \
  -f docker/Dockerfile.stable.vllm .
```

构建文件与目标 GPU/驱动不兼容时，优先采用目标平台已有的验证镜像，再执行
`pip install --no-deps -e .`，避免安装过程覆盖底层 PyTorch/vLLM。

## 3. 单机冒烟基线

上游 quickstart 使用 GSM8K、Qwen2.5-0.5B-Instruct 和函数型 reward。先在容器内
准备数据与模型：

```bash
python3 examples/data_preprocess/gsm8k.py --local_save_dir /data/gsm8k
python3 -c "import transformers; transformers.pipeline('text-generation', model='Qwen/Qwen2.5-0.5B-Instruct')"
```

运行一个小规模 PPO 基线。以下参数刻意缩小 batch 和 epoch 以用于运维冒烟，不是
训练效果基准：

```bash
export PYTHONUNBUFFERED=1
python3 -m verl.trainer.main_ppo \
  data.train_files=/data/gsm8k/train.parquet \
  data.val_files=/data/gsm8k/test.parquet \
  data.train_batch_size=32 \
  data.max_prompt_length=512 \
  data.max_response_length=256 \
  actor_rollout_ref.model.path=Qwen/Qwen2.5-0.5B-Instruct \
  actor_rollout_ref.actor.optim.lr=1e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size=16 \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.4 \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
  critic.optim.lr=1e-5 \
  critic.model.path=Qwen/Qwen2.5-0.5B-Instruct \
  critic.ppo_micro_batch_size_per_gpu=1 \
  algorithm.kl_ctrl.kl_coef=0.001 \
  trainer.logger=console \
  trainer.val_before_train=False \
  trainer.n_gpus_per_node=1 \
  trainer.nnodes=1 \
  trainer.default_local_dir=/checkpoints/agenticsre-rl-smoke \
  trainer.save_freq=2 \
  trainer.test_freq=2 \
  trainer.total_epochs=1 2>&1 | tee /logs/verl-smoke.log
```

验收不是只看进程退出码。日志必须持续产生 step，reward/score、rollout 和 actor
更新时延为有限数值，并成功生成可重新加载的 checkpoint。

## 4. Agentic rollout 基线

Agentic RL 需要异步 rollout、多轮对话和工具调用。固定提交的
`docs/start/agentic_rl.rst` 仍列出了一个已经不在该提交中的旧启动脚本，因此不能
照抄该脚本名。可复现做法是先运行提交内现存的单轮 GRPO 脚本，验证异步 Agent
Loop，再基于同一提交的配置字段接入工具：

```bash
export MODEL_PATH=Qwen/Qwen3-8B
export INFER_BACKEND=vllm
export NGPUS_PER_NODE=8
bash examples/grpo_trainer/run_qwen3_8b_fsdp.sh \
  "data.train_files=['/data/gsm8k/train.parquet']" \
  "data.val_files=['/data/gsm8k/test.parquet']" \
  data.return_raw_chat=True \
  actor_rollout_ref.rollout.mode=async \
  'trainer.logger=["console"]'
```

这一步的硬件需求高于第 3 节 0.5B 冒烟；资源不足时应缩小模型或采用已经在目标
环境验证的等价启动配置，但不能把未经验证的参数组合写成基线。加入工具调用时：

```bash
python examples/data_preprocess/gsm8k_tool_agent_loop.py \
  --local_save_dir /data/gsm8k-tool
```

数据预处理脚本会引用名为 `calc_gsm8k_reward` 的工具，但固定提交没有在
可直接运行的示例目录中交付该工具文件。实施人员需先提供受审的
`function_tool_path` 或 `tool_config_path`，再使用如下完整配置项执行工具基线：

```text
data.train_files=['/data/gsm8k-tool/train.parquet']
data.val_files=['/data/gsm8k-tool/test.parquet']
data.return_raw_chat=True
actor_rollout_ref.rollout.mode=async
actor_rollout_ref.rollout.multi_turn.enable=True
actor_rollout_ref.rollout.multi_turn.function_tool_path=/absolute/path/to/reviewed_tools.py
actor_rollout_ref.rollout.agent.default_agent_loop=tool_agent
```

未提供工具实现前，只能验证单轮异步 rollout，不能标记为 Agentic 工具调用
基线已通过。工具配置字段见固定提交的 `docs/sglang_multiturn/multiturn.rst`；
模型 chat template、工具 parser 和工具 schema 必须成套验证。

Agentic rollout 还应设置：

- `data.return_raw_chat=True`；
- `actor_rollout_ref.rollout.mode=async`；
- 每条轨迹稳定的 trajectory/run ID；
- 工具超时、最大轮次、重试上限和沙箱生命周期；
- rollout token 与训练 token 的一致性检查。

偶发 tool-call 解码错误可以是样本级失败，但必须计数；错误率持续上升或导致有效
rollout 耗尽时应判为故障。需要轨迹查看器时再接入受控实验跟踪服务，不把它作为
训练启动的隐式依赖。

## 5. Kubernetes 部署合同

建议将控制面、rollout、训练和工具环境分别部署，至少使用以下稳定标签：

```text
testbed.agenticsre.io/component=controller
testbed.agenticsre.io/component=trainer
testbed.agenticsre.io/component=rollout
testbed.agenticsre.io/component=reward
testbed.agenticsre.io/component=environment
```

创建隔离 namespace：

```bash
export RL_NAMESPACE=agentic-rl-testbed
kubectl create namespace "${RL_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
```

训练 Job/控制器应满足：

- 使用固定 digest 和显式 GPU 资源请求；
- `/data` 只读，`/checkpoints` 为独立 PVC，`/logs` 可持久化；
- `/dev/shm` 使用 memory `emptyDir` 并设置容量；
- 设置 termination grace period，在终止前保存状态或明确丢弃当前 step；
- 不把数据集、模型和 checkpoint 混在同一个可写目录；
- checkpoint 采用临时目录写入、校验完成后原子发布；
- ServiceAccount 只允许管理本 namespace 内本实验的 worker。

一个容器资源片段示例：

```yaml
resources:
  requests:
    cpu: "8"
    memory: 64Gi
    nvidia.com/gpu: "1"
  limits:
    cpu: "16"
    memory: 96Gi
    nvidia.com/gpu: "1"
volumeMounts:
  - {name: data, mountPath: /data, readOnly: true}
  - {name: checkpoints, mountPath: /checkpoints}
  - {name: logs, mountPath: /logs}
  - {name: dshm, mountPath: /dev/shm}
```

该片段不是完整 Job。目标团队应把已经完成单机冒烟的启动命令放入自己的清单，
先执行 `kubectl apply --dry-run=server` 再部署。

## 6. 多节点 Ray

多节点模式需先建立 Ray head，再让 worker 加入。至少暴露 Ray client/dashboard 所需
端口并限制在集群内：

```bash
ray start --head --port=6379 --dashboard-host=0.0.0.0
ray status
```

worker：

```bash
export RAY_HEAD_ADDRESS=ray-head.ray.svc.cluster.local:6379
ray start --address="${RAY_HEAD_ADDRESS}"
ray status
```

训练提交前验证所有节点看到一致的：源码提交、Python 依赖、模型路径、数据路径、
checkpoint 路径、GPU 数量、网卡和 NCCL 环境。Ray worker Ready 不能证明 NCCL 或
共享存储可用，必须完成一个最小多 GPU collective 和 checkpoint 写读测试。

## 7. 可观测性

### 训练状态

至少采集：

- 当前 global step/epoch、step duration 和最近进展时间；
- actor/critic/reward、KL、entropy、grad norm 和有效样本数；
- rollout QPS、P95/P99、pending、生成 token、失败/截断/解码错误；
- tool call 成功率、时延、超时、sandbox 失败和轨迹轮次；
- GPU utilization/memory、SM clock、温度、XID/ECC；
- NCCL collective 时延/超时、网络吞吐/重传；
- checkpoint 开始/结束、字节、耗时、校验和和最近成功时间；
- Ray node/actor/task 状态和对象存储压力。

仅有 GPU 利用率不能判断训练是否健康；必须设置“step 在给定窗口内推进”的
progress heartbeat。

### 日志和 Trace

日志至少带 `experiment_id`、`job_id`、`global_step`、`worker_role`、`rank`、
`trajectory_id` 和 `checkpoint_id`。Agentic rollout Trace 应关联：

```text
train.step -> rollout.batch -> agent.trajectory -> model.generate -> tool.call -> reward
```

## 8. Checkpoint 恢复演练

列出最后一次完整 checkpoint，校验 manifest/分片，然后使用显式恢复模式启动新的
短 Job。若框架配置支持，应设置 `trainer.resume_mode` 和明确的
`trainer.resume_from_path`；不要依赖目录扫描“猜”最新 checkpoint。

恢复后验证：

- global step 从预期位置继续，而不是静默从 0 开始；
- optimizer/scheduler/actor/critic 等要求的状态均恢复；
- 第一批 rollout、reward 和参数更新为有限值；
- 新 checkpoint 可再次保存、合并或加载。

## 9. AgenticSRE 接入

可在 Web“管理目标”新建档案。如果改 `configs/clusters.yaml`，将下面的
列表项合并到已有 `fault_targets.k8s_clusters` 列表；不要替换整个文件：

```yaml
fault_targets:
  k8s_clusters:
    - id: agentic-rl
      name: Agentic RL 训练集群
      kind: ai
      kubectl_path: kubectl
      kubeconfig: /root/.kube/ai-testbed-config
      context: ai-testbed
      default_namespace: agentic-rl-testbed
      prometheus_url: http://prometheus.example:9090
      description: Agentic rollout、reward、训练和 checkpoint 被测环境
```

当前 AgenticSRE 内置智算场景主要面向 vLLM/GPU。训练专有故障应先按照
[故障目录](fault-catalog.md#8-agentic-rl) 生成独立受控合同，再映射到具体
controller/worker/PVC；不能把整个训练 Job 当成 `vllm-server` 执行通用恢复。

## 10. 安全注入顺序

1. 单个非 leader worker Pod 删除；
2. 一个 rollout worker 的短时 CPU/网络延迟；
3. reward 服务超时或返回可识别错误；
4. 工具沙箱超时和单轨迹失败；
5. checkpoint 临时目录写入失败；
6. 多节点 NCCL/RDMA、GPU driver、节点和正式 checkpoint 损坏。

后两类只在一次性训练、有效 checkpoint 备份和独占节点上执行。每次注入都要检查
训练是否停止推进、是否产生 NaN/Inf、有效 rollout 是否下降，以及恢复后 step 能否
继续。

## 11. 上线验收

- 单机冒烟至少完成 2 个训练 step 和 1 次 checkpoint；
- Agentic 基线产生多轮工具轨迹，错误率在设定阈值内；
- GPU、Ray、rollout、reward、训练 step 和 checkpoint 指标都可查询；
- 一个 worker 失败后控制器能检测并按预期恢复或明确失败；
- checkpoint 恢复 Job 从正确 step 继续；
- 停止实验后无 Ray worker、rollout server、工具沙箱和 GPU 压力进程残留。

## 12. 清理

优先停止提交入口，等待或取消训练 Job，再导出日志和 checkpoint manifest：

```bash
kubectl -n "${RL_NAMESPACE}" get job,pod,pvc
kubectl -n "${RL_NAMESPACE}" delete job experiment-job-name
kubectl -n "${RL_NAMESPACE}" get pod
```

不要自动删除 checkpoint PVC。经实验负责人确认后，再单独删除一次性 PVC 或整个
namespace。

## 13. 参考来源

- verl：<https://github.com/verl-project/verl>
- 安装：<https://verl.readthedocs.io/en/latest/start/install.html>
- PPO quickstart：<https://verl.readthedocs.io/en/latest/start/quickstart.html>
- Agentic RL：<https://verl.readthedocs.io/en/latest/start/agentic_rl.html>
