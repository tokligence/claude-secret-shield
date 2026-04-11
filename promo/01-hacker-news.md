# Hacker News — Show HN

**发帖地址:** https://news.ycombinator.com/submit
**最佳时间:** 美西时间 周二-周四 早上 8-10 点

---

**Title:**
Show HN: Claude Secret Shield – Auto-redact API keys and wallet keys from Claude Code

**URL:**
https://github.com/tokligence/claude-secret-shield

**Text (留空即可，HN 如果填了 URL 就不需要 text):**

---

如果帖子上了首页，准备好在评论区回复。第一条评论建议自己发：

**首条评论（发帖后立即评论）:**

Hey HN, I built this because I kept worrying about Claude Code reading my .env files and API keys during coding sessions.

Claude Secret Shield is a hook that sits between Claude Code and your files. When Claude reads a file, secrets are replaced with placeholders like `{{OPENAI_KEY_a1b2c3d4}}`. When Claude writes code back, placeholders are silently restored to real values. Claude never sees the real key.

Key design decisions:
- Context-based matching for Web3 keys (bare `0x` + 64 hex matches too many things — tx hashes, block hashes, contract addresses). So we require keywords like `private_key =` or `secret_key =` before the hex string.
- HMAC-based deterministic placeholders — same secret always gets the same placeholder, even across sessions.
- The file is temporarily overwritten during Read (then restored by PostToolUse), so Claude Code's internal "has this file been read?" check passes normally.

183 patterns covering AI providers, cloud, DevOps, payment, database URLs, Web3 wallets, and more. One-line install: `curl ... | sh`.

Happy to answer questions about the architecture or regex design.
