# Reddit r/ClaudeAI

**发帖地址:** https://www.reddit.com/r/ClaudeAI/submit
**Flair:** 选 "Tool" 或 "Resource"

---

**Title:**
I built a tool that prevents Claude Code from seeing your real API keys, database passwords, and wallet private keys

**Body:**

Every time Claude Code reads your files, it could be seeing your real secrets — API keys, database passwords, private keys. I built **Claude Secret Shield** to fix this.

**What it does:**

When Claude reads a file like this:

```
OPENAI_API_KEY=sk-proj-abc123...
DATABASE_URL=postgres://user:realpassword@db.example.com/mydb
```

Claude actually sees:

```
OPENAI_API_KEY={{OPENAI_PROJECT_KEY_a1b2c3d4}}
DATABASE_URL={{POSTGRES_URL_e5f6a7b8}}
```

When Claude writes code back, the placeholders are silently restored to real values. Your code on disk always has the real credentials. Claude never knows the difference.

**Features:**
- 183 secret patterns (OpenAI, Anthropic, AWS, GitHub, Stripe, database URLs, Web3 wallets, etc.)
- 48 blocked file types (.env, credentials.json, id_rsa, hardhat.config.js, etc.)
- Blocks secrets pasted directly in prompts
- Encrypted mapping storage
- One-line install

**Install:**

```bash
curl -fsSL https://raw.githubusercontent.com/tokligence/claude-secret-shield/main/install.sh | sh
```

GitHub: https://github.com/tokligence/claude-secret-shield

Open source, MIT licensed. Would love feedback!
