#!/usr/bin/env bash
# Expire browser sessions server-side (plan M4, "session expiry"): the next navigation shows the login form.
#   session.sh <session_id>     delete that PHP session. The id is the value of the DOLSESSID_<hash> cookie.
#   session.sh                  delete every session of the run user (CUA_RUN_LOGIN, default cua_operator).
# Dolibarr keeps PHP sessions as files (session.save_handler=files; save_path is empty in the image, so PHP's default
# /tmp), one file sess_<id> per session. A logged-in file holds `dol_login|s:<len>:"<login>"`; that is how the run
# user's sessions are found. A browser that still sends the old cookie gets a fresh, anonymous session with that id.
# Prints "expired <n> session(s)" after the "compose project:" line from _env.sh. Never prints a session id's content.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

sid="${1:-}"
login="${CUA_RUN_LOGIN:-cua_operator}"
if [ -n "$sid" ]; then
  [[ "$sid" =~ ^[A-Za-z0-9,-]{1,128}$ ]] || { echo "not a PHP session id" >&2; exit 2; }
else
  [[ "$login" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]] || { echo "CUA_RUN_LOGIN is not a plain login name" >&2; exit 2; }
fi

# runs in the web container; $1 is the session id (or empty), $2 the login
compose exec -T web sh -c '
  dir=$(php -r "echo session_save_path();" 2>/dev/null || true)
  dir=${dir##*;}; dir=${dir:-/tmp}
  n=0
  if [ -n "$1" ]; then
    if [ -f "$dir/sess_$1" ]; then rm -f "$dir/sess_$1"; n=1; fi
  else
    needle="dol_login|s:${#2}:\"$2\""
    for f in "$dir"/sess_*; do
      [ -f "$f" ] || continue
      if grep -qF -- "$needle" "$f"; then rm -f "$f"; n=$((n + 1)); fi
    done
  fi
  echo "expired $n session(s)"
' _ "$sid" "$login"
