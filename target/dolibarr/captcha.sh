#!/usr/bin/env bash
# Toggle Dolibarr's login CAPTCHA (MAIN_SECURITY_ENABLECAPTCHA, a row in llx_const): the human-only gate for the
# handoff tests. `reset.sh` switches it off again, because the post-seed dump has it off.
# Usage: captcha.sh on|off
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

case "${1:-on}" in
  on)
    db_root -N -e "DELETE FROM llx_const WHERE name='MAIN_SECURITY_ENABLECAPTCHA';
                   INSERT INTO llx_const (name, entity, value, type, visible) VALUES ('MAIN_SECURITY_ENABLECAPTCHA', 1, '1', 'chaine', 0);"
    ;;
  off)
    db_root -N -e "DELETE FROM llx_const WHERE name='MAIN_SECURITY_ENABLECAPTCHA';"
    ;;
  *)
    echo "usage: captcha.sh on|off" >&2
    exit 2
    ;;
esac
echo "captcha constant: $(db_root -N -e "SELECT COALESCE(MAX(value), 'off') FROM llx_const WHERE name='MAIN_SECURITY_ENABLECAPTCHA';")"
# the page must show the field exactly when it is on
if curl -s --max-time 5 "$(login_url)" | grep -q 'name="code"'; then echo "login page: security code field shown"; else echo "login page: no security code field"; fi
