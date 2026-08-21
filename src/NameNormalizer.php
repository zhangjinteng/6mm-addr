<?php

declare(strict_types=1);

namespace SixMm\Addr;

final class NameNormalizer
{
    public static function normalize(mixed $value): string
    {
        $value = trim((string) $value);
        if ($value === '') {
            return '';
        }

        $value = preg_replace('/\s+/u', ' ', $value) ?? $value;

        return mb_strtolower($value, 'UTF-8');
    }
}
