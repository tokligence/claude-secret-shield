# 掘金

**发帖地址:** https://juejin.cn/editor/drafts/new
**分类:** 前端 / 后端 / AI
**标签:** Claude Code, 安全, 开源, Web3

---

**标题:**
Claude Code 密钥泄漏防护实践：183 种密钥自动脱敏，一行命令安装

**正文:**

## 问题

用 Claude Code 写代码时，你有没有想过——Claude 在读你的文件时，能看到所有内容，包括：

- `.env` 里的 `OPENAI_API_KEY`
- 数据库连接串里的密码
- Hardhat 配置里的钱包私钥
- `.aws/credentials` 里的 Access Key

这些密钥会被发送到 Anthropic 的 API。虽然 Anthropic 声明不会用于训练，但你真的放心吗？

## 解决方案

我开源了 **Claude Secret Shield**，一个 Claude Code hook，在 Claude 读文件时自动把密钥替换成占位符：

```
# Claude 看到的：
OPENAI_API_KEY={{OPENAI_PROJECT_KEY_a1b2c3d4}}
DATABASE_URL={{POSTGRES_URL_e5f6a7b8}}
private_key={{WALLET_PRIVATE_KEY_c1d2e3f4}}

# 你磁盘上的真实文件不受任何影响
```

当 Claude 写回代码时，占位符自动还原为真实值。整个过程对 Claude 完全透明。

## 一行安装

```bash
curl -fsSL https://raw.githubusercontent.com/tokligence/claude-secret-shield/main/install.sh | sh
```

## 核心特性

- **183 种密钥模式**：OpenAI、AWS、GitHub、Stripe、数据库连接串、Web3 钱包私钥等
- **48 种文件拦截**：`.env`、`credentials.json`、`id_rsa`、`hardhat.config.js` 等直接拒绝读取
- **Prompt 扫描**：在对话框里粘贴密钥也会被拦截
- **自动还原**：写文件时占位符→真实值
- **加密存储**：映射文件用 Fernet (AES) 加密
- **325 个 E2E 测试**

## Web3 开发者特别注意

最新版本新增了 Web3 钱包保护：

| 类型 | 防护方式 |
|------|----------|
| ETH/EVM 私钥 | 上下文匹配（`private_key = "0x..."`），不会误报合约地址和交易哈希 |
| BIP39 助记词 | 上下文匹配（`mnemonic = "..."`），不会误报普通英文 |
| 比特币 WIF | 格式匹配（51-52 字符 Base58） |
| Infura/Alchemy URL | 含 WebSocket，`wss://mainnet.infura.io/ws/v3/<key>` |
| Etherscan/Ankr/QuickNode | API key 和 RPC URL |
| hardhat.config.js 等 | 直接拦截 |

## 原理

四层防护：

1. **Prompt 扫描** — 粘贴密钥直接拦截
2. **文件拦截** — 敏感文件拒绝读取
3. **模式脱敏** — 183 个正则替换密钥为占位符
4. **自动还原** — 写文件时还原

技术细节：用 HMAC 生成确定性占位符，同一个密钥永远是同一个占位符。读文件时先备份、覆盖为脱敏版本、让 Claude 读、再还原。

GitHub: https://github.com/tokligence/claude-secret-shield

MIT 开源，一行安装，欢迎 star！
