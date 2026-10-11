#!/usr/bin/env bash
# Spike: toggle Dolibarr's login CAPTCHA (MAIN_SECURITY_ENABLECAPTCHA) directly in llx_const.
# Usage: captcha.sh on|off
set -euo pipefail
db() { docker exec cua-dolibarr-spike-db-1 mariadb -udolidbuser -pdolidbpass dolidb -N -e "$1"; }
if [ "${1:-on}" = "on" ]; then
  db "DELETE FROM llx_const WHERE name='MAIN_SECURITY_ENABLECAPTCHA';
      INSERT INTO llx_const (name, entity, value, type, visible) VALUES ('MAIN_SECURITY_ENABLECAPTCHA', 1, '1', 'chaine', 0);"
else
  db "DELETE FROM llx_const WHERE name='MAIN_SECURITY_ENABLECAPTCHA';"
fi
db "SELECT name, value FROM llx_const WHERE name LIKE 'MAIN_SECURITY_ENABLECAPTCHA%';"
docker exec cua-dolibarr-spike-web-1 php -r 'echo "gd:", (function_exists("imagepng") ? "yes" : "no"), PHP_EOL;'
curl -s http://localhost:8085/index.php | grep -io -E 'captcha[^"]{0,40}|name="code"' | sort -u | head -5
