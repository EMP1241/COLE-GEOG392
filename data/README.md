# COLE data preparation

The national prepared release contains EPI's 2026 county household estimates in
2025 dollars, joined to 2021 Census county/state cartographic boundaries, 2020
place/county and ZCTA/county relationships, and NOAA nClimDiv 1991–2020 monthly
county-area mean-temperature estimates. Normal application use reads the local
prepared files and needs no download.

## Reproduce

From the `cole-app` directory, using the project's Python environment:

```powershell
.\.venv\Scripts\python.exe scripts\fetch_sources.py
.\.venv\Scripts\python.exe scripts\prepare_data.py
.\.venv\Scripts\python.exe -m pytest research_checks\test_data.py -q
```

Run preparation while COLE is stopped. It builds and validates a temporary set
before replacing outputs. Each file replacement is atomic and `manifest.json`
is committed last, with the hashes of all output files. A multi-file update is
not a transaction for an application already reading those files.

The download command preserves publisher bytes. Existing files must match their
recorded SHA-256 before they are reused. `--check-remote` downloads temporary
copies and verifies the publisher still serves the same bytes. It refuses a
changed remote source instead of overwriting the existing edition. A dataset
update needs an explicit source/vintage review and a new release identifier.
Downloads have size limits, expected workbook/text headers, ZIP-member checks,
and CRC checks. Archives are read in memory without extracting members.

## Source register

The exact URL, retrieval time in UTC, size, checksum, content type, and available
HTTP metadata are in `source/sources.json` and copied into the prepared manifest.
All eight sources were fetched on September 13, 2026 UTC. The EPI download's hash
matches the workbook previously held in the project's `tmp/col-data-check`.

| Source | Selected file and primary reference |
| --- | --- |
| EPI household costs | [2026 workbook](https://files.epi.org/uploads/fbc_data_2026.xlsx); [download and terms page](https://www.epi.org/resources/budget/); [methodology](https://www.epi.org/publication/family-budget-calculator-documentation/) |
| County geometry | [2021 counties, 1:5,000,000](https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_county_5m.zip) |
| State context | [2021 states, 1:5,000,000](https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_state_5m.zip) |
| Place relationships | [2020 places by county](https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt) |
| ZIP-area relationships | [2020 ZCTA/county relationships](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt); [Census ZCTA explanation](https://www.census.gov/programs-surveys/geography/guidance/geo-areas/zctas.html) |
| County temperature | [NOAA nClimDiv dated county normals file, released 2026-09-04](https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/climdiv-norm-tmpccy-v1.0.0-20260904); [current directory](https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/) |
| NOAA county format | [County README](https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/county-readme.txt) |
| NOAA normals format and caveat | [Normals README](https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/normals-readme.txt) |

The pinned NOAA data file is 3,712,401 bytes with SHA-256
`19f5769e118925d8371db37167bf1785ba9ecc25972227ddf804b2295bc88851`.
The county and normals README hashes are respectively
`4e208f4c090e63bdc4133de367e40635dfcb668bee17c14f8a85a6418b249810`
and `5938381f789451e584178a603c01e24e6215b5ee670d58e778a749156a5fea32`.
The prepared manifest is the authoritative register for all eight source bodies.

EPI attribution: Economic Policy Institute. 2026. *Family Budget Calculator*.
The official download page identifies the material as CC BY-NC-SA 4.0 and
describes academic, student and other noncommercial reuse. Retain attribution and
these source/terms references in distribution. This project does not add claims
of city-level costs, personalized expense forecasts, or measured accuracy.

## Prepared file meanings

- `households.csv`: 31,430 county/profile records for 3,143 counties and county
  equivalents. Every county retains all ten profiles: one or two adults and zero
  through four children. County IDs are five-character strings.
- `counties.geojson` and `states.geojson`: the 3,143 covered counties and 51
  states/DC. GeoJSON uses WGS84 longitude/latitude. Source NAD83 coordinates are
  transformed through offline non-ballpark PROJ operations. Geometry remains at
  the publisher's 1:5,000,000 scale, without additional simplification or rounding.
- `lookups.json`: 31,617 supported place IDs and 33,642 supported ZCTAs.
  Place IDs combine two-digit state and five-digit place codes. Place labels
  retain the state abbreviation. Every supported relationship is retained in a
  `county_fips` array; 1,294 places and 10,111 ZCTAs have multiple counties.
  `unsupported_county_fips` retains any associated county outside EPI coverage.
- `climate.csv`: twelve signed integer tenths-of-a-degree Fahrenheit values for
  each of 3,142 counties. It retains NOAA source state/county, element and period
  codes. The application derives extrema and a one-decimal 12-month average from
  those twelve values; it does not rewrite the source months.
- `manifest.json`: release identity, edition/cost/geography years, provenance,
  coverage, field mapping, interpretation, output hashes, and validation summary.
- `validation.json`: component differences, exact profile/geography checks,
  excluded feature identifiers, unsupported location identifiers, and source-row
  references for discarded blank or unsupported relationships.

Annual source columns N:U of the workbook's `County` sheet are housing, food,
transportation, healthcare, other necessities, childcare, taxes, and total.
Prepared CSV retains each original annual value, source sheet, and source row.
The baseline is exactly `published_annual_total - published_annual_taxes`.
All annual source cells in this edition are integers; a future noninteger source
is blocked for a precision review rather than converted through binary floats.

Published component sums differ from their published total in 15,480 rows, by
-2 to +3 dollars (total minus component sum). These differences are consistent
with separately rounded published values, but the workbook does not establish
their cause. COLE retains the published total and taxes without adjusting any
component. The report lists every affected county/profile and source row.

The selected NOAA record uses element `02` (average temperature) and period
`0010` (1991–2020). Each monthly field is a county-area long-term mean in degrees
Fahrenheit, published to one decimal place. NOAA describes the county series as
area-weighted estimates derived from gridded station observations. Its documentation
calls the long-term averages “normals” for convenience but says they are not the
official normals produced by the NCEI Normals Program. COLE therefore labels them
as estimates and does not treat them as forecasts, daily extrema, station values,
or city-specific climate.

NOAA's first two identifier positions are sequential state codes rather than
Census state FIPS. Preparation crosswalks NOAA state code to USPS abbreviation and
then to the Census state FIPS present in COLE's EPI identities. Washington, DC uses
the verified special source identifier `18511`, mapped to Census county-equivalent
FIPS `11001`; `18510` remains Baltimore city (`24510`). Lexington city, Virginia
(`51678`) has no matching county record in the dated NOAA file. The release records
that sole gap and does not substitute a neighboring county, climate division, or
station. An active temperature filter excludes it from discovery; direct lookup
still returns it with climate marked unavailable.

The selected county geometry contains 3,234 source features; 91 territories or
other uncovered features are excluded from the EPI-supported application, with
all IDs retained in the report. State context excludes five territories.
Relationships exclude 571 wholly unsupported place IDs and 149 wholly unsupported
ZCTAs. The ZCTA source also contains 903 rows with blank ZCTA IDs; these are not
invented into searchable ZIP areas. No covered cost county lacks geometry.

## Three-county integration sample

The `sample_` files are a separate, explicitly limited integration sample. They
include the three cost counties and matching NOAA monthly profiles:
Autauga County, Alabama (`01001`), Brazos County, Texas (`48041`), and Robertson
County, Texas (`48395`). It has 30 household records and three county geometries.
Autauga tests leading-zero identifiers. Brazos provides local context, while
ZCTAs `77807`, `77808`, and `77859` have genuine Brazos/Robertson relationships.

Sample lookup entries include `outside_sample_county_fips` so a partial sample
cannot silently erase another supported relationship. For example, Navasota
city spans Brazos and Grimes counties; the sample identifies Grimes as outside
its three-county coverage. A sample interface must visibly state its coverage.
The application can use the separate complete national files when ready.

## Interpretation and verification limits

These are generalized county household estimates using EPI's specified family,
employment, rental, and child-age assumptions. EPI's ten profiles do not support
arbitrary ages or households. ZCTAs are generalized Census ZIP areas, not a
complete postal ZIP service. Location lookup identifies county estimates and
does not make the estimates specific to a city, neighborhood, or ZIP code.

Data checks include file hashes, all county/profile keys, all financial baseline
subtractions, every geometry join, every supported source relationship, every
NOAA identifier and month, exact Census/NOAA crosswalk coverage, all sample source
totals/taxes through independent XML extraction, and archive rejection.
They establish preparation consistency, not source estimates' personal accuracy,
map usability, browser behavior, or offline package behavior.
