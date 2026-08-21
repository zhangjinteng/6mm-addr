#!/usr/bin/env python3
"""Complete missing Chinese place names with CLDR, Wikidata, and local ML."""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import build_database as database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "scripts" / "data" / "supplemental-names.json.gz"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "scripts" / ".cache"
CLDR_RELEASE = "release-48-2"
CLDR_BASE_URL = (
    f"https://raw.githubusercontent.com/unicode-org/cldr/{CLDR_RELEASE}"
    "/common/subdivisions"
)
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = (
    "6mm-addr-builder/1.0 (https://github.com/zhangjinteng/6mm-addr)"
)
CHINESE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ALLOWED_GENERATED_PATTERN = re.compile(
    r"[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff0-9·\- ]+"
)


@dataclass(frozen=True)
class Place:
    geoname_id: int
    level: str
    country_code: str
    name_en: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate supplemental Chinese names for the 6mm address database."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--city-source", default="cities500")
    parser.add_argument("--skip-wikidata", action="store_true")
    parser.add_argument("--skip-machine", action="store_true")
    parser.add_argument("--force-wikidata", action="store_true")
    parser.add_argument("--epochs", type=int, default=18)
    parser.add_argument(
        "--machine-limit",
        type=int,
        default=0,
        help="Limit generated machine names for a smoke test; 0 means all missing names.",
    )
    parser.add_argument("--seed", type=int, default=20260821)
    return parser.parse_args()


def download(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_USER_AGENT})
    temporary = target.with_suffix(target.suffix + ".download")
    temporary.unlink(missing_ok=True)
    print(f"[download] {url}")
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
        "wb"
    ) as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    os.replace(temporary, target)


def source_paths(cache_dir: Path, city_source: str) -> dict[str, Path]:
    geonames = cache_dir / "geonames"
    paths = {
        city_source: geonames / f"{city_source}.zip",
        "alternateNamesV2": geonames / "alternateNamesV2.zip",
        "countryInfo": geonames / "countryInfo.txt",
        "admin1CodesASCII": geonames / "admin1CodesASCII.txt",
    }
    remote = {
        city_source: f"{city_source}.zip",
        "alternateNamesV2": "alternateNamesV2.zip",
        "countryInfo": "countryInfo.txt",
        "admin1CodesASCII": "admin1CodesASCII.txt",
    }
    for key, path in paths.items():
        database.download(
            f"{database.GEONAMES_DUMP_URL}/{remote[key]}", path, force=False
        )
    return paths


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def simplified(value: str) -> str:
    try:
        from opencc import OpenCC
    except ImportError as error:
        raise RuntimeError(
            "opencc-python-reimplemented is required to generate supplemental names"
        ) from error
    if not hasattr(simplified, "converter"):
        simplified.converter = OpenCC("t2s")  # type: ignore[attr-defined]
    return simplified.converter.convert(value).strip()  # type: ignore[attr-defined]


def load_places(
    paths: dict[str, Path], city_source: str
) -> tuple[list[Place], dict[int, str]]:
    countries = database.read_countries(paths["countryInfo"])
    cities = database.read_cities(paths[city_source], set(countries))
    regions = database.read_regions(paths["admin1CodesASCII"], set(countries))
    ids = {country.geoname_id for country in countries.values()}
    ids.update(region.geoname_id for region in regions)
    ids.update(city.geoname_id for city in cities)
    english, chinese = database.choose_localized_names(paths["alternateNamesV2"], ids)

    places: list[Place] = []
    known: dict[int, str] = {}
    for country in countries.values():
        name_en = english.get(country.geoname_id)
        places.append(
            Place(
                country.geoname_id,
                "country",
                country.code,
                name_en.value if name_en else country.name_en,
            )
        )
    for region in regions:
        places.append(
            Place(
                region.geoname_id,
                "region",
                region.country_code,
                region.ascii_name or region.name,
            )
        )
    for city in cities:
        name_en = english.get(city.geoname_id)
        places.append(
            Place(
                city.geoname_id,
                "city",
                city.country_code,
                name_en.value if name_en else city.ascii_name or city.name,
            )
        )
    for geoname_id, name in chinese.items():
        value = simplified(name.value)
        if CHINESE_PATTERN.search(value):
            known[geoname_id] = value
    return places, known


def read_cldr_names(cache_dir: Path) -> dict[tuple[str, str], str]:
    cldr_dir = cache_dir / "cldr" / CLDR_RELEASE
    english_path = cldr_dir / "en.xml"
    chinese_path = cldr_dir / "zh.xml"
    download(f"{CLDR_BASE_URL}/en.xml", english_path)
    download(f"{CLDR_BASE_URL}/zh.xml", chinese_path)

    def entries(path: Path) -> dict[str, str]:
        root = ET.parse(path).getroot()
        return {
            item.attrib["type"].lower(): (item.text or "").strip()
            for item in root.iter("subdivision")
            if item.attrib.get("type") and (item.text or "").strip()
        }

    english = entries(english_path)
    chinese = entries(chinese_path)
    names: dict[tuple[str, str], str] = {}
    for subdivision, name_en in english.items():
        name_zh = simplified(chinese.get(subdivision, ""))
        if len(subdivision) < 3 or not CHINESE_PATTERN.search(name_zh):
            continue
        names[(subdivision[:2].upper(), normalize(name_en))] = name_zh
    return names


def apply_cldr(
    places: list[Place], known: dict[int, str], cache_dir: Path
) -> dict[int, dict[str, str]]:
    cldr_names = read_cldr_names(cache_dir)
    results: dict[int, dict[str, str]] = {}
    for place in places:
        if place.level != "region" or place.geoname_id in known:
            continue
        name = cldr_names.get((place.country_code, normalize(place.name_en)))
        if name:
            results[place.geoname_id] = {"nameZh": name, "source": "cldr"}
    print(f"[cldr] {len(results):,} missing regions resolved")
    return results


def load_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


def query_wikidata(ids: list[int], retries: int = 5) -> dict[int, str]:
    values = " ".join(json.dumps(str(item)) for item in ids)
    query = f"""
SELECT ?geonameId ?label WHERE {{
  VALUES ?geonameId {{ {values} }}
  ?item wdt:P1566 ?geonameId.
  ?item rdfs:label ?label.
  FILTER(LANG(?label) IN ("zh", "zh-hans", "zh-cn"))
}}
"""
    request = urllib.request.Request(
        WIKIDATA_ENDPOINT,
        data=urllib.parse.urlencode({"query": query}).encode(),
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": WIKIDATA_USER_AGENT,
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read())
            break
        except (
            http.client.IncompleteRead,
            json.JSONDecodeError,
            TimeoutError,
            urllib.error.URLError,
        ) as error:
            if attempt >= retries:
                raise RuntimeError(f"Wikidata request failed: {error}") from error
            print(
                f"[wikidata] retry {attempt + 1}/{retries}: {error}",
                flush=True,
            )
            time.sleep(min(30, 2**attempt))

    priorities = {"zh-cn": 300, "zh-hans": 290, "zh": 200}
    results: dict[int, tuple[int, str]] = {}
    for binding in payload.get("results", {}).get("bindings", []):
        raw_id = binding.get("geonameId", {}).get("value", "")
        raw_name = binding.get("label", {}).get("value", "")
        language = binding.get("label", {}).get("xml:lang", "").lower()
        try:
            geoname_id = int(raw_id)
        except ValueError:
            continue
        name = simplified(raw_name)
        if not CHINESE_PATTERN.search(name):
            continue
        candidate = (priorities.get(language, 100), name)
        if geoname_id not in results or candidate[0] > results[geoname_id][0]:
            results[geoname_id] = candidate
    return {key: value for key, (_, value) in results.items()}


def apply_wikidata(
    missing_ids: list[int], cache_dir: Path, force: bool
) -> dict[int, dict[str, str]]:
    cache_path = cache_dir / "wikidata" / "chinese-names.json"
    cache = load_json(cache_path, {"checked": [], "labels": {}})
    if not isinstance(cache, dict):
        cache = {"checked": [], "labels": {}}
    checked = set() if force else {int(item) for item in cache.get("checked", [])}
    labels = {} if force else {
        int(key): str(value) for key, value in cache.get("labels", {}).items()
    }
    pending = [item for item in missing_ids if item not in checked]
    chunk_size = 1000
    chunks = math.ceil(len(pending) / chunk_size)
    for index in range(0, len(pending), chunk_size):
        chunk = pending[index : index + chunk_size]
        labels.update(query_wikidata(chunk))
        checked.update(chunk)
        chunk_number = index // chunk_size + 1
        write_json(
            cache_path,
            {
                "checked": sorted(checked),
                "labels": {str(key): value for key, value in sorted(labels.items())},
            },
        )
        print(
            f"[wikidata] {chunk_number:,}/{chunks:,} batches, "
            f"{len(labels):,} Chinese labels",
            flush=True,
        )
        time.sleep(0.15)
    return {
        geoname_id: {"nameZh": name, "source": "wikidata"}
        for geoname_id, name in labels.items()
        if geoname_id in missing_ids
    }


def normalized_source(place: Place) -> str:
    return f"{place.country_code}|{normalize(place.name_en)}"[:80]


def sanitize_generated(value: str) -> str:
    value = simplified(value)
    value = ALLOWED_GENERATED_PATTERN.sub("", value)
    value = re.sub(
        r"[ ·\-]+",
        lambda match: (
            "·" if "·" in match.group() else "-" if "-" in match.group() else ""
        ),
        value,
    )
    value = value.strip("·- ")
    return value if CHINESE_PATTERN.search(value) else ""


LETTER_NAMES = {
    "a": "阿", "b": "布", "c": "茨", "d": "德", "e": "埃", "f": "弗",
    "g": "格", "h": "赫", "i": "伊", "j": "杰", "k": "克", "l": "勒",
    "m": "姆", "n": "恩", "o": "奥", "p": "普", "q": "库", "r": "尔",
    "s": "斯", "t": "特", "u": "乌", "v": "维", "w": "沃", "x": "克斯",
    "y": "伊", "z": "兹",
}


def deterministic_fallback(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z]+", ascii_value.casefold())
    generated = "·".join(
        "".join(LETTER_NAMES[character] for character in word) for word in words
    )
    return generated or "音译地名"


def machine_names(
    places: list[Place],
    known: dict[int, str],
    supplemental: dict[int, dict[str, str]],
    epochs: int,
    seed: int,
    limit: int = 0,
) -> dict[int, dict[str, str]]:
    try:
        import torch
        from torch import nn
        from torch.utils.data import DataLoader, Dataset
    except ImportError as error:
        raise RuntimeError("PyTorch is required for machine transliteration") from error

    random.seed(seed)
    torch.manual_seed(seed)
    place_by_id = {place.geoname_id: place for place in places}
    targets = dict(known)
    targets.update(
        (geoname_id, item["nameZh"]) for geoname_id, item in supplemental.items()
    )
    pairs = [
        (normalized_source(place_by_id[geoname_id]), sanitize_generated(name))
        for geoname_id, name in targets.items()
        if geoname_id in place_by_id
    ]
    pairs = sorted({pair for pair in pairs if pair[0] and pair[1]})
    missing = [place for place in places if place.geoname_id not in targets]
    if limit > 0:
        missing = missing[:limit]
    print(f"[machine] training on {len(pairs):,} names; filling {len(missing):,}")

    special = ["<pad>", "<bos>", "<eos>", "<unk>"]
    source_chars = special + sorted({char for source, _ in pairs for char in source})
    target_chars = special + sorted({char for _, target in pairs for char in target})
    source_ids = {char: index for index, char in enumerate(source_chars)}
    target_ids = {char: index for index, char in enumerate(target_chars)}
    pad, bos, eos, unk = 0, 1, 2, 3
    max_source_length = 82
    max_target_length = 34

    def encode(value: str, vocabulary: dict[str, int], maximum: int) -> list[int]:
        return [vocabulary.get(char, unk) for char in value[: maximum - 2]]

    class PairDataset(Dataset):
        def __init__(self, values: list[tuple[str, str]]) -> None:
            self.values = values

        def __len__(self) -> int:
            return len(self.values)

        def __getitem__(self, index: int) -> tuple[list[int], list[int]]:
            source, target = self.values[index]
            return (
                [bos] + encode(source, source_ids, max_source_length) + [eos],
                [bos] + encode(target, target_ids, max_target_length) + [eos],
            )

    def collate(batch: list[tuple[list[int], list[int]]]):
        source_length = max(len(item[0]) for item in batch)
        target_length = max(len(item[1]) for item in batch)
        source = torch.full((len(batch), source_length), pad, dtype=torch.long)
        target = torch.full((len(batch), target_length), pad, dtype=torch.long)
        for index, (source_item, target_item) in enumerate(batch):
            source[index, : len(source_item)] = torch.tensor(source_item)
            target[index, : len(target_item)] = torch.tensor(target_item)
        return source, target

    class Transliterator(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            width = 128
            self.source_embedding = nn.Embedding(len(source_chars), width, padding_idx=pad)
            self.target_embedding = nn.Embedding(len(target_chars), width, padding_idx=pad)
            self.positions = nn.Embedding(max(max_source_length, max_target_length), width)
            self.transformer = nn.Transformer(
                d_model=width,
                nhead=4,
                num_encoder_layers=3,
                num_decoder_layers=3,
                dim_feedforward=384,
                dropout=0.1,
                batch_first=True,
                norm_first=True,
            )
            self.output = nn.Linear(width, len(target_chars))

        def position(self, value):
            positions = torch.arange(value.shape[1], device=value.device)
            return self.positions(positions).unsqueeze(0)

        def encode(self, source):
            embedded = self.source_embedding(source) + self.position(source)
            return self.transformer.encoder(
                embedded, src_key_padding_mask=source.eq(pad)
            )

        def decode(self, target, memory, source_padding):
            embedded = self.target_embedding(target) + self.position(target)
            mask = torch.triu(
                torch.ones(
                    target.shape[1],
                    target.shape[1],
                    dtype=torch.bool,
                    device=target.device,
                ),
                diagonal=1,
            )
            decoded = self.transformer.decoder(
                embedded,
                memory,
                tgt_mask=mask,
                tgt_key_padding_mask=target.eq(pad),
                memory_key_padding_mask=source_padding,
            )
            return self.output(decoded)

        def forward(self, source, target):
            return self.decode(target, self.encode(source), source.eq(pad))

    device = (
        torch.device("mps")
        if torch.backends.mps.is_available()
        else torch.device("cuda")
        if torch.cuda.is_available()
        else torch.device("cpu")
    )
    model = Transliterator().to(device)
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(
        PairDataset(pairs),
        batch_size=256,
        shuffle=True,
        generator=generator,
        collate_fn=collate,
        num_workers=0,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=4e-4, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss(ignore_index=pad, label_smoothing=0.05)
    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for source, target in loader:
            source = source.to(device)
            target = target.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(source, target[:, :-1])
            loss = criterion(logits.reshape(-1, logits.shape[-1]), target[:, 1:].reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += float(loss.detach().cpu())
        print(
            f"[machine] epoch {epoch + 1:02d}/{epochs}: "
            f"loss={total_loss / max(len(loader), 1):.4f}",
            flush=True,
        )

    model.eval()
    results: dict[int, dict[str, str]] = {}
    batch_size = 512
    with torch.inference_mode():
        for offset in range(0, len(missing), batch_size):
            batch = missing[offset : offset + batch_size]
            encoded = [
                [bos]
                + encode(normalized_source(place), source_ids, max_source_length)
                + [eos]
                for place in batch
            ]
            length = max(len(item) for item in encoded)
            source = torch.full((len(batch), length), pad, dtype=torch.long)
            for index, item in enumerate(encoded):
                source[index, : len(item)] = torch.tensor(item)
            source = source.to(device)
            memory = model.encode(source)
            generated = torch.full((len(batch), 1), bos, dtype=torch.long, device=device)
            finished = torch.zeros(len(batch), dtype=torch.bool, device=device)
            for _ in range(max_target_length - 1):
                logits = model.decode(generated, memory, source.eq(pad))[:, -1, :]
                next_token = logits.argmax(dim=-1)
                next_token = torch.where(finished, torch.full_like(next_token, eos), next_token)
                generated = torch.cat([generated, next_token.unsqueeze(1)], dim=1)
                finished |= next_token.eq(eos)
                if bool(finished.all()):
                    break
            for place, token_ids in zip(batch, generated[:, 1:].cpu().tolist()):
                characters = []
                for token_id in token_ids:
                    if token_id == eos:
                        break
                    if token_id >= len(target_chars) or token_id < len(special):
                        continue
                    characters.append(target_chars[token_id])
                name = sanitize_generated("".join(characters))
                source_name = "machine"
                if not name:
                    name = deterministic_fallback(place.name_en)
                    source_name = "machine-fallback"
                results[place.geoname_id] = {
                    "nameZh": name,
                    "source": source_name,
                }
            if offset % (batch_size * 20) == 0:
                print(
                    f"[machine] generated {min(offset + batch_size, len(missing)):,}"
                    f"/{len(missing):,}",
                    flush=True,
                )
    return results


def write_output(
    path: Path,
    places: list[Place],
    names: dict[int, dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    place_by_id = {place.geoname_id: place for place in places}
    counts = Counter(item["source"] for item in names.values())
    level_counts = Counter(place_by_id[item].level for item in names if item in place_by_id)
    payload = {
        "meta": {
            "cldrRelease": CLDR_RELEASE,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nameCount": len(names),
            "sourceCounts": dict(sorted(counts.items())),
            "levelCounts": dict(sorted(level_counts.items())),
        },
        "names": {
            str(key): value for key, value in sorted(names.items())
        },
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as output:
        json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)
    print(f"[done] {path} ({database.format_bytes(path.stat().st_size)})")
    print(json.dumps(payload["meta"], ensure_ascii=False, indent=2))


def main() -> int:
    args = parse_args()
    cache_dir = args.cache_dir.resolve()
    paths = source_paths(cache_dir, args.city_source)
    places, known = load_places(paths, args.city_source)
    print(f"[base] {len(places):,} places; {len(known):,} already Chinese")

    supplemental = apply_cldr(places, known, cache_dir)
    missing_ids = [
        place.geoname_id
        for place in places
        if place.geoname_id not in known and place.geoname_id not in supplemental
    ]
    if not args.skip_wikidata:
        wikidata = apply_wikidata(missing_ids, cache_dir, args.force_wikidata)
        supplemental.update(wikidata)
    if not args.skip_machine:
        supplemental.update(
            machine_names(
                places,
                known,
                supplemental,
                args.epochs,
                args.seed,
                args.machine_limit,
            )
        )
    write_output(args.output.resolve(), places, supplemental)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
