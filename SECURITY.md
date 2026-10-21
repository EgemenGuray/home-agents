# Security rules for this repo

This repo is part of a home system that controls real devices and accounts, so these rules are hard requirements.

- Never commit API keys, tokens, passwords, private keys, Wi-Fi passwords, the home's public IP, hostnames, Tailscale names, MAC addresses, account emails or phone numbers.
- Real values live only in ignored files (`.env`, `secrets.yaml`, `*.local.*`) on the machine that needs them, copied from the vault. Each repo ships `*.example` files with fake values.
- Run `pre-commit install` after cloning. The gitleaks hook and the `secret-scan` workflow block known key formats, including Anthropic, Tailscale, Home Assistant, Telegram and WireGuard keys and public IPv4 addresses.
- If a secret is ever pushed, treat it as leaked: rotate it first, then remove it from history.
- Nothing in these repos opens an inbound port. Services bind to localhost and are published only on the tailnet with `tailscale serve`.
