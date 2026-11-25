# 联名滑雪手套的跨厂包贴换版

客户网页按地区列授权资料。包装主管为同一款不同加热组件批次采购印刷件。

本地来源与合作端均使用合成业务资料。Python 3.11 运行来源服务，服务默认绑定回环地址。

```sh
python3 source_fixture.py --port 8765
python3 -m unittest discover -s tests -v
```

浏览器测试使用 Playwright 1.56.0 和随该版本安装的 Chromium。首次建立环境时运行 `python3 -m pip install -r requirements-browser.txt` 与 `python3 -m playwright install chromium`。

## 数据交换

- `/brand/catalog` 是地区目录。CSS 展开部分在浏览器里可见，签署件由声明链接提供。
- `/brand/notices` 给出生效组件批号下限和授权继承字段。同一地区内授权按明确列出的款式或组件适用。
- `/factory/cartons` 记录真实组件批号、使用语种、需要张数和允许的版型。印厂的每一版用 print_line 标识。
- `/printer/queue` 给出旧单逐版已印量和未印量。取消只作用于该印单未印部分。可领已印量必须按箱使用量核对。

演练运维端 `GET /__scene` 返回当前时间点，`POST /__scene` 接受 JSON 字段 `index`，从 0 开始顺序切换，每次加 1。换场不会清除合作端当次运行已生成的回执。需要开始新的独立演练时重启服务。业务时间以各来源的当前版本为准。

合作端允许按 `request` 字段查找同路径下的业务请求号，提交使用 `request`、`operation`、`items`（字符串数组）。业务明细使用可选 JSON 对象 `details`，以此请求内的条目编号为键。界面端填多项时按逗号分隔。来源约定的业务动作按下表允许值填写。统一回执含请求号、模拟回执号与各行状态。相同请求号的原样重交返回原回执，参数有变则返回 HTTP 409。回执检索的 404 表示该时刻未查到。描述业务状态的可读来源会随演练进度独立更新，某次提交回执不会提前改写该来源。

## 合作端动作

- `/printer/portal` 接受 `print`、`cancel`、`replace`。

## 包贴换版工具 `packaging_tool`

跟着印单核对客户网页实际发布的文字与签署件，并在上述门户完成交接。仅用
Python 标准库（浏览器测试除外），可在隔离模式下运行：

```sh
# 只核对，不做门户交接
python3 -m packaging_tool.cli --base http://127.0.0.1:8765 audit
# 核对并在门户执行取消/替换/补印，回执持久化到 JSONL，重跑幂等
python3 -m packaging_tool.cli --base http://127.0.0.1:8765 \
    handoff --receipt-store receipts.jsonl
# 已实物预配到换版箱上的旧贴张数（收件登记口径），用于隔离归类
python3 -m packaging_tool.cli --base http://127.0.0.1:8765 \
    --reserved '{"P-4A": 3}' handoff --receipt-store receipts.jsonl
```

### 模块划分

- `sources.py`：从地区目录做有界同源遍历，区分普通链接与 `details/summary`
  展开区链接，跟随共用说明页/可打印版本；只把 `/signed/` 或 XML 当签署件，
  没有签署链接的叶子页登记为“未签署来源”，不能代替签署件。
- `authorization.py`：授权严格按款式、区域、语言隔离，组件级与整款级声明
  分开，整款声明不能扩用到可拆加热组件。公告从指定组件批号起生效，在途货
  按实际组件领用批号决定版本；公告声明沿用旧版字段时，重新拉取旧版签署件，
  确认同一适用对象的声明仍可核对，否则转主管确认。
- `inventory.py`：逐印线核对已印、待印、取消与剩余有效张数。已印旧贴分为
  可继续领用、隔离、等主管确认三类；共享一版贴的其他订单保留有效印量。
  取消只针对未印部分，替换单以逐版回执的真实取消结果（accepted 且队列
  印证）为前提，“暂时查不到回执”不会被当成取消成功。
- `portal.py`：稳定业务请求号，同号原样重交取回同一回执；提交后短暂查不到
  时先按请求号轮询逐版回执，确认无存根才用相同参数重交；409 不覆盖本地记录。
- `service.py` / `cli.py`：编排快照、逐箱核对、印量规划与门户交接，输出
  主管工单（文本或 JSON），保留适用对象、区域、语言、发行日和来源证据链。

### 工单状态

- `ready` 现行签署件覆盖，可贴/可领；`awaiting_edition` 新版生效等替换贴；
  `suspended` 缺授权或声明，只暂停需要该语种和声明的订单行；
  `pending_supervisor` 证据不足，等主管确认。

