#!/usr/bin/env bash
# Chuck's own test suite. Run from anywhere: ./run_tests.sh
cd "$(dirname "$0")" || exit 1
fail=0
for t in tests/test_*.py; do
  # every suite gets its own throwaway HOME: no suite may read or write the
  # real ~/.config/chucknorris, or leak settings (Agent mode, recent folders)
  # into the next one
  th=$(mktemp -d)
  out=$(HOME="$th" python3 "$t" 2>&1 | tail -1)
  rm -rf -- "$th"
  case "$out" in
    *PASSED*|*VERIFIED*)
      printf "  \033[32mPASS\033[0m  %-22s %s\n" "$(basename "$t")" "$out" ;;
    *)
      printf "  \033[31mFAIL\033[0m  %-22s %s\n" "$(basename "$t")" "$out"
      fail=1 ;;
  esac
done
echo
if [ "$fail" -eq 0 ]; then
  echo "All suites passed."
else
  echo "Some suites failed."
fi
exit "$fail"
