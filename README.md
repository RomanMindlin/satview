# satview

Downloads the latest (or a chosen) satellite scene from each enabled source and saves a
visible-light picture of Israel showing clouds: same area, projection and style for every
satellite, with coastlines, borders and a timestamp label. Two areas: all of Israel (`israel`,
the default) or the same picture with everything south of Mitzpe Ramon cut off (`israel_north`).

| Source | Satellite / data | Login | New image |
| --- | --- | --- | --- |
| `meteosat12_fci` | Meteosat-12 FCI true color, EUMETSAT Data Store | EUMETSAT key + secret | hourly (10-min scans an hour later) |
| `meteosat9_iodc` | Meteosat-9 SEVIRI natural color (45.5°E) | same | hourly (15-min scans an hour later) |
| `viirs_gibs` | VIIRS true color, ready-made from NASA GIBS | none | daily, ~midday |
| `viirs_l1b` | VIIRS L1B (S-NPP, NOAA-20/21) via NASA Earthdata | Earthdata login | 1-3 passes/day |
| `sentinel3_olci` | Sentinel-3 OLCI, Copernicus Data Space | CDSE account | ~1 morning pass/day |
| `sentinel2_msi` | Sentinel-2A/B/C MSI L2A (10 m), Earth Search on AWS | none | full pass every 2-5 days, ~5 h later |
| `landsat_oli` | Landsat 8/9 OLI L2 (30 m), Microsoft Planetary Computer | none | full pass every 8 days, 1-2 weeks later |

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync                      # creates .venv with all dependencies
cp config.example.yaml config.yaml
cp .env.example .env         # then fill in the credentials you have
```

Credentials are read from environment variables, or from a `.env` file next to the config.
`config.yaml` only names the variables. Free accounts:

- EUMETSAT: <https://api.eumetsat.int/api-key/> (key + secret). Without an NRT licence the
  Data Store refuses scans under an hour old except the hourly ones (403 "NRTLicense required"),
  so `latest` gets the hourly scan ~10-15 min after the hour; the others are skipped until they
  are an hour old.
- NASA Earthdata: <https://urs.earthdata.nasa.gov> (username + password, or a token). For
  `viirs_l1b` with `nrt: true`, the LANCE near-real-time server also wants its licence accepted
  once in a browser: `satview list-sources` prints a file link to open, log in and accept.
- Copernicus Data Space: <https://dataspace.copernicus.eu> (username + password)

Sentinel-2 and Landsat need no account. Landsat Level-1, out within hours instead of weeks,
needs a USGS login (M2M access) and is not supported yet.

## Usage

```bash
uv run satview run                              # all enabled sources, latest scene
uv run satview run --source meteosat12_fci      # one source (even if disabled in the config)
uv run satview run --area israel_north          # the same picture, cut south of Mitzpe Ramon
uv run satview run --source sentinel2_msi --set output.width_px=null   # full 100 m detail
uv run satview run --at 2026-10-30T10:00Z
uv run satview run --start 2026-10-30T06:00Z --end 2026-10-30T14:00Z --animate
uv run satview run --format png --format geotiff --set output.width_px=800
uv run satview list-sources                     # enabled sources + credential and API check
uv run satview clean                            # delete cached raw data older than keep_raw_days
```

`--set KEY=VALUE` overrides any config key (`sources.viirs_l1b.pick=all`,
`output.overlays.cities=true`, ...). Add `-v` for debug output, `-vv` to include library logs.

Pictures land in `output/<source>/<YYYY-MM-DD>/<source>[_<satellite>]_<YYYYMMDDTHHMM>.png`;
animations in `output/<source>/`. Daily pictures (`viirs_gibs`) carry only the date and get a
folder per satellite, `output/<source>_<satellite>/`, so each folder's newest picture is that
satellite's latest. Areas other than `israel` get their own folder,
`output/<area>/<source>/...`. Reruns skip scenes that already exist (`--force` redoes them),
and raw downloads are cached in `cache/`, so a range rerun rebuilds from cache.

At night the visible channels are dark: scenes with the sun below 5° are skipped with a log
message. Exit code is 0 if at least one source worked, 1 if all failed, 2 for config errors.

### Scheduling

```cron
*/15 6-16 * * *  cd /path/to/satweather && .venv/bin/satview run >> satview.log 2>&1
0 3 * * *        cd /path/to/satweather && .venv/bin/satview clean >> satview.log 2>&1
```

### Docker

The image runs cron in the foreground with the schedule in `crontab`, in Israel time (the
image sets `TZ=Asia/Jerusalem`, so summer time is followed): `satview run` at minute 20 and
`telegram_sender.py output/israel_north` at minute 45 of every hour from 07:00 to 16:00, and
`satview clean` daily at 03:00. `config.yaml` and `.env` are mounted into `/app`; `output/`
and `cache/` are bind-mounted so they outlive the container. Job output goes to
`docker logs satview`.

```bash
docker compose up -d --build
docker logs -f satview
docker exec satview sh -c 'cd /app && .venv/bin/satview run'   # a run now, without waiting
```

Pushes to `main` deploy through `.github/workflows/deploy.yml`: it writes `.env` from
`.env.template` and the repository secrets (`EUMETSAT_KEY`, `EUMETSAT_SECRET`,
`EARTHDATA_TOKEN` or `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`, `CDSE_USER`,
`CDSE_PASSWORD`, `TG_BOT_TOKEN`, `TG_CHANNEL_ID`), copies the checkout to
`/home/mindlin/satview` over SSH (`SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SEHEL_SSH_KEY`) and
runs `docker compose up -d --build` there. `config.yaml` is not in git: put it in that folder
once, with `area: israel_north` to match the sender.

## How it works

Each source in `satview/sources/` implements `find` (search), `download` (into the cache) and
`load` (to an RGBA image on the target area, usually with Satpy). Everything after that is in
`satview/pipeline.py` and `satview/render.py` and shared by all sources.

- **Areas**: `satview/areas.py`, in UTM 36N at 500 m, upscaled to `width_px` for the PNG.
  `israel`: 33.8-36.2°E, 29.0-33.6°N (476 x 1029 px); `israel_north`: the top 697 rows of the
  same grid, down to 30.5°N, so its picture is an exact crop of the `israel` one. Searches, pass
  coverage and the daylight check use the selected area; raw downloads are shared between areas,
  while the crops from GIBS, Sentinel-2 and Landsat are cached per extent.
- **Meteosat-12**: only the 4 of 40 full-disk chunk files covering Israel are downloaded
  (~115 MB instead of ~1 GB).
- **Polar orbiters**: granules are grouped into passes; the pass covering most of the area,
  closest to nadir, wins (`pick: all` keeps every daytime pass). The swath is cut to the area
  before Satpy builds the composite, to keep memory low. OLCI downloads only the 3 bands and
  geometry files true color needs (~140 MB per frame instead of ~700 MB).
- **Sentinel-2 / Landsat** (`satview/sources/stac.py`): found through STAC catalogues; tiles of
  one pass are mosaicked. Their files are cloud-optimized GeoTIFFs, so only the area is read,
  from the built-in overview just finer than the grid (~130 MB for a 24-tile Sentinel-2 pass,
  in ~30 s). They are drawn on a finer grid, `resolution_m` (default 100 m, same extent and
  projection). The PNG is still scaled to `output.width_px`: for full detail use the GeoTIFF or
  `--set output.width_px=null`. Colors come from the red, green and blue surface reflectances
  with Satpy's true color stretch, as for VIIRS and OLCI; ESA's ready-made image would make
  bright desert as white as cloud. Faint seams between Sentinel-2 tiles come from ESA's
  per-tile atmospheric correction.
- **Overlays**: Natural Earth 1:10m coastlines, lakes, borders and cities, downloaded once to
  `cache/naturalearth/`.
- Meteosat-12 views Israel at a slant, so high cloud tops appear shifted up to ~10 km
  north-east (parallax, not corrected).

## Tests

```bash
uv run --extra dev pytest
```

Offline: a recorded GIBS sample drives the pipeline, and `tests/data/regression_gibs.png` is
the rendering reference. After an intended visual change, regenerate it with
`SATVIEW_UPDATE_REFERENCE=1 uv run --extra dev pytest tests/test_pipeline.py`.
