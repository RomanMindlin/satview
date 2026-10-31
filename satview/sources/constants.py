"""Collection IDs, product names and endpoints, kept in one place so API changes are one edit.

`satview list-sources` checks the ones used by enabled sources.
"""

# EUMETSAT Data Store
FCI_FDHSI_COLLECTION = "EO:EUM:DAT:0662"  # FCI L1c normal resolution (1 km VIS), 0 degree
FCI_HRFI_COLLECTION = "EO:EUM:DAT:0665"  # FCI L1c high resolution (500 m red), 0 degree
FCI_PLATFORM = "MTI1"  # Meteosat-12; the collection may later hold other MTG-I satellites
SEVIRI_IODC_COLLECTION = "EO:EUM:DAT:MSG:HRSEVIRI-IODC"

# NASA GIBS
GIBS_WMS_URL = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"

# NASA Earthdata Login: returns the user's bearer token for a username and password
EDL_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"

# NASA CMR / LAADS, VIIRS L1B M-band radiances (02MOD) and geolocation (03MOD).
# The I-band files (02IMG) have no blue channel, so true color needs the M-bands.
VIIRS_PREFIXES = {"snpp": "VNP", "noaa20": "VJ1", "noaa21": "VJ2"}
VIIRS_PLATFORM_NAMES = {"snpp": "Suomi NPP", "noaa20": "NOAA-20", "noaa21": "NOAA-21"}

# Copernicus Data Space Ecosystem
CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products"
OLCI_PRODUCT_TYPE = "OL_1_EFR___"

# STAC catalogues of cloud-optimized GeoTIFFs, read in parts over HTTP without a login.
# Earth Search: Sentinel-2 L2A Collection 1 in a public bucket, ~5 h after the pass, and
# unlike Planetary Computer it gives each tile's own sensing time.
EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1"
S2_COLLECTION = "sentinel-2-c1-l2a"
# Planetary Computer: Landsat Collection 2 L2 surface reflectance, ~1-2 weeks after the pass.
# (Level-1, out within hours, needs a USGS login or a requester-pays AWS bucket.)
# File URLs need a free anonymous token from the token endpoint + /<collection>.
PLANETARY_COMPUTER_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PLANETARY_COMPUTER_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
LANDSAT_COLLECTION = "landsat-c2-l2"
