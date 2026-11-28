#!/bin/sh
# Offline RC6 field check. Run from the extracted package or with edgewalker on PATH.
set -eu
usage() {
    echo 'Usage: sh field-check.sh review RUN_DIR' >&2
    echo '       sh field-check.sh test GATEWAY:PORT [tcp|combined]' >&2
    exit 2
}
[ "$#" -ge 2 ] || usage
cli=./edgewalker
[ -x "$cli" ] || cli=edgewalker
python=${EDGEWALKER_PYTHON:-python3}
if [ -z "${EDGEWALKER_PYTHON:-}" ] && [ -x ./runtime/bin/python3.11 ]; then
    python=./runtime/bin/python3.11
fi
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 6))' || {
    echo 'Run from the extracted compatibility package, or provide Python 3.6+ for the reader.' >&2
    exit 2
}
rc=0
case "$1" in
review)
    [ "$#" -eq 2 ] || usage
    out=$2
    ;;
test)
    [ "$#" -le 3 ] || usage
    mode=${3:-tcp}
    case "$mode" in tcp|combined) ;; *) usage ;; esac
    [ "$("$cli" --version)" = 'edgewalker 0.2.2-rc.6' ] || { echo 'This test expects RC6.' >&2; exit 2; }
    out="./field-${mode}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    echo "Results: $out"
    "$cli" run --connect "$2" --mode "$mode" --forward-max 1M --return-max 1M \
        --duration 60 --flows 1 --flow-limit 1 --output "$out" || rc=$?
    ;;
*) usage ;;
esac
[ -f "$out/session.json" ] || { echo "No session manifest in $out" >&2; exit 1; }
"$python" - "$out" <<'PY'
import json, os, sys
root = sys.argv[1]
with open(os.path.join(root, "session.json")) as f:
    run = json.load(f)
print("Status: {} | Sessions: {} | Stop acknowledged: {}".format(
    run.get("status"), run.get("sessions"), run.get("stop_acknowledged")))
print("TCP rates in kbps (active sender-window duration)")
print("Session Window Direction Flows  Written  Cutoff  Through drain")
path = os.path.join(root, "windows.jsonl")
if os.path.isfile(path):
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                if line.endswith("\n"):
                    raise
                print("Incomplete trailing receipt")
                break
            w = r["window"]
            for lane, name, sender, receiver in (
                    ("forward","forward","server","client"), ("reverse","return","client","server")):
                if not w["rates"][lane] or not r[sender]["tcp"]:
                    continue
                tx = sum(flow["sent"] for flow in r[sender]["tcp"]) * 1000
                rx = [flow["received"] for flow in r[receiver]["tcp"]]
                values = (tx, sum(x["window_bytes"] for x in rx), sum(x["bytes"] for x in rx))
                print("{:7} {:6} {:9} {:5} {:8.1f} {:7.1f} {:13.1f}".format(
                    r["session"], w["id"], name, w["flows"][lane],
                    *(v * 8 / w["ms"] for v in values)))
else:
    print("No recorded receiver windows")
PY
exit "$rc"
