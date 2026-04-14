# Setting Up Watch Mode as a Daemon

redmem's archive hooks only fire on `/compact`. For continuous incremental
archival of running sessions, use `redmem_catchup.py --watch` as a background
service.

## macOS (launchd)

Create `~/Library/LaunchAgents/com.tokligence.redmem.catchup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tokligence.redmem.catchup</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOUR_USERNAME/.claude/hooks/redmem_catchup.py</string>
        <string>--watch</string>
        <string>--interval</string>
        <string>60</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/.claude/vault/catchup.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/.claude/vault/catchup.err</string>
</dict>
</plist>
```

Load and start:

```bash
# Replace YOUR_USERNAME in the plist first!
launchctl load ~/Library/LaunchAgents/com.tokligence.redmem.catchup.plist

# Check it's running
launchctl list | grep redmem

# Stop
launchctl unload ~/Library/LaunchAgents/com.tokligence.redmem.catchup.plist
```

## Linux (systemd user service)

Create `~/.config/systemd/user/redmem-catchup.service`:

```ini
[Unit]
Description=redmem session archive watcher
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.claude/hooks/redmem_catchup.py --watch --interval 60
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now redmem-catchup.service

# Check status
systemctl --user status redmem-catchup.service

# View logs
journalctl --user -u redmem-catchup.service -f
```

## Simple Alternative: cron

If you don't need continuous watching, a cron job every 5 minutes catches most
sessions:

```bash
crontab -e
# Add:
*/5 * * * * /usr/bin/python3 $HOME/.claude/hooks/redmem_catchup.py >/dev/null 2>&1
```

## Do I Need This?

**Probably not.** Since `/compact` triggers the PreCompact hook (which archives),
and the JSONL file on disk is the source of truth, running `redmem_catchup.py`
manually before `/compact` or on-demand is usually sufficient.

Watch mode is useful if:
- You run very long sessions that rarely `/compact`
- You want near-real-time searchable history via `redmem search`
- You want session_state.md to stay fresh for mid-session inspection

Otherwise, skip it — hooks + occasional manual catchup is enough.
