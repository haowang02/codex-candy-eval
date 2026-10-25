# Codex 降智测试

用本地 Codex CLI 批量测试一道糖果数学题，并统计 reasoning tokens 与正确率。

![example](./example.png)

## 用法

该脚本需要使用 [Codex CLI](https://github.com/openai/codex)

```bash
python3 codex_candy_eval.py -m gpt-6.1-sol -r low -n 3
```

### 一键运行

以下任选其一

```bash
wget -qO- https://is.gd/codex_candy | python3 - -m gpt-6.1-sol -r low -n 3
```

```bash
curl -fsSL https://is.gd/codex_candy | python3 - -m gpt-6.1-sol -r low -n 3
```

参数：

- `-m, --model`：codex 模型名，省略则用本地默认
- `-r, --reasoning-effort`：`low/medium/high/xhigh/max/ultra`（默认 `medium`）
- `-n, --tests`：测试次数（默认 1）

正确答案为 **21**，脚本直接判断回答中是否出现独立的 `21`。

## 致谢

- [LINUX DO](https://linux.do/) - 新的理想型社区
