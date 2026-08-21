<?php

declare(strict_types=1);

namespace SixMm\Addr;

use PDO;

final class AddressRepository
{
    private readonly PDO $connection;

    public function __construct(?AddressDatabase $database = null)
    {
        $this->connection = ($database ?? new AddressDatabase())->connection();
    }

    /** @return array<string, mixed>|null */
    public function country(string $countryCode): ?array
    {
        $statement = $this->connection->prepare(
            'SELECT code, geoname_id, name_en, name_zh, zh_language, zh_source
             FROM countries
             WHERE code = :code'
        );
        $statement->execute(['code' => $this->normalizeCountryCode($countryCode)]);

        return $this->place($statement->fetch());
    }

    /** @return array<string, mixed>|null */
    public function region(
        string $countryCode,
        ?string $regionCode = null,
        ?string $regionName = null
    ): ?array {
        $countryCode = $this->normalizeCountryCode($countryCode);
        $normalizedName = NameNormalizer::normalize($regionName);

        if ($normalizedName !== '') {
            $statement = $this->connection->prepare(
                'SELECT r.code, r.geoname_id, r.country_code, r.name_en, r.name_zh,
                        r.zh_language, r.zh_source
                 FROM region_aliases a
                 INNER JOIN regions r ON r.geoname_id = a.region_id
                 WHERE a.country_code = :country_code AND a.alias_normalized = :alias
                 ORDER BY a.priority DESC
                 LIMIT 1'
            );
            $statement->execute([
                'country_code' => $countryCode,
                'alias' => $normalizedName,
            ]);
            $place = $this->place($statement->fetch());
            if ($place !== null) {
                return $place;
            }
        }

        $regionCode = strtoupper(trim((string) $regionCode));
        if ($regionCode === '') {
            return null;
        }

        $statement = $this->connection->prepare(
            'SELECT r.code, r.geoname_id, r.country_code, r.name_en, r.name_zh,
                    r.zh_language, r.zh_source
             FROM region_code_aliases a
             INNER JOIN regions r ON r.geoname_id = a.region_id
             WHERE a.country_code = :country_code AND a.provider = :provider
               AND a.code = :code
             LIMIT 1'
        );
        $statement->execute([
            'country_code' => $countryCode,
            'provider' => 'ipinfo',
            'code' => $regionCode,
        ]);
        $place = $this->place($statement->fetch());
        if ($place !== null) {
            return $place;
        }

        $statement = $this->connection->prepare(
            'SELECT code, geoname_id, country_code, name_en, name_zh, zh_language, zh_source
             FROM regions
             WHERE country_code = :country_code AND UPPER(code) = :code
             LIMIT 1'
        );
        $statement->execute(['country_code' => $countryCode, 'code' => $regionCode]);

        return $this->place($statement->fetch());
    }

    /** @return array<string, mixed>|null */
    public function city(
        string $countryCode,
        ?string $regionCode,
        string $cityName
    ): ?array {
        $countryCode = $this->normalizeCountryCode($countryCode);
        $normalizedName = NameNormalizer::normalize($cityName);
        if ($countryCode === '' || $normalizedName === '') {
            return null;
        }

        $regionCode = strtoupper(trim((string) $regionCode));
        if ($regionCode !== '') {
            $statement = $this->connection->prepare(
                'SELECT c.geoname_id, c.country_code, c.region_code, c.name_en, c.name_zh,
                        c.zh_language, c.zh_source
                 FROM city_aliases a
                 INNER JOIN cities c ON c.geoname_id = a.city_id
                 WHERE a.country_code = :country_code AND a.region_code = :region_code
                   AND a.alias_normalized = :alias
                 ORDER BY a.priority DESC
                 LIMIT 1'
            );
            $statement->execute([
                'country_code' => $countryCode,
                'region_code' => $regionCode,
                'alias' => $normalizedName,
            ]);
            $place = $this->place($statement->fetch());
            if ($place !== null) {
                return $place;
            }
        }

        $statement = $this->connection->prepare(
            'SELECT c.geoname_id, c.country_code, c.region_code, c.name_en, c.name_zh,
                    c.zh_language, c.zh_source
             FROM city_aliases a
             INNER JOIN cities c ON c.geoname_id = a.city_id
             WHERE a.country_code = :country_code AND a.alias_normalized = :alias
             GROUP BY c.geoname_id
             ORDER BY a.priority DESC, c.population DESC, c.geoname_id ASC
             LIMIT 2'
        );
        $statement->execute(['country_code' => $countryCode, 'alias' => $normalizedName]);
        $matches = $statement->fetchAll();

        return count($matches) === 1 ? $this->place($matches[0]) : null;
    }

    public function metadata(string $key): ?string
    {
        $statement = $this->connection->prepare('SELECT value FROM metadata WHERE key = :key');
        $statement->execute(['key' => $key]);
        $value = $statement->fetchColumn();

        return is_string($value) ? $value : null;
    }

    private function normalizeCountryCode(string $countryCode): string
    {
        $countryCode = strtoupper(trim($countryCode));

        return preg_match('/^[A-Z]{2}$/', $countryCode) ? $countryCode : '';
    }

    /** @return array<string, mixed>|null */
    private function place(mixed $row): ?array
    {
        if (!is_array($row)) {
            return null;
        }

        return [
            'geoname_id' => isset($row['geoname_id']) ? (int) $row['geoname_id'] : null,
            'country_code' => isset($row['country_code'])
                ? (string) $row['country_code']
                : null,
            'region_code' => isset($row['region_code'])
                ? (string) $row['region_code']
                : null,
            'code' => isset($row['code']) ? (string) $row['code'] : null,
            'names' => [
                'en' => trim((string) ($row['name_en'] ?? '')),
                'zh' => trim((string) ($row['name_zh'] ?? '')),
            ],
            'zh_language' => isset($row['zh_language'])
                ? (string) $row['zh_language']
                : null,
            'zh_source' => isset($row['zh_source'])
                ? (string) $row['zh_source']
                : null,
        ];
    }
}
