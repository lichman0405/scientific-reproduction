# Bundled fonts for the Unicode PDF backend

The deterministic PDF writer's Unicode backend embeds TrueType fonts from
this directory (see `src/scientific_reproduction/rendering/fonts.py`).

## Not bundled anymore

The Microsoft YaHei files (`msyh.ttc` / `msyhbd.ttc`) used to be bundled
here for the Unicode backend. They are **Windows system fonts and NOT
redistributable** (see license note below), so they are **no longer
shipped with this skill**: `FontConfig.default()` now prefers the host's
system CJK fonts (Windows YaHei, Linux Noto/WenQuanYi, macOS PingFang --
referenced, never copied), then the env override, then this directory as
a compatibility fallback.

**License note**: these are Windows system fonts, bundled here for
**local use on the machine that owns the Windows license**. They are NOT
redistributable. If you ship this skill elsewhere, replace these files
with an open font pair (OFL) — e.g. Noto Sans CJK SC Regular/Bold in
TrueType (glyf) format — and point `SCIENTIFIC_REPRODUCTION_FONT_DIR`
at them (or drop them into this directory with the same file names).

## Override

Environment variable `SCIENTIFIC_REPRODUCTION_FONT_DIR` names a directory
containing `msyh.ttc` and `msyhbd.ttc` (or any TrueType pair you prefer);
it takes precedence over this bundled directory. This pins the font
version and keeps renders byte-identical across machines.

## Why not STSong-Light

PDF standard CID fonts (STSong-Light et al.) need no font file, but they
give no control over the actual glyphs and cannot cover Greek letters and
technical symbols (μm, ℃, ±, ≤, →, ²) that scientific reports need. The
embedded-TrueType backend renders them from one coherent font family.

## Subsetting

The backend subsets the bundled fonts deterministically: only the glyphs
actually used are embedded, so a typical report adds a few hundred KB,
not 20 MB. Subsetting is a pure function of (font file, used-character
sequence), so repeated renders stay byte-identical.
