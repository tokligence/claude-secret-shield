# Reddit r/ethdev

**发帖地址:** https://www.reddit.com/r/ethdev/submit

---

**Title:**
PSA: If you use Claude Code for smart contract development, it can see your wallet private keys. I built a tool to prevent this.

**Body:**

If you're using Claude Code to write Solidity or interact with Hardhat/Foundry configs, Claude has full access to read your files — including any private keys, mnemonics, or RPC endpoint URLs with API keys.

I built **Claude Secret Shield** — a hook that automatically redacts secrets before Claude sees them:

**What gets protected:**
- ETH/EVM private keys (`private_key = "0x..."` → `{{WALLET_PRIVATE_KEY_a1b2c3d4}}`)
- BIP39 seed phrases (`mnemonic = "abandon ability..."` → `{{WALLET_MNEMONIC_e5f6a7b8}}`)
- Bitcoin WIF keys (compressed and uncompressed)
- Solana keypairs
- Infura/Alchemy RPC URLs (HTTP and WebSocket)
- Etherscan, Ankr, QuickNode API keys
- `hardhat.config.js`, `truffle-config.js`, `foundry.toml`, `mnemonic.txt` are blocked entirely

**How it works:**
Uses context-based regex matching — it won't false-positive on contract addresses, tx hashes, or block hashes (those are just `0x` + 64 hex without `private_key =` context). When Claude writes code back, placeholders are silently restored.

```bash
curl -fsSL https://raw.githubusercontent.com/tokligence/claude-secret-shield/main/install.sh | sh
```

GitHub: https://github.com/tokligence/claude-secret-shield

183 patterns total, not just Web3. Also covers AWS, OpenAI, Stripe, database URLs, etc. MIT licensed.
