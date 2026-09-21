## 结论
macOS自进程内存扫描方案验证成功；跨进程需root/Frida(不可逆，待用户决策)。
建议在procmem_scanner.py中加macOS适配层。

## 记忆更新建议
1. **procmem_scanner_sop.md**：补充"macOS注意事项"一节 — `procmem_scanner.py`为Windows专用(ctypes.windll)，macOS替代方案：`ctypes.addressof`自进程扫描 + `vmmap <pid>`获取layout，跨进程需root/Frida/lldb。
2. **global_mem_insight.txt**：进程内存/扫描条目补注 `(macOS: ctypes自进程✅; 跨进程需root/Frida)`。
3. **下批TODO候选**：为procmem_scanner.py添加macOS适配层(platform判断 + ctypes.mach替代方案)，可作7/10价值实战任务。