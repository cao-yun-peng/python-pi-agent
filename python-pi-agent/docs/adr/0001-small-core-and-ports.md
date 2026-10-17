# ADR 0001: Small Core with Explicit Ports

## Status

Accepted

## Context

项目既要展示 Agent 的底层机制，又要允许未来替换模型、存储、工具和应用外壳。如果核心直接依赖某个供应商 SDK，教程会被供应商细节淹没，复用也会变得困难。

## Decision

使用单一 Python distribution 内的模块化结构：纯领域对象、端口、核心运行时和适配器。组装由应用入口显式完成，不使用 import-time 注册。

初期不拆多个 distribution。只有组件确实需要独立发布或版本演进时才拆包。

## Alternatives

- 直接围绕 OpenAI-compatible SDK 编写循环：起步更快，但核心会继承供应商协议和错误语义。
- 一开始采用大型插件单体仓库：扩展性强，但会遮蔽教学目标并提高维护成本。

## Consequences

- 第一阶段需要先定义少量协议和规范化事件。
- 新 Provider 可以独立实现和测试。
- 核心循环保持可读，但适配器需要承担协议转换责任。

## Verification

`FakeProvider` 在不修改 `Agent` 的情况下实现 `ModelProvider`，成功、协议失败和取消测试全部通过。
