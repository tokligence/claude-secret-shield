---
description: Start autopilot. Usage — /autopilot [max_loop=150] <full-spec-path>
---

<!-- autopilot-init: $ARGUMENTS -->
<!-- autopilot-continuation -->

Autopilot 启动。参数：`$ARGUMENTS`

规则（自动触发的消息，不必寒暄）：
1. 每轮开头重读 spec 文件（参数里 $ARGUMENTS 中的路径；如果首个 token 是数字，说明那是 max_loop，spec 是后面的路径）。
2. 做完所有你能做的之后，在最后一条消息里原样输出 `[[AUTOPILOT_DONE]]` 然后停下。
3. 禁止反问用户。需要用户决定的事追加到 `.autopilot/QUESTIONS.md`，跳过它继续做下一件。
4. 发现 spec 本身需要改进或澄清的地方：把建议写到 `.autopilot/IMPROVE.md`（**不要改 spec 本身**），按当前理解继续。
5. 维护 `.autopilot/TASKS.md` 勾选框；所有运行产物都写进 `.autopilot/` 目录（已加入 `.git/info/exclude`，不会进 git）。
6. 现在开始第 1 轮。
