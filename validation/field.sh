#!/bin/sh
# Run from a writable field directory. Use the same EdgeWalker build at both ends.
set -eu
umask 077
here=$(CDPATH='' cd -P "$(dirname "$0")" && pwd)
cli=${EDGEWALKER_CLI:-edgewalker}
if [ -z "${EDGEWALKER_CLI:-}" ]; then
    if [ -x ./edgewalker ]; then cli=./edgewalker
    elif [ -x "$here/../edgewalker" ]; then cli="$here/../edgewalker"; fi
fi
python=${EDGEWALKER_PYTHON:-python3}
if [ -z "${EDGEWALKER_PYTHON:-}" ]; then
    cli_path=$(command -v "$cli" || true)
    bundled="$(dirname "$cli_path")/runtime/bin/python3.11"
    if [ -x "$bundled" ]; then python=$bundled; fi
fi
usage() {
    echo 'Usage: sh validation/field.sh baseline|higher-forward|higher-return GATEWAY:PORT' >&2
    echo '       sh validation/field.sh review RUN_DIR' >&2
    exit 2
}
[ "$#" -eq 2 ] || usage
case "$1" in
review) exec "$python" "$here/review.py" "$2" ;;
baseline) set -- "$1" "$2" --forward-start 1M --forward-max 1M --return-start 1M --return-max 1M ;;
higher-forward) set -- "$1" "$2" --forward-start 1.5M --forward-max 1.5M ;;
higher-return) set -- "$1" "$2" --return-start 1.5M --return-max 1.5M ;;
*) usage ;;
esac
profile=$1
gateway=$2
shift 2
"$python" -c 'import zipfile, sys; sys.exit(sys.version_info < (3, 6))'
"$cli" --version
work=$(mktemp -d "./field-${profile}-XXXXXXXX")
printf 'Local results: %s\n' "$work"
rc=0
"$cli" run --mode tcp --connect "$gateway" --duration 60 --window-ms 5000 --flows 1 --flow-limit 1 "$@" --output "$work/run" || rc=$?
printf '%s\n' "$rc" > "$work/client-exit.txt"
"$python" "$here/review.py" "$work/run" --profile "$profile" --exit-code "$rc" || exit 1
exit "$rc"
