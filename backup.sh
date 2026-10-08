#!/usr/bin/env bash
cd "$(dirname "$0")"
mkdir -p backups
tar czf "backups/roshd-$(date +%F).tar.gz" data
ls -1t backups/*.tar.gz | tail -n +15 | xargs -r rm --   # فقط ۱۴ بکاپ آخر نگه داشته می‌شود
echo "Backup done."
