#!/usr/bin/env bash
# Write the live config for a cloud build session from three environment
# variables, each holding the JSON text of one file:
#   KEY -> conf/keys.json   PROVIDER -> conf/provider.json
#   PERSONA -> conf/persona_oprtr.json
# and derive conf/cloud.yaml from conf/init.yaml: `venv` becomes this
# checkout and `persona` becomes the first persona named in PERSONA.
# Nothing written here is tracked; the paths go into .git/info/exclude.
# The values are never echoed. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

for var in KEY PROVIDER PERSONA; do
    if [ -z "${!var:-}" ]; then
        echo "cloud-conf: environment variable $var is not set" >&2
        exit 1
    fi
done

write() {  # write <VAR> <path>: validate as a JSON object, then write
    printenv "$1" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
if not isinstance(doc, dict) or not doc:
    sys.exit("not a non-empty JSON object")
json.dump(doc, open(sys.argv[1], "w"), indent=2)
' "$2" || { echo "cloud-conf: $1 is not usable JSON" >&2; exit 1; }
    echo "cloud-conf: wrote $2 from \$$1"
}
write KEY conf/keys.json
write PROVIDER conf/provider.json
write PERSONA conf/persona_oprtr.json

persona=$(printenv PERSONA | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print(next(iter(doc.get("persona", doc))))')
sed -e "s|^\( *venv:\) *[^ #]*|\1 $PWD|" \
    -e "s|^\( *persona:\) *[^ #]*|\1 $persona|" conf/init.yaml > conf/cloud.yaml
echo "cloud-conf: wrote conf/cloud.yaml (venv $PWD, persona $persona)"

base=$(sed -n 's|^ *base: *\([^ #]*\).*|\1|p' conf/cloud.yaml)
for path in conf/cloud.yaml "${base:+/$base/}"; do
    [ -n "$path" ] || continue
    grep -qxF "$path" .git/info/exclude 2>/dev/null || echo "$path" >> .git/info/exclude
done
