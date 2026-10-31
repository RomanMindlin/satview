# How satview works

satview downloads recent satellite scenes from seven sources and turns each one into the same
kind of picture: a visible-light view of Israel showing clouds. Every picture has the same area,
the same map projection and the same style, with coastlines, borders and a timestamp label.

This document explains the whole path from a `satview run` command to a PNG on disk:

- what the program asks each satellite data server, and what comes back;
- the file formats it reads;
- the algorithms that choose scenes, correct the physics and build the colours;
- how the final picture is drawn and saved.

The [README](README.md) covers installation and day-to-day use. Code references below point to
the modules in [satview/](satview/).

---

## 1. The big picture

```
 satview run
     │
     ▼
 config.yaml + .env + CLI flags ──► Config (validated)            satview/config.py
     │
     ▼
 target grid: UTM 36N, 500 m (or finer)                           satview/areas.py
     │
     ▼  for every enabled source
 ┌───────────────────────── source adapter ─────────────────────┐ satview/sources/*.py
 │ find()      search the server's catalogue, pick the scenes    │
 │ download()  fetch only the files (or byte ranges) needed      │ → cache/<source>/…
 │ load()      calibrate, correct, compose RGB, resample, stretch│ → Image (RGBA on the grid)
 └───────────────────────────────────────────────────────────────┘
     │
     ▼  shared by every source                                    satview/pipeline.py
 skip if already rendered · skip at night · optional colour tweak
     │
     ▼                                                            satview/render.py
 black background · scale to width_px · coastlines/borders/cities · label
     │
     ▼
 output/<source>/<date>/<source>[_<sat>]_<YYYYMMDDTHHMM>.png  (+ .tif, .gif, .mp4)
```

Each source module knows how to talk to one data server and read one file format. Everything
after `load()` is shared, so all satellites produce pictures that can be compared side by side.

### The seven sources

| Source | Satellite / instrument | Orbit | Server | Login |
| --- | --- | --- | --- | --- |
| `meteosat12_fci` | Meteosat-12, FCI | geostationary, 0° | EUMETSAT Data Store | API key + secret |
| `meteosat9_iodc` | Meteosat-9, SEVIRI | geostationary, 45.5°E | EUMETSAT Data Store | API key + secret |
| `viirs_gibs` | NOAA-20 / Suomi NPP, VIIRS (ready-made image) | polar | NASA GIBS (WMS) | none |
| `viirs_l1b` | Suomi NPP, NOAA-20, NOAA-21, VIIRS | polar | NASA CMR + LAADS / LANCE | Earthdata |
| `sentinel3_olci` | Sentinel-3A/B, OLCI | polar | Copernicus Data Space (OData) | CDSE account |
| `sentinel2_msi` | Sentinel-2A/B/C, MSI L2A | polar | Earth Search on AWS (STAC + COG) | none |
| `landsat_oli` | Landsat 8/9, OLI L2 | polar | Microsoft Planetary Computer (STAC + COG) | anonymous token |

A geostationary satellite stays above one point on the equator and scans the whole visible
disk of the Earth every 10–15 minutes. A polar orbiter flies low and sees each place once or
twice a day, but in much more detail. The two kinds need different search logic (section 4).

---

## 2. One run, step by step

### 2.1 Configuration

[config.py](satview/config.py) builds one validated `Config` object:

1. It reads `config.yaml`. The file never holds secrets, only the *names* of the environment
   variables that hold them (for example `key_env: EUMETSAT_KEY`).
2. It loads a `.env` file next to the config into the environment. Variables already set in
   the environment win.
3. It applies the CLI flags as dotted keys on top of the YAML: `--set output.width_px=800`,
   `--at …` becomes `time.mode=at` plus `time.at=…`, `--source X` turns on X and turns off all
   other sources, and so on.
4. Pydantic checks the result. Unknown keys are errors. Times are parsed as UTC. A time range
   may not have more than 1000 steps, and a time in the future is rejected.
5. Relative paths are resolved against the config file's folder, so a cron job writes to the
   same place as a manual run. Areas other than `israel` get their own subfolder,
   `output/<area>/`.

Before any network traffic, `check_credentials()` checks that the environment variables of the
selected sources are set. If one is missing, the program stops with exit code 2.

### 2.2 Time selection

`TimeSpec` ([base.py](satview/sources/base.py)) describes what the user asked for:

| Mode | Meaning | Target times ("slots") |
| --- | --- | --- |
| `latest` | the newest scene available now | `[now]` |
| `at` | the scene nearest a given UTC time | `[at]` |
| `range` | one scene per step between start and end | `start, start+step, …, end` |

Every source turns this into a search window on its server (section 4). For geostationary
sources, `select_times()` then picks:

- for `latest`, the newest product;
- otherwise, for each slot, the product nearest to it, but only if it is within a tolerance
  (10 min for FCI, 15 min for SEVIRI). The same product is never used twice, so a 5-minute step
  over 15-minute data does not give duplicate frames.

### 2.3 The target grid

All sources are resampled to one grid defined in [areas.py](satview/areas.py):

- **Projection:** UTM zone 36N (EPSG:32636). Distances are true in metres, and GeoTIFF readers
  know the EPSG code.
- **Areas:**

  | Area | lon/lat box | Grid at 500 m | PNG at `width_px: 1200` |
  | --- | --- | --- | --- |
  | `israel` | 33.8–36.2°E, 29.0–33.6°N | 476 × 1029 px | 1200 × 2594 px |
  | `israel_north` | 34.0–36.0°E, 30.4–33.6°N | 392 × 717 px | 1200 × 2195 px |

- **How the extent is computed:** the four edges of the lon/lat box are sampled with 50 points
  each, so that curved edges survive the reprojection. The points are projected to UTM. The
  minimum and maximum x and y are then rounded outwards to whole pixels. For `israel` this
  gives x 574 000–812 000 m and y 3 208 000–3 722 500 m.
- **Finer grids:** Sentinel-2 and Landsat use the same extent at `resolution_m` (default
  100 m, so 2376 × 5142 px for `israel`). The pictures still line up with the other sources.

The grid is a pyresample `AreaDefinition`, the object Satpy resamples to.

### 2.4 Per-scene pipeline

`pipeline.process()` ([pipeline.py](satview/pipeline.py)) handles one scene (a "granule"):

1. **Already done?** If every requested output file exists, the scene is skipped (status
   `exists`). `--force` turns this off.
2. **Daylight check.** The sun's elevation is computed with `pyorbital.astronomy.sun_zenith_angle`
   at the area's centre (35.0°E 31.5°N for `israel`). Below 5° the visible channels are too dark
   to be useful, so the scene is skipped (status `night`). Daily composites (GIBS) skip this
   check.
3. **Download** into `cache/<source>/<granule id>/` (source-specific, section 3).
4. **Load** into an `Image`: a `(rows, cols, 4)` uint8 RGBA array on the target grid. Alpha 0
   means "no data" (section 5).
5. **Optional colour tweak** (`enhance:` in the config, section 5.7).
6. **Render** and save the PNG, and/or save the GeoTIFF (section 6).

For a time range, `pipeline.animate()` then builds a GIF and/or MP4 from the PNGs.

---

## 3. Network exchange with the satellite servers

### 3.1 The shared HTTP layer

[net.py](satview/net.py) and [cache.py](satview/cache.py) give every source the same
behaviour:

- **One `requests.Session`** with the header `User-Agent: satview/0.1.0`. Timeouts are 15 s to
  connect and 120 s to read.
- **Transport retries** (urllib3 `Retry`): up to 4 retries for GET and POST on HTTP 429, 500,
  502, 503 and 504, with exponential backoff (factor 2).
- **Operation retries** (`net.retry`): a whole search or download is tried 3 times, waiting 5 s
  and then 10 s. This also covers client libraries that have no retries of their own (eumdac,
  earthaccess).
- **Atomic downloads** (`cached_file`): data is streamed in 1 MiB chunks into `<name>.part`.
  The file is renamed to `<name>` only after it is complete. An interrupted download therefore
  never looks like a cached file, and the next run starts it again.
- **Login pages are detected.** If a server answers with `Content-Type: text/html` instead of
  data, the download fails with a clear message. This usually means that a redirect led to a
  login or licence page.
- **Authorization on redirects.** By default `requests` drops the `Authorization` header when
  a redirect changes host. LAADS (VIIRS) needs exactly this: it redirects to storage that rejects
  the header. CDSE (OLCI) needs the opposite. With `keep_auth_on_redirect=True`, satview follows
  up to 5 redirects itself and sends the header each time.

### 3.2 EUMETSAT Data Store (Meteosat-12 FCI, Meteosat-9 SEVIRI)

Shared code: [eumetsat.py](satview/sources/eumetsat.py). It uses the `eumdac` library, which
makes these HTTP calls:

| Step | Request | Notes |
| --- | --- | --- |
| Token | `POST https://api.eumetsat.int/token`, HTTP Basic auth `key:secret`, form `grant_type=client_credentials` | Returns a bearer token with `expires_in`. eumdac renews it before it expires. |
| Search | `GET https://api.eumetsat.int/data/search-products/1.0.0/os?format=json&pi=<collection>&dtstart=…&dtend=…&si=0&c=<page size>` | OpenSearch. Returns GeoJSON features, one per product. Times are sent as naive UTC (`2026-10-31T10:00:00`). |
| File list | `GET https://api.eumetsat.int/data/browse/1.0.0/collections/<collection>/products/<id>?format=json` | Lists the files ("entries") inside the product. |
| Download one file | `GET https://api.eumetsat.int/data/download/1.0.0/collections/<collection>/products/<id>/entry?name=<file>` | Bearer token. satview downloads single files, never the product zip. |

Collections used ([constants.py](satview/sources/constants.py)):

- `EO:EUM:DAT:0662`: FCI Level 1c, normal resolution (FDHSI, 1 km visible), 0°.
- `EO:EUM:DAT:0665`: FCI Level 1c, high resolution (HRFI, 500 m red), 0°.
- `EO:EUM:DAT:MSG:HRSEVIRI-IODC`: SEVIRI Level 1.5 over the Indian Ocean.

FCI products are also filtered on `MTI1+` in the product id (Meteosat-12). Later MTG-I
satellites may share the collection.

#### The licence rule

A free EUMETSAT account can *search* all products. It can *download* in real time only the
hourly scans (those starting at minute 00). In the EUMETSAT data policy, these hourly scans
are "Core" data. Every other scan returns `403 "NRTLicense required"` until it is one hour old.
`downloadable()` therefore keeps a product if its scan started at minute 0, or if it started at
least 61 minutes ago (one hour plus clock slack). Search windows for `latest` are made wide
enough to always contain such a product:

- FCI: now − (40 min + 61 min) … now
- SEVIRI: now − (60 min + 61 min) … now

In `at`/`range` mode the window is the requested time(s) ± 10 min (FCI) or ± 15 min (SEVIRI).

#### Meteosat-12 FCI: downloading only the chunks over Israel

An FCI full-disk product is split into 40 "BODY" chunk files, numbered from south (1) to north
(40), plus a "TRAIL" file. In all it is about 1 GB. Israel lies in only a few of the row bands.
`fci_chunks()` ([meteosat12_fci.py](satview/sources/meteosat12_fci.py)) finds them:

1. Put a 20 × 20 grid of points over the area's lon/lat bounds.
2. Project the points into FCI's geostationary projection
   (`+proj=geos +lon_0=0 +h=35786400 +ellps=WGS84 +sweep=y`) and keep the `y` coordinate. If a
   point is not visible from 0°, the result is not finite, and the area is rejected.
3. Turn `y` into a fraction of the disk height: `f = (y + 5 568 000) / (2 × 5 568 000)`, where
   0 is the south edge. 5 568 km is half the full-disk extent, from Satpy's FCI area definition.
4. Chunk number = `floor(f × 40) + 1`. Take the range from the smallest to the largest, plus
   one extra chunk on each side, because the real chunk heights differ by a few rows.

For both areas this gives chunks **30–33**: 4 of 40 files, about 29 MB each, so ~115 MB per scan
instead of ~1 GB. The file names match `…-BODY-…_00NN.nc`. The TRAIL file is not needed.

#### Meteosat-9 SEVIRI

Each IODC scan is one `.nat` file covering the full disk (~180 MB). It cannot be split, so it is
downloaded whole.

#### Health check

`satview list-sources` asks for a token (which fails on bad credentials) and reads the
collection's title.

### 3.3 NASA GIBS (`viirs_gibs`)

GIBS serves ready-made daily true-colour mosaics through a public OGC WMS. No login is needed.
[viirs_gibs.py](satview/sources/viirs_gibs.py) makes one kind of request:

```
GET https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi
    ?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0
    &LAYERS=VIIRS_NOAA20_CorrectedReflectance_TrueColor&STYLES=
    &CRS=EPSG:4326&BBOX=<lat_min>,<lon_min>,<lat_max>,<lon_max>   ← WMS 1.3: latitude first
    &WIDTH=1065&HEIGHT=1890&FORMAT=image/png&TRANSPARENT=TRUE
    &TIME=2026-10-30
```

- **Box:** the target grid's lon/lat bounds, padded by 0.02° and rounded to 0.01°, so that the
  box (and the cache key) is stable. For `israel` it is 33.73–36.39°E, 28.94–33.66°N.
- **Size:** 0.0025° per pixel (~250 m, about the layer's native resolution), so 1065 × 1890 px
  for `israel`.
- **Response:** a PNG in plate carrée (a regular lon/lat grid). Missing data is transparent or
  black. If the response is not an image, the text of the error is shown.
- **Which day for `latest`:** the image over Israel comes from the midday pass (~10–12 UTC). In
  the morning, today's image is still empty. satview first asks for a 48-px-wide thumbnail of
  today. If at least 95 % of it has data, it uses today; otherwise it uses yesterday.
- **Coverage rules:** a cached image that covers less than 95 % of the area was saved while the
  day's passes were still arriving, so it is fetched again. An image with less than 50 % data is
  rejected.
- The area name is part of the granule id, so each area's crop is cached separately.

### 3.4 NASA Earthdata (`viirs_l1b`)

[viirs_l1b.py](satview/sources/viirs_l1b.py) downloads raw VIIRS Level-1B swath files.

**Authentication.** It uses the token from `EARTHDATA_TOKEN`. If that is not set, it creates
one:

```
POST https://urs.earthdata.nasa.gov/api/users/find_or_create_token
     HTTP Basic auth username:password, Accept: application/json
→ {"access_token": "<JWT>", …}
```

**Search.** `earthaccess.search_data()` queries NASA's Common Metadata Repository (CMR) for
granules by `short_name`, bounding box (the area's lon/lat box), time window and
`day_night_flag=day`. All result pages are read (`count=-1`). Two collections are searched for
each satellite:

| Satellite | Radiances (M-bands) | Geolocation |
| --- | --- | --- |
| Suomi NPP | `VNP02MOD` | `VNP03MOD` |
| NOAA-20 | `VJ102MOD` | `VJ103MOD` |
| NOAA-21 | `VJ202MOD` | `VJ203MOD` |

With `nrt: true`, the names get an `_NRT` suffix. The files then come from LANCE, the
near-real-time system, ~3 h after the pass instead of ~6–12 h.

satview needs the M-bands (750 m), because the I-band files (`02IMG`, 375 m) have no blue
channel. Each radiance granule is paired with its geolocation granule through the time key in
the file name, for example `VJ102MOD.A2026303.1130.021.….nc` → `A2026303.1130` (year, day of
year, HHMM). A granule without its pair is skipped. From the UMM-JSON metadata, satview takes
the start and end times (`TemporalExtent`) and the footprint (`GPolygons` or
`BoundingRectangles`). These are used to choose a pass (section 4.2).

**Download.** For every granule of the chosen pass, satview fetches the first data link, with
`Authorization: Bearer <token>`. The data server redirects to storage on another host, and
`requests` drops the header there, which the storage requires. A 6-minute granule is about
200 MB of radiances plus 55 MB of geolocation. A pass over Israel usually takes two granules,
so about 500 MB.

**Health check.** It verifies that the collections exist in CMR. It decodes the token (a JWT)
to see when it expires. Then it sends a `GET` without following redirects to a recent granule
URL and reads only the headers:

- a redirect to `…/profiles/licenses` means that the LANCE licence has not been accepted yet
  (open the URL once in a browser);
- 401/403, or a redirect to `urs.earthdata.nasa.gov`, means that the token was rejected.

`GET` is used instead of `HEAD` because LANCE answers `HEAD` with 200 either way.

### 3.5 Copernicus Data Space Ecosystem (`sentinel3_olci`)

[sentinel3_olci.py](satview/sources/sentinel3_olci.py) uses CDSE's OData API directly.

**Search** (no login):

```
GET https://catalogue.dataspace.copernicus.eu/odata/v1/Products
    ?$filter=Collection/Name eq 'SENTINEL-3'
        and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'
            and att/OData.CSC.StringAttribute/Value eq 'OL_1_EFR___')
        and OData.CSC.Intersects(area=geography'SRID=4326;POLYGON((…area box…))')
        and ContentDate/Start gt 2026-10-29T10:00:00.000Z
        and ContentDate/Start lt 2026-10-31T10:00:00.000Z
    &$orderby=ContentDate/Start desc&$top=1000
```

`OL_1_EFR___` is OLCI Level-1B, Earth-observation Full Resolution (300 m). Each result has a
`Name` (`S3A_OL_1_EFR____<start>_<end>_<created>_…_NR_004.SEN3`), an `Id`, `ContentDate`, a
GeoJSON `GeoFootprint` and an `Online` flag.

The same frame is often published up to three times, as processing improves:

- NR (near real time, hours);
- ST (short time critical, days);
- NT (non time critical, weeks).

For each (satellite, start time), satview keeps the best one: NT > ST > NR. Offline products are
skipped.

**Token** (only when a file is actually missing from the cache):

```
POST https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token
     form: client_id=cdse-public, grant_type=password, username=…, password=…
→ {"access_token": …}
```

**Download of single files.** A full frame is ~700 MB, but true colour needs only 3 of the 21
bands plus geometry. satview asks for each file through the product's node tree:

```
GET https://download.dataspace.copernicus.eu/odata/v1/Products(<Id>)/Nodes(<Name>)/Nodes(<file>)/$value
    Authorization: Bearer <token>        (kept on every redirect)
```

The files are:

- `Oa03_radiance.nc`, `Oa06_radiance.nc`, `Oa08_radiance.nc` (442, 560 and 665 nm);
- `geo_coordinates.nc` (latitude and longitude of every pixel);
- `tie_geometries.nc` (sun and view angles, for the Rayleigh correction);
- `instrument_data.nc` (solar flux, to turn radiance into reflectance).

That is ~150 MB per 3-minute frame. The files are stored inside a `<Name>.SEN3/` folder, as
Satpy's reader expects.

**Health check:** logs in and counts the frames over the area in the last 3 days.

### 3.6 STAC catalogues and cloud-optimized GeoTIFFs (`sentinel2_msi`, `landsat_oli`)

[stac.py](satview/sources/stac.py) serves both sources. They differ only in catalogue,
collection and platform names:

| | Sentinel-2 | Landsat |
| --- | --- | --- |
| Catalogue | `https://earth-search.aws.element84.com/v1` | `https://planetarycomputer.microsoft.com/api/stac/v1` |
| Collection | `sentinel-2-c1-l2a` (Level-2A surface reflectance) | `landsat-c2-l2` (Collection 2 Level-2 surface reflectance) |
| Native pixels | 10 m | 30 m |
| Tile size | 110 km (MGRS tile) | 185 km (WRS-2 scene) |
| `latest` looks back | 10 days | 30 days |
| `at` tolerance | ± 3 days | ± 8 days |
| Access to files | public | an anonymous SAS token |

**Search** (STAC API, no login):

```
POST <catalogue>/search
{"collections": ["sentinel-2-c1-l2a"], "bbox": [33.8, 29.0, 36.2, 33.6],
 "datetime": "2026-10-21T10:00:00Z/2026-10-31T10:00:00Z", "limit": 100}
```

The response is a GeoJSON FeatureCollection. Each feature ("item") is one tile, with
`properties.datetime`, `properties.platform` (`sentinel-2b`, `landsat-9`, …), a footprint
`geometry`, and `assets` named `red`, `green`, `blue`. Each asset has the URL of a single-band
COG file and a `raster:bands` entry with `scale` and `offset`. More pages are followed through
the `next` link. Its `body` may be partial (`"merge": true`), and in that case it is merged
into the previous body.

**Planetary Computer token.** File URLs on Azure Blob Storage need a short-lived SAS token. It is
free and anonymous:

```
GET https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2
→ {"token": "st=…&se=…&sig=…", "msft:expiry": "2026-10-31T11:00:00Z"}
```

The token is appended to every file URL (`<href>?<token>`). It is renewed 5 minutes before it
expires. It is fetched once, before the parallel reads start.

**Reading only what is needed.** A cloud-optimized GeoTIFF (COG) stores its pixels in internal
tiles. It also stores *overviews*: copies at 1/2, 1/4, 1/8, … resolution. Its header lists the
byte offsets of all of them. rasterio/GDAL opens an `https://` URL through `/vsicurl/` and reads
only the byte ranges it needs, with HTTP `Range` requests. `read_tile()` works as follows:

1. Open the three band files (red, green, blue) of a tile. GDAL is told not to look for sidecar
   files (`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`). It may merge neighbouring ranges and send
   several ranges in one request, and it retries 4 times.
2. Choose the overview: the largest decimation factor `f` with `f × native pixel ≤ grid
   resolution`. For Sentinel-2 at a 100 m grid, this is usually the 8× overview (80 m). For
   Landsat it is the 2× overview (60 m).
3. Transform the area's lon/lat bounds into the tile's own CRS (its UTM zone, 21 points per
   edge). Turn them into a pixel window. Round the window outwards to whole overview pixels and
   clip it to the file.
4. Read the window with `out_shape` = window ÷ `f`. GDAL then takes the data from that overview
   with nearest-neighbour sampling, so the stored values stay as they are.
5. Write the three bands into an in-memory GeoTIFF: uint16 stored values, nodata 0, deflate
   compression, the scale and offset in the band metadata, and the window's geotransform. These
   bytes are what gets cached, as `<item id>_<area>_<res>m.tif`.

Eight tiles are read in parallel (`ThreadPoolExecutor(8)`). A 24-tile Sentinel-2 pass costs about
130 MB and ~30 s, instead of several GB for the full files.

### 3.7 Other downloads

- **Natural Earth** (overlays): `https://naciscdn.org/naturalearth/10m/<category>/<layer>.zip`
  for `ne_10m_coastline`, `ne_10m_lakes`, `ne_10m_admin_0_boundary_lines_land` and
  `ne_10m_populated_places_simple`. Each zip is downloaded once. Its shapefile parts are unpacked
  into `cache/naturalearth/` and the zip is deleted. `satview clean` never removes this folder.
- **pyspectral data** (through Satpy, the first time a Rayleigh correction runs): the
  instruments' spectral response functions and the Rayleigh look-up tables are downloaded from
  Zenodo into `~/.local/share/pyspectral/`.
- **Telegram** (optional, [telegram_sender.py](telegram_sender.py), not part of `satview`):
  finds the newest PNG of each source in `output/` and posts it with
  `POST https://api.telegram.org/bot<token>/sendPhoto` (multipart: `chat_id`, `caption`,
  `photo`). For EUMETSAT and Copernicus sources the caption carries the credit that their
  licences require ("Contains modified EUMETSAT Meteosat data 2026").

### 3.8 Summary of the network traffic

| Source | Requests per scene | Typical download |
| --- | --- | --- |
| `meteosat12_fci` | token, 1 search, 1 file list, 4 file GETs | ~115 MB |
| `meteosat9_iodc` | token, 1 search, 1 file list, 1 file GET | ~180 MB |
| `viirs_gibs` | 1 thumbnail (for `latest`) + 1 GetMap per layer | ~4 MB PNG |
| `viirs_l1b` | (token) + 2 CMR searches per satellite + 2 files per granule | ~250 MB per granule |
| `sentinel3_olci` | 1 OData search, token, 6 file GETs per frame | ~150 MB per frame |
| `sentinel2_msi` | 1+ STAC searches, then HTTP range reads (file header + the window's internal tiles) for 3 bands per tile | ~5 MB per tile at 100 m (~130 MB for a 24-tile pass) |
| `landsat_oli` | 1+ STAC searches, 1 token, then range reads as for Sentinel-2 | only the window, at the chosen overview |

All raw files land in `cache/<source>/<granule id>/`. A rerun, including a rerun of a time range,
builds pictures from the cache without network traffic. `satview clean` deletes cached files
older than `keep_raw_days` (by modification time) and then removes empty folders.

---

## 4. Choosing scenes

### 4.1 Geostationary sources

Every scan covers the area, so the only question is time. See `select_times()` in section 2.2.

### 4.2 Polar orbiters: grouping granules into passes

[polar.py](satview/sources/polar.py) is shared by VIIRS L1B, OLCI, Sentinel-2 and Landsat.

**Search window.**

- `latest`: the last 48 hours, which always includes yesterday's midday passes. Sentinel-2 uses
  10 days and Landsat 30 days, because of their revisit time and publishing delay.
- `at`: ± 12 h (± 3 days for Sentinel-2, ± 8 days for Landsat).
- `range`: exactly from start to end.

**Records.** Each search hit becomes a `Record`: satellite, start, end, footprint polygons, and
the server's metadata.

**Passes.** One overflight of Israel can be split over several files: two 6-minute VIIRS
granules, two 3-minute OLCI frames, or up to 24 Sentinel-2 tiles. `group_passes()` sorts the
records by (satellite, start time). It merges a record into the previous pass if it is from the
same satellite and starts no more than a gap after the previous end. The gap is 2 min for VIIRS
and STAC, and 1 min for OLCI.

**Coverage of the area.** `coverage()` puts a 24 × 24 grid of test points over the area's
lon/lat box. It counts the share of points that fall inside any footprint polygon of the pass.
The point-in-polygon test is the even-odd (ray casting) rule, vectorised with NumPy over all
576 points: for each polygon edge, a point toggles "inside" when a horizontal ray from it
crosses the edge.

**Distance from nadir.** `nadir_offset` is the distance in degrees between the area's centre and
the centre of the footprint. The longitude difference is scaled by cos(latitude). A smaller
value means the satellite flew closer to straight overhead, which gives smaller pixels and less
distortion at the swath edge.

**Choice.** `choose_passes()`:

1. keeps the passes that cover at least `min_coverage` (default 60 %) of the area, and whose
   start is in daylight (sun ≥ 5° at the area's centre);
2. for `range`, keeps only passes inside the range;
3. for `at`, returns the single pass closest in time;
4. otherwise groups the passes by UTC day. For `latest` it takes the most recent day; for
   `range`, every day. With `pick: best`, the winner of each day is the pass with the highest
   coverage, rounded to 10 % steps, and then the smallest nadir offset. With `pick: all`, every
   pass of the day is kept.

Rounding the coverage means that a pass that is nearer to nadir wins over one that covers only a
little more of the area.

---

## 5. From raw data to an RGB image

There are three ways to get from downloaded files to the `Image` (RGBA on the target grid):

| Path | Used by | Function |
| --- | --- | --- |
| Satpy | FCI, SEVIRI, VIIRS L1B, OLCI | `pipeline.satpy_image()` |
| Plain lon/lat resampling | GIBS | `pipeline.resample_latlon()` |
| Reflectance mosaic | Sentinel-2, Landsat | `stac.mosaic()` + `pipeline.reflectance_image()` |

### 5.1 Input file formats

| Source | Format | What is inside |
| --- | --- | --- |
| FCI | NetCDF-4 / HDF5, one file per BODY chunk. Compressed with HDF5 filters that the `hdf5plugin` package registers. | Raw counts of 16 channels and their calibration coefficients, on the fixed geostationary grid. FDHSI: visible/near-IR at 1 km, IR at 2 km. Chunk = one band of rows across the full disk. |
| SEVIRI | EUMETSAT native format (`.nat`), MSG Level 1.5 | 12 channels (3 km at the sub-satellite point, HRV 1 km) for the full disk, with headers holding calibration and navigation. |
| VIIRS L1B | NetCDF-4 (`*02MOD*.nc` + `*03MOD*.nc`) | 16 M-bands as scaled integers with factors for reflectance and radiance (750 m), plus a separate geolocation file: latitude, longitude, sun and satellite angles for each swath pixel. |
| OLCI L1B EFR | SAFE folder `*.SEN3/`, one NetCDF-4 file per band and per auxiliary data set | Radiance per band (300 m), pixel lat/lon, angles on a tie-point grid, solar flux. |
| GIBS | PNG (8-bit RGBA) in EPSG:4326 | An already finished, colour-enhanced true-colour image. |
| Sentinel-2 L2A | Cloud-optimized GeoTIFF, uint16, one file per band, in the tile's UTM zone | Surface reflectance × 10 000, plus an offset (Collection 1: scale 0.0001, offset −0.1). Read from the STAC metadata. |
| Landsat C2 L2 | Cloud-optimized GeoTIFF, uint16, one file per band | Surface reflectance (scale 0.0000275, offset −0.2). Read from the STAC metadata. |
| Natural Earth | ESRI shapefile (`.shp/.shx/.dbf/.prj/.cpg`) | Polylines (coastlines, lake shores, borders) and points with attributes (cities). |

### 5.2 The Satpy path

`satpy_image()` takes a reader name, the downloaded files, a composite name and the target grid:

```python
scn = Scene(reader="fci_l1c_nc", filenames=files)        # or seviri_l1b_native, viirs_l1b, olci_l1b
scn.load([composite], generate=False, upper_right_corner="NE")   # geostationary: north up
scn = scn.slice(swath_slices(...))                       # polar swaths only
scn.generate_possible_composites(unload=True)
dataset = scn.resample(area, resampler=...)[composite]
rgba = enhanced_rgba(dataset)
```

1. **Reading and calibration.** The reader turns raw counts into physical values. For visible
   channels this is the reflectance in %. It uses the calibration coefficients stored in the
   file: counts → radiance L → reflectance = π·L·d² / E_sun, where d is the Sun–Earth distance
   in AU and E_sun is the band's solar irradiance. The height of the sun is not taken into
   account yet; the sun-zenith correction does that (section 5.3). For OLCI the solar flux comes
   from `instrument_data.nc`.
2. **Channels first, composite later.** `load(…, generate=False)` loads only the channels the
   composite needs. It does not build the composite yet.
3. **Cutting the swath.** For polar data (a `SwathDefinition`, which has a lat/lon for each
   pixel), `swath_slices()` looks at every 8th pixel's lat/lon. It finds the rows and columns
   that fall inside the target grid's lon/lat bounds, adds one step (8 pixels) of margin, and
   Satpy slices the swath to that block. Satpy does this by itself only for gridded
   (geostationary) data. Without it, the Rayleigh correction over a whole OLCI or VIIRS swath
   would need several GB of memory.
4. **Composite generation** with its modifiers (section 5.3).
5. **Resampling** to the UTM grid.
   - `nearest` (the default for VIIRS and OLCI): pyresample's KD-tree nearest neighbour. For
     each target pixel it takes the closest swath pixel within a search radius.
   - `bilinear` (the default for FCI and SEVIRI): bilinear interpolation between the four
     surrounding source pixels. This avoids blocky 1–3 km pixels on the 500 m grid.

   Pixels that no source pixel reaches become NaN, and later transparent.
6. **Enhancement** to 8 bits (section 5.4).

Dask settings: the HDF5 library bundled with the netCDF4 wheels is not thread-safe, so
everything runs in dask's *single-threaded* scheduler. Blocks are 32 MiB, which keeps peak
memory low. The area is small, so this costs little time.

The picture title is `"<platform> <instrument> · <composite>"`, for example
"Meteosat-12 FCI · true color". The time is the scan's `start_time`.

### 5.3 Composite recipes (Satpy 0.60)

| Source / composite | Red | Green | Blue | Modifiers |
| --- | --- | --- | --- | --- |
| FCI `true_color` | `vis_06` (0.64 µm) | *NDVI hybrid green* from `vis_05` (0.51 µm) and `vis_08` (0.86 µm) | `vis_04` (0.44 µm) | sun-zenith correction → Rayleigh correction → sun-zenith reduction |
| FCI `natural_color` | `nir_16` (1.6 µm) | `vis_08` | `vis_06` | sun-zenith correction |
| FCI `vis_06` | single channel, shown as grey | | | sun-zenith correction |
| SEVIRI `natural_color` | `IR_016` (1.6 µm) | `VIS008` (0.8 µm) | `VIS006` (0.6 µm) | sun-zenith correction |
| VIIRS `true_color` | `M05` (0.67 µm) | `M04` (0.55 µm) | `M03` (0.49 µm) | sun-zenith correction → Rayleigh correction |
| OLCI `true_color` | `Oa08` (665 nm) | `Oa06` (560 nm) | `Oa03` (442 nm) | effective solar path length correction → Rayleigh correction |

SEVIRI has no blue channel, so it cannot make true colour. Natural colour shows clouds of ice
in cyan and clouds of water in white, vegetation in green and desert in brown–pink.

**Sun-zenith correction** (`SunZenithCorrector`). Each reflectance is divided by cos θ_s, where
θ_s is the solar zenith angle. This makes the brightness of a surface independent of how high
the sun is. Above θ_s = 88° the correction is gradually reduced. Above 95° the data is set to 0.

**Effective solar path length correction** (OLCI). Like the sun-zenith correction, but it uses
the effective path length of sunlight through a curved atmosphere (Li and Shibata, 2006)
instead of 1/cos θ_s, so it does not blow up near the horizon.

**Rayleigh correction** (`PSPRayleighReflectance`, pyspectral). Air molecules scatter blue light
much more than red (∝ λ⁻⁴). This adds a blue haze that grows with the slant of the light path.
Pyspectral looks up the Rayleigh reflectance for this band in a pre-computed table. The table is
indexed by sun zenith angle, satellite zenith angle and relative azimuth angle, for the US
standard atmosphere and no aerosols (`rayleigh_only`). The band's spectral response function is
used to weight the wavelengths. The table value is subtracted from the reflectance. The red
band (0.67 µm) is used as a brightness reference, so that clouds stay white instead of turning
yellow: where the red reflectance is below 20 %, the full correction is subtracted; above that
it is scaled by `1 − (red − 20) / 80`, which reaches 0 at 100 %.

**Sun-zenith reduction** (FCI only, `SunZenithReducer`). Between θ_s = 80° and 90°, the signal
is multiplied by a factor that falls from 1 to 0 (non-linear, strength 1.3). This darkens the
terminator smoothly, instead of showing the bright, reddish artefacts that the 1/cos θ_s
correction makes there.

**NDVI hybrid green** (FCI). The FCI "green" channel at 0.51 µm lies outside the chlorophyll
reflectance peak. With it alone, vegetation looks brown and desert looks reddish. Satpy mixes in
part of the 0.86 µm near-infrared channel:

```
NDVI  = (vis_08 − vis_06) / (vis_08 + vis_06)
f     = fraction of vis_08: from 0.15 at NDVI 0 to 0.05 at NDVI 1,
        with a non-linear curve (strength 3)
green = (1 − f) · vis_05 + f · vis_08
```

**Sharpening.** Satpy's FCI and VIIRS true-colour compositors can sharpen the colour with a
finer channel. Here the input channels already have the same resolution (FCI FDHSI 1 km; VIIRS
M-bands only, no I01 file downloaded), so the sharpening does nothing.

### 5.4 Enhancement: from reflectance to 8-bit colour

`enhanced_rgba()` applies Satpy's default enhancement for the dataset's name and
`standard_name`. Then it calls `finalize(fill_value=None)`. This scales 0–1 to 0–255 and clips,
and it adds an alpha band that is 0 wherever the data is NaN.

**True colour: the CIRA logarithmic stretch** (`cira_stretch`). The stretch was designed for
human vision. Dark surfaces get more contrast, and bright clouds are compressed:

```
r   = reflectance(%) / 100, clipped to > 0
out = (log10(r) − log10(0.0223)) / (0.75 · (1 − log10(0.0223)))
    = (log10(r) + 1.652) / 1.989           → clipped to 0…1, × 255
```

| Reflectance | 2.2 % | 5 % | 10 % | 30 % (desert) | 50 % | 80 % (thick cloud) | 100 % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Output level | 0 | 45 | 84 | 145 | 173 | 199 | 212 |

Bright Negev desert (~30 %) stays clearly darker than cloud (~80 %). This is why satview builds
Sentinel-2 and Landsat colours from reflectances with the same stretch, and does not use ESA's
ready-made true-colour image (`visual`), which saturates both desert and cloud.

**Natural colour:** a linear stretch from 0 to 120 % reflectance, then gamma 1.8.

**Single channel** (`vis_06`, `VIS006`): a linear stretch from 0 to 100 %, then gamma 1.5.

`to_rgba()` turns the result (mode L, LA, RGB or RGBA) into RGBA. Grey is copied to R, G and B.
If there is no alpha band, an opaque alpha band is added.

### 5.5 The GIBS path

GIBS delivers a finished 8-bit image on a regular lon/lat grid, so it only needs to be moved to
the UTM grid:

1. Pure black pixels are marked as no data (alpha 0), the same as transparent ones.
2. `resample_latlon()` computes the lon/lat of every target pixel centre
   (`area.get_lonlats()`). It turns these into fractional row and column positions in the PNG:

   ```
   col = (lon − lon_min) / (lon_max − lon_min) · width  − 0.5
   row = (lat_max − lat) / (lat_max − lat_min) · height − 0.5
   ```

   Then `scipy.ndimage.map_coordinates` interpolates each of the four bands bilinearly
   (`order=1`). Positions outside the PNG become 0, which is transparent.
3. The title is "NOAA-20 VIIRS · true color (GIBS)". The picture is marked *daily*: its file name
   and label carry only the date.

### 5.6 The Sentinel-2 / Landsat path

1. **Mosaic** (`stac.mosaic()`). An output array of shape (3, rows, cols) is filled with NaN.
   Each cached tile is reprojected from its UTM zone to the target grid with
   `rasterio.warp.reproject` and **average** resampling. Each target pixel is the mean of the
   source pixels inside it, which gives smooth down-sampling without aliasing. A tile only fills
   pixels that are still NaN and that have valid data in all three bands, so the first tile wins
   where tiles overlap. Stored values are turned into reflectance with each band's
   `value × scale + offset`.
2. **Enhancement** (`reflectance_image()`). Reflectance (0–1) × 100 gives %, the unit Satpy
   expects. The array is wrapped as an xarray/dask array named `true_color`, in blocks of
   1024 × 1024 pixels (single-threaded; the stretch makes several float64 copies, and doing the
   whole 100 m grid at once would take ~1 GB). Satpy's `true_color` enhancement is applied: the
   same CIRA stretch as above, so the colours match VIIRS and OLCI.

Surface reflectance has already been corrected for the atmosphere by ESA/USGS, so no Rayleigh
correction is applied. ESA corrects each Sentinel-2 tile separately, which can leave faint seams
between tiles.

### 5.7 Per-source colour tweak

An optional `enhance: {gamma, contrast, brightness}` in a source's config is applied after
Satpy's enhancement. All three default to 1, which means "do nothing". It helps to make
satellites look alike:

```
v = RGB / 255
v = v ^ (1 / gamma)                            # gamma > 1 brightens dark areas
v = ((v − 0.5) · contrast + 0.5) · brightness
RGB = clip(round(v · 255), 0, 255)             # alpha is unchanged
```

---

## 6. Drawing and saving the picture

[render.py](satview/render.py) turns the `Image` into the final picture.

### 6.1 Background and size

1. The RGBA image is pasted onto a black RGB canvas, with alpha as the mask. Areas without data
   become black.
2. If `output.width_px` is set (default 1200) and differs from the grid width, the picture is
   scaled with the **Lanczos** filter, keeping the aspect ratio. With `width_px: null`, the grid
   size is kept (476 px wide at 500 m, 2376 px at 100 m).

### 6.2 Overlays

Overlays are loaded once per run (`load_overlays()`), for the area's lon/lat bounds plus 0.5°:

- **coastlines:** `ne_10m_coastline` plus the shores from `ne_10m_lakes` (Sea of Galilee, Dead
  Sea), drawn in amber `(255, 215, 64)` at alpha 230;
- **borders:** `ne_10m_admin_0_boundary_lines_land`, white at alpha 170;
- **cities** (off by default): `ne_10m_populated_places_simple` with a population (`pop_max`) of
  at least 40 000, inside the area.

From each shapefile, only shapes whose bounding box meets the area are kept. Each shape is split
into its parts (polylines with at least two points).

Drawing (`_draw_overlays()`):

1. A transparent layer is created at **2× the picture size**. This is supersampling: lines drawn
   at double size and then scaled down look smooth (anti-aliased).
2. Each lon/lat point is projected to UTM 36N with pyproj and then to pixels:
   `px = (x − xmin)/(xmax − xmin) · W`, `py = (ymax − y)/(ymax − ymin) · H`.
3. Lines are drawn with Pillow: width `max(1, round(W/800 · 2))`, with rounded joints. Borders
   are drawn first and coastlines on top. Cities are drawn as a white dot with a black outline,
   with the name to the right in white text with a black outline.
4. The layer is scaled down to the picture size with Lanczos and alpha-composited over the
   picture.

### 6.3 Label

`_draw_label()` writes `"<title> · YYYY-MM-DD HH:MM UTC"` in the bottom-left corner. Daily images
show `"YYYY-MM-DD (daily)"` instead. The text is white on a black box with alpha 150. The font is
Pillow's built-in font, at a size of `max(12, width/50)` px, so pictures look the same on every
machine.

### 6.4 Output files

All files are written to a temporary `<name>.part.<ext>` and then renamed, so another program
(for example the Telegram sender) never reads a half-written file.

- **PNG:** the rendered picture, at `output/<source>/<YYYY-MM-DD>/<source>[_<tag>]_<YYYYMMDDTHHMM>.png`.
  The tag tells apart products of one source at the same time (the satellite: `noaa20`, `S3A`,
  `S2B`, …). Daily images carry only the date.
- **GeoTIFF** (`formats: [geotiff]`): the `Image` at the grid's native resolution, without
  overlays or label. It has 4 uint8 bands (RGB + alpha), deflate compression, CRS EPSG:32636, a
  geotransform from the grid extent, and the TIFF tags `DATETIME` and `IMAGEDESCRIPTION` (the
  label). Use it in a GIS or for full detail.
- **Animations** (time ranges with `gif` or `mp4`). They are built from the range's PNGs,
  including PNGs from earlier runs. Frames are grouped by tag and sorted by time; at least two
  frames are needed. An animation newer than all its frames is left alone.
  - GIF: imageio, frame duration = 1000 / `animation_fps` ms, loops forever.
  - MP4: H.264 (`libx264`) with `yuv420p` and quality 8. H.264 with yuv420p needs even
    dimensions, so the frames are cropped by a pixel if needed.

  File: `output/<source>/<source>[_<tag>]_<start>_<end>.<ext>`.

---

## 7. Robustness

- **Caching and idempotence.** Raw downloads are cached and finished pictures are skipped, so
  cron can run `satview run` every 15 minutes. Only new scenes cost traffic and CPU.
- **Isolation.** Each source and each scene runs in its own `try` block. One failing server, or
  one broken file, does not stop the others.
- **Exit codes.** 0 if at least one source worked, 1 if all failed, 2 for configuration or
  credential errors, 130 on Ctrl-C.
- **Logging.** `-v` shows debug messages (cache hits, skipped scans). `-vv` also shows the logs
  and warnings of Satpy, pyresample, eumdac and the other libraries, which are silenced by
  default.
- **Dependency pinning.** Satpy is pinned to 0.60.0 because its FCI reader is still in beta. An
  offline regression test renders a recorded GIBS sample and compares it with
  `tests/data/regression_gibs.png`.

## 8. Known limitations

- **Parallax (Meteosat-12).** The satellite sees Israel at a slant from 0°. The tops of high
  clouds are drawn where the line of sight hits them, up to ~10 km north-east of the ground
  below them. This is not corrected.
- **Latency.** Without a EUMETSAT NRT licence, only hourly scans are available in real time.
  VIIRS L1B arrives ~3 h (NRT) or ~6–12 h after the pass. Sentinel-2 arrives ~5 h after the pass.
  Landsat Level-2 arrives 1–2 weeks after the pass (fresher Level-1 data needs a USGS M2M
  account, which is not supported yet).
- **Night.** All composites use visible channels, so scenes with the sun below 5° are skipped.
- **Sentinel-2 seams.** ESA's per-tile atmospheric correction can leave faint brightness steps
  between tiles.
