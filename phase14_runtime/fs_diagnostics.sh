#!/bin/bash
echo "========================================================"
echo "FREESWITCH ↔ AI TELEPHONY DIAGNOSTICS"
echo "========================================================"

echo "--- 1. SOFIA STATUS ---"
fs_cli -x "sofia status"
echo ""

echo "--- 2. SOFIA GATEWAYS ---"
fs_cli -x "sofia status gateway"
echo ""

echo "--- 3. ACTIVE CALLS / CHANNELS ---"
fs_cli -x "show calls"
fs_cli -x "show channels"
echo ""

echo "--- 4. MOD_AUDIO_STREAM STATUS ---"
fs_cli -x "module_exists mod_audio_stream"
echo ""

echo "--- 5. RELOADXML ---"
fs_cli -x "reloadxml"
echo ""

echo "--- 6. PORT STATUS (9000-9004, SIP 5060/5080) ---"
netstat -tlnp 2>/dev/null | grep -E "900[0-4]|5060|5080" || ss -tlnp 2>/dev/null | grep -E "900[0-4]|5060|5080"
echo ""

echo "--- 7. RECENT FREESWITCH LOGS (ERRORS/WARNINGS/HANGUPS) ---"
if [ -f /usr/local/freeswitch/log/freeswitch.log ]; then
    tail -n 200 /usr/local/freeswitch/log/freeswitch.log | grep -E "USER_BUSY|NORMAL_CLEARING|mod_audio_stream|playback|bridge|err|warn|hangup" -i
elif [ -f /var/log/freeswitch/freeswitch.log ]; then
    tail -n 200 /var/log/freeswitch/freeswitch.log | grep -E "USER_BUSY|NORMAL_CLEARING|mod_audio_stream|playback|bridge|err|warn|hangup" -i
else
    echo "Freeswitch log file not found in standard locations."
fi

echo "========================================================"
