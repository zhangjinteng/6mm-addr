# 6mm Address Data

`zhangjinteng/6mm-addr` provides a versioned, read-only SQLite database and a
small PHP lookup API for English and Chinese country, region, and city names.

The package does not perform IP geolocation. IP providers such as IPinfo return
the raw location; `6mm-addr` resolves its stable multilingual display names.

## Install

```bash
composer require zhangjinteng/6mm-addr
```

Requirements:

- PHP 8.2+
- `ext-mbstring`
- `ext-pdo`
- `ext-pdo_sqlite`

## Usage

```php
use SixMm\Addr\AddressLocalizer;

$location = (new AddressLocalizer())->enrich([
    'country_code' => 'JP',
    'country' => 'Japan',
    'region_code' => '13',
    'region' => 'Tokyo',
    'city' => 'Tokyo',
]);

$location['country_names']; // ['en' => 'Japan', 'zh' => '日本']
$location['region_names'];  // ['en' => 'Tokyo', 'zh' => '东京都']
$location['city_names'];    // ['en' => 'Tokyo', 'zh' => '东京']
```

The original provider values remain unchanged. Three `*_names` maps are added,
allowing UI clients to select `en` or `zh` without another network request.
Missing Chinese names fall back to English.

## Database

The bundled database is located at:

```text
resources/world-places.sqlite
```

It is opened with SQLite `query_only` mode. Applications must not modify this
file at runtime. Address updates are distributed as new Composer package
versions.

Current source data includes countries, first-level administrative regions,
and cities from GeoNames `cities500`. City rows retain their GeoNames admin-1
code, so cities with the same name in different regions are not merged.

## Rebuild

```bash
python3 scripts/build_database.py
composer test
```

Downloaded source archives are cached under `scripts/.cache/geonames` and are
not committed. Curated Chinese corrections and provider region-code mappings
live in `scripts/data/address-overrides.json`.

City overrides can use either a country-wide key or a region-specific key:

```json
{
  "cities": {
    "BD:Jessore": { "nameZh": "杰索尔" },
    "US:IL:Springfield": { "nameZh": "斯普林菲尔德" }
  }
}
```

## Data Attribution

The generated database contains data derived from
[GeoNames](https://www.geonames.org/) under the
[Creative Commons Attribution 4.0 License](https://creativecommons.org/licenses/by/4.0/).
See `resources/licenses/GEONAMES.md`.
