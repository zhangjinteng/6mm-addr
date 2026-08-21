<?php

declare(strict_types=1);

use SixMm\Addr\AddressDatabase;
use SixMm\Addr\AddressLocalizer;
use SixMm\Addr\AddressRepository;

require dirname(__DIR__) . '/vendor/autoload.php';

$assertions = 0;

$assertSame = static function (mixed $expected, mixed $actual, string $message) use (&$assertions): void {
    $assertions++;
    if ($expected !== $actual) {
        throw new RuntimeException(sprintf(
            "%s\nExpected: %s\nActual:   %s",
            $message,
            var_export($expected, true),
            var_export($actual, true)
        ));
    }
};

$assertTrue = static function (bool $condition, string $message) use (&$assertions): void {
    $assertions++;
    if (!$condition) {
        throw new RuntimeException($message);
    }
};

$database = new AddressDatabase();
$repository = new AddressRepository($database);

$assertTrue(is_file($database->path()), 'The bundled SQLite database must exist.');
$assertSame('1', $repository->metadata('schemaVersion'), 'The schema version must be available.');

$japan = $repository->country('jp');
$assertSame('Japan', $japan['names']['en'] ?? null, 'Country English names should resolve.');
$assertSame('日本', $japan['names']['zh'] ?? null, 'Country Chinese names should resolve.');

$tokyoRegion = $repository->region('JP', '13', 'Tokyo');
$assertSame('40', $tokyoRegion['code'] ?? null, 'Region names should bridge provider code differences.');
$assertSame('东京都', $tokyoRegion['names']['zh'] ?? null, 'Region Chinese names should resolve.');

$tokyoByProviderCode = $repository->region('JP', '13');
$assertSame(1850144, $tokyoByProviderCode['geoname_id'] ?? null, 'Provider region aliases should resolve.');

$tokyo = $repository->city('JP', '40', 'Tokyo');
$assertSame('东京', $tokyo['names']['zh'] ?? null, 'Cities should resolve inside their region.');

$kualaLumpur = $repository->city('MY', null, 'Kuala Lumpur');
$assertSame('吉隆坡', $kualaLumpur['names']['zh'] ?? null, 'Unique cities should resolve without a region.');

$localizer = new AddressLocalizer($repository);
$location = $localizer->enrich([
    'country_code' => 'JP',
    'country' => 'Japan',
    'region_code' => '13',
    'region' => 'Tokyo',
    'city' => 'Tokyo',
]);

$assertSame(['en' => 'Japan', 'zh' => '日本'], $location['country_names'], 'Country names should be enriched.');
$assertSame(['en' => 'Tokyo', 'zh' => '东京都'], $location['region_names'], 'Region names should be enriched.');
$assertSame(['en' => 'Tokyo', 'zh' => '东京'], $location['city_names'], 'City names should be enriched.');

$sameNameLocation = $localizer->enrich([
    'country_code' => 'MY',
    'country' => 'Malaysia',
    'region' => 'Kuala Lumpur',
    'city' => 'Kuala Lumpur',
]);
$assertSame('吉隆坡', $sameNameLocation['region_names']['zh'], 'Same-name regions should reuse a translated city name.');

$hongKong = $localizer->enrich([
    'country_code' => 'HK',
    'country' => 'Hong Kong',
    'region' => 'Islands',
    'city' => 'Tung Chung',
]);
$assertSame('离岛区', $hongKong['region_names']['zh'], 'Curated region overrides should be applied.');
$assertSame('东涌', $hongKong['city_names']['zh'], 'Region-specific city overrides should be applied.');

$fallback = $localizer->enrich([
    'country_code' => 'ZZ',
    'country' => 'Unknown Country',
    'region' => 'Unknown Region',
    'city' => 'Unknown City',
]);
$assertSame('Unknown City', $fallback['city_names']['zh'], 'Missing Chinese names should fall back to English.');

$writeWasBlocked = false;
try {
    $database->connection()->exec("INSERT INTO metadata (key, value) VALUES ('write-test', '1')");
} catch (PDOException) {
    $writeWasBlocked = true;
}
$assertTrue($writeWasBlocked, 'The bundled database connection must be query-only.');

fwrite(STDOUT, sprintf("6mm-addr: %d assertions passed.\n", $assertions));
