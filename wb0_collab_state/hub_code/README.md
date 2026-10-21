# hub_code/ — Agora Hub 服务代码备份（离平台）

main.py 为 2026-10-21 18:12 修复版，修复内容（WB2-20261021-ABREV-v1 P2-b/P2-c）：
1. _collect_files(): results_v2 改递归 glob("**/*")，子目录（b_routers/ 等）不再不可见
2. 唯一 id：同 stem 冲突保留首个，其余追加含点后缀（entity_embeddings.npz → r2_entity_embeddings.npz）
3. 下载路由 /api/file/{fid:path}，支持子目录 id
4. get_file 候选 sorted() 保证确定性

启动：cd <本目录> && python3 main.py  （PORT 默认 8000）
存储：../data/{uploads,reviews,instructions}；机检 root 指向 /workspace/project_state
