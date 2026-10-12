# workspace（沙箱根目录）

这是 Agent 唯一能读写的地方。所有工具路径都会经过 `ToolContext.resolve_path()` 授权：

- 越界（`../`、绝对路径指向外部）会被拒绝
- 符号链接指向外部也会被拒绝

## 里面有什么

- `README.md` —— 本文件
- `notes.txt` —— 一份随手记的笔记，用来演示 word_count
- `sample.py` —— 一小段示例代码

## 试一试

让 Agent 统计 `*.md` 的行数与高频词，观察者它调用 `word_count` 而不是自己猜。
