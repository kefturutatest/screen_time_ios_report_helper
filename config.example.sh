# Copy to config.local.sh (gitignored) and fill in your own values.

# mDNS name of the iPhone on your LAN. Settings > General > About > Name,
# lowercased with spaces as hyphens, plus ".local":
#   "Sam's iPhone"  ->  sams-iphone.local
# Check with:  ping -c1 sams-iphone.local
PHONE_HOST="my-iphone.local"

# xcrun devicectl list devices   (the "udid" field, not the "identifier")
DEVICE_UDID="00000000-0000000000000000"

# Your Apple Developer team id, used to sign WebDriverAgent:
#   security find-identity -v -p codesigning
DEV_TEAM="XXXXXXXXXX"

# Where you cloned https://github.com/appium/WebDriverAgent
# WDA_DIR="$HOME/WebDriverAgent"
