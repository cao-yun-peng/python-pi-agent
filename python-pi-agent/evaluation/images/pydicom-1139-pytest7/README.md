# pydicom-1139 的旧测试兼容环境

原任务镜像中的 Python 3.8 / pytest 8.3.5 不会执行测试类的旧式 setup/teardown，两个 PASS_TO_PASS 测试因此缺少 self.tag。pytest 官方说明此 nose 兼容功能在 8.0 移除：[说明](https://pytest.org/en/stable/deprecations.html#support-for-tests-written-for-nose)。

本配方从固定 digest 的官方镜像派生，只安装 pytest 7.4.4。wheel 的 SHA-256 来自 PyPI，安装使用 --no-deps、--only-binary 和 --require-hashes；其他依赖沿用基础镜像并执行 pip check。源码、官方测试、测试列表、参考补丁和评测脚本保持原内容。环境与任务依赖分开，不修改宿主评测虚拟环境。

在仓库根运行；构建阶段会下载公开 pytest wheel，后续探针和解题环境可禁网：

```powershell
docker build --pull=false --tag pi-agent/swebench-pydicom-1139:pytest-7.4.4-v1 python-pi-agent/evaluation/images/pydicom-1139-pytest7
docker run --rm --network none --entrypoint /opt/miniconda3/envs/testbed/bin/python pi-agent/swebench-pydicom-1139:pytest-7.4.4-v1 /opt/pi-agent-eval/pydicom-1139/check_legacy_setup.py
```

check_legacy_setup.py 是最小回归：同时检查 setup 准备数据和 teardown 执行顺序。原 pytest 8.3.5 为 1 failed / 1 error，兼容镜像为 1 passed。7.4.4 的旧接口弃用警告属于预期结果，不过滤它们。

原题数据按教程 45 prepare 下载。生成一个新目录，只选 pydicom-1139 并替换两份数据中的 image 字段；prepare.py 在读取本地 tag 后将 image 固定为不可变 ID，记录 profile、上游 revision 和原始数据 SHA-256。已有输出目录会拒绝覆盖。

```powershell
.\.tmp\swebench-env\Scripts\python.exe python-pi-agent/evaluation/images/pydicom-1139-pytest7/prepare.py --source .tmp/evaluation/data/dev-smoke-001 --output .tmp/evaluation/data/pydicom-1139-pytest7-next
```

后续正式 run/batch 必须同时使用新目录的 tasks.json 与 grader-data.json。官方 swebench 5.0.2 的 make_test_spec 直接使用原始行 image，因而 Agent 和 gold/candidate 判分使用同一个固定镜像；无需修改运行时或官方安装包。重新构建镜像后应重新生成输入并记录新 image ID，不在旧批次 resume 中替换环境。

2026-11-23 环境验证位于 `.tmp/evaluation/controls/pydicom-pytest7-v1/`，由正式 OfficialGrader 执行。原批次 dev-smoke-001 的环境失败仍保留。准备好的单题数据为 `.tmp/evaluation/data/pydicom-1139-pytest7-v1/`；这次验证不调用模型。

| 对照 | FAIL_TO_PASS | PASS_TO_PASS | 官方结论 |
|---|---|---|---|
| 官方参考补丁 | 3 通过 / 0 失败 | 38 通过 / 0 失败 | 41 passed，resolved=true |
| 无功能修复的标记文件补丁 | 0 通过 / 3 失败 | 38 通过 / 0 失败 | 38 passed / 3 failed，resolved=false |

验证时的新镜像 ID 为 `sha256:207a12ce24421c982205adaf70219e1339b9b647cc298f4d586c6ed3a0e36a7c`。verification.json 连接两份官方摘要，逐题 report/test_output 位于 logs/run_evaluation 下。正式 DockerTaskEnvironment 另验证了 pytest 版本、旧 hooks、base commit、仅一条 Git 历史及空工作区 diff；结果在 runtime-check.json。prepare.py 重建三份输入与现有输入逐项相同，重复输出目录被拒绝。评测聚焦离线回归运行 30 项，2 项显式集成跳过，其他通过。
