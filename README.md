# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /batches`、`POST /reports` 和 `POST /releases`。采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

## 地区更正与双口径报告

来源登记的地区是**登记口径**。若调查核实某媒体在研究期内先后服务两个发行地区，研究员用 `POST /sources/region-corrections` 记录一条带 `effective_at`（生效时间）、`basis`（依据）和可选 `reason` 的更正；更正只追加、不覆盖，`source_id` 与原有观察记录身份保持不变。`GET /sources/{source_id}/regions` 返回登记口径加按生效时间排序的发行地区区段及各区段稿件，`GET /sources/{source_id}/region-at?at=...` 回答某发表时间适用的地区。

报告的 `region_basis` 选择口径，默认 `registered`：

- `registered`：沿用来源登记地区，旧结果不变。
- `distribution`：按**稿件发表时间**落入的来源地区区段归属。更正到达时间、录入时间都不替代发表时间；较晚录入的旧稿仍归到发表时适用的地区。

无论选哪种口径，`POST /reports` 都返回 `region_bases.registered` 与 `region_bases.distribution` 两套完整结果，并在 `region_attribution` 中列出跨口径移动的稿件、迟到稿件和公告后补录稿件。`POST /reports/region-reconciliation` 对账两套口径的地区差异，直接回答某地区变化有多少来自归属修正而不是趋势。同一稿件只计入一个地区，地区分母不会因更正而重复。

发布报告固化 `region_basis`：已发布的冻结报告保持旧口径和旧结果；新的发布必须显式选择口径，导出和审计都会校验口径一致，绝不混用两套地区定义。撤回或修订稿件的处理不变，当前分母、来源贡献和证据链按当前有效记录重新计算。

采集窗口仍绑定原来源和原 `expected_count`。窗口跨过更正生效点时，`/frames/{frame_id}` 按发表时间把成员分到各自一侧（`sealed_region_counts`），并标记 `crosses_region_correction`；完整预期数只计一次，不会写成两个地区各自完整。来源身份不因地区更正而改变，任一发行地区涉及的查询都会继续检查该来源的窗口覆盖。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
