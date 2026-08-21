<?php

declare(strict_types=1);

namespace SixMm\Addr;

final class AddressLocalizer
{
    public function __construct(
        private readonly AddressRepository $repository = new AddressRepository()
    ) {
    }

    /**
     * @param array<string, mixed> $location
     * @return array<string, mixed>
     */
    public function enrich(array $location): array
    {
        $countryCode = strtoupper(trim((string) ($location['country_code'] ?? '')));
        $regionCode = trim((string) ($location['region_code'] ?? ''));
        $country = $this->repository->country($countryCode);
        $region = $this->repository->region(
            $countryCode,
            $regionCode,
            (string) ($location['region'] ?? '')
        );
        $city = $this->repository->city(
            $countryCode,
            (string) ($region['code'] ?? $regionCode),
            (string) ($location['city'] ?? '')
        );

        $countryNames = $this->names($country, $location['country'] ?? '');
        $regionNames = $this->names($region, $location['region'] ?? '');
        $cityNames = $this->names($city, $location['city'] ?? '');

        if ($this->isUntranslated($regionNames)) {
            if ($this->sameName($regionNames['en'], $cityNames['en'])
                && !$this->isUntranslated($cityNames)) {
                $regionNames['zh'] = $cityNames['zh'];
            } elseif ($this->sameName($regionNames['en'], $countryNames['en'])
                && !$this->isUntranslated($countryNames)) {
                $regionNames['zh'] = $countryNames['zh'];
            }
        }

        $location['country_names'] = $countryNames;
        $location['region_names'] = $regionNames;
        $location['city_names'] = $cityNames;

        return $location;
    }

    /** @return array{en: string, zh: string} */
    private function names(?array $place, mixed $fallback): array
    {
        $fallback = trim((string) $fallback);
        $names = is_array($place['names'] ?? null) ? $place['names'] : [];
        $english = trim((string) ($names['en'] ?? $fallback));
        $chinese = trim((string) ($names['zh'] ?? ''));

        return [
            'en' => $english !== '' ? $english : $fallback,
            'zh' => $chinese !== '' ? $chinese : ($english !== '' ? $english : $fallback),
        ];
    }

    /** @param array{en: string, zh: string} $names */
    private function isUntranslated(array $names): bool
    {
        return $names['zh'] === '' || $this->sameName($names['en'], $names['zh']);
    }

    private function sameName(string $left, string $right): bool
    {
        $left = NameNormalizer::normalize($left);

        return $left !== '' && $left === NameNormalizer::normalize($right);
    }
}
