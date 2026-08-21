<?php

declare(strict_types=1);

namespace SixMm\Addr;

use PDO;
use RuntimeException;

final class AddressDatabase
{
    private ?PDO $connection = null;

    public function __construct(
        private readonly string $path = __DIR__ . '/../resources/world-places.sqlite'
    ) {
    }

    public function path(): string
    {
        return $this->path;
    }

    public function connection(): PDO
    {
        if ($this->connection instanceof PDO) {
            return $this->connection;
        }

        if (!is_file($this->path) || !is_readable($this->path)) {
            throw new RuntimeException(sprintf('The 6mm address database is not readable: %s', $this->path));
        }

        $connection = new PDO('sqlite:' . $this->path, null, null, [
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_STRINGIFY_FETCHES => false,
        ]);
        $connection->exec('PRAGMA query_only = ON');

        return $this->connection = $connection;
    }
}
