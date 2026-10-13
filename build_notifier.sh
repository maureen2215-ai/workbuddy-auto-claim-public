#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/WorkBuddy领取提醒.app"
mkdir -p "$APP/Contents/MacOS"
/usr/bin/clang -fobjc-arc -O2 -Wall -Wextra -Wno-unused-parameter \
  -mmacosx-version-min=13.0 -framework Cocoa -framework UserNotifications \
  "$ROOT/src/notifier.m" -o "$APP/Contents/MacOS/WorkBuddyReminder"
/usr/bin/python3 - "$APP/Contents/Info.plist" <<'PY'
import plistlib,sys
with open(sys.argv[1],'wb') as f:
    plistlib.dump({'CFBundleIdentifier':'local.workbuddy.autoclaim.reminder',
                  'CFBundleName':'WorkBuddy领取提醒','CFBundleDisplayName':'WorkBuddy领取提醒',
                  'CFBundleExecutable':'WorkBuddyReminder','CFBundlePackageType':'APPL',
                  'CFBundleShortVersionString':'1.0.0','CFBundleVersion':'1',
                  'LSMinimumSystemVersion':'13.0','LSUIElement':True,
                  'NSHighResolutionCapable':True},f)
PY
/usr/bin/codesign --force --sign - --identifier local.workbuddy.autoclaim.reminder "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/plutil -lint "$APP/Contents/Info.plist"
