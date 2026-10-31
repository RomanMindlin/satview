# Israel Satellite Imagery Script — Implementation Plan

Oct 30, 2026 · @Roman

## Goals and scope

One Python command-line script that downloads the latest (or a chosen) satellite scene from each enabled source, and saves a visible-light picture of Israel showing clouds.

**In scope**

- Sources chosen and configured in one YAML file; each source is a pluggable adapter.
- Area fixed in code for now: Israel and surroundings, about 29.0–33.6°N, 33.8–36.2°E, with a small margin.
- Daytime visible composites (true color where the satellite has a blue channel, natural color otherwise).
- Output: PNG per scene, optional animated GIF/MP4 for geostationary loops, coastline and border overlays, timestamp label.
- Runs manually now; later from cron or a scheduled task.

**Out of scope for v1**

- Configurable areas (designed for, not exposed).
- Night-time infrared products, a web UI, cloud-mask or other derived science products.
- Very-high-resolution imagery (Sentinel-2, Landsat).

## Architecture

Adapters only know how to find and download their own data; everything after that is shared, so every picture has the same area, projection and style.

&#91;embedded content: architecture · 5 source adapters, 6 shared pipeline steps\]

The CLI reads the config, runs each enabled adapter, and passes its files through one pipeline that crops to Israel and saves the picture.

## Configuration

All behavior is driven by one `config.yaml`; secrets never go in it, only the names of environment variables that hold them.

```yaml
sources:
  meteosat12_fci:
    enabled: true
    composite: true_color        # true_color | natural_color | vis_06
    resolution: 1km              # 1km (FDHSI) | 500m (HRFI, red only)
    credentials:
      key_env: EUMETSAT_KEY
      secret_env: EUMETSAT_SECRET
  meteosat9_iodc:
    enabled: true
    composite: natural_color     # SEVIRI has no blue channel
    credentials:
      key_env: EUMETSAT_KEY
      secret_env: EUMETSAT_SECRET
  viirs_gibs:
    enabled: true
    layers: [VIIRS_NOAA20_CorrectedReflectance_TrueColor,
             VIIRS_SNPP_CorrectedReflectance_TrueColor]
  viirs_l1b:
    enabled: false
    platforms: [noaa20, noaa21, snpp]
    credentials:
      token_env: EARTHDATA_TOKEN
  sentinel3_olci:
    enabled: false
    credentials:
      user_env: CDSE_USER
      password_env: CDSE_PASSWORD

time:
  mode: latest                   # latest | at | range
  at: null                       # e.g. 2026-10-30T10:00Z
  range: {start: null, end: null, step_minutes: 30}

output:
  dir: ./output
  cache_dir: ./cache
  formats: [png]                 # png, geotiff, gif, mp4
  width_px: 1200
  overlays: {coastlines: true, borders: true, cities: false}
  label: true                    # satellite name + UTC timestamp
  keep_raw_days: 2
```

- Config is validated at startup (pydantic) with clear errors for unknown sources, missing env vars or bad times.
- CLI flags can override any key, e.g. `--source meteosat12_fci --at 2026-10-30T10:00Z`.
- The Israel area lives in `areas.py`, not here, so it can move into config later without breaking the format.

## Source adapters

Each source is a class implementing one small interface, so adding a satellite means adding one file.

```python
class Source(Protocol):
    name: str
    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]: ...
    def download(self, granule: Granule, cache: Path) -> list[Path]: ...
    def load(self, files: list[Path], area: AreaDef) -> Image: ...  # usually via Satpy
```

| Source | Access | Auth | Satpy reader | Composite | New image |
| --- | --- | --- | --- | --- | --- |
| Meteosat-12 FCI (0°) | EUMETSAT Data Store, `eumdac` | Free EUMETSAT key + secret | `fci_l1c_nc` | `true_color` | Every 10 min |
| Meteosat-9 SEVIRI (IODC, 45.5°E) | EUMETSAT Data Store, collection `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | Same as above | `seviri_l1b_native` | `natural_color` | Every 15 min |
| VIIRS true color (GIBS) | NASA GIBS WMS, ready-made imagery | None | Not needed | Pre-rendered | Daily, \~midday pass |
| VIIRS L1B (S-NPP, NOAA-20/21) | NASA Earthdata, `earthaccess` (VNP02IMG / VJ102IMG + geolocation) | Free Earthdata token | `viirs_l1b` | `true_color` | 1–3 daytime passes/day |
| Sentinel-3 OLCI | Copernicus Data Space, OData API (`OL_1_EFR___`) | Free CDSE account | `olci_l1b` | `true_color` | \~1 morning pass/day |

**Adapter notes**

- **Meteosat-12 FCI.** A full-disk FDHSI product is split into 40 BODY chunk files plus a TRAIL file. Download only the chunks whose rows cover Israel, a few files instead of the whole disk; compute the chunk list once from the FCI grid and cache it. Collection ID for FDHSI is believed to be `EO:EUM:DAT:0662` (HRFI `EO:EUM:DAT:0665`): confirm with `eumdac describe` in phase 1. Load with `upper_right_corner='NE'` and `import hdf5plugin` for decompression.
- **Meteosat-9 IODC.** One native `.nat` file per scan covering the full disk (a few hundred MB). Optionally use the Data Tailor (`--tailor` with an ROI) to cut the download server-side; evaluate in phase 2.
- **VIIRS via GIBS.** The fastest win: one HTTP request per day returns a georeferenced true-color PNG/GeoTIFF for the Israel bbox. Good for v1 and as a sanity check against our own processing.
- **VIIRS L1B and OLCI.** Larger raw granules (hundreds of MB) with the best detail; search by bbox and time, skip night passes and granules that only clip the area. Lower priority than the geostationary sources.

## Processing pipeline

Every source ends in the same Satpy-based steps, so all outputs share one projection, size and style and can be compared side by side.

1. **Daylight check.** Compute the solar zenith angle at the area centre (31.5°N, 35.0°E) for the scan time; skip if the sun is below \~5° elevation, since visible channels are dark. Log the skip rather than failing.
2. **Load.** `Scene(reader=..., filenames=...)`, then `scn.load([composite])`. Satpy applies calibration, Rayleigh correction and the green-band simulation for FCI true color.
3. **Resample to the Israel area.** One hardcoded `AreaDefinition` in `areas.py`:
   - Projection: local equirectangular or UTM 36N (EPSG:32636), chosen once and kept for every source.
   - Extent: 33.8–36.2°E, 29.0–33.6°N (Eilat to the Golan, plus the coast and some sea).
   - Grid: \~500 m pixels (about 450 × 1000 px); upscaling 1 km geostationary data is fine for viewing.
   - Resampler: `nearest` first; try `bilinear` for smoother geostationary images.
4. **Enhance.** Satpy's default enhancement for the composite, plus an optional gamma or contrast tweak per source in config so the satellites look alike.
5. **Render.** `pycoast` or `cartopy` adds coastlines and borders; Pillow adds a label such as `Meteosat-12 FCI · true color · 2026-10-30 10:00 UTC`.
6. **Save.** `output/<source>/<YYYY-MM-DD>/<source>_<YYYYMMDDTHHMM>.png`, optional GeoTIFF. Skip work if the file already exists (idempotent reruns).
7. **Animate (optional).** For a time range on a geostationary source, collect the PNGs and build a GIF/MP4 with `imageio` or ffmpeg.

At Israel's longitude, Meteosat-12 views at a slant (\~45° from vertical), so high cloud tops appear shifted up to \~10 km north-east of their true position. Meteosat-9 shows less of this. Accept it for v1; parallax correction is a later option (Satpy has one).

## Project structure, dependencies and CLI

A small installable Python package (`satview`) with one entry point; Python 3.11+, managed with a `pyproject.toml`.

```text
satview/
  pyproject.toml
  config.example.yaml
  satview/
    cli.py            # argparse/typer entry point
    config.py         # load + validate YAML, env-var secrets
    areas.py          # hardcoded Israel AreaDefinition
    pipeline.py       # daylight check, load, resample, enhance, save
    render.py         # overlays, labels, animation
    cache.py          # download cache, cleanup by age
    sources/
      base.py         # Source protocol, Granule, TimeSpec
      meteosat12_fci.py
      meteosat9_iodc.py
      viirs_gibs.py
      viirs_l1b.py
      sentinel3_olci.py
  tests/
```

**Dependencies:** `satpy`, `pyresample`, `hdf5plugin`, `eumdac`, `earthaccess`, `requests`, `pycoast` (or `cartopy`), `pillow`, `imageio`, `pyyaml`, `pydantic`, `pyorbital` (sun angle). Conda-forge is the easiest install route for Satpy's compiled dependencies.

**CLI**

```bash
satview run                              # all enabled sources, latest scene
satview run --source meteosat12_fci      # one source
satview run --at 2026-10-30T10:00Z
satview run --start 2026-10-30T06:00Z --end 2026-10-30T14:00Z --animate
satview list-sources                     # enabled sources + credential check
satview clean                            # remove cached raw data older than keep_raw_days
```

Exit code is non-zero only if every enabled source failed; per-source errors are logged and the rest continue.

## Implementation phases

Build in five phases, each ending with something that produces a picture; the first useful image arrives in phase 1.

1. **Skeleton + quick win.** Config loading, CLI, Israel area, output layout, and the `viirs_gibs` adapter (no auth, no Satpy). Done when `satview run` saves today's VIIRS true-color PNG of Israel.
2. **Meteosat-12 FCI.** EUMETSAT credentials, `eumdac` search for the latest FDHSI product, chunk selection for Israel, Satpy load, resample, daylight check, overlays and labels. Done when a 10-minute-old true-color image of Israel is produced in under \~2 minutes.
3. **Meteosat-9 IODC.** Second geostationary adapter reusing the same pipeline; tune enhancement so both look comparable. Done when both satellites render the same time slot side by side.
4. **Time ranges + animation.** `--at`, `--start/--end`, caching, idempotent reruns, GIF/MP4 output, `clean` command. Done when a daytime loop (e.g. 06:00–14:00 UTC) builds from cache without re-downloading.
5. **Polar orbiters (optional).** `viirs_l1b` and `sentinel3_olci` adapters with bbox/time search and night-pass filtering. Done when the best daily pass is picked automatically.

After phase 5: schedule it (cron or a scheduled task), then move the area into config.

## Risks, testing and open questions

The main risks are data volume and source outages; both are handled by partial downloads, caching and per-source failure isolation.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Large downloads (full-disk files) | Slow runs, disk fills | FCI chunk selection, Data Tailor ROI for SEVIRI, `keep_raw_days` cleanup |
| Satellite or data-store outages (Meteosat-12 had several multi-day FCI outages in 2025) | Missing images | Per-source isolation, retries with backoff, fall back to the other geostationary source |
| Satpy reader changes (FCI reader is still marked Beta) | Breakage on upgrade | Pin Satpy version, upgrade deliberately with a regression image |
| Collection IDs or API changes | Search returns nothing | IDs in one constants file, `list-sources` checks them at startup |
| Night-time runs | Black images | Daylight check skips with a log message |
| Credential leaks | Account misuse | Env vars only, `config.yaml` holds no secrets, `.env` in `.gitignore` |

**Testing**

- Unit tests for config validation, time selection, chunk selection and file naming.
- Recorded small sample files per source (cropped) for offline pipeline tests.
- A regression test that renders a fixed scene and compares it to a stored PNG within a tolerance.
- One manual end-to-end run per source before each phase is marked done.

**Open questions**

- Where will it run (laptop, home server, cloud) and how often? This sets cache size and scheduling.
- Preferred projection: equirectangular (simple) or UTM 36N (true distances)?
- Should outputs also be published somewhere (a web page, Telegram, a shared folder)?
- Is Meteosat-11's 5-minute rapid scan worth adding if it covers Israel?

**Sources**

- [Meteosat-12 begins prime service (SpaceDaily)](https://www.spacedaily.com/reports/Meteosat_12_begins_prime_service_delivering_enhanced_weather_data_for_Europe_999.html)
- [EUMETSAT MSG services: IODC by Meteosat-9](https://eumetsat.int/msg-services)
- [Satpy FCI L1c natural color example](https://satpy.readthedocs.io/en/latest/examples/fci_l1c_natural_color.html)
- [Satpy FCI L1c reader API](https://satpy.readthedocs.io/en/stable/api/satpy.readers.fci_l1c_nc.html)
- [Satpy issue on FCI chunk files (40 BODY + TRAIL)](https://github.forks.workers.dev/pytroll/satpy/issues/3076)
- [EUMETSAT eumdac training notes, incl. Data Tailor](https://coemct.met.gov.om/mod/resource/view.php?id=1696)
- [NOAA notice: HRSEVIRI-IODC collection reference](https://www.ssd.noaa.gov/PS/SATS/MESS/MSG0681448.28.txt)
- [NOAA notice: Meteosat-12 FCI outage, July 2025](https://ospo.noaa.gov/data/messages/2025/07/MSG_20250724_1052.html)
