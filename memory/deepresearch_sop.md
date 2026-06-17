# DeepResearch SOP

适用: 多来源信息整合(网页+本地+记忆+代码)，单次检索无法完整回答时启用。单源1-2步能解决的禁用。

## 核心架构
- Main Agent(Planner): 构建DAG、收集子结论、汇总输出。禁止自己读原文。
- Sub Agent(Searcher): 单一信息源检索+总结，写结论到output.txt。禁止越界。

## 执行流程
1. 问题分解: Q -> 原子子问题列表 -> 判断信息源(WEB/LOCAL/MEMORY/CODE/SYNTH) + 依赖关系 -> dag.md
2. 启动subagent: 无依赖节点并行启动，每个subagent只收context.json(root_question/parent_conclusions/sub_question/source/output_file)
3. 收集+判断: 结论够则进SYNTH；引出新问题则追加节点；失败则检查stderr决定重试或跳过
4. SYNTH: Main自己做汇总，不启subagent。从各output.txt读结论合成报告

## 上下文隔离红线
- context.json只含5个字段，不塞原文
- parent_conclusions控制在200字内
- 大文件给路径让subagent自己读

## 关键坑点
- input.txt禁止塞原文，给路径
- 启动+轮询禁止同一code_run，Popen后立即返回
- 复用task_dir先删旧output.txt
- SYNTH节点Main自己做，不启subagent
- 多subagent并行操作浏览器会冲突，降级为requests是正常fallback
- 至少2轮(初探+深化)再SYNTH，除非问题极浅
- 追加节点不超原始2倍，防死循环

## 资源并行限制
- 浏览器: 同时只1个subagent操作
- 本地文件读: 无限制
- 同一文件写: 禁止并行
- code_run: 无限制

## 报告长度控制
- 默认不截断；总报告超80KB时压缩次要节点至3000字
- 截断处标注"完整结论见原始output.txt"
