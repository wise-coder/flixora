#!/usr/bin/env sh
set -eu

OUT_DIR="${1:-static-dist}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

cp \
  index.html \
  about.html \
  category.html \
  contact.html \
  movie.html \
  privacy.html \
  terms.html \
  styles.css \
  script.js \
  config.js \
  logo.svg \
  "$OUT_DIR"/
