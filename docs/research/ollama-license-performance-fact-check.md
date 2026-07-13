# Ollama 许可证与性能争议查证

> 查证日期：2026-07-13（Asia/Shanghai）  
> 范围：Ollama 官方源码与 Windows 发行包、llama.cpp 许可证、上游提交/问题、当前 Windows 性能回归。新闻、博客和 Reddit 只用来定位争议，不作为最终依据。  
> 法律边界：本文是工程与许可文本的事实查证，不是法律意见。

## 结论先行

1. **“Ollama 整体不开源”不对。** Ollama 主仓库是 MIT 许可。争议的核心是更窄且更具体的：它分发了含 llama.cpp/ggml 代码的官方二进制包，但抽查的 Windows 包中没有随包保留上游 MIT 版权与许可声明。
2. **这个“二进制包缺少声明”问题有很强证据。** llama.cpp 的 MIT 文本要求在拷贝或重要部分中保留版权和许可声明；Ollama 相关 issue 自 2024-03-16 仍未关闭，提供 `ollama licenses` 的 PR 也尚未合并。2026-07 官方 Windows ARM64 包的完整抽查，以及 AMD64 包的 ZIP 中央目录抽查，都未发现 `LICENSE`/`NOTICE`/上游 MIT 声明。但没有找到法院裁判、权利人执法结论或 Ollama 的正式认责，因此准确措辞是“表面上/很可能不合规”，不是“已被司法认定侵权”。
3. **“Ollama 普遍比 llama.cpp 慢 30%\-70%”没有被可靠证明。** 病毒传播的 `161 vs 89 tok/s` 没有模型、版本、命令、GPU 卸载和上下文等关键控制项；所谓“Qwen-3 Coder 32B FP16”例子连模型名和显存可行性都受到质疑。它们不能支持一个跨硬件、跨版本的固定损失比例。
4. **具体配置下的大幅慢速确实存在。** Windows 上已有详细样例：显存估算改变导致 10% 层卸载到 CPU，生成从 33.8 掉到 4.7 tok/s；混合显卡选错 Vulkan iGPU 时可慢约 9 倍。这类证据指向**后端选择、显存边界、卸载、上游版本或具体回归**，而不是一个固定的“Ollama 封装税”。
5. **不建议仅因为这些文章就卸载 Ollama。** 对 Windows 用户，最有价值的是用同一 GGUF、同一上下文、同一 GPU 卸载做本机对照，并同时看生成、预填充/首 token、显存和并发。对 ReadFellow 的索引主路径，应首先测 `/api/embed` 的批量吞吐，而不是拿聊天生成 tok/s 代替。

## 证据分级

- **强**：官方许可/源码/发行记录，或可重复的官方二进制检查。
- **中**：参数较完整的 issue/对照测试，有维护者确认或独立复现，但未由本报告重跑。
- **弱**：缺少命令、版本、模型文件哈希或关键控制项的社区帖子。

## 争议文章逐项核对

| 说法 | 判定 | 直接证据与日期 | 强度 | 局限/修正 |
|---|---|---|---|---|
| Ollama 本身不是开源软件 | 错误 | [Ollama `LICENSE`](https://github.com/ollama/ollama/blob/main/LICENSE)，当前为 MIT | 强 | 这不自动消除第三方依赖的声明义务。 |
| Ollama 发行包没有保留 llama.cpp/ggml 的 MIT 声明 | 得到强力支持 | [llama.cpp MIT 文本](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE)；[Ollama #3185](https://github.com/ollama/ollama/issues/3185)，2024-03-16 开启且截至查证日仍 open；本报告对 2026-07-06 发行包的抽查 | 强 | “包中没有声明”是事实性结论；“构成侵权”的最终法律认定不在证据范围内。 |
| README “超过一年完全没提 llama.cpp” | 错误/夸大 | 2023-07-03 已写 [`powered by llama.cpp`](https://github.com/ollama/ollama/commit/76cb60d496f4b7c86989888e57d92763470ce278)；2023-07-05 [再次明示提及](https://github.com/ollama/ollama/commit/6559a5b48f33a2f876cbe7facd0c40ddfc282a0a)；2023-07-18 [重写时移除](https://github.com/ollama/ollama/commit/e3cc4d5eac0afd8bdbb923efcbfef0ef83b8eccf)；2024-04-17 [恢复致谢](https://github.com/ollama/ollama/commit/9755cf9173152047030b6d080c29c829bb050a15) | 强 | 明示致谢的空窗约 9 个月，不是“从未提及”或“超过一年”。README 致谢又不等于二进制许可声明。 |
| Ollama 长期拒绝任何致谢 | 不准确 | [#3697](https://github.com/ollama/ollama/issues/3697) 于 2024-04-17 提出，同日由联合创始人提交上述 README 致谢 | 强 | 可批评致谢曾不明显，但“拒绝任何致谢”与历史不符。 |
| 当前 Ollama 仍使用自制/长期落后的 GGML 推理引擎 | 已过时 | [PR #16031](https://github.com/ollama/ollama/pull/16031) 于 2026-05-07 提出；[v0.30.0](https://github.com/ollama/ollama/releases/tag/v0.30.0) 于 2026-05-13 发布；[v0.31.2 `llama_server.go`](https://github.com/ollama/ollama/blob/v0.31.2/llm/llama_server.go) 明示包装 `llama-server` 子进程 | 强 | 仍可有上游版本、编译选项、默认参数、HTTP/调度和回归差异；“同一上游引擎”不等于每次测试都同速。 |
| Ollama 普遍慢 30%\-70% | 证据不足 | [2024-05-25 的 `161 vs 89` 帖子](https://www.reddit.com/r/LocalLLaMA/comments/1d05x6v/llamacpp_runs_18_times_faster_than_ollama/)；[所谓 70% 帖子](https://www.reddit.com/r/LocalLLaMA/comments/1q64f26/llamacpp_vs_ollama_70_higher_code_generation/)；争议文章 [Sleeping Robots，2026-04-15](https://sleepingrobots.com/dreams/stop-using-ollama/) 与 [Hypho，2026-04-17](https://blog.hypho.cn/posts/local-llm-ollama-llama-cpp/) | 弱 | 缺少同一 GGUF 哈希、完整命令、构建/后端、GPU 层数、上下文和多次运行。后一个帖子的模型名称与 FP16 显存可行性也被质疑。 |
| Ollama 新 GUI 仍是闭源 | 已过时 | [#11634](https://github.com/ollama/ollama/issues/11634) 于 2025-08-01 质疑许可；[PR #12933](https://github.com/ollama/ollama/pull/12933) 于 2025-11-04 合并并开放源码 | 强 | 初发时的批评有历史依据，用现在时应加上后续变化。 |

## 许可证争议：能确认到什么程度

### 1. 依赖与许可条件

Ollama v0.31.2 在 [`LLAMA_CPP_VERSION`](https://github.com/ollama/ollama/blob/v0.31.2/LLAMA_CPP_VERSION) 锁定 llama.cpp `b9888`，并在 [`llama/server/CMakeLists.txt`](https://github.com/ollama/ollama/blob/v0.31.2/llama/server/CMakeLists.txt) 从官方 llama.cpp Git 仓库取源码构建。对应上游 [b9888 的 `LICENSE`](https://github.com/ggml-org/llama.cpp/blob/b9888/LICENSE) 是 MIT，要求分发拷贝或重要部分时保留版权和许可声明。MIT 允许商用、修改、转授权和放入专有产品；它不要求把 README 的致谢放到显眼位置，也不明文要求随包分发完整 `AUTHORS`。

### 2. 官方 Windows 包抽查

| 发行物 | 检查 | 结果 | 强度 | 局限 |
|---|---|---|---|---|
| [v0.31.2 Windows ARM64](https://github.com/ollama/ollama/releases/download/v0.31.2/ollama-windows-arm64.zip)，2026-07-06，SHA-256 `52d2273a05e1d939f112aff9f160e87d64ee01a47c81d93c1549fe22ffe8773a` | 用官方 checksum 校验；完整列出/解压 ZIP；在 ASCII 和 UTF-16LE strings 中查找 `MIT License`、`The ggml authors`、`Georgi Gerganov`、MIT 许可句 | 包内只见可执行文件/库；无 `LICENSE`/`NOTICE`/`AUTHORS`，也无上述字符串 | 强 | 检查一个架构的完整包；字符串检查不能证明网页/安装器 UI 从未展示许可。 |
| [v0.31.2 Windows AMD64](https://github.com/ollama/ollama/releases/download/v0.31.2/ollama-windows-amd64.zip)，2026-07-06，总大小 1,502,730,186 bytes | HTTP Range 取 ZIP 尾部并完整解析中央目录 | 中央目录只列出 Ollama/llama-server/GGML/CUDA/Vulkan 等二进制，无许可/声明文件 | 强 | 没有下载 1.5 GB 整包，因而未对 AMD64 二进制本体做 strings 搜索。ZIP 中央目录足以判断没有独立声明文件。 |
| [v0.7.0 Windows ARM64](https://github.com/ollama/ollama/releases/tag/v0.7.0)，2025-05-23，SHA-256 `7f68a41d361f618d50d6bfa4919585e6e7c5652cb9ec6ee130bc6dab7e82c8d7` | 同样完整检查 | 同样无独立声明文件和相关字符串 | 强 | 只是另一个时点/架构；源码树本身存在 vendored llama.cpp `LICENSE`。 |

Windows 构建脚本 [`scripts/build_windows.ps1`](https://github.com/ollama/ollama/blob/v0.31.2/scripts/build_windows.ps1) 直接将 `dist/windows-*` 压成发行 ZIP；在相关 CMake/打包源码中没看到将 llama.cpp 许可文本 stage/install 进发行物的步骤。这与二进制抽查一致。

### 3. 时间线与准确措辞

- 2024-03-16：[#3185](https://github.com/ollama/ollama/issues/3185) 指出 Linux/Windows 发行物不附带第三方声明；截至 2026-07-13 仍为 open。
- 2024-04-17：[#3697](https://github.com/ollama/ollama/issues/3697) 对 README 致谢提问；联合创始人同日 [增加 llama.cpp 致谢](https://github.com/ollama/ollama/commit/9755cf9173152047030b6d080c29c829bb050a15)。这解决的是 README 展示，不是发行包声明。
- 2025-05-23：[PR #10825](https://github.com/ollama/ollama/pull/10825) 提议内嵌第三方许可并提供 `ollama licenses`；截至查证日仍 open/未合并。
- 2026-07-06：官方 [v0.31.2 发行页](https://github.com/ollama/ollama/releases/tag/v0.31.2) 仍提供上述无声明文件的 Windows ZIP。

综合判断：**证据强力支持“至少抽查的官方 Windows 二进制包没有随包保留 llama.cpp/ggml 的 MIT 版权和许可声明”。** 按 MIT 文本的通常合规理解，这构成表面上/很可能的许可不合规。可能的反方观点是，同一 GitHub release 页可取得含许可的源码，或许可通过链接“made available”；但 MIT 原文的条件是声明应被“included”在拷贝/重要部分中，而本报告未找到解决该争点的司法或权利人结论。

## 性能争议：“慢”必须先定义

### 1. 2026-05 以后的架构与旧文章不同

Ollama [v0.30.0](https://github.com/ollama/ollama/releases/tag/v0.30.0) 于 2026-05-13 发布，官方说明切向 llama.cpp 以改善兼容性与性能。v0.31.2 的 [`llm/llama_server.go`](https://github.com/ollama/ollama/blob/v0.31.2/llm/llama_server.go) 将上游 `llama-server` 当作子进程，通过 HTTP 调用；[`server/sched.go`](https://github.com/ollama/ollama/blob/v0.31.2/server/sched.go) 负责模型加载和调度。v0.31.2 锁定的 llama.cpp `b9888` 对应 2026-07-06 的上游提交。

所以，2026-04 文章中“Ollama 当前仍用自制 GGML runner，因长期落后上游而必然巨慢”的架构前提已经过时。但下列差异仍可以影响成绩：

- Ollama 锁定的上游 commit 和独立 llama.cpp 测试版本不同；
- CUDA/ROCm/Vulkan/CPU 构建和 GPU 选择不同；
- `n_gpu_layers`、flash attention、batch、线程、KV cache、context 与 parallel 默认值不同；
- 模型估算导致的 GPU/CPU 卸载不同；
- 调度、进程间/HTTP 传输、模板和采样器路径不同。

### 2. 要分开的性能维度

| 维度 | 建议计算/观察 | 为什么不能混在一起 |
|---|---|---|
| 生成/decode | `eval_count / eval_duration * 1e9`，tok/s | 主要衡量稳态逐 token 解码，病毒图表多数只看这一项。 |
| 预填充/prompt processing | `prompt_eval_count / prompt_eval_duration * 1e9` | 长文档、首 token 和批量嵌入更容易受它影响。 |
| TTFT | 从发请求到流式首 token | 包含排队、冷加载和预填充；不等于 decode tok/s。 |
| 冷加载 | API `load_duration` 和首次请求时间 | `keep_alive` 后的热运行不应与冷启动混合。 |
| VRAM/RAM | `ollama ps` 的 PROCESSOR/上下文/量化，API [`/api/ps`](https://docs.ollama.com/api/ps) 的 `size_vram`，再配合驱动监控 | 即便只卸载一小部分层到系统内存，也可能从 GPU 带宽掉到 PCIe/内存带宽。 |
| 并发 | 1/2/4 并发下的聚合 tok/s 和 p95 延迟 | 单请求封装开销可能存在，但服务器批处理的总吞吐可能持平或更好。 |
| 量化 | 同一 GGUF 文件哈希，同一权重量化与 KV-cache 类型 | Q4/Q8/FP16 会同时改变内存、速度和输出质量；“同一模型名”不够。 |

Ollama 的 [`/api/generate`](https://docs.ollama.com/api/generate) 会返回 `load_duration` / `prompt_eval_*` / `eval_*` 等纳秒级统计，足以把前四项分开。

### 3. 实际案例：有真回归，也有不可推广的数字

| 案例 | 日期与结果 | 可以证明什么 | 强度 | 局限 |
|---|---|---|---|---|
| [#17099：v0.31.2 Windows/RTX 3090 显存估算回归](https://github.com/ollama/ollama/issues/17099) | 2026-07-09；同一 `gemma4:31b`/64K，v0.31.1 估算 18.8 GiB、100% GPU、33.8 tok/s；v0.31.2 估算 20.0 GiB、90% GPU + 10% CPU、4.7 tok/s。维护者 2026-07-10 [确认并给出临时规避](https://github.com/ollama/ollama/issues/17099#issuecomment-4930804178) `LLAMA_ARG_FIT_TARGET=1024` | 当模型处于 VRAM 边界时，小幅估算改变可导致巨大慢速；当前 Windows 回归是真实可能的 | 中-强 | 这是视觉模型 + 64K + 24 GB 显存边界的特定回归，不是全部模型的固定 7 倍差距。 |
| [#16667：Windows 混合 GPU 选错后端](https://github.com/ollama/ollama/issues/16667) | 2026-06-10；Intel iGPU + RTX 4080，Vulkan 路径因共享内存识别选中 iGPU，约 3.8 s vs CUDA 0.42 s。2026-06-22 [PR #16669](https://github.com/ollama/ollama/pull/16669) 已修复 | 后端/GPU 选择可制造级数差距；先查实际用的 GPU，再谈框架性能 | 强 | 已修复且 v0.31.2 包含修复；不能当作当前版本的普遍问题。 |
| [#16624：Windows RX 9070/ROCm 单流回归](https://github.com/ollama/ollama/issues/16624) | 2026-06-08；`qwen3:14b Q4_K_M` 从 v0.24.0 约 53 tok/s 到 v0.30.6 约 42 tok/s（约 -21%）。后续测试在同一 `libggml-hip.so` 下观察 native/in-process 51\-54，`llama-server` 路径 46 tok/s，并发聚合吞吐在噪声内 | 可能存在约 3 ms/token 的单流主机路径开销；单流延迟和并发总吞吐不能混为一谈 | 中 | 主要来自社区 issue 追踪，并非 Ollama 官方 benchmark；Windows 首报与后续 Linux 定位环境不完全同。 |
| [#16721：Windows Radeon 780M/Vulkan Q4 回归](https://github.com/ollama/ollama/issues/16721) | 2026-06-14；3 次热运行均值，v0.30.6 生成 28.18/预填充 368 tok/s，v0.30.7\-.8 约 25.45/294（约 -10%/-20%） | 特定量化与 Vulkan 构建可出现可观测回归 | 中-弱 | 维护者在相似 iGPU 上未复现；Q6/Q8 未受影响；Vulkan 仍是实验性后端。 |
| [#15601：旧版 Linux/AMD Vulkan 落后上游](https://github.com/ollama/ollama/issues/15601) | 2026-04-15；Ollama v0.20.5/b7437 约 34 tok/s，llama.cpp b8765 约 52\-56 tok/s，定位到缺失的上游 Vulkan 优化 | 在旧架构/旧锁定版本上，上游滞后确实可造成约 56% 差距 | 中 | Linux Strix Halo，不是 Windows；v0.31.2/b9888 已包含当时缺失的优化，不能用这个数字描述当前版本。 |
| [`161 vs 89 tok/s` 的 Reddit 帖子](https://www.reddit.com/r/LocalLLaMA/comments/1d05x6v/llamacpp_runs_18_times_faster_than_ollama/) | 2024-05-25；楼主称同量化下 llama.cpp Python 161、Ollama 89；但楼主自报 CPU 测试反而是 Ollama 11 vs llama.cpp 9。另一 M3 Max 用 `llama-3-8b-instruct-q8_0` 对照约 35.63 vs 35.40 tok/s | 可以证明某个未披露环境存在异常，也显示匹配配置时可几乎持平 | 弱 | 楼主没有公布模型、GPU、上下文、版本、命令或卸载设置；不能用作 1.8× 通则。 |

### 4. Windows 用户现在怎么判断

Ollama 官方 [GPU 文档](https://docs.ollama.com/gpu) 将 NVIDIA CUDA 和列明型号的 AMD ROCm 作为支持路径，而 Vulkan 明确是实验性功能。因此：

- **NVIDIA**：先确认实际选中 CUDA 而非集成 GPU/Vulkan，并用 `ollama ps` 检查 `PROCESSOR` 是否 100% GPU。若未主动测 Vulkan，不要开启实验性 `OLLAMA_VULKAN=1`。
- **AMD 独显**：先核对型号是否在 Windows ROCm 支持列表；如果走 Vulkan，应把它当成需单独回归测试的实验后端。
- **Intel/AMD 集显或混合显卡**：风险更高；务必同时核对 Ollama 日志、`ollama ps`、任务管理器/驱动监控中的实际 GPU。
- **24 GB 左右显存 + 大模型/长上下文**：认真检查是否有少量 CPU 卸载。Ollama [context 文档](https://docs.ollama.com/context-length) 说明上下文增大会增加内存使用；`#17099` 证明“只差一点就能全进显存”是最危险的配置。
- **v0.31.2 + `gemma4:31b`/64K/24 GB 附近**：查看 [#17099](https://github.com/ollama/ollama/issues/17099) 是否已在更新版本修复。在未修复时，维护者给出的临时规避是 `LLAMA_ARG_FIT_TARGET=1024`；回退/锁定 v0.31.1 也是短期对照方法，但需自行考量旧版其他缺陷。

## 建议的本机 A/B 测试

1. 记录 `ollama --version`、Windows/驱动/GPU，以及 llama.cpp 的完整 commit 和编译后端。
2. 用 Ollama [GGUF 导入](https://docs.ollama.com/import) 的 `FROM /absolute/path/model.gguf` 导入**同一个文件**，记录 SHA-256；不要用两个同名 tag 代替。
3. 匹配 context、batch、线程、GPU layers/offload、flash attention、parallel、权重量化、KV-cache 类型、prompt template、seed 和采样参数。
4. 在 `ollama ps` 和系统监控中确认两边的 GPU/CPU 驻留一致；如果一边有任何 CPU 卸载，先解决可比性。
5. 冷启动单独测；热身 1\-2 次后至少跑 5 次，报中位数，不只截一次最好成绩。
6. 分别报 decode tok/s、prefill tok/s、TTFT、cold load、VRAM/RAM；再在 1/2/4 并发下报聚合吞吐与 p95。
7. `llama-bench` 适合看后端上限，不要把它的微基准直接与 Ollama API 端到端 TTFT 比。要测封装/服务路径，应同时用 `llama-server` 和 Ollama API 跑同一请求驱动。

经这些控制后，若你的真实负载仍稳定差超过约 10%\-15%，且你不需要 Ollama 的模型管理/API/调度便利，直接用 llama.cpp 就有充分的工程理由。若持平或差距只是几个百分点，则性能不足以单独否定 Ollama；是否更换应由可用性、运维复杂度和许可风险偏好决定。

## 对 ReadFellow 的直接含义

ReadFellow 当前的主索引路径批量调用 Ollama `/api/embed`，默认 `qwen3-embedding:8b` 且 `keep_alive: 30m`；图谱抽取才使用 `/api/generate`。因此：

- 索引性能应测“每秒字符/tokens/文档块”、batch 大小、预填充/嵌入时间、冷加载和峰值 VRAM，而非文本生成 tok/s。
- 要更换嵌入后端，必须保持完全相同的嵌入模型、池化/归一化和输出维度，并核对向量数值；否则应重建 zvec 索引，不能混用旧向量。
- 一个合理的决策顺序是：先记录当前 Ollama 的 `/api/embed` 基线，再用相同模型做另一后端的小样本对照，只在向量语义和性能都过关后才考虑迁移。

## 最终判断

- **许可证批评：窄义核心成立，广义叙事有夸大。** 二进制包缺失上游 MIT 声明的证据很强，值得 Ollama 修复；但“Ollama 不开源”、“从未致谢”、“README 超过一年完全没提 llama.cpp”都不准确。
- **性能批评：不能支持统一的 30%\-70% 结论，但不能当作纯粹造谣。** 旧版上游滞后、Windows GPU 选错、ROCm/Vulkan 特定回归、VRAM 边界上的 CPU 卸载都能产生真实且有时巨大的差距。但 v0.30+ 已改用上游 llama-server，所以必须按当前版本、当前硬件和当前工作负载测量。
- **对 Windows 的实用建议：暂不因文章直接卸载，但值得做一次受控 A/B。** 如果你重视许可链条的干净程度，现有二进制声明问题本身就是一个合理的不采用/隔离理由；如果决策主要是性能，则应以本机数据而不是病毒数字为准。

