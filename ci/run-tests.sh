#!/usr/bin/env bash
# Module CI on a throwaway PostgreSQL 16 + pgvector container:
#   1. install the module on an empty database and run its tests;
#   2. install FROM_REF (the released version), store a provider key, upgrade
#      to this checkout, run the tests, check the key still works, uninstall.
# Needs Docker, Odoo 20 and Odoo Enterprise sources (Enterprise is private, so
# this runs locally or on a self-hosted runner, see README).
#
#   ODOO_SRC=~/src/odoo ENTERPRISE_SRC=~/src/enterprise PYTHON=~/venv/bin/python \
#     [FROM_REF=origin/master] ci/run-tests.sh
set -euo pipefail

PGVECTOR_IMAGE=pgvector/pgvector:0.8.1-pg16@sha256:33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337
MODULE=ibt_custom_ai_provider
REPO="$(cd "$(dirname "$0")/.." && pwd)"
: "${ODOO_SRC:?set ODOO_SRC}" "${ENTERPRISE_SRC:?set ENTERPRISE_SRC}"
PYTHON="${PYTHON:-python3}"
FROM_REF="${FROM_REF:-origin/master}"
[[ "$FROM_REF" =~ ^0+$ ]] && FROM_REF=origin/master  # first push of a branch
PORT="${CI_DB_PORT:-55432}"
WORK="$(mktemp -d)"
NAME="ibt-ci-$$"
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"' EXIT

password="$(openssl rand -hex 16)"
IBT_AI_SECRET_KEYS="$("$PYTHON" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export IBT_AI_SECRET_KEYS
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:5432" -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$password" \
    "$PGVECTOR_IMAGE" >/dev/null
until docker exec "$NAME" pg_isready -q -h 127.0.0.1 -U odoo; do sleep 1; done
(umask 077; cat > "$WORK/odoo.conf" <<CONF
[options]
db_host = 127.0.0.1
db_port = $PORT
db_user = odoo
db_password = $password
data_dir = $WORK/data
http_port = ${CI_HTTP_PORT:-18069}
gevent_port = ${CI_GEVENT_PORT:-18072}
CONF
)
mkdir -p "$WORK/current" "$WORK/previous/$MODULE"
ln -s "$REPO" "$WORK/current/$MODULE"
git -C "$REPO" archive "$FROM_REF" | tar -x -C "$WORK/previous/$MODULE"

odoo() {  # odoo <addons dir> [shell] <options>; a subcommand must come first
    local addons="$1"; shift
    (cd "$ODOO_SRC" && "$PYTHON" odoo-bin "$@" -c "$WORK/odoo.conf" \
        --addons-path="$ODOO_SRC/odoo/addons,$ODOO_SRC/addons,$ENTERPRISE_SRC,$addons")
}
tests() {  # tests <addons dir> <database> <-i|-u>: fails unless every test passed
    odoo "$1" -d "$2" "$3" "$MODULE" --test-tags "/$MODULE" --stop-after-init 2>&1 | tee "$WORK/$2.log" | grep -E "tests when|ERROR|FAIL:" || true
    grep -q " 0 failed, 0 error(s) of [1-9]" "$WORK/$2.log"
}

echo "Odoo $(git -C "$ODOO_SRC" rev-parse --short HEAD), Enterprise $(git -C "$ENTERPRISE_SRC" rev-parse --short HEAD)," \
     "pgvector 0.8.1, upgrade from $FROM_REF ($(git -C "$REPO" rev-parse --short "$FROM_REF"))"
echo "== 1/2 install on an empty database"
tests "$WORK/current" ci_install -i
echo "== 2/2 upgrade from $FROM_REF"
odoo "$WORK/previous" -d ci_upgrade -i "$MODULE" --stop-after-init >"$WORK/previous.log" 2>&1 || { tail -30 "$WORK/previous.log"; exit 1; }
odoo "$WORK/previous" shell -d ci_upgrade --no-http >"$WORK/shell.log" 2>&1 <<'PY' || { tail -20 "$WORK/shell.log"; exit 1; }
env["ibt.ai.provider"].create({"name": "CI upgrade", "base_url": "https://ai.example.com/v1", "api_key": "sk-ci-upgrade-key"})
env.cr.commit()
PY
tests "$WORK/current" ci_upgrade -u
if ! odoo "$WORK/current" shell -d ci_upgrade --no-http 2>/dev/null <<'PY' | grep -q CI-KEY-OK; then
provider = env["ibt.ai.provider"].search([("name", "=", "CI upgrade")])
print("CI-KEY-OK" if provider._snapshot()["api_key"] == "sk-ci-upgrade-key" else "CI-KEY-LOST")
PY
    echo "FAIL: the provider key did not survive the upgrade"; exit 1
fi
odoo "$WORK/current" shell -d ci_upgrade --no-http >"$WORK/uninstall.log" 2>&1 <<'PY' || { tail -20 "$WORK/uninstall.log"; exit 1; }
env["ir.module.module"].search([("name", "=", "ibt_custom_ai_provider")]).button_immediate_uninstall()
env.cr.commit()
PY
echo "== uninstall: OK"
echo "All CI checks passed"
