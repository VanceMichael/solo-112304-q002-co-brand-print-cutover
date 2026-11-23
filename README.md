# 联名滑雪手套的跨厂包贴换版

客户网页按地区列授权资料。包装主管为同一款不同加热组件批次采购印刷件。

本地来源与合作端均使用合成业务资料。Python 3.11 运行来源服务，服务默认绑定回环地址。

```sh
python3 source_fixture.py --port 8765
python3 -m unittest -v
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
