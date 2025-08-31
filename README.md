# NetSpeed - Network Speed Testing App

A Flask-based application for testing both LAN and WAN network speeds with Telegram notifications.

## Features

- **LAN Speed Testing**: HTTP-based download/upload tests for local network performance
- **WAN Speed Testing**: Internet speed tests using speedtest-cli with live progress updates
- **Telegram Notifications**: Get notified of test results and performance changes with web links
- **Real-time Updates**: Server-Sent Events (SSE) for live progress updates
- **Scheduled Testing**: Automatic WAN tests based on cron schedule with user-friendly management
- **Performance Tracking**: 3-day averages with change alerts and percentage indicators
- **Cron Management**: Web-based interface to configure and manage automated testing schedules

## Quick Start

### 1. Set up Telegram Bot

1. Create a new bot with [@BotFather](https://t.me/botfather) on Telegram
2. Get your bot token (looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)
3. Start a chat with your bot and send `/start`
4. Get your chat ID (you can use [@userinfobot](https://t.me/userinfobot))

### 2. Run with Docker

```bash
docker run -d \
  --name netspeed \
  -p 8080:8080 \
  -v /path/to/data:/data \
  -e TELEGRAM_BOT_TOKEN="your_bot_token_here" \
  -e TELEGRAM_CHAT_ID="your_chat_id_here" \
  -e PAGE_URL="http://your-server-ip:8080" \
  -e WAN_SCHEDULE_CRON="*/30 * * * *" \
  netspeed:latest
```

### 3. Test Configuration

Before running the main app, test your Telegram setup:

```bash
# Set environment variables
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"

# Run test script
python test_telegram.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | (required) | Your Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | Your Telegram chat ID |
| `TELEGRAM_TOPIC_ID` | "" | Forum topic ID for organized discussions (optional) |
| `PAGE_URL` | "" | Public URL for the app (used in Telegram links) |
| `WEB_URL` | "http://localhost:8080" | Web interface URL for Telegram messages |
| `WAN_SCHEDULE_CRON` | "*/30 * * * *" | Cron schedule for automatic WAN tests ("off" to disable) |
| `DB_PATH` | "/data/netspeed.sqlite" | SQLite database path |
| `PORT` | 8080 | HTTP port to listen on |
| `ALERT_THRESHOLD_PCT` | 5.0 | Percentage change threshold for alerts |

## API Endpoints

### Speed Testing
- `POST /api/run-wan` - Run WAN speed test
- `POST /api/run-all` - Run LAN tests + WAN test
- `POST /api/report` - Report custom test results

### Data
- `GET /api/results` - Get test history
- `GET /api/averages` - Get 3-day averages and changes
- `GET /api/wan-status` - Get WAN test status with percentage changes

### Cron Management
- `GET /api/cron/status` - Get current cron job status and configuration
- `POST /api/cron/update` - Update cron schedule (frequency or custom)
- `POST /api/cron/disable` - Disable cron job

### Debug
- `GET /api/debug/telegram` - Check Telegram configuration
- `POST /api/debug/test-telegram` - Send test message

### Real-time Updates
- `GET /events` - Server-Sent Events stream

## Cron Management

The app includes a user-friendly web interface for managing automated WAN speed tests:

### Features
- **Easy Schedule Selection**: Choose from common frequencies (hourly, every 15/30 minutes, daily)
- **Custom Schedules**: Support for custom cron expressions
- **Real-time Status**: See current cron job status and configuration
- **Web Interface**: Modal-based management accessible from the main page

### Usage
1. Click the "🕐 Cron" button in the header
2. Select your desired frequency from the dropdown
3. For custom schedules, choose "Custom" and enter cron expression
4. Click "Update Schedule" to apply changes
5. Use "Disable Cron" to stop automated testing

### Cron Format
The app uses standard cron format: `minute hour day month weekday`
- `*/15 * * * *` = Every 15 minutes
- `0 * * * *` = Every hour at minute 0
- `0 0 * * *` = Daily at midnight

## Troubleshooting

### Telegram Messages Not Sending

1. **Check Configuration**: Visit `/api/debug/telegram` to verify settings
2. **Test Bot**: Run `python test_telegram.py` to test your setup
3. **Check Logs**: Look for Telegram-related error messages in the app logs
4. **Verify Bot Permissions**: Ensure your bot can send messages to the chat
5. **Check Network**: Ensure the app can reach `api.telegram.org`

### Common Issues

- **Bot token invalid**: Double-check the token from @BotFather
- **Chat ID wrong**: Use @userinfobot to get the correct chat ID
- **Bot not started**: Send `/start` to your bot in Telegram
- **Network blocked**: Check firewall/proxy settings

## Development

### Local Setup

```bash
# Install dependencies
pip install flask sqlalchemy requests speedtest-cli

# Set environment variables
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"

# Run app
python app.py
```

### Building Docker Image

```bash
docker build -t netspeed .
```

## License

MIT License
