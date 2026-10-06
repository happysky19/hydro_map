# Reproduce the bounded AORC audit

`audit_aorc_cache.py` rechecks recorded public NOAA payloads offline. It does not download data and is not a production extraction tool.

Requirements: Python with NumPy, Shapely 2, pyproj and netCDF4, plus the `zstd` executable. If netCDF4 is in another Python environment, pass `--netcdf-python /path/to/python`.

```bash
python research/audit_aorc_cache.py \
  --geojson outputs/catchments.geojson \
  --cache-dir outputs/source_validation/aorc_resolution \
  --cache-dir outputs/source_validation/aorc \
  --output outputs/source_validation/aorc_audit \
  --cache-only
```

Use both evidence directories. Each contains payloads and `requests.jsonl` or `sample_requests.json`. Payload locations are resolved relative to each cache directory; original absolute paths are not required. Preserve the resolution cache's `request_file_aliases.json`: one early native filename was corrected after reading its actual NetCDF timestamp. The audit validates every used payload against its recorded SHA-256 and accepts only successful HTTP 200/206 source records. No stored scientific result or previously generated weight file is trusted.

The script rebuilds:

- Every hourly timestamp for 1995–2024 and the first 144 hours of 2025, with all eight variable shapes checked against the coordinate dimensions.
- Fractional cell–polygon weights for the 26 supplied polygons, using actual coordinate axes and EPSG:6933. Complete stored coordinate axes for 1995 and 2025 must agree.
- All eight fields at 1995-01-01 00Z and 2025-01-01 00Z. Fill values are applied before unpacking; decoded ranges and basic physical checks are reported.
- Native NWRFC four-field coverage at 1996-01-12 23Z and 2025-01-01 00Z. The same-hour 2025 comparison checks time units, meteorological units, fill values, scale and offset before requiring raw value equality.

Native files were retrieved with HTTP Range: inspect a 512-byte TAR header, skip directory entries by their padded lengths, then request exactly one regular member. The 1996 TAR is not chronologically ordered: its first regular member is January 12 at 23Z, not January 1. No whole monthly TAR was downloaded.

Zarr meteorological chunks have C-order shape `(144, 128, 256)` and int16 storage. Their first hour occupies the first 65,536 decoded bytes. Bounded compressed prefixes were requested, usually 128 KiB. The audit accepts a decoder's expected truncated-frame result only when the complete first hour was emitted. It cross-checks the prefix against complete cached temperature chunks where available. This does **not** verify the rest of a compressed frame or subsequent hours. Manifest hashes identify the actual downloaded prefixes, not whole remote objects.

`audit_results.json` is written only after all checks pass. A rerun removes the previous result first; failures leave `audit_status.json` marked failed and retain a manifest of verified inputs. Do not interpret a failed run or an old report as a current success.

Passing two sampled hours does not establish complete 30-year meteorological coverage, hydrologic skill, or a global resolution of NOAA's masking notice. The complete calendar plus the verified 2025-01-01 00Z precipitation values closes the UTC endpoint requirement for a 1995–2024 daily window only.
