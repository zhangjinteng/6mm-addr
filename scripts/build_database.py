#!/usr/bin/env python3
"""Build the versioned 6mm address SQLite database from GeoNames."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import sqlite3
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


GEONAMES_DUMP_URL = "https://download.geonames.org/export/dump"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "scripts" / ".cache" / "geonames"
DEFAULT_OUTPUT = PROJECT_ROOT / "resources" / "world-places.sqlite"
DEFAULT_OVERRIDES = PROJECT_ROOT / "scripts" / "data" / "address-overrides.json"
DEFAULT_SUPPLEMENTAL_NAMES = (
    PROJECT_ROOT / "scripts" / "data" / "supplemental-names.json.gz"
)
CITY_SOURCES = ("cities500", "cities1000", "cities5000", "cities15000")
OBSOLETE_COUNTRY_CODES = {"AN", "CS"}
CHINESE_CHARACTER_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass
class LocalizedName:
    value: str
    language: str
    score: int
    source: str = "geonames"


@dataclass
class Country:
    code: str
    geoname_id: int
    name_en: str


@dataclass
class City:
    geoname_id: int
    country_code: str
    region_code: str
    name: str
    ascii_name: str
    population: int


@dataclass
class Region:
    geoname_id: int
    country_code: str
    code: str
    name: str
    ascii_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the read-only 6mm English-Chinese address database."
    )
    parser.add_argument(
        "--city-source",
        choices=CITY_SOURCES,
        default="cities500",
        help="GeoNames population threshold dataset (default: cities500).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output SQLite path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Download cache directory (default: {DEFAULT_CACHE_DIR}).",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=DEFAULT_OVERRIDES,
        help=f"Curated Chinese name overrides (default: {DEFAULT_OVERRIDES}).",
    )
    parser.add_argument(
        "--supplemental-names",
        type=Path,
        default=DEFAULT_SUPPLEMENTAL_NAMES,
        help=(
            "CLDR, Wikidata, and generated Chinese names "
            f"(default: {DEFAULT_SUPPLEMENTAL_NAMES})."
        ),
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Download source files again even when cached files exist.",
    )
    return parser.parse_args()


def download(url: str, target: Path, force: bool) -> None:
    if target.exists() and target.stat().st_size > 0 and not force:
        print(f"[cache] {target.name} ({format_bytes(target.stat().st_size)})")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".download")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "6mm-agent-console-geonames-generator/1.0"},
    )
    print(f"[download] {url}")

    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
            "wb"
        ) as output:
            total = int(response.headers.get("Content-Length", "0"))
            received = 0
            next_report = 10
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                if total:
                    percent = int(received * 100 / total)
                    if percent >= next_report:
                        print(
                            f"  {percent:3d}%  {format_bytes(received)} / {format_bytes(total)}"
                        )
                        next_report = percent + 10
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def read_countries(path: Path) -> dict[str, Country]:
    countries: dict[str, Country] = {}
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < 17 or not fields[16]:
                continue
            if fields[0] in OBSOLETE_COUNTRY_CODES:
                continue
            country = Country(
                code=fields[0],
                geoname_id=int(fields[16]),
                name_en=fields[4],
            )
            countries[country.code] = country
    return countries


def zip_text_lines(path: Path) -> Iterable[str]:
    with zipfile.ZipFile(path) as archive:
        text_files = [
            item for item in archive.infolist() if item.filename.endswith(".txt")
        ]
        if not text_files:
            raise RuntimeError(f"No text file found in {path}")
        main_file = max(text_files, key=lambda item: item.file_size)
        with archive.open(main_file) as raw, io.TextIOWrapper(
            raw, encoding="utf-8", errors="replace", newline=""
        ) as source:
            yield from source


def read_cities(path: Path, valid_country_codes: set[str]) -> list[City]:
    cities: list[City] = []
    for line in zip_text_lines(path):
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) < 19 or fields[8] not in valid_country_codes:
            continue
        cities.append(
            City(
                geoname_id=int(fields[0]),
                country_code=fields[8],
                region_code=fields[10],
                name=fields[1],
                ascii_name=fields[2],
                population=int(fields[14] or 0),
            )
        )
    return cities


def read_regions(path: Path, valid_country_codes: set[str]) -> list[Region]:
    regions: list[Region] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < 4 or "." not in fields[0]:
                continue
            country_code, region_code = fields[0].split(".", 1)
            if country_code not in valid_country_codes:
                continue
            regions.append(
                Region(
                    geoname_id=int(fields[3]),
                    country_code=country_code,
                    code=region_code,
                    name=fields[1],
                    ascii_name=fields[2],
                )
            )
    return regions


def normalize_override_key(
    country_code: str, name: str, region_code: str = ""
) -> str:
    normalized_name = " ".join(name.strip().casefold().split())
    parts = [country_code.strip().upper()]
    if region_code:
        parts.append(region_code.strip().upper())
    parts.append(normalized_name)
    return ":".join(parts)


def read_overrides(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"cities": {}, "regions": {}, "regionCodeAliases": []}

    with path.open("r", encoding="utf-8") as source:
        payload = json.load(source)

    normalized: dict[str, object] = {
        "cities": {},
        "regions": {},
        "regionCodeAliases": payload.get("regionCodeAliases", []),
    }
    for group in ("cities", "regions"):
        for key, value in payload.get(group, {}).items():
            parts = key.split(":", 2)
            if len(parts) < 2 or not isinstance(value, dict) or not value.get("nameZh"):
                continue
            country_code = parts[0]
            if group == "cities" and len(parts) == 3:
                normalized[group][
                    normalize_override_key(country_code, parts[2], parts[1])
                ] = value
            else:
                normalized[group][
                    normalize_override_key(country_code, ":".join(parts[1:]))
                ] = value
    return normalized


def read_supplemental_names(path: Path) -> dict[int, LocalizedName]:
    if not path.exists():
        return {}

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        payload = json.load(source)

    names: dict[int, LocalizedName] = {}
    for raw_id, item in payload.get("names", {}).items():
        if not isinstance(item, dict):
            continue
        value = str(item.get("nameZh", "")).strip()
        source_name = str(item.get("source", "supplemental")).strip().lower()
        if not value or not CHINESE_CHARACTER_PATTERN.search(value):
            continue
        try:
            geoname_id = int(raw_id)
        except ValueError:
            continue
        names[geoname_id] = LocalizedName(
            value=value,
            language="zh-Hans",
            score=500,
            source=source_name or "supplemental",
        )
    return names


def chinese_source(name: LocalizedName | None) -> str | None:
    return name.source if name else None


def language_score(language: str, chinese: bool) -> int | None:
    normalized = language.strip()
    if chinese:
        priorities = {
            "zh-CN": 220,
            "zh-Hans": 210,
            "zh-SG": 200,
            "zh": 150,
            "zh-Hant": 130,
            "zh-TW": 120,
            "zh-HK": 110,
        }
        if normalized in priorities:
            return priorities[normalized]
        return 90 if normalized.startswith("zh-") else None

    if normalized == "en":
        return 130
    return 110 if normalized.startswith("en-") else None


def choose_localized_names(
    path: Path, relevant_ids: set[int]
) -> tuple[dict[int, LocalizedName], dict[int, LocalizedName]]:
    english: dict[int, LocalizedName] = {}
    chinese: dict[int, LocalizedName] = {}
    processed = 0

    for line in zip_text_lines(path):
        processed += 1
        if processed % 5_000_000 == 0:
            print(f"[parse] alternate names: {processed:,} rows")

        fields = line.rstrip("\r\n").split("\t")
        if len(fields) < 4:
            continue
        try:
            geoname_id = int(fields[1])
        except ValueError:
            continue
        if geoname_id not in relevant_ids:
            continue

        language = fields[2]
        value = fields[3].strip()
        if not value:
            continue

        is_preferred = len(fields) > 4 and fields[4] == "1"
        is_short = len(fields) > 5 and fields[5] == "1"
        is_colloquial = len(fields) > 6 and fields[6] == "1"
        is_historic = len(fields) > 7 and fields[7] == "1"

        for is_chinese, destination in ((False, english), (True, chinese)):
            base_score = language_score(language, is_chinese)
            if base_score is None:
                continue
            if is_chinese and not CHINESE_CHARACTER_PATTERN.search(value):
                continue

            score = base_score
            score += 30 if is_preferred else 0
            score += 5 if is_short else 0
            score -= 40 if is_colloquial else 0
            score -= 200 if is_historic else 0
            current = destination.get(geoname_id)
            if current is None or score > current.score:
                destination[geoname_id] = LocalizedName(value, language, score)

    print(f"[parse] alternate names complete: {processed:,} rows")
    return english, chinese


def build_payload(
    city_source: str,
    countries: dict[str, Country],
    cities: list[City],
    regions: list[Region],
    english_names: dict[int, LocalizedName],
    chinese_names: dict[int, LocalizedName],
    overrides: dict[str, object],
) -> dict[str, object]:
    cities_by_country: dict[str, list[dict[str, object]]] = {
        code: [] for code in countries
    }
    regions_by_country: dict[str, list[dict[str, object]]] = {
        code: [] for code in countries
    }
    cities_with_chinese = 0
    city_overrides_applied = 0

    for city in cities:
        english = english_names.get(city.geoname_id)
        chinese = chinese_names.get(city.geoname_id)
        name_en = english.value if english else city.ascii_name or city.name
        city_overrides = overrides["cities"]
        override = city_overrides.get(
            normalize_override_key(city.country_code, name_en, city.region_code)
        ) or city_overrides.get(normalize_override_key(city.country_code, name_en))
        if override:
            chinese = LocalizedName(
                override["nameZh"], "zh-Hans", 1000, source="override"
            )
            city_overrides_applied += 1
        if chinese:
            cities_with_chinese += 1
        cities_by_country[city.country_code].append(
            {
                "geonameId": city.geoname_id,
                "regionCode": city.region_code,
                "nameEn": name_en,
                "nameZh": chinese.value if chinese else None,
                "zhLanguage": chinese.language if chinese else None,
                "zhSource": chinese_source(chinese),
                "population": city.population,
                "aliasesEn": sorted(
                    {
                        value
                        for value in (name_en, city.name, city.ascii_name)
                        if value and value != name_en
                    },
                    key=str.casefold,
                ),
            }
        )

    regions_with_chinese = 0
    region_overrides_applied = 0
    for region in regions:
        chinese = chinese_names.get(region.geoname_id)
        # admin1CodesASCII names match the region strings returned by IP providers
        # more reliably than longer English alternate names.
        name_en = region.ascii_name or region.name
        region_overrides = overrides["regions"]
        override = region_overrides.get(
            normalize_override_key(region.country_code, name_en)
        )
        if override:
            chinese = LocalizedName(
                override["nameZh"], "zh-Hans", 1000, source="override"
            )
            region_overrides_applied += 1
        if chinese:
            regions_with_chinese += 1
        regions_by_country[region.country_code].append(
            {
                "geonameId": region.geoname_id,
                "code": region.code,
                "nameEn": name_en,
                "nameZh": chinese.value if chinese else None,
                "zhLanguage": chinese.language if chinese else None,
                "zhSource": chinese_source(chinese),
                "aliasesEn": sorted(
                    {
                        value
                        for value in (region.name, region.ascii_name)
                        if value and value != name_en
                    },
                    key=str.casefold,
                ),
            }
        )

    country_items: list[dict[str, object]] = []
    countries_with_chinese = 0
    for code in sorted(countries):
        country = countries[code]
        english = english_names.get(country.geoname_id)
        chinese = chinese_names.get(country.geoname_id)
        if chinese:
            countries_with_chinese += 1
        country_cities = cities_by_country[code]
        country_cities.sort(
            key=lambda item: (str(item["nameEn"]).casefold(), int(item["geonameId"]))
        )
        country_regions = regions_by_country[code]
        country_regions.sort(
            key=lambda item: (str(item["nameEn"]).casefold(), int(item["geonameId"]))
        )
        country_items.append(
            {
                "code": code,
                "geonameId": country.geoname_id,
                "nameEn": english.value if english else country.name_en,
                "nameZh": chinese.value if chinese else None,
                "zhLanguage": chinese.language if chinese else None,
                "zhSource": chinese_source(chinese),
                "regions": country_regions,
                "cities": country_cities,
            }
        )

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "meta": {
            "source": "GeoNames",
            "sourceUrl": GEONAMES_DUMP_URL + "/",
            "license": "CC BY 4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
            "generatedAt": generated_at,
            "citySource": city_source + ".zip",
            "countryCount": len(country_items),
            "countryWithChineseNameCount": countries_with_chinese,
            "regionCount": len(regions),
            "regionWithChineseNameCount": regions_with_chinese,
            "regionOverrideCount": region_overrides_applied,
            "cityCount": len(cities),
            "cityWithChineseNameCount": cities_with_chinese,
            "cityOverrideCount": city_overrides_applied,
            "schemaVersion": "1",
            "fallbackRule": "Use nameZh when present; otherwise display nameEn.",
        },
        "countries": country_items,
        "regionCodeAliases": overrides["regionCodeAliases"],
    }


def normalize_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def write_output(payload: dict[str, object], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.unlink(missing_ok=True)

    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;

            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE countries (
                code TEXT PRIMARY KEY,
                geoname_id INTEGER NOT NULL UNIQUE,
                name_en TEXT NOT NULL,
                name_zh TEXT,
                zh_language TEXT,
                zh_source TEXT
            ) WITHOUT ROWID;

            CREATE TABLE regions (
                geoname_id INTEGER PRIMARY KEY,
                country_code TEXT NOT NULL,
                code TEXT NOT NULL,
                name_en TEXT NOT NULL,
                name_zh TEXT,
                zh_language TEXT,
                zh_source TEXT,
                UNIQUE (country_code, code)
            );

            CREATE TABLE region_aliases (
                country_code TEXT NOT NULL,
                alias_normalized TEXT NOT NULL,
                region_id INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY (country_code, alias_normalized, region_id)
            ) WITHOUT ROWID;

            CREATE TABLE region_code_aliases (
                provider TEXT NOT NULL,
                country_code TEXT NOT NULL,
                code TEXT NOT NULL,
                region_id INTEGER NOT NULL,
                PRIMARY KEY (provider, country_code, code)
            ) WITHOUT ROWID;

            CREATE TABLE cities (
                geoname_id INTEGER PRIMARY KEY,
                country_code TEXT NOT NULL,
                region_code TEXT NOT NULL DEFAULT '',
                name_en TEXT NOT NULL,
                name_zh TEXT,
                zh_language TEXT,
                zh_source TEXT,
                population INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE city_aliases (
                country_code TEXT NOT NULL,
                region_code TEXT NOT NULL DEFAULT '',
                alias_normalized TEXT NOT NULL,
                city_id INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY (country_code, region_code, alias_normalized, city_id)
            ) WITHOUT ROWID;

            CREATE INDEX idx_region_alias_lookup
                ON region_aliases (country_code, alias_normalized, priority DESC);
            CREATE INDEX idx_city_alias_lookup
                ON city_aliases (country_code, region_code, alias_normalized, priority DESC);
            CREATE INDEX idx_city_alias_country_lookup
                ON city_aliases (country_code, alias_normalized, priority DESC);
            CREATE INDEX idx_city_country_region
                ON cities (country_code, region_code, population DESC);
            """
        )

        meta = payload["meta"]
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [(str(key), str(value)) for key, value in meta.items()],
        )

        for country in payload["countries"]:
            connection.execute(
                """
                INSERT INTO countries
                    (code, geoname_id, name_en, name_zh, zh_language, zh_source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    country["code"],
                    country["geonameId"],
                    country["nameEn"],
                    country["nameZh"],
                    country["zhLanguage"],
                    country["zhSource"],
                ),
            )

            for region in country["regions"]:
                connection.execute(
                    """
                    INSERT INTO regions
                        (geoname_id, country_code, code, name_en, name_zh,
                         zh_language, zh_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        region["geonameId"],
                        country["code"],
                        region["code"],
                        region["nameEn"],
                        region["nameZh"],
                        region["zhLanguage"],
                        region["zhSource"],
                    ),
                )
                for priority, alias in [
                    (100, region["nameEn"]),
                    *((80, value) for value in region["aliasesEn"]),
                ]:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO region_aliases
                            (country_code, alias_normalized, region_id, priority)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            country["code"],
                            normalize_name(alias),
                            region["geonameId"],
                            priority,
                        ),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO region_code_aliases
                        (provider, country_code, code, region_id)
                    VALUES ('ipinfo', ?, ?, ?)
                    """,
                    (country["code"], region["code"].upper(), region["geonameId"]),
                )

            for city in country["cities"]:
                connection.execute(
                    """
                    INSERT INTO cities
                        (geoname_id, country_code, region_code, name_en, name_zh,
                         zh_language, zh_source, population)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        city["geonameId"],
                        country["code"],
                        city["regionCode"],
                        city["nameEn"],
                        city["nameZh"],
                        city["zhLanguage"],
                        city["zhSource"],
                        city["population"],
                    ),
                )
                for priority, alias in [
                    (100, city["nameEn"]),
                    *((80, value) for value in city["aliasesEn"]),
                ]:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO city_aliases
                            (country_code, region_code, alias_normalized, city_id, priority)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            country["code"],
                            city["regionCode"],
                            normalize_name(alias),
                            city["geonameId"],
                            priority,
                        ),
                    )

        for alias in payload.get("regionCodeAliases", []):
            if not isinstance(alias, dict):
                continue
            provider = str(alias.get("provider", "ipinfo")).strip().lower()
            country_code = str(alias.get("countryCode", "")).strip().upper()
            code = str(alias.get("code", "")).strip().upper()
            region_id = alias.get("geonameId")
            if not provider or len(country_code) != 2 or not code or not region_id:
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO region_code_aliases
                    (provider, country_code, code, region_id)
                VALUES (?, ?, ?, ?)
                """,
                (provider, country_code, code, int(region_id)),
            )

        connection.commit()
        connection.execute("ANALYZE")
        connection.execute("VACUUM")
    finally:
        connection.close()

    os.replace(temporary_path, output_path)
    return output_path


def main() -> int:
    args = parse_args()
    cache_dir = args.cache_dir.resolve()
    output_path = args.output.resolve()
    overrides_path = args.overrides.resolve()
    supplemental_names_path = args.supplemental_names.resolve()
    sources = {
        args.city_source: cache_dir / f"{args.city_source}.zip",
        "alternateNamesV2": cache_dir / "alternateNamesV2.zip",
        "countryInfo": cache_dir / "countryInfo.txt",
        "admin1CodesASCII": cache_dir / "admin1CodesASCII.txt",
    }

    remote_names = {
        args.city_source: f"{args.city_source}.zip",
        "alternateNamesV2": "alternateNamesV2.zip",
        "countryInfo": "countryInfo.txt",
        "admin1CodesASCII": "admin1CodesASCII.txt",
    }
    for name, path in sources.items():
        download(
            f"{GEONAMES_DUMP_URL}/{remote_names[name]}",
            path,
            args.force_download,
        )

    print("[parse] countries")
    countries = read_countries(sources["countryInfo"])
    print(f"  {len(countries):,} countries")
    print(f"[parse] cities from {args.city_source}.zip")
    cities = read_cities(sources[args.city_source], set(countries))
    print(f"  {len(cities):,} cities")
    print("[parse] first-level administrative regions")
    regions = read_regions(sources["admin1CodesASCII"], set(countries))
    print(f"  {len(regions):,} regions")
    overrides = read_overrides(overrides_path)
    print(
        "[parse] curated overrides: "
        f"{len(overrides['cities']):,} cities, {len(overrides['regions']):,} regions"
    )

    relevant_ids = {city.geoname_id for city in cities}
    relevant_ids.update(region.geoname_id for region in regions)
    relevant_ids.update(country.geoname_id for country in countries.values())
    english_names, chinese_names = choose_localized_names(
        sources["alternateNamesV2"], relevant_ids
    )
    supplemental_names = read_supplemental_names(supplemental_names_path)
    supplemental_applied = 0
    for geoname_id, localized_name in supplemental_names.items():
        if geoname_id in relevant_ids and geoname_id not in chinese_names:
            chinese_names[geoname_id] = localized_name
            supplemental_applied += 1
    print(
        f"[parse] supplemental Chinese names: {supplemental_applied:,} applied "
        f"from {supplemental_names_path.name}"
    )
    payload = build_payload(
        args.city_source,
        countries,
        cities,
        regions,
        english_names,
        chinese_names,
        overrides,
    )
    database_path = write_output(payload, output_path)
    meta = payload["meta"]

    print("[done]")
    print(
        f"  SQLite: {database_path} "
        f"({format_bytes(database_path.stat().st_size)})"
    )
    print(
        "  Chinese city names: "
        f"{meta['cityWithChineseNameCount']:,} / {meta['cityCount']:,}"
    )
    print(
        "  Chinese region names: "
        f"{meta['regionWithChineseNameCount']:,} / {meta['regionCount']:,}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
